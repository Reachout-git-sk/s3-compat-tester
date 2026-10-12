import hashlib

import pytest

from s3compat import operations as ops
from s3compat.client import CallRecorder, make_client
from s3compat.operations import (
    MiB,
    Context,
    RunOptions,
    expected_multipart_etag,
    generate_parts,
    make_bucket_name,
    run_endpoint,
)


@pytest.fixture
def ctx(mocked, aws_endpoint, small_options):
    rec = CallRecorder("aws")
    client = make_client(aws_endpoint, rec)
    c = Context(aws_endpoint, client, rec, make_bucket_name("t", "aws"), small_options)
    with rec.step("create_bucket"):
        ops.op_create_bucket(c)
    return c


def test_generate_parts_is_deterministic_and_sized():
    parts = list(generate_parts(12 * MiB + 7, 5 * MiB, seed=1))
    assert [len(p) for p in parts] == [5 * MiB, 5 * MiB, 2 * MiB + 7]
    again = list(generate_parts(12 * MiB + 7, 5 * MiB, seed=1))
    assert hashlib.sha256(b"".join(parts)).digest() == hashlib.sha256(b"".join(again)).digest()


def test_expected_multipart_etag_format():
    md5s = [hashlib.md5(b"a").digest(), hashlib.md5(b"b").digest()]
    etag = expected_multipart_etag(md5s)
    assert etag.endswith("-2") and len(etag) == 34


def test_bucket_name_is_valid():
    name = make_bucket_name("S3Compat_Prefix", "my_endpoint" * 10)
    assert len(name) <= 63 and name == name.lower() and "_" not in name and not name.endswith("-")


def test_create_bucket_records_probes(mocked, aws_endpoint, small_options):
    rec = CallRecorder("aws")
    c = Context(aws_endpoint, make_client(aws_endpoint, rec), rec, "probe-bucket", small_options)
    obs = ops.op_create_bucket(c)
    # moto mirrors us-east-1: re-creating your own bucket succeeds
    assert obs["recreate_existing"]["outcome"] == "ok"
    assert obs["head_missing_bucket"]["status"] == 404
    assert [r.operation for r in rec.records] == [
        "CreateBucket", "HeadBucket", "GetBucketLocation", "CreateBucket", "HeadBucket",
    ]


def test_put_get_verifies_content(ctx):
    obs = ops.op_put_get(ctx)
    assert obs["etag_is_md5"] is True
    assert obs["range_status"] == 206
    assert obs["get_missing_key"]["code"] == "NoSuchKey"
    assert obs["head_missing_key"]["status"] == 404


def test_put_get_detects_corruption(ctx, monkeypatch):
    real_get = ctx.client.get_object

    class Corrupt:
        def __init__(self, resp):
            self._resp = resp

        def __getitem__(self, k):
            if k == "Body":
                return type("B", (), {"read": lambda _self: b"garbage"})()
            return self._resp[k]

        def get(self, k, d=None):
            return self._resp.get(k, d)

    monkeypatch.setattr(ctx.client, "get_object", lambda **kw: Corrupt(real_get(**kw)))
    with pytest.raises(ops.CheckFailed, match="differ"):
        ops.op_put_get(ctx)


def test_multipart_etag_and_checksum(ctx):
    obs = ops.op_multipart(ctx)
    assert obs["parts"] == 3
    assert obs["listed_parts"] == 3
    assert obs["etag_part_suffix"] == "3"
    assert obs["etag_matches_md5_of_md5s"] is True
    assert obs["undersized_part_complete"]["code"] == "EntityTooSmall"
    assert ctx.state["upload_ids"] == []


def test_presigned_get_and_put(ctx):
    obs = ops.op_presigned(ctx)
    assert obs["get_status"] == 200
    assert obs["put_status"] == 200
    ops_seen = [r.operation for r in ctx.recorder.records]
    assert "PresignedGet" in ops_seen and "PresignedPut" in ops_seen


def test_versioning_lists_two_versions(ctx):
    obs = ops.op_versioning(ctx)
    assert obs["listed_versions"] == 2
    assert obs["delete_marker"] is True
    assert obs["get_after_delete"]["status"] == 404
    assert obs["delete_markers_listed"] == 1


def test_list_objects_paginates(ctx):
    obs = ops.op_list_objects(ctx)
    # 12 flat + 3 nested keys in pages of 5
    assert obs["objects"] == 15
    assert obs["v2_pages"] == 3
    assert obs["v2_key_counts"] == [5, 5, 5]
    assert obs["common_prefixes"] == 2


def test_cleanup_removes_versions_and_bucket(ctx):
    ops.op_versioning(ctx)
    ctx.client.create_multipart_upload(Bucket=ctx.bucket, Key="dangling")
    obs = ops.cleanup(ctx)
    assert obs["aborted_uploads"] == 1
    assert obs["deleted"] == 3  # two versions plus the delete marker
    assert obs["delete_non_empty"]["code"] == "BucketNotEmpty"
    assert obs["head_after_delete"]["status"] == 404


def test_run_endpoint_all_operations_pass(mocked, aws_endpoint, small_options):
    run = run_endpoint(aws_endpoint, small_options)
    statuses = {r.name: r.status for r in run.results}
    assert statuses == {
        "create_bucket": "passed",
        "put_get": "passed",
        "multipart": "passed",
        "presigned": "passed",
        "versioning": "passed",
        "list_objects": "passed",
        "cleanup": "passed",
    }
    assert all(r.endpoint == "aws" for r in run.records)
    assert {r.step for r in run.records} == set(statuses)


def test_cleanup_runs_even_when_an_operation_blows_up(mocked, aws_endpoint, small_options, monkeypatch):
    def boom(ctx):
        ctx.client.put_object(Bucket=ctx.bucket, Key="left-behind", Body=b"x")
        raise RuntimeError("kaboom")

    monkeypatch.setitem(ops.OPERATIONS, "put_get", boom)
    small_options.operations = ["put_get"]
    run = run_endpoint(aws_endpoint, small_options)
    assert run.result("put_get").status == "error"
    assert "kaboom" in run.result("put_get").error
    assert run.result("cleanup").status == "passed"
    assert run.result("cleanup").observations["deleted"] == 1


def test_operations_skip_when_bucket_creation_fails(mocked, aws_endpoint, small_options, monkeypatch):
    def fail(ctx):
        raise ops.CheckFailed("no bucket for you")

    monkeypatch.setitem(ops.OPERATIONS, "create_bucket", fail)
    small_options.operations = ["put_get"]
    run = run_endpoint(aws_endpoint, small_options)
    assert [(r.name, r.status) for r in run.results] == [
        ("create_bucket", "failed"), ("put_get", "skipped"), ("cleanup", "skipped"),
    ]


def test_run_against_moto_server_over_http(moto_url, small_options):
    from s3compat.config import Endpoint

    ep = Endpoint("aws", "us-east-1", "testing", "testing", endpoint_url=moto_url, addressing_style="path")
    run = run_endpoint(ep, small_options)
    assert all(r.ok for r in run.results), [(r.name, r.error) for r in run.results]
    presigned = [r for r in run.records if r.operation == "PresignedGet"]
    assert presigned and presigned[0].status == 200
