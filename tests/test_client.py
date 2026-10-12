import socket
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from s3compat.client import CallRecorder, capture_headers, make_client
from s3compat.config import Endpoint


def test_capture_headers_lowercases_and_picks_key_headers():
    raw = {"ETag": '"abc"', "X-Amz-Request-Id": "R1", "Server": "AmazonS3", "X-Custom": "1", "Content-Length": "5"}
    picked, names = capture_headers(raw)
    assert picked == {"x-amz-request-id": "R1", "etag": '"abc"', "server": "AmazonS3", "content-length": "5"}
    assert names == ["content-length", "etag", "server", "x-amz-request-id", "x-custom"]


def test_capture_headers_handles_none():
    assert capture_headers(None) == ({}, [])


def test_recorder_captures_status_headers_latency_and_step(mocked, aws_endpoint):
    rec = CallRecorder("aws")
    client = make_client(aws_endpoint, rec)
    with rec.step("setup"):
        client.create_bucket(Bucket="bucket-one")
    with rec.step("upload"):
        client.put_object(Bucket="bucket-one", Key="k", Body=b"hello")

    assert [(r.step, r.operation) for r in rec.records] == [("setup", "CreateBucket"), ("upload", "PutObject")]
    put = rec.records[1]
    assert put.status == 200
    assert put.headers["etag"] == '"5d41402abc4b2a76b9719d911017c592"'
    assert put.latency_ms >= 0
    assert put.error_code is None
    assert "etag" in put.header_names


def test_recorder_records_error_responses(mocked, aws_endpoint):
    rec = CallRecorder("aws")
    client = make_client(aws_endpoint, rec)
    client.create_bucket(Bucket="bucket-one")
    with pytest.raises(ClientError):
        client.get_object(Bucket="bucket-one", Key="missing")
    last = rec.records[-1]
    assert (last.operation, last.status, last.error_code) == ("GetObject", 404, "NoSuchKey")


def test_step_context_restores_previous_step():
    rec = CallRecorder("x")
    with rec.step("outer"):
        with rec.step("inner"):
            assert rec.current_step == "inner"
        assert rec.current_step == "outer"
    assert rec.current_step == "setup"


def test_recorder_records_transport_failures():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # nothing listens on the port once the socket closes
    ep = Endpoint("dead", "us-east-1", "a", "b", endpoint_url=f"http://127.0.0.1:{port}", addressing_style="path")
    rec = CallRecorder("dead")
    client = make_client(ep, rec)
    with pytest.raises(EndpointConnectionError):
        client.list_buckets()
    assert len(rec.records) == 1
    r = rec.records[0]
    assert r.operation == "ListBuckets"
    assert r.status is None
    assert r.error_code == "EndpointConnectionError"


def test_record_http_parses_xml_error_code():
    rec = CallRecorder("x")
    resp = SimpleNamespace(
        status_code=403,
        headers={"Server": "MinIO", "X-Amz-Request-Id": "abc"},
        text="<Error><Code>SignatureDoesNotMatch</Code><Message>nope</Message></Error>",
    )
    r = rec.record_http("PresignedGet", resp, 12.345)
    assert (r.status, r.error_code, r.latency_ms) == (403, "SignatureDoesNotMatch", 12.35)
    assert r.headers["server"] == "MinIO"


def test_recorder_over_real_http(moto_url):
    ep = Endpoint("moto", "us-east-1", "testing", "testing", endpoint_url=moto_url, addressing_style="path")
    rec = CallRecorder("moto")
    client = make_client(ep, rec)
    client.create_bucket(Bucket="http-bucket")
    client.put_object(Bucket="http-bucket", Key="a", Body=b"abc")
    head = rec.records[-1]
    assert head.status == 200
    assert "server" in head.headers
    assert int(rec.records[-1].headers.get("content-length", "0")) >= 0
