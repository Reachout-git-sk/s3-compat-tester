import json

from s3compat import cli


def test_endpoints_command_lists_builtins(clean_env, capsys):
    assert cli.main(["endpoints"]) == 0
    out = capsys.readouterr().out
    assert "wasabi" in out and "missing WASABI_ACCESS_KEY_ID" in out


def test_run_refuses_without_credentials(clean_env):
    assert cli.main(["run", "-t", "aws,wasabi", "--no-report"]) == 2


def test_run_rejects_unknown_operation(clean_env):
    assert cli.main(["run", "--ops", "teleport"]) == 2


def test_run_rejects_tiny_parts(clean_env):
    assert cli.main(["run", "--part-size-mb", "1"]) == 2


def test_run_two_endpoints_on_moto_server(moto_url, tmp_path, monkeypatch):
    # Point "aws" and a TOML-defined second endpoint at the same moto server:
    # the comparison should find no error-level differences.
    cfg = tmp_path / "endpoints.toml"
    cfg.write_text(f'[endpoints.twin]\nendpoint_url = "{moto_url}"\naddressing_style = "path"\n', encoding="utf-8")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", moto_url)
    monkeypatch.setenv("TWIN_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("TWIN_SECRET_ACCESS_KEY", "testing")
    code = cli.main([
        "run", "-t", "aws,twin", "-c", str(cfg),
        "--multipart-size-mb", "6", "--part-size-mb", "5", "--objects", "6", "--page-size", "4",
        "--no-expiry-probe", "--report-dir", str(tmp_path / "reports"), "--fail-on-diff",
    ])
    assert code == 0
    (report,) = (tmp_path / "reports").glob("*.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["comparison"]["summary"]["error"] == 0
    assert all(s == "passed" for s in data["comparison"]["step_results"]["twin"].values())


def test_run_with_builtin_moto_flag(fake_aws_env, tmp_path):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    code = cli.main([
        "run", "-t", "aws", "--moto", str(port), "--ops", "put_get,list_objects",
        "--objects", "3", "--no-report",
    ])
    assert code == 0
