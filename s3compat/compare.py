"""Line up the calls made against each endpoint and flag where they disagree."""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .client import KEY_HEADERS, CallRecord
from .operations import EndpointRun

# Headers whose absence on one side is worth flagging. Request ids are listed
# separately so they don't drown out the more interesting ones.
TRACKED_HEADERS = tuple(h for h in KEY_HEADERS if h not in ("location", "x-amz-mp-parts-count"))

# Observations that legitimately differ between runs or endpoints.
VOLATILE_OBSERVATIONS = {
    "version_id_sample",
    "upload_mib_per_s",
    "download_mib_per_s",
}

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

_MD5 = re.compile(r"^[0-9a-f]{32}$")
_MULTIPART = re.compile(r"^[0-9a-f]{32}-(\d+)$")


def etag_format(etag: str | None) -> str | None:
    """Classify an ETag: 'md5', 'multipart-N', or 'other'."""
    if etag is None:
        return None
    value = etag.strip().strip('"').lower()
    if value.startswith("w/"):
        return "weak"
    if _MD5.match(value):
        return "md5"
    m = _MULTIPART.match(value)
    if m:
        return f"multipart-{m.group(1)}"
    return "other"


def is_quoted(etag: str | None) -> bool | None:
    if etag is None:
        return None
    return etag.startswith('"') and etag.endswith('"')


@dataclass
class Difference:
    category: str
    severity: str
    step: str
    operation: str
    detail: str
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class AlignedCall:
    step: str
    operation: str
    occurrence: int
    calls: dict[str, CallRecord | None]
    flags: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.step, self.operation, self.occurrence)


@dataclass
class LatencyStats:
    endpoint: str
    operation: str
    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float


@dataclass
class Comparison:
    endpoints: list[str]
    aligned: list[AlignedCall]
    differences: list[Difference]
    latency: list[LatencyStats]
    step_results: dict[str, dict[str, str]]

    def counts(self) -> dict[str, int]:
        out = {"error": 0, "warning": 0, "info": 0}
        for d in self.differences:
            out[d.severity] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoints": self.endpoints,
            "summary": self.counts(),
            "step_results": self.step_results,
            "differences": [asdict(d) for d in self.differences],
            "latency": [asdict(s) for s in self.latency],
            "aligned_calls": [
                {
                    "step": a.step,
                    "operation": a.operation,
                    "occurrence": a.occurrence,
                    "flags": a.flags,
                    "calls": {k: (v.to_dict() if v else None) for k, v in a.calls.items()},
                }
                for a in self.aligned
            ],
        }


def align(runs: list[EndpointRun]) -> list[AlignedCall]:
    """Pair up calls by (step, operation, nth occurrence), keeping first-seen order."""
    order: list[tuple[str, str, int]] = []
    by_endpoint: dict[str, dict[tuple[str, str, int], CallRecord]] = {}
    for run in runs:
        seen: dict[tuple[str, str], int] = defaultdict(int)
        table = {}
        for rec in run.records:
            n = seen[(rec.step, rec.operation)]
            seen[(rec.step, rec.operation)] += 1
            key = (rec.step, rec.operation, n)
            table[key] = rec
            if key not in order:
                order.append(key)
        by_endpoint[run.endpoint] = table

    step_rank = {}
    for run in runs:
        for r in run.results:
            step_rank.setdefault(r.name, len(step_rank))
    order.sort(key=lambda k: step_rank.get(k[0], len(step_rank)))  # stable within a step

    return [
        AlignedCall(step, op, n, {run.endpoint: by_endpoint[run.endpoint].get((step, op, n)) for run in runs})
        for step, op, n in order
    ]


def _differs(values: Iterable[Any]) -> bool:
    return len({repr(v) for v in values}) > 1


def compare_calls(aligned: list[AlignedCall]) -> list[Difference]:
    diffs: list[Difference] = []
    # (header, endpoints lacking it) -> {step/operation: calls}
    missing_headers: dict[tuple[str, tuple[str, ...]], dict[str, int]] = {}
    missing_calls: dict[tuple[str, str, tuple[str, ...]], int] = {}
    servers: dict[str, set[str]] = defaultdict(set)

    for a in aligned:
        present = {ep: rec for ep, rec in a.calls.items() if rec is not None}
        absent = [ep for ep, rec in a.calls.items() if rec is None]
        if absent:
            a.flags.append("missing")
            slot_key = (a.step, a.operation, tuple(absent))
            missing_calls[slot_key] = missing_calls.get(slot_key, 0) + 1
        if len(present) < 2:
            continue

        statuses = {ep: r.status for ep, r in present.items()}
        if _differs(statuses.values()):
            a.flags.append("status")
            diffs.append(Difference("status", "error", a.step, a.operation, "HTTP status differs", statuses))

        codes = {ep: r.error_code for ep, r in present.items()}
        if _differs(codes.values()):
            a.flags.append("error-code")
            diffs.append(Difference("error_code", "error", a.step, a.operation, "error code differs", codes))

        if _differs(statuses.values()) and any((s or 0) >= 300 or s is None for s in statuses.values()):
            # An error response naturally lacks ETag and object headers; the
            # status difference already says everything worth saying.
            continue

        etags = {ep: r.headers.get("etag") for ep, r in present.items()}
        formats = {ep: etag_format(v) for ep, v in etags.items()}
        if _differs(formats.values()):
            a.flags.append("etag")
            diffs.append(Difference("etag_format", "error", a.step, a.operation, "ETag format differs", formats))
        quoted = {ep: is_quoted(v) for ep, v in etags.items() if v is not None}
        if len(quoted) > 1 and _differs(quoted.values()):
            a.flags.append("etag-quoting")
            diffs.append(Difference("etag_quoting", "warning", a.step, a.operation, "ETag quoting differs", quoted))

        for header in TRACKED_HEADERS:
            has = {ep: header in r.headers for ep, r in present.items()}
            if _differs(has.values()):
                a.flags.append(f"-{header}")
                lacking = tuple(ep for ep, ok in has.items() if not ok)
                slot = missing_headers.setdefault((header, lacking), defaultdict(int))
                slot[f"{a.step}/{a.operation}"] += 1

        for ep, r in present.items():
            if "server" in r.headers:
                servers[ep].add(r.headers["server"])

    for (step, op, absent), n in missing_calls.items():
        diffs.append(
            Difference(
                "call_missing", "warning", step, op,
                f"{n} call(s) not made on {', '.join(absent)} (an earlier call failed)",
                {ep: "not called" for ep in absent},
            )
        )

    for (header, lacking), where in missing_headers.items():
        total = sum(where.values())
        ops = sorted({w.split("/", 1)[1] for w in where})
        shown = ", ".join(ops[:6]) + (f" +{len(ops) - 6} more" if len(ops) > 6 else "")
        diffs.append(
            Difference(
                "header_missing", "warning", "*", "*",
                f"{header} not returned by {', '.join(lacking)} on {total} call(s): {shown}",
                {ep: f"missing ({total})" for ep in lacking},
            )
        )

    if servers and _differs(sorted(v) for v in servers.values()):
        diffs.append(
            Difference("server", "info", "*", "*", "Server header differs",
                       {ep: sorted(v) for ep, v in servers.items()})
        )
    return diffs


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else k, v, out)
    else:
        out[prefix] = value


def compare_results(runs: list[EndpointRun]) -> list[Difference]:
    diffs: list[Difference] = []
    names: list[str] = []
    for run in runs:
        for r in run.results:
            if r.name not in names:
                names.append(r.name)

    for name in names:
        results = {run.endpoint: run.result(name) for run in runs}
        statuses = {ep: (r.status if r else "not run") for ep, r in results.items()}
        if _differs(statuses.values()):
            errors = {ep: (r.error if r else None) for ep, r in results.items()}
            detail = "; ".join(f"{ep}: {e}" for ep, e in errors.items() if e)
            diffs.append(Difference("result", "error", name, "*", f"outcome differs ({detail})" if detail else "outcome differs", statuses))

        flat: dict[str, dict[str, Any]] = {}
        for ep, r in results.items():
            if r is None or not r.observations:
                continue
            f: dict[str, Any] = {}
            _flatten("", r.observations, f)
            flat[ep] = f
        if len(flat) < 2:
            continue
        keys = sorted({k for f in flat.values() for k in f})
        for key in keys:
            if key.split(".")[0] in VOLATILE_OBSERVATIONS:
                continue
            values = {ep: f.get(key) for ep, f in flat.items()}
            if _differs(values.values()):
                diffs.append(Difference("behaviour", "warning", name, "*", key, values))
    return diffs


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * pct
    lo, hi = int(idx), min(int(idx) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def latency_stats(runs: list[EndpointRun]) -> list[LatencyStats]:
    out = []
    for run in runs:
        grouped: dict[str, list[float]] = defaultdict(list)
        for rec in run.records:
            grouped[rec.operation].append(rec.latency_ms)
        for op, values in grouped.items():
            out.append(
                LatencyStats(
                    run.endpoint, op, len(values),
                    round(statistics.fmean(values), 2),
                    round(_percentile(values, 0.5), 2),
                    round(_percentile(values, 0.95), 2),
                    round(max(values), 2),
                )
            )
    return out


def compare(runs: list[EndpointRun]) -> Comparison:
    aligned = align(runs)
    diffs = compare_results(runs) + compare_calls(aligned)
    diffs.sort(key=lambda d: SEVERITY_ORDER[d.severity])
    step_results = {
        run.endpoint: {r.name: r.status for r in run.results} for run in runs
    }
    return Comparison([r.endpoint for r in runs], aligned, diffs, latency_stats(runs), step_results)
