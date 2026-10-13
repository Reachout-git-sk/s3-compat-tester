"""Console, JSON and Markdown output for a comparison run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .compare import AlignedCall, Comparison, etag_format
from .operations import EndpointRun, RunOptions

SEVERITY_STYLE = {"error": "bold red", "warning": "yellow", "info": "cyan"}
STATUS_STYLE = {"passed": "green", "failed": "red", "error": "red", "skipped": "dim"}


def _cell(rec) -> str:
    if rec is None:
        return "-"
    status = str(rec.status) if rec.status is not None else "ERR"
    parts = [status, f"{rec.latency_ms:.0f}ms"]
    fmt = etag_format(rec.headers.get("etag"))
    if fmt:
        parts.append(fmt)
    if rec.error_code:
        parts.append(rec.error_code)
    return " ".join(parts)


def _fmt(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def print_report(comparison: Comparison, runs: list[EndpointRun], console: Console | None = None, *, all_calls: bool = False) -> None:
    console = console or Console()
    eps = comparison.endpoints

    steps = Table(title="Operation results", show_lines=False)
    steps.add_column("Operation")
    for ep in eps:
        steps.add_column(ep)
    names = list(dict.fromkeys(n for ep in eps for n in comparison.step_results[ep]))
    for name in names:
        row = [name]
        for ep in eps:
            status = comparison.step_results[ep].get(name, "-")
            row.append(f"[{STATUS_STYLE.get(status, '')}]{status}[/]")
        steps.add_row(*row)
    console.print(steps)

    for run in runs:
        for r in run.results:
            if r.error:
                console.print(f"  [red]{run.endpoint}/{r.name}[/]: {r.error}")

    calls = Table(title="Calls side by side (status, latency, ETag format, error code)")
    calls.add_column("Step")
    calls.add_column("API call")
    for ep in eps:
        calls.add_column(ep)
    calls.add_column("Flags", style="yellow")
    for a in comparison.aligned:
        # Header gaps are summarised in the differences table; only list calls
        # that differ in status, error code or ETag unless asked for everything.
        if not all_calls and not any(not f.startswith("-") for f in a.flags):
            continue
        calls.add_row(a.step, _call_name(a), *[_cell(a.calls[ep]) for ep in eps], ", ".join(a.flags))
    if calls.row_count:
        console.print(calls)
    else:
        console.print("[green]Every aligned call matched on status, error code and ETag format.[/]")

    diffs = Table(title="Differences")
    diffs.add_column("Severity")
    diffs.add_column("Category")
    diffs.add_column("Where")
    diffs.add_column("Detail")
    for ep in eps:
        diffs.add_column(ep, overflow="fold")
    for d in comparison.differences:
        where = d.step if d.operation == "*" else f"{d.step}/{d.operation}"
        diffs.add_row(
            f"[{SEVERITY_STYLE[d.severity]}]{d.severity}[/]",
            d.category,
            where,
            d.detail,
            *[_fmt(d.values.get(ep, "")) for ep in eps],
        )
    console.print(diffs)

    lat = Table(title="Latency per API call (ms)")
    lat.add_column("API call")
    for ep in eps:
        lat.add_column(f"{ep} p50", justify="right")
        lat.add_column(f"{ep} p95", justify="right")
    by_op = _latency_by_op(comparison)
    for op, per_ep in by_op.items():
        row = [op]
        for ep in eps:
            s = per_ep.get(ep)
            row += [f"{s.p50_ms:.1f}", f"{s.p95_ms:.1f}"] if s else ["-", "-"]
        lat.add_row(*row)
    console.print(lat)

    c = comparison.counts()
    console.print(f"[bold]{c['error']} errors, {c['warning']} warnings, {c['info']} info[/]")


def _call_name(a: AlignedCall) -> str:
    return a.operation if a.occurrence == 0 else f"{a.operation} #{a.occurrence + 1}"


def _latency_by_op(comparison: Comparison):
    by_op: dict[str, dict[str, Any]] = {}
    for s in comparison.latency:
        by_op.setdefault(s.operation, {})[s.endpoint] = s
    return dict(sorted(by_op.items()))


def build_json(comparison: Comparison, runs: list[EndpointRun], options: RunOptions) -> dict[str, Any]:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "options": vars(options),
        "comparison": comparison.to_dict(),
        "runs": [r.to_dict() for r in runs],
    }


def _md_escape(text: Any) -> str:
    return _fmt(text).replace("|", "\\|").replace("\n", " ")


def build_markdown(comparison: Comparison, runs: list[EndpointRun], options: RunOptions) -> str:
    eps = comparison.endpoints
    lines: list[str] = []
    add = lines.append
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    c = comparison.counts()

    add("# S3 compatibility report")
    add("")
    add(f"Run at {now}. Multipart object: {options.multipart_size_mb} MiB in "
        f"{options.part_size_mb} MiB parts; listing {options.list_object_count} objects, page size {options.page_size}.")
    add("")
    add(f"**{c['error']} errors, {c['warning']} warnings, {c['info']} info.**")
    add("")

    add("## Endpoints")
    add("")
    add("| Name | URL | Region | Bucket |")
    add("|---|---|---|---|")
    for r in runs:
        add(f"| {r.endpoint} | {r.endpoint_url or 'AWS default'} | {r.region} | `{r.bucket}` |")
    add("")

    add("## Operation results")
    add("")
    add("| Operation | " + " | ".join(eps) + " |")
    add("|---|" + "---|" * len(eps))
    names = list(dict.fromkeys(n for ep in eps for n in comparison.step_results[ep]))
    for name in names:
        add(f"| {name} | " + " | ".join(comparison.step_results[ep].get(name, "-") for ep in eps) + " |")
    add("")
    errors = [(r.endpoint, x.name, x.error) for r in runs for x in r.results if x.error]
    for ep, name, err in errors:
        add(f"- `{ep}/{name}`: {_md_escape(err)}")
    if errors:
        add("")

    add("## Differences")
    add("")
    if comparison.differences:
        add("| Severity | Category | Where | Detail | " + " | ".join(eps) + " |")
        add("|---|---|---|---|" + "---|" * len(eps))
        for d in comparison.differences:
            where = d.step if d.operation == "*" else f"{d.step}/{d.operation}"
            vals = " | ".join(_md_escape(d.values.get(ep, "")) for ep in eps)
            add(f"| {d.severity} | {d.category} | {where} | {_md_escape(d.detail)} | {vals} |")
    else:
        add("None.")
    add("")

    add("## Calls side by side")
    add("")
    add("Each cell: HTTP status, latency, ETag format, error code.")
    add("")
    add("| Step | API call | " + " | ".join(eps) + " | Flags |")
    add("|---|---|" + "---|" * len(eps) + "---|")
    for a in comparison.aligned:
        cells = " | ".join(_cell(a.calls[ep]) for ep in eps)
        add(f"| {a.step} | {_call_name(a)} | {cells} | {', '.join(a.flags)} |")
    add("")

    add("## Latency (ms)")
    add("")
    add("| API call | " + " | ".join(f"{ep} n | {ep} p50 | {ep} p95" for ep in eps) + " |")
    add("|---|" + "---|---|---|" * len(eps))
    for op, per_ep in _latency_by_op(comparison).items():
        cells = []
        for ep in eps:
            s = per_ep.get(ep)
            cells += [str(s.count), f"{s.p50_ms:.1f}", f"{s.p95_ms:.1f}"] if s else ["-", "-", "-"]
        add(f"| {op} | " + " | ".join(cells) + " |")
    add("")

    mp = {r.endpoint: r.result("multipart") for r in runs}
    if any(m and m.observations for m in mp.values()):
        add("## Multipart throughput")
        add("")
        add("| Endpoint | Upload MiB/s | Download MiB/s | ETag |")
        add("|---|---|---|---|")
        for ep, m in mp.items():
            if m and m.observations:
                o = m.observations
                add(f"| {ep} | {o.get('upload_mib_per_s')} | {o.get('download_mib_per_s')} | `{o.get('etag')}` |")
        add("")
    return "\n".join(lines)


def write_reports(out_dir: str | Path, comparison: Comparison, runs: list[EndpointRun], options: RunOptions) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out / f"report-{stamp}.json"
    md_path = out / f"report-{stamp}.md"
    json_path.write_text(json.dumps(build_json(comparison, runs, options), indent=2, default=str), encoding="utf-8")
    md_path.write_text(build_markdown(comparison, runs, options), encoding="utf-8")
    return json_path, md_path
