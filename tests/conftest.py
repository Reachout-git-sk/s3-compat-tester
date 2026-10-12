import os
import socket
from urllib.parse import urlparse

import pytest
from moto import mock_aws

from s3compat.config import Endpoint
from s3compat.operations import RunOptions


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="run tests against real endpoints")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live test; pass --live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def fake_aws_env(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL_S3", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def mocked(fake_aws_env):
    with mock_aws():
        yield


@pytest.fixture
def aws_endpoint():
    return Endpoint("aws", "us-east-1", "testing", "testing")


@pytest.fixture
def small_options():
    return RunOptions(
        multipart_size_mb=11,
        part_size_mb=5,
        small_object_kb=16,
        list_object_count=12,
        page_size=5,
        presign_expiry_probe=False,
    )


@pytest.fixture
def moto_url(fake_aws_env):
    from moto.server import ThreadedMotoServer

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


def reachable(url: str) -> bool:
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture
def clean_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("AWS_", "WASABI_", "MINIO_", "LOCALSTACK_")):
            monkeypatch.delenv(var, raising=False)
