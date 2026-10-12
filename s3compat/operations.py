"""The S3 operations each endpoint is put through.

Each operation function takes a Context, performs its calls, and returns a dict
of observations (things worth comparing across endpoints that aren't just
status codes or headers). An operation raises CheckFailed when the endpoint
does something functionally wrong, e.g. returns bytes that don't hash right.
"""

from __future__ import annotations

import hashlib
import random
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator

import requests
from botocore.exceptions import ClientError

from .client import CallRecord, CallRecorder, make_client
from .config import Endpoint

MiB = 1024 * 1024


class CheckFailed(AssertionError):
    pass


@dataclass
class RunOptions:
    multipart_size_mb: int = 100
    part_size_mb: int = 8
    small_object_kb: int = 256
    list_object_count: int = 25
    page_size: int = 10
    bucket_prefix: str = "s3compat"
    operations: list[str] | None = None
    seed: int = 1234
    checksums: str = "when_supported"
    presign_expiry_probe: bool = True


@dataclass
class OpResult:
    name: str
    status: str  # "passed", "failed", "error", "skipped"
    duration_ms: float = 0.0
    observations: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "passed"


@dataclass
class EndpointRun:
    endpoint: str
    endpoint_url: str | None
    region: str
    bucket: str
    results: list[OpResult] = field(default_factory=list)
    records: list[CallRecord] = field(default_factory=list)

    def result(self, name: str) -> OpResult | None:
        return next((r for r in self.results if r.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "endpoint_url": self.endpoint_url,
            "region": self.region,
            "bucket": self.bucket,
            "results": [asdict(r) for r in self.results],
            "records": [r.to_dict() for r in self.records],
        }


@dataclass
class Context:
    endpoint: Endpoint
    client: Any
    recorder: CallRecorder
    bucket: str
    options: RunOptions
    state: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- helpers


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _strip_etag(etag: str | None) -> str:
    return (etag or "").strip('"')


def probe_error(fn: Callable[[], Any]) -> dict[str, Any]:
    """Run a call we expect might fail and summarise how it ended."""
    try:
        resp = fn()
    except ClientError as exc:
        meta = exc.response.get("ResponseMetadata", {})
        return {
            "outcome": "error",
            "status": meta.get("HTTPStatusCode"),
            "code": exc.response.get("Error", {}).get("Code"),
        }
    meta = (resp or {}).get("ResponseMetadata", {}) if isinstance(resp, dict) else {}
    return {"outcome": "ok", "status": meta.get("HTTPStatusCode"), "code": None}


def generate_parts(total: int, part_size: int, seed: int) -> Iterator[bytes]:
    """Yield deterministic pseudo-random parts without ever holding the whole object."""
    rng = random.Random(seed)
    remaining = total
    while remaining > 0:
        n = min(part_size, remaining)
        yield rng.randbytes(n)
        remaining -= n


def expected_multipart_etag(part_md5s: list[bytes]) -> str:
    return f"{hashlib.md5(b''.join(part_md5s)).hexdigest()}-{len(part_md5s)}"


# --------------------------------------------------------------------------- operations


def op_create_bucket(ctx: Context) -> dict[str, Any]:
    c, b = ctx.client, ctx.bucket
    kwargs: dict[str, Any] = {"Bucket": b}
    if ctx.endpoint.region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": ctx.endpoint.region}
    c.create_bucket(**kwargs)
    ctx.state["bucket_created"] = True

    head = c.head_bucket(Bucket=b)
    location = c.get_bucket_location(Bucket=b).get("LocationConstraint")
    region_header = head["ResponseMetadata"]["HTTPHeaders"].get("x-amz-bucket-region")

    # Creating a bucket you already own: AWS answers 200 in us-east-1 and
    # BucketAlreadyOwnedByYou elsewhere. Other implementations vary.
    recreate = probe_error(lambda: c.create_bucket(**kwargs))

    missing = probe_error(lambda: c.head_bucket(Bucket=f"{b}-does-not-exist"))
    return {
        "location_constraint": location,
        "bucket_region_header": region_header,
        "recreate_existing": recreate,
        "head_missing_bucket": missing,
    }


def op_put_get(ctx: Context) -> dict[str, Any]:
    c, b = ctx.client, ctx.bucket
    data = random.Random(ctx.options.seed).randbytes(ctx.options.small_object_kb * 1024)
    key = "objects/small.bin"

    put = c.put_object(Bucket=b, Key=key, Body=data, ContentType="application/octet-stream")
    etag = _strip_etag(put.get("ETag"))

    got = c.get_object(Bucket=b, Key=key)
    body = got["Body"].read()
    _check(hashlib.sha256(body).digest() == hashlib.sha256(data).digest(), "downloaded bytes differ from upload")
    _check(got["ContentLength"] == len(data), f"Content-Length {got['ContentLength']} != {len(data)}")

    head = c.head_object(Bucket=b, Key=key)
    ranged = c.get_object(Bucket=b, Key=key, Range="bytes=0-99")
    ranged_body = ranged["Body"].read()
    _check(ranged_body == data[:100], "range GET returned wrong bytes")

    return {
        "etag_is_md5": etag == _md5(data),
        "get_etag_matches_put": _strip_etag(got.get("ETag")) == etag,
        "head_content_type": head.get("ContentType"),
        "range_status": ranged["ResponseMetadata"]["HTTPStatusCode"],
        "range_content_range": ranged.get("ContentRange"),
        "get_missing_key": probe_error(lambda: c.get_object(Bucket=b, Key="objects/nope")),
        "head_missing_key": probe_error(lambda: c.head_object(Bucket=b, Key="objects/nope")),
        "invalid_range": probe_error(
            lambda: c.get_object(Bucket=b, Key=key, Range=f"bytes={len(data) + 10}-{len(data) + 20}")
        ),
    }


def op_multipart(ctx: Context) -> dict[str, Any]:
    c, b, opts = ctx.client, ctx.bucket, ctx.options
    key = "objects/multipart.bin"
    total = opts.multipart_size_mb * MiB
    part_size = opts.part_size_mb * MiB

    upload_id = c.create_multipart_upload(Bucket=b, Key=key)["UploadId"]
    ctx.state.setdefault("upload_ids", []).append((key, upload_id))

    whole = hashlib.sha256()
    part_md5s: list[bytes] = []
    parts = []
    upload_started = time.perf_counter()
    for number, chunk in enumerate(generate_parts(total, part_size, opts.seed), start=1):
        whole.update(chunk)
        digest = hashlib.md5(chunk)
        part_md5s.append(digest.digest())
        resp = c.upload_part(Bucket=b, Key=key, UploadId=upload_id, PartNumber=number, Body=chunk)
        _check(_strip_etag(resp["ETag"]) == digest.hexdigest(), f"part {number} ETag is not its MD5")
        parts.append({"PartNumber": number, "ETag": resp["ETag"]})
    upload_seconds = time.perf_counter() - upload_started

    listed = c.list_parts(Bucket=b, Key=key, UploadId=upload_id)
    listed_count = len(listed.get("Parts", []))

    done = c.complete_multipart_upload(
        Bucket=b, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
    )
    ctx.state["upload_ids"].remove((key, upload_id))
    etag = _strip_etag(done.get("ETag"))
    expected = expected_multipart_etag(part_md5s)

    head = c.head_object(Bucket=b, Key=key)
    _check(head["ContentLength"] == total, f"object size {head['ContentLength']} != {total}")

    download = hashlib.sha256()
    download_started = time.perf_counter()
    got = c.get_object(Bucket=b, Key=key)
    for chunk in got["Body"].iter_chunks(MiB):
        download.update(chunk)
    download_seconds = time.perf_counter() - download_started
    _check(download.digest() == whole.digest(), "multipart download checksum mismatch")

    parts_count_header = None
    try:
        h = c.head_object(Bucket=b, Key=key, PartNumber=1)
        part_head = {"outcome": "ok", "status": h["ResponseMetadata"]["HTTPStatusCode"], "code": None}
        parts_count_header = h["ResponseMetadata"]["HTTPHeaders"].get("x-amz-mp-parts-count")
    except ClientError as exc:
        part_head = {
            "outcome": "error",
            "status": exc.response["ResponseMetadata"].get("HTTPStatusCode"),
            "code": exc.response["Error"].get("Code"),
        }

    # A non-final part under 5 MiB should be rejected at completion time.
    small_key = "objects/too-small.bin"
    small_id = c.create_multipart_upload(Bucket=b, Key=small_key)["UploadId"]
    ctx.state["upload_ids"].append((small_key, small_id))
    small_parts = []
    for n in (1, 2):
        r = c.upload_part(Bucket=b, Key=small_key, UploadId=small_id, PartNumber=n, Body=b"x" * 1024)
        small_parts.append({"PartNumber": n, "ETag": r["ETag"]})
    small = probe_error(
        lambda: c.complete_multipart_upload(
            Bucket=b, Key=small_key, UploadId=small_id, MultipartUpload={"Parts": small_parts}
        )
    )
    if small["outcome"] == "error":
        c.abort_multipart_upload(Bucket=b, Key=small_key, UploadId=small_id)
    ctx.state["upload_ids"].remove((small_key, small_id))

    mb = total / MiB
    return {
        "size_bytes": total,
        "parts": len(parts),
        "listed_parts": listed_count,
        "etag": etag,
        "etag_matches_md5_of_md5s": etag == expected,
        "etag_part_suffix": etag.rsplit("-", 1)[1] if "-" in etag else None,
        "head_part_number": part_head,
        "parts_count_header": parts_count_header,
        "undersized_part_complete": small,
        "upload_mib_per_s": round(mb / upload_seconds, 1) if upload_seconds else None,
        "download_mib_per_s": round(mb / download_seconds, 1) if download_seconds else None,
    }


def _timed_get(recorder: CallRecorder, op: str, url: str, **kwargs) -> requests.Response:
    started = time.perf_counter()
    resp = requests.get(url, timeout=30, **kwargs)
    recorder.record_http(op, resp, (time.perf_counter() - started) * 1000)
    return resp


def op_presigned(ctx: Context) -> dict[str, Any]:
    c, b, rec = ctx.client, ctx.bucket, ctx.recorder
    key = "objects/presigned.txt"
    payload = f"presigned {uuid.uuid4()}".encode()
    c.put_object(Bucket=b, Key=key, Body=payload)

    url = c.generate_presigned_url("get_object", Params={"Bucket": b, "Key": key}, ExpiresIn=300)
    resp = _timed_get(rec, "PresignedGet", url)
    _check(resp.status_code == 200, f"presigned GET returned {resp.status_code}")
    _check(resp.content == payload, "presigned GET body mismatch")

    sig_at = url.find("X-Amz-Signature=") + len("X-Amz-Signature=")
    flipped = "0" if url[sig_at] != "0" else "1"
    tampered = _timed_get(rec, "PresignedGetTampered", url[:sig_at] + flipped + url[sig_at + 1 :])

    put_key = "objects/presigned-put.txt"
    put_url = c.generate_presigned_url("put_object", Params={"Bucket": b, "Key": put_key}, ExpiresIn=300)
    started = time.perf_counter()
    put_resp = requests.put(put_url, data=b"uploaded via presigned PUT", timeout=30)
    rec.record_http("PresignedPut", put_resp, (time.perf_counter() - started) * 1000)
    _check(put_resp.status_code == 200, f"presigned PUT returned {put_resp.status_code}")
    stored = c.get_object(Bucket=b, Key=put_key)["Body"].read()
    _check(stored == b"uploaded via presigned PUT", "presigned PUT stored wrong bytes")

    tampered_rec = next(r for r in reversed(rec.records) if r.operation == "PresignedGetTampered")
    obs: dict[str, Any] = {
        "get_status": resp.status_code,
        "tampered_status": tampered.status_code,
        "tampered_code": tampered_rec.error_code,
        "put_status": put_resp.status_code,
    }

    if ctx.options.presign_expiry_probe:
        short = c.generate_presigned_url("get_object", Params={"Bucket": b, "Key": key}, ExpiresIn=1)
        time.sleep(2.5)
        expired = _timed_get(rec, "PresignedGetExpired", short)
        obs["expired_status"] = expired.status_code
        obs["expired_code"] = rec.records[-1].error_code
    return obs


def op_versioning(ctx: Context) -> dict[str, Any]:
    c, b = ctx.client, ctx.bucket
    key = "versioned/doc.txt"
    before = c.get_bucket_versioning(Bucket=b).get("Status")
    c.put_bucket_versioning(Bucket=b, VersioningConfiguration={"Status": "Enabled"})
    ctx.state["versioning"] = True
    status = c.get_bucket_versioning(Bucket=b).get("Status")
    _check(status == "Enabled", f"versioning status is {status!r} after enabling")

    v1 = c.put_object(Bucket=b, Key=key, Body=b"first version").get("VersionId")
    v2 = c.put_object(Bucket=b, Key=key, Body=b"second version").get("VersionId")
    _check(bool(v1) and bool(v2) and v1 != v2, f"expected two distinct version ids, got {v1!r}, {v2!r}")

    listed = c.list_object_versions(Bucket=b, Prefix=key)
    versions = listed.get("Versions", [])
    _check(len(versions) == 2, f"expected 2 versions, listed {len(versions)}")
    latest = [v["VersionId"] for v in versions if v.get("IsLatest")]
    _check(latest == [v2], f"latest version should be {v2}, got {latest}")

    old = c.get_object(Bucket=b, Key=key, VersionId=v1)["Body"].read()
    _check(old == b"first version", "fetching the old version returned the wrong bytes")

    deleted = c.delete_object(Bucket=b, Key=key)
    after_delete = probe_error(lambda: c.get_object(Bucket=b, Key=key))
    listed_after = c.list_object_versions(Bucket=b, Prefix=key)

    return {
        "status_before": before,
        "version_id_length": len(v1),
        "version_id_sample": v1,
        "listed_versions": len(versions),
        "delete_marker": deleted.get("DeleteMarker"),
        "get_after_delete": after_delete,
        "delete_markers_listed": len(listed_after.get("DeleteMarkers", [])),
        "bogus_version_id": probe_error(lambda: c.get_object(Bucket=b, Key=key, VersionId="not-a-real-version")),
    }


def op_list_objects(ctx: Context) -> dict[str, Any]:
    c, b, opts = ctx.client, ctx.bucket, ctx.options
    expected = [f"listing/obj-{i:04d}" for i in range(opts.list_object_count)]
    nested = ["listing/dir-a/one", "listing/dir-a/two", "listing/dir-b/three"]
    for key in expected + nested:
        c.put_object(Bucket=b, Key=key, Body=b"")
    expected_all = sorted(expected + nested)

    seen: list[str] = []
    pages = 0
    token = None
    key_counts = []
    while True:
        kwargs: dict[str, Any] = {"Bucket": b, "Prefix": "listing/", "MaxKeys": opts.page_size}
        if token:
            kwargs["ContinuationToken"] = token
        page = c.list_objects_v2(**kwargs)
        pages += 1
        seen.extend(o["Key"] for o in page.get("Contents", []))
        key_counts.append(page.get("KeyCount"))
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        _check(bool(token), "IsTruncated set but no NextContinuationToken")
        _check(pages < 1000, "pagination never terminated")
    _check(seen == expected_all, f"v2 pagination returned {len(seen)} keys, expected {len(expected_all)}")

    # ListObjects v1: AWS only returns NextMarker when a delimiter is given.
    v1 = c.list_objects(Bucket=b, Prefix="listing/", MaxKeys=opts.page_size)
    v1_marker_pages = 1
    marker = v1["Contents"][-1]["Key"] if v1.get("Contents") else None
    v1_seen = [o["Key"] for o in v1.get("Contents", [])]
    while v1.get("IsTruncated") and marker:
        v1 = c.list_objects(Bucket=b, Prefix="listing/", MaxKeys=opts.page_size, Marker=marker)
        v1_marker_pages += 1
        v1_seen.extend(o["Key"] for o in v1.get("Contents", []))
        marker = v1["Contents"][-1]["Key"] if v1.get("Contents") else None
    _check(v1_seen == expected_all, f"v1 pagination returned {len(v1_seen)} keys")
    first_v1 = c.list_objects(Bucket=b, Prefix="listing/", MaxKeys=opts.page_size)

    delim = c.list_objects_v2(Bucket=b, Prefix="listing/", Delimiter="/")
    prefixes = sorted(p["Prefix"] for p in delim.get("CommonPrefixes", []))
    _check(prefixes == ["listing/dir-a/", "listing/dir-b/"], f"unexpected common prefixes {prefixes}")

    after = c.list_objects_v2(Bucket=b, Prefix="listing/", StartAfter=expected[-2])
    start_after_keys = [o["Key"] for o in after.get("Contents", [])]

    return {
        "objects": len(expected_all),
        "page_size": opts.page_size,
        "v2_pages": pages,
        "v2_key_counts": key_counts,
        "v1_pages": v1_marker_pages,
        "v1_next_marker_without_delimiter": "NextMarker" in first_v1,
        "common_prefixes": len(prefixes),
        "start_after_count": len(start_after_keys),
        "max_keys_zero": probe_error(lambda: c.list_objects_v2(Bucket=b, MaxKeys=0)),
    }


def cleanup(ctx: Context) -> dict[str, Any]:
    """Remove everything we created. Runs even if earlier steps blew up."""
    c, b = ctx.client, ctx.bucket
    obs: dict[str, Any] = {"aborted_uploads": 0, "deleted": 0, "delete_errors": 0}

    try:
        uploads = c.list_multipart_uploads(Bucket=b).get("Uploads", [])
    except ClientError as exc:
        if exc.response["Error"].get("Code") in ("NoSuchBucket", "404"):
            obs["bucket_missing"] = True
            return obs
        uploads = [{"Key": k, "UploadId": u} for k, u in ctx.state.get("upload_ids", [])]
    for up in uploads:
        try:
            c.abort_multipart_upload(Bucket=b, Key=up["Key"], UploadId=up["UploadId"])
            obs["aborted_uploads"] += 1
        except ClientError:
            pass

    targets: list[dict[str, str]] = []
    try:
        for page in c.get_paginator("list_object_versions").paginate(Bucket=b):
            for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
                targets.append({"Key": item["Key"], "VersionId": item["VersionId"]})
    except ClientError:
        for page in c.get_paginator("list_objects_v2").paginate(Bucket=b):
            targets.extend({"Key": o["Key"]} for o in page.get("Contents", []))

    if targets:
        obs["delete_non_empty"] = probe_error(lambda: c.delete_bucket(Bucket=b))

    for i in range(0, len(targets), 1000):
        resp = c.delete_objects(Bucket=b, Delete={"Objects": targets[i : i + 1000], "Quiet": True})
        obs["delete_errors"] += len(resp.get("Errors", []))
        obs["deleted"] += min(1000, len(targets) - i) - len(resp.get("Errors", []))

    c.delete_bucket(Bucket=b)
    obs["head_after_delete"] = probe_error(lambda: c.head_bucket(Bucket=b))
    return obs


OPERATIONS: dict[str, Callable[[Context], dict[str, Any]]] = {
    "create_bucket": op_create_bucket,
    "put_get": op_put_get,
    "multipart": op_multipart,
    "presigned": op_presigned,
    "versioning": op_versioning,
    "list_objects": op_list_objects,
}


def _run_one(ctx: Context, name: str, fn: Callable[[Context], dict[str, Any]]) -> OpResult:
    started = time.perf_counter()
    with ctx.recorder.step(name):
        try:
            obs = fn(ctx)
            status, error = "passed", None
        except CheckFailed as exc:
            obs, status, error = {}, "failed", str(exc)
        except ClientError as exc:
            err = exc.response.get("Error", {})
            obs, status, error = {}, "error", f"{err.get('Code')}: {err.get('Message')}"
        except Exception as exc:  # network trouble, bugs, etc.
            obs, status, error = {}, "error", f"{type(exc).__name__}: {exc}"
            if ctx.state.get("debug"):
                traceback.print_exc()
    return OpResult(name, status, round((time.perf_counter() - started) * 1000, 1), obs, error)


def make_bucket_name(prefix: str, endpoint: str) -> str:
    name = f"{prefix}-{endpoint}-{uuid.uuid4().hex[:10]}".lower().replace("_", "-")
    return name[:63].rstrip("-")


def run_endpoint(
    endpoint: Endpoint,
    options: RunOptions | None = None,
    *,
    client: Any = None,
    progress: Callable[[str, OpResult], None] | None = None,
) -> EndpointRun:
    options = options or RunOptions()
    recorder = CallRecorder(endpoint.name)
    if client is None:
        client = make_client(endpoint, checksums=options.checksums)
    # One unrecorded call first so connection setup and any lazy server-side
    # initialisation don't land on CreateBucket's latency.
    try:
        client.list_buckets()
    except Exception:
        pass
    recorder.attach(client)

    bucket = make_bucket_name(options.bucket_prefix, endpoint.name)
    ctx = Context(endpoint=endpoint, client=client, recorder=recorder, bucket=bucket, options=options)
    run = EndpointRun(endpoint.name, endpoint.endpoint_url, endpoint.region, bucket, records=recorder.records)

    selected = options.operations or list(OPERATIONS)
    names = ["create_bucket"] + [n for n in selected if n != "create_bucket"]
    try:
        for name in names:
            if name != "create_bucket" and not ctx.state.get("bucket_created"):
                result = OpResult(name, "skipped", error="bucket was not created")
            else:
                result = _run_one(ctx, name, OPERATIONS[name])
            run.results.append(result)
            if progress:
                progress(endpoint.name, result)
    finally:
        if ctx.state.get("bucket_created"):
            result = _run_one(ctx, "cleanup", cleanup)
        else:
            result = OpResult("cleanup", "skipped", error="nothing to clean up")
        run.results.append(result)
        if progress:
            progress(endpoint.name, result)
    return run
