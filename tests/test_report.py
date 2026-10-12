import json

from rich.console import Console

from s3compat.client import CallRecord
from s3compat.compare import compare
from s3compat.operations import EndpointRun, OpResult, RunOptions
from s3compat.report import build_markdown, print_report, write_reports


def sample_runs():
    def r(ep, status, etag, code=None):
        return CallRecord(ep, "multipart", "HeadObject", status, 12.5, {"etag": etag}, ["etag"], code)

    aws = EndpointRun("aws", None, "us-east-1", "b-aws", records=[r("aws", 200, '"' + "a" * 32 + '-3"')])
    aws.results = [OpResult("multipart", "passed", 100, {"etag": "x-3", "upload_mib_per_s": 50.0, "download_mib_per_s": 80.0})]
    other = EndpointRun("minio", "http://localhost:9000", "us-east-1", "b-minio", records=[r("minio", 200, '"' + "b" * 32 + '"')])
    other.results = [OpResult("multipart", "failed", 100, {}, error="bad | pipe")]
    return [aws, other]


def test_markdown_has_all_sections_and_escapes_pipes():
    runs = sample_runs()
    md = build_markdown(compare(runs), runs, RunOptions())
    for heading in ("# S3 compatibility report", "## Endpoints", "## Operation results", "## Differences",
                    "## Calls side by side", "## Latency (ms)", "## Multipart throughput"):
        assert heading in md
    assert r"bad \| pipe" in md
    assert "| multipart | HeadObject | 200 12ms multipart-3 | 200 12ms md5 |" in md


def test_write_reports_creates_json_and_markdown(tmp_path):
    runs = sample_runs()
    json_path, md_path = write_reports(tmp_path / "out", compare(runs), runs, RunOptions())
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["comparison"]["endpoints"] == ["aws", "minio"]
    assert data["runs"][0]["records"][0]["status"] == 200
    assert {d["category"] for d in data["comparison"]["differences"]} >= {"etag_format", "result"}
    assert md_path.read_text(encoding="utf-8").startswith("# S3 compatibility report")


def test_console_report_shows_flagged_calls():
    runs = sample_runs()
    console = Console(record=True, width=200)
    print_report(compare(runs), runs, console)
    text = console.export_text()
    assert "HeadObject" in text and "multipart-3" in text
    assert "etag_format" in text
