import pytest

from s3compat.config import load_endpoints, load_specs, resolve, BUILTIN_SPECS


def test_aws_reads_standard_env_vars():
    env = {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret", "AWS_REGION": "eu-west-1"}
    ep = resolve(BUILTIN_SPECS["aws"], env)
    assert ep.has_credentials
    assert (ep.access_key, ep.secret_key, ep.region) == ("AKIA", "secret", "eu-west-1")
    assert ep.endpoint_url is None


def test_wasabi_defaults_endpoint_and_reports_missing_credentials():
    ep = resolve(BUILTIN_SPECS["wasabi"], {})
    assert ep.endpoint_url == "https://s3.wasabisys.com"
    assert not ep.has_credentials
    assert ep.missing == ["WASABI_ACCESS_KEY_ID", "WASABI_SECRET_ACCESS_KEY"]


def test_wasabi_endpoint_and_region_override():
    env = {
        "WASABI_ACCESS_KEY_ID": "k",
        "WASABI_SECRET_ACCESS_KEY": "s",
        "WASABI_ENDPOINT": "https://s3.eu-central-1.wasabisys.com",
        "WASABI_REGION": "eu-central-1",
    }
    ep = resolve(BUILTIN_SPECS["wasabi"], env)
    assert ep.endpoint_url == "https://s3.eu-central-1.wasabisys.com"
    assert ep.region == "eu-central-1"


def test_minio_falls_back_to_image_defaults():
    ep = resolve(BUILTIN_SPECS["minio"], {})
    assert ep.has_credentials
    assert ep.endpoint_url == "http://localhost:9000"
    assert ep.addressing_style == "path"


def test_blank_env_values_count_as_missing():
    ep = resolve(BUILTIN_SPECS["aws"], {"AWS_ACCESS_KEY_ID": "  ", "AWS_SECRET_ACCESS_KEY": ""})
    assert not ep.has_credentials


def test_toml_adds_endpoint_and_overrides_builtin(tmp_path):
    cfg = tmp_path / "endpoints.toml"
    cfg.write_text(
        """
[endpoints.ceph]
endpoint_url = "https://ceph.example:7480"
region = "default"
addressing_style = "path"

[endpoints.wasabi]
region = "ap-northeast-1"
endpoint_url = "https://s3.ap-northeast-1.wasabisys.com"
""",
        encoding="utf-8",
    )
    specs = load_specs(cfg)
    assert specs["ceph"].access_key_env == "CEPH_ACCESS_KEY_ID"
    env = {"CEPH_ACCESS_KEY_ID": "a", "CEPH_SECRET_ACCESS_KEY": "b"}
    ceph, wasabi = load_endpoints(["ceph", "wasabi"], cfg, env)
    assert ceph.endpoint_url == "https://ceph.example:7480"
    assert ceph.has_credentials and ceph.addressing_style == "path"
    assert wasabi.region == "ap-northeast-1"
    assert wasabi.endpoint_url == "https://s3.ap-northeast-1.wasabisys.com"
    assert wasabi.missing == ["WASABI_ACCESS_KEY_ID", "WASABI_SECRET_ACCESS_KEY"]


def test_unknown_endpoint_raises():
    with pytest.raises(KeyError, match="unknown endpoint"):
        load_endpoints(["nope"], env={})
