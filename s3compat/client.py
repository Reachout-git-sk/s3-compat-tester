"""boto3 client construction plus per-call capture via botocore event hooks.

Every API call goes through ``before-call`` (we stamp a start time into the
request context) and ``after-call`` (we read the raw HTTP status and headers
off the response). Transport failures never reach ``after-call``, so
``after-call-error`` is hooked as well.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

import boto3
from botocore.config import Config

from .config import Endpoint

KEY_HEADERS = (
    "x-amz-request-id",
    "x-amz-id-2",
    "etag",
    "server",
    "x-amz-version-id",
    "x-amz-delete-marker",
    "content-length",
    "content-type",
    "last-modified",
    "accept-ranges",
    "x-amz-bucket-region",
    "x-amz-server-side-encryption",
    "x-amz-mp-parts-count",
    "location",
)

_START = "s3compat_start"


@dataclass
class CallRecord:
    endpoint: str
    step: str
    operation: str
    status: int | None
    latency_ms: float
    headers: dict[str, str] = field(default_factory=dict)
    header_names: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_headers(raw: Any) -> tuple[dict[str, str], list[str]]:
    """Pick out the interesting headers (lower-cased) and list all names seen."""
    lowered = {str(k).lower(): str(v) for k, v in dict(raw or {}).items()}
    picked = {k: lowered[k] for k in KEY_HEADERS if k in lowered}
    return picked, sorted(lowered)


class CallRecorder:
    """Collects a CallRecord for every request made through attached clients."""

    def __init__(self, endpoint_name: str):
        self.endpoint_name = endpoint_name
        self.records: list[CallRecord] = []
        self._step = "setup"

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        previous, self._step = self._step, name
        try:
            yield
        finally:
            self._step = previous

    @property
    def current_step(self) -> str:
        return self._step

    def attach(self, client: Any) -> Any:
        events = client.meta.events
        events.register("before-call.s3", self._before_call)
        events.register("after-call.s3", self._after_call)
        events.register("after-call-error.s3", self._after_call_error)
        return client

    def _before_call(self, model, context, **_):
        context[_START] = time.perf_counter()

    def _elapsed_ms(self, context) -> float:
        start = context.get(_START)
        return round((time.perf_counter() - start) * 1000, 2) if start else 0.0

    def _after_call(self, http_response, parsed, model, context, **_):
        headers, names = capture_headers(getattr(http_response, "headers", {}))
        error = (parsed or {}).get("Error") or {}
        self.records.append(
            CallRecord(
                endpoint=self.endpoint_name,
                step=self._step,
                operation=model.name,
                status=getattr(http_response, "status_code", None),
                latency_ms=self._elapsed_ms(context),
                headers=headers,
                header_names=names,
                error_code=error.get("Code"),
                error_message=error.get("Message"),
            )
        )

    def _after_call_error(self, exception, context, **kwargs):
        # The event name carries the operation; botocore doesn't pass the model here.
        op = kwargs.get("event_name", "").rsplit(".", 1)[-1] or "Unknown"
        self.records.append(
            CallRecord(
                endpoint=self.endpoint_name,
                step=self._step,
                operation=op,
                status=None,
                latency_ms=self._elapsed_ms(context),
                error_code=type(exception).__name__,
                error_message=str(exception),
            )
        )

    def record_http(self, operation: str, response: Any, latency_ms: float) -> CallRecord:
        """Record a plain HTTP response (e.g. a presigned URL fetched with requests)."""
        headers, names = capture_headers(response.headers)
        code = None
        if response.status_code >= 300:
            code = _xml_error_code(response.text)
        rec = CallRecord(
            endpoint=self.endpoint_name,
            step=self._step,
            operation=operation,
            status=response.status_code,
            latency_ms=round(latency_ms, 2),
            headers=headers,
            header_names=names,
            error_code=code,
        )
        self.records.append(rec)
        return rec


def _xml_error_code(body: str) -> str | None:
    start = body.find("<Code>")
    end = body.find("</Code>")
    if start == -1 or end == -1:
        return None
    return body[start + 6 : end]


def make_client(endpoint: Endpoint, recorder: CallRecorder | None = None, *, checksums: str = "when_supported"):
    config = Config(
        region_name=endpoint.region,
        signature_version="s3v4",
        s3={"addressing_style": endpoint.addressing_style},
        # One attempt per call: we want to see what the server actually returned,
        # not what it returned after botocore quietly retried a 500.
        retries={"max_attempts": 1, "mode": "standard"},
        request_checksum_calculation=checksums,
        response_checksum_validation=checksums,
        connect_timeout=10,
        read_timeout=120,
    )
    session = boto3.session.Session(
        aws_access_key_id=endpoint.access_key,
        aws_secret_access_key=endpoint.secret_key,
        aws_session_token=endpoint.session_token,
        region_name=endpoint.region,
    )
    client = session.client(
        "s3",
        endpoint_url=endpoint.endpoint_url,
        verify=endpoint.verify_tls,
        config=config,
    )
    if recorder is not None:
        recorder.attach(client)
    return client
