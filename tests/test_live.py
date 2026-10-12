"""Runs the full suite against real endpoints.

Only runs with ``pytest --live`` and only for endpoints whose credentials are
in the environment (or, for local MinIO/LocalStack, whose port is reachable).
"""

import os

import pytest

from s3compat.compare import compare
from s3compat.config import BUILTIN_SPECS, resolve
from s3compat.operations import RunOptions, run_endpoint

from .conftest import reachable

pytestmark = pytest.mark.live


def _endpoint_or_skip(name):
    spec = BUILTIN_SPECS[name]
    ep = resolve(spec)
    if spec.default_access_key is None:
        if not (os.environ.get(spec.access_key_env) and os.environ.get(spec.secret_key_env)):
            pytest.skip(f"set {spec.access_key_env} and {spec.secret_key_env} to run against {name}")
    elif not reachable(ep.endpoint_url):
        pytest.skip(f"{name} not reachable at {ep.endpoint_url}")
    return ep


def _options():
    return RunOptions(
        multipart_size_mb=int(os.environ.get("S3COMPAT_LIVE_MULTIPART_MB", "16")),
        part_size_mb=5,
        list_object_count=12,
        page_size=5,
        bucket_prefix="s3compat-live",
        checksums=os.environ.get("S3COMPAT_CHECKSUMS", "when_supported"),
    )


@pytest.mark.parametrize("name", ["aws", "wasabi", "minio", "localstack"])
def test_full_suite_against_live_endpoint(name):
    ep = _endpoint_or_skip(name)
    run = run_endpoint(ep, _options())
    assert run.result("cleanup").status in ("passed", "skipped")
    failures = [(r.name, r.error) for r in run.results if r.status in ("failed", "error")]
    assert not failures, failures


def test_aws_vs_wasabi():
    aws, wasabi = _endpoint_or_skip("aws"), _endpoint_or_skip("wasabi")
    runs = [run_endpoint(aws, _options()), run_endpoint(wasabi, _options())]
    cmp = compare(runs)
    assert cmp.endpoints == ["aws", "wasabi"]
    for diff in cmp.differences:
        print(diff.severity, diff.category, diff.step, diff.operation, diff.detail, diff.values)
