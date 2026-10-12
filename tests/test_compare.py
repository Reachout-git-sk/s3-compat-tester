import pytest

from s3compat.client import CallRecord
from s3compat.compare import align, compare, etag_format, _percentile
from s3compat.operations import EndpointRun, OpResult


def rec(ep, step, op, status=200, latency=10.0, error=None, **headers):
    hdrs = {k.replace("_", "-"): v for k, v in headers.items()}
    return CallRecord(ep, step, op, status, latency, hdrs, sorted(hdrs), error)


def run(ep, records, results=None):
    r = EndpointRun(ep, None, "us-east-1", f"bucket-{ep}", records=records)
    r.results = results or [OpResult(s, "passed") for s in dict.fromkeys(x.step for x in records)]
    return r


@pytest.mark.parametrize(
    "etag, expected",
    [
        ('"5d41402abc4b2a76b9719d911017c592"', "md5"),
        ("5d41402abc4b2a76b9719d911017c592", "md5"),
        ('"5D41402ABC4B2A76B9719D911017C592-13"', "multipart-13"),
        ('W/"abc"', "weak"),
        ('"not-an-md5"', "other"),
        (None, None),
    ],
)
def test_etag_format(etag, expected):
    assert etag_format(etag) == expected


def test_identical_runs_have_no_differences():
    a = run("a", [rec("a", "put", "PutObject", etag='"' + "0" * 32 + '"', server="S")])
    b = run("b", [rec("b", "put", "PutObject", etag='"' + "1" * 32 + '"', server="S")])
    assert compare([a, b]).differences == []


def test_status_and_error_code_mismatch_are_errors():
    a = run("aws", [rec("aws", "create", "CreateBucket", 200)])
    b = run("wasabi", [rec("wasabi", "create", "CreateBucket", 409, error="BucketAlreadyExists")])
    cmp = compare([a, b])
    cats = {(d.category, d.severity) for d in cmp.differences}
    assert ("status", "error") in cats
    assert ("error_code", "error") in cats
    status = next(d for d in cmp.differences if d.category == "status")
    assert status.values == {"aws": 200, "wasabi": 409}
    assert cmp.aligned[0].flags == ["status", "error-code"]


def test_multipart_etag_suffix_difference_is_flagged():
    a = run("aws", [rec("aws", "mp", "HeadObject", etag='"' + "a" * 32 + '-13"')])
    b = run("minio", [rec("minio", "mp", "HeadObject", etag='"' + "b" * 32 + '"')])
    d = next(d for d in compare([a, b]).differences if d.category == "etag_format")
    assert d.values == {"aws": "multipart-13", "minio": "md5"}


def test_etag_quoting_difference():
    a = run("a", [rec("a", "s", "PutObject", etag='"' + "a" * 32 + '"')])
    b = run("b", [rec("b", "s", "PutObject", etag="a" * 32)])
    cats = [d.category for d in compare([a, b]).differences]
    assert "etag_quoting" in cats and "etag_format" not in cats


def test_missing_headers_are_aggregated_across_calls():
    a = run("aws", [rec("aws", "s", "PutObject", x_amz_request_id="1"), rec("aws", "s", "GetObject", x_amz_request_id="2")])
    b = run("moto", [rec("moto", "s", "PutObject"), rec("moto", "s", "GetObject")])
    diffs = [d for d in compare([a, b]).differences if d.category == "header_missing"]
    assert len(diffs) == 1
    assert "x-amz-request-id not returned by moto on 2 call(s)" in diffs[0].detail
    assert diffs[0].values == {"moto": "missing (2)"}


def test_server_header_difference_is_info():
    a = run("aws", [rec("aws", "s", "PutObject", server="AmazonS3")])
    b = run("minio", [rec("minio", "s", "PutObject", server="MinIO")])
    d = compare([a, b]).differences
    assert [(x.category, x.severity) for x in d] == [("server", "info")]


def test_alignment_pairs_by_occurrence_and_reports_missing_calls():
    a = run("a", [rec("a", "mp", "UploadPart"), rec("a", "mp", "UploadPart"), rec("a", "mp", "UploadPart")])
    b = run("b", [rec("b", "mp", "UploadPart", status=400, error="InvalidRequest")])
    aligned = align([a, b])
    assert [x.occurrence for x in aligned] == [0, 1, 2]
    assert aligned[1].calls["b"] is None
    cmp = compare([a, b])
    missing = [d for d in cmp.differences if d.category == "call_missing"]
    assert len(missing) == 1 and missing[0].detail.startswith("2 call(s) not made on b")


def test_alignment_follows_step_order():
    a = run("a", [rec("a", "one", "X"), rec("a", "two", "Y")])
    b = run("b", [rec("b", "two", "Y"), rec("b", "one", "X")], results=[OpResult("one", "passed"), OpResult("two", "passed")])
    assert [x.step for x in align([a, b])] == ["one", "two"]


def test_behaviour_differences_come_from_observations_and_skip_volatile():
    a = run("aws", [], [OpResult("versioning", "passed", observations={"listed": 2, "version_id_sample": "x", "probe": {"code": "NoSuchVersion"}})])
    b = run("wasabi", [], [OpResult("versioning", "passed", observations={"listed": 2, "version_id_sample": "y", "probe": {"code": "InvalidArgument"}})])
    diffs = compare([a, b]).differences
    assert [(d.category, d.detail) for d in diffs] == [("behaviour", "probe.code")]
    assert diffs[0].values == {"aws": "NoSuchVersion", "wasabi": "InvalidArgument"}


def test_outcome_difference_includes_error_text():
    a = run("a", [], [OpResult("multipart", "passed")])
    b = run("b", [], [OpResult("multipart", "error", error="InvalidRequest: checksum type mismatch")])
    d = compare([a, b]).differences[0]
    assert d.category == "result" and d.severity == "error"
    assert "checksum type mismatch" in d.detail


def test_three_way_comparison():
    runs = [
        run("aws", [rec("aws", "s", "HeadObject", 200)]),
        run("wasabi", [rec("wasabi", "s", "HeadObject", 200)]),
        run("minio", [rec("minio", "s", "HeadObject", 206)]),
    ]
    d = next(x for x in compare(runs).differences if x.category == "status")
    assert d.values == {"aws": 200, "wasabi": 200, "minio": 206}


def test_differences_sorted_by_severity():
    a = run("a", [rec("a", "s", "PutObject", 200, server="A")])
    b = run("b", [rec("b", "s", "PutObject", 500, server="B")])
    severities = [d.severity for d in compare([a, b]).differences]
    assert severities == sorted(severities, key=["error", "warning", "info"].index)


def test_latency_stats():
    records = [rec("a", "s", "GetObject", latency=v) for v in (10, 20, 30, 40, 100)]
    stats = compare([run("a", records)]).latency
    (s,) = stats
    assert (s.count, s.mean_ms, s.p50_ms, s.max_ms) == (5, 40.0, 30.0, 100)
    assert s.p95_ms == pytest.approx(88.0)


def test_percentile_single_value():
    assert _percentile([7.0], 0.95) == 7.0


def test_to_dict_is_json_serialisable():
    import json

    a = run("a", [rec("a", "s", "PutObject", 200)])
    b = run("b", [rec("b", "s", "PutObject", 403, error="AccessDenied")])
    data = compare([a, b]).to_dict()
    json.dumps(data)
    assert data["summary"]["error"] == 2
