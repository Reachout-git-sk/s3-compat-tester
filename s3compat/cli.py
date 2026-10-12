"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import ExitStack, contextmanager
from typing import Iterator

from rich.console import Console

from . import __version__
from .compare import compare
from .config import Endpoint, load_endpoints, load_specs
from .operations import OPERATIONS, OpResult, RunOptions, run_endpoint
from .report import print_report, write_reports

# Wide tables are the whole point; don't squeeze them to 80 columns when piped.
console = Console(width=None if sys.stdout.isatty() else 200)


@contextmanager
def moto_server(port: int) -> Iterator[str]:
    """Run moto's S3 server in a thread as an offline stand-in for AWS."""
    try:
        from moto.server import ThreadedMotoServer
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("--moto needs moto[server]: pip install 'moto[server]'") from exc
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="s3compat", description="Compare S3 API behaviour across S3-compatible endpoints.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the operation suite against endpoints and compare them")
    run.add_argument("-t", "--targets", default="aws,wasabi", help="comma separated endpoint names (default: aws,wasabi)")
    run.add_argument("-c", "--config", help="TOML file with extra endpoint definitions")
    run.add_argument("--ops", help=f"comma separated subset of: {', '.join(OPERATIONS)}")
    run.add_argument("--multipart-size-mb", type=int, default=100, help="multipart object size in MiB (default 100)")
    run.add_argument("--part-size-mb", type=int, default=8, help="multipart part size in MiB, min 5 (default 8)")
    run.add_argument("--small-object-kb", type=int, default=256)
    run.add_argument("--objects", type=int, default=25, help="objects created for the listing test")
    run.add_argument("--page-size", type=int, default=10, help="MaxKeys used when paginating")
    run.add_argument("--bucket-prefix", default="s3compat")
    run.add_argument("--checksums", choices=["when_supported", "when_required"], default="when_supported",
                     help="botocore request/response checksum mode")
    run.add_argument("--no-expiry-probe", action="store_true", help="skip the expired presigned URL check (saves ~3s)")
    run.add_argument("--report-dir", default="reports", help="where JSON and Markdown reports go")
    run.add_argument("--no-report", action="store_true", help="don't write report files")
    run.add_argument("--all-calls", action="store_true", help="show every call in the side-by-side table, not just flagged ones")
    run.add_argument("--moto", nargs="?", const=5000, type=int, metavar="PORT",
                     help="start a local moto server and use it as the 'aws' endpoint")
    run.add_argument("--fail-on-diff", action="store_true", help="exit 1 if any error-level difference is found")

    ls = sub.add_parser("endpoints", help="show configured endpoints and whether credentials are present")
    ls.add_argument("-c", "--config")
    return p


def _progress(endpoint: str, result: OpResult) -> None:
    style = {"passed": "green", "failed": "red", "error": "red", "skipped": "dim"}[result.status]
    console.print(f"  {endpoint:<10} {result.name:<14} [{style}]{result.status:<7}[/] {result.duration_ms:>9.0f} ms")


def cmd_endpoints(args) -> int:
    for name in load_specs(args.config):
        ep = load_endpoints([name], args.config)[0]
        state = "[green]credentials found[/]" if ep.has_credentials else f"[red]missing {', '.join(ep.missing)}[/]"
        console.print(f"{ep.describe()}: {state}")
    return 0


def cmd_run(args) -> int:
    names = [n.strip() for n in args.targets.split(",") if n.strip()]
    ops = [o.strip() for o in args.ops.split(",")] if args.ops else None
    if ops:
        bad = [o for o in ops if o not in OPERATIONS]
        if bad:
            console.print(f"[red]unknown operation(s): {', '.join(bad)}[/]")
            return 2
    if args.part_size_mb < 5:
        console.print("[red]--part-size-mb must be at least 5 (S3 minimum for non-final parts)[/]")
        return 2

    options = RunOptions(
        multipart_size_mb=args.multipart_size_mb,
        part_size_mb=args.part_size_mb,
        small_object_kb=args.small_object_kb,
        list_object_count=args.objects,
        page_size=args.page_size,
        bucket_prefix=args.bucket_prefix,
        operations=ops,
        checksums=args.checksums,
        presign_expiry_probe=not args.no_expiry_probe,
    )

    with ExitStack() as stack:
        try:
            endpoints = load_endpoints(names, args.config)
        except KeyError as exc:
            console.print(f"[red]{exc.args[0]}[/]")
            return 2

        if args.moto is not None:
            url = stack.enter_context(moto_server(args.moto))
            endpoints = [
                Endpoint("aws", "us-east-1", "testing", "testing", endpoint_url=url, addressing_style="path")
                if e.name == "aws" else e
                for e in endpoints
            ]
            console.print(f"[dim]moto server standing in for aws at {url}[/]")

        usable: list[Endpoint] = []
        for ep in endpoints:
            if ep.has_credentials:
                usable.append(ep)
            else:
                console.print(f"[yellow]skipping {ep.name}: set {', '.join(ep.missing)}[/]")
        if not usable:
            console.print("[red]no endpoint has credentials; nothing to run[/]")
            return 2

        runs = []
        for ep in usable:
            console.print(f"[bold]{ep.describe()}[/]")
            runs.append(run_endpoint(ep, options, progress=_progress))

    comparison = compare(runs)
    console.print()
    print_report(comparison, runs, console, all_calls=args.all_calls)

    if not args.no_report:
        json_path, md_path = write_reports(args.report_dir, comparison, runs, options)
        console.print(f"reports: {json_path}  {md_path}")

    if len(runs) < 2:
        console.print("[yellow]only one endpoint ran, so there was nothing to compare against[/]")
    if args.fail_on_diff and comparison.counts()["error"]:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "endpoints":
        return cmd_endpoints(args)
    return cmd_run(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
