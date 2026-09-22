"""Command-line scanning and report publication."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .baseline import compare, make_baseline, read_baseline, review
from .config import Config, load_config
from .diagram import render_html, render_mermaid
from .model import INCOMPLETE_CODES, Report
from .rules import RULES
from .scanner import scan
from .source_io import is_link

INCOMPLETE = INCOMPLETE_CODES


def render_text(report: Report) -> str:
    summary = report.summary()
    lines = [
        "FlowSignal - Python flow and observability review",
        f"Scan status: {summary['status']} (completion does not establish semantic completeness).",
        f"Scanned {summary['files_scanned']} file(s), {summary['symbols']} symbol(s), {summary['resolved_internal_calls']} resolved internal call(s).",
        f"{summary['findings']} finding(s); {summary['unresolved_calls']} unresolved call(s); {summary['diagnostics']} diagnostic(s).",
        f"{summary['propagation_uncertain_calls']} call(s) inside contexts with uncertain exception propagation.",
        "",
    ]
    for finding in report.findings:
        # Finding state remains independent of runtime observations and cache use.
        location = finding.evidence[0].location
        lines.extend(
            [
                f"[{finding.priority.upper()} / {finding.confidence} confidence] {finding.rule_id}: {finding.title}",
                f"  {location.file}:{location.line}  {finding.symbol}",
            ]
        )
        lines.append(
            f"  Fingerprint: {finding.fingerprint}; review: {finding.review_status}; baseline: {finding.baseline_status or 'not compared'}"
        )
        if finding.review_owner:
            lines.append(
                f"  Review owner: {finding.review_owner}; expires: {finding.review_expires or 'unspecified'}; expired: {finding.review_expired}"
            )
        if report.baseline and finding.fingerprint in report.baseline.get(
            "renewal_required", []
        ):
            lines.append(
                "  Changed evidence/advice: prior dismissal requires renewed review."
            )
        if finding.review_reason:
            lines.append(f"  Review reason: {finding.review_reason}")
        for evidence in finding.evidence:
            lines.extend(
                [
                    f"  Evidence: {evidence.fact}",
                    f"    {evidence.location.file}:{evidence.location.line}: {evidence.code}",
                ]
            )
        for recommendation in finding.recommendations:
            label = f"{recommendation.kind} {recommendation.level or ''}".strip()
            lines.extend(
                [
                    f"  Recommend {label}: {recommendation.action}",
                    f"    When: {recommendation.condition}",
                ]
            )
        for path in finding.paths:
            lines.append(f"  Possible path: {' -> '.join(path)}")
        for assumption in finding.assumptions:
            lines.append(f"  Review: {assumption}")
        lines.append("")
    if report.resolution_gaps:
        lines.append(
            "Unresolved call review: "
            + json.dumps(summary["unresolved_reasons"], sort_keys=True)
        )
    for gap in report.resolution_gaps:
        lines.append(
            f"  [{gap.reason}] {gap.location.file}:{gap.location.line}: {gap.expression}"
        )
        lines.append(f"    {gap.explanation} Next: {gap.action}")
        lines.append(
            "    Potential entrypoints via known edges: "
            + (
                ", ".join(gap.entrypoints)
                or "none found; this is not proof of unreachability"
            )
            + (" (partial context)" if gap.context_truncated else "")
        )
    if report.analysis_stats.get("cache"):
        lines.append(
            "Incremental scan: "
            + json.dumps(report.analysis_stats["cache"], sort_keys=True)
        )
    for name in (
        "operation_outcomes",
        "contextual_calls",
        "task_ownership",
        "retry_scopes",
    ):
        for record in getattr(report, name):
            lines.append(
                name.replace("_", " ") + ": " + json.dumps(record, sort_keys=True)
            )
    if report.runtime:
        if report.runtime.get("run_comparison"):
            lines.append(
                "Runtime run comparison: "
                + json.dumps(report.runtime["run_comparison"], sort_keys=True)
            )
        lines.append(
            "Runtime comparison: "
            + json.dumps(report.runtime["summary"], sort_keys=True)
        )
        lines.append(report.runtime["note"])
        lines.append(
            f"Static pairs not observed: {len(report.runtime['static_not_observed'])}"
        )
        for pair in report.runtime["pairs"]:
            lines.append(
                f"  {pair['status']}: {pair['caller']} -> {pair['callee']} ({pair['count']} observations)"
            )
    for diagnostic in report.diagnostics:
        where = f" {diagnostic.file or ''}{':' + str(diagnostic.line) if diagnostic.line else ''}".rstrip()
        lines.append(f"Diagnostic [{diagnostic.code}]{where}: {diagnostic.message}")
    for signal in report.reporting_signals:
        lines.append(
            f"Configured {signal.kind} reporter: {signal.owner} at {signal.location.file}:{signal.location.line}; {signal.basis}; conditional={signal.conditional}"
        )
    for decision in report.coverage:
        lines.append(
            f"Boundary coverage: {decision.status} at {decision.location.file}:{decision.location.line}"
        )
        for item in decision.evidence:
            lines.append(
                f"  [{item.reason}] {item.location.file}:{item.location.line}: {item.message}"
                + (f" Owner: {item.owner}." if item.owner else "")
            )
        if decision.truncated:
            lines.append(
                "  Explanation truncated at 64 evidence items; the verdict uses the full bounded review."
            )
    if report.baseline:
        lines.append(
            "Baseline changes: " + json.dumps(report.baseline["counts"], sort_keys=True)
        )
        for status in ("resolved", "unverified"):
            for entry in report.baseline[status]:
                lines.append(
                    f"  {status}: {entry['rule_id']} {entry['symbol']} [{entry['fingerprint']}]"
                )
    lines.extend(
        [
            "",
            "Static paths and log levels require operational context. A clean scan does not prove observability coverage.",
        ]
    )
    return "\n".join(lines) + "\n"


def validate_output_path(path: Path) -> None:
    if path.suffix.lower() in {
        ".py",
        ".pyw",
        ".pyi",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".lock",
        ".ipynb",
    } or path.name.lower() in {
        "package.json",
        "package-lock.json",
        "pipfile",
        "requirements.txt",
        "setup.cfg",
    }:
        raise ValueError(
            "Refusing to use a source or configuration file as report output"
        )
    if path.exists() or path.is_symlink():
        if is_link(path) or not path.is_file():
            raise ValueError(
                "Report output must be a regular file, not a link or directory"
            )


def write_report(path: Path, content: str) -> None:
    validate_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".flowsignal-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            name = handle.name
            handle.write(content)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="flowsignal",
        description="Deterministic Python execution-flow and observability review.",
    )
    result.add_argument("--version", action="version", version="FlowSignal 0.1.0")
    commands = result.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "scan", help="Scan Python sources without importing or executing them"
    )
    command.add_argument("path", nargs="?", default=".")
    command.add_argument(
        "--cache-dir",
        type=Path,
        help="Reuse trusted local JSON scan/AST facts; changed sources rebuild global relationships",
    )
    command.add_argument(
        "--previous-runtime-trace",
        type=Path,
        help="Compare runs against --runtime-trace using the current source snapshot",
    )
    command.add_argument(
        "--timeout-seconds",
        type=float,
        help="Supervise scan/render wall time; enables a worker (default companion memory limit: 1024 MiB)",
    )
    command.add_argument(
        "--memory-mb",
        type=int,
        help="Enforce worker allocation limit in MiB on Windows/Linux (default companion timeout: 300 seconds)",
    )
    command.add_argument(
        "--baseline", type=Path, help="Compare with a saved structural baseline"
    )
    command.add_argument(
        "--save-baseline", type=Path, help="Save this complete scan as a baseline"
    )
    command.add_argument(
        "--fail-on-new",
        action="store_true",
        help="Apply --fail-on to new active findings and expired dismissals; requires --baseline",
    )
    command.add_argument(
        "--source-root",
        action="append",
        default=[],
        help="Relative Python import root; repeat for monorepos",
    )
    command.add_argument(
        "--no-source",
        action="store_true",
        help="Omit source-code excerpts from evidence (names and paths remain)",
    )
    command.add_argument(
        "--format", choices=["text", "json", "html", "mermaid", "sarif"], default="text"
    )
    command.add_argument(
        "--diagram-focus",
        help="Initial diagram focus: exact symbol ID or qualified name",
    )
    command.add_argument(
        "--diagram-max-nodes",
        type=int,
        default=60,
        help="Maximum nodes in the initial diagram (1-200; default 60)",
    )
    command.add_argument(
        "--output", type=Path, help="Write the report atomically instead of to stdout"
    )
    command.add_argument(
        "--config", type=Path, help="FlowSignal TOML or pyproject.toml configuration"
    )
    command.add_argument(
        "--runtime-trace",
        type=Path,
        help="Compare externally collected flowsignal-runtime-1 JSON call pairs; never executes the target",
    )
    command.add_argument(
        "--entrypoint",
        action="append",
        default=[],
        help="Symbol ID or qualified-name glob; repeatable",
    )
    command.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Relative path or directory-name glob; repeatable",
    )
    command.add_argument(
        "--min-confidence", choices=["low", "medium", "high"], default="low"
    )
    command.add_argument(
        "--fail-on",
        choices=["low", "medium", "high", "none"],
        default="none",
        help="Return 1 if a reported finding meets this review priority",
    )
    command.add_argument(
        "--lifecycle-info",
        action="store_true",
        help="Review INFO lifecycle events for explicitly configured entrypoints",
    )
    commands.add_parser("rules", help="List deterministic rule IDs")
    decision = commands.add_parser(
        "review", help="Record a dismissal or restore findings in a baseline"
    )
    decision.add_argument("action", choices=["dismiss", "restore"])
    decision.add_argument("baseline", type=Path)
    decision.add_argument("fingerprint")
    decision.add_argument("--reason", help="Required explanation for a dismissal")
    decision.add_argument(
        "--owner", help="Review owner (a declaration, not authenticated identity)"
    )
    decision.add_argument(
        "--expires",
        help="Dismissal expiry YYYY-MM-DD at 00:00 UTC; defaults to 30 days",
    )
    return result


def main(
    argv: list[str] | None = None,
    *,
    _stage: Path | None = None,
    _worker_limits: dict | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    if args.command == "rules":
        for identity, title in RULES.items():
            print(f"{identity}  {title}")
        return 0
    try:
        if (
            args.command == "scan"
            and _stage is None
            and (args.timeout_seconds is not None or args.memory_mb is not None)
        ):
            from .supervisor import run

            return run(argv, args)
        if args.command == "review":
            document = read_baseline(args.baseline)
            review(
                document,
                args.fingerprint,
                args.action,
                args.reason,
                owner=args.owner,
                expires=args.expires,
            )
            write_report(
                args.baseline, json.dumps(document, indent=2, ensure_ascii=True) + "\n"
            )
            return 0
        if args.fail_on_new and not args.baseline:
            raise ValueError("--fail-on-new requires --baseline")
        if args.previous_runtime_trace and not args.runtime_trace:
            raise ValueError("--previous-runtime-trace requires --runtime-trace")
        previous = read_baseline(args.baseline) if args.baseline else None
        target = Path(args.path).absolute()
        root = target.parent if target.is_file() else target
        config_path = args.config
        if config_path is None:
            config_path = next(
                (
                    candidate
                    for candidate in [root / "flowsignal.toml", root / "pyproject.toml"]
                    if candidate.is_file()
                ),
                None,
            )
        config = load_config(config_path) if config_path else Config()
        protected = {
            p.resolve()
            for p in (
                config_path,
                args.baseline,
                args.runtime_trace,
                args.previous_runtime_trace,
            )
            if p
        }
        if args.output and args.output.resolve() in protected:
            raise ValueError(
                "Report output must not overwrite configuration or baseline input"
            )
        if args.save_baseline and (
            config_path
            and args.save_baseline.resolve() == config_path.resolve()
            or args.output
            and args.save_baseline.resolve() == args.output.resolve()
        ):
            raise ValueError(
                "Baseline output must differ from configuration and report output"
            )
        if (
            args.save_baseline
            and args.runtime_trace
            and args.save_baseline.resolve() == args.runtime_trace.resolve()
        ):
            raise ValueError("Baseline output must not overwrite runtime trace input")
        for output in (args.output, args.save_baseline):
            if (
                output
                and args.previous_runtime_trace
                and output.resolve() == args.previous_runtime_trace.resolve()
            ):
                raise ValueError("Output must not overwrite previous runtime trace")
            if output:
                validate_output_path(output)
        config.entrypoints.extend(args.entrypoint)
        config.exclude.extend(args.exclude)
        config.source_roots.extend(args.source_root)
        if args.no_source:
            config.include_source = False
        config.lifecycle_info |= args.lifecycle_info
        report = (
            scan(target, config, cache_dir=args.cache_dir)
            if args.cache_dir
            else scan(target, config)
        )
        if _worker_limits:
            report.analysis_stats["worker_limits"] = _worker_limits
        rank = {"low": 1, "medium": 2, "high": 3}
        report.findings = [
            finding
            for finding in report.findings
            if rank[finding.confidence] >= rank[args.min_confidence]
        ]
        report.settings["min_confidence"] = args.min_confidence
        from .review_queue import attach

        attach(report)
        if previous is not None:
            compare(report, previous)
        if args.runtime_trace:
            from .runtime import compare_trace

            compare_trace(report, args.runtime_trace)
            if args.previous_runtime_trace:
                from .runtime import compare_runs

                compare_runs(report, args.previous_runtime_trace)
        baseline_content = (
            json.dumps(make_baseline(report), indent=2, ensure_ascii=True) + "\n"
            if args.save_baseline
            else None
        )
        if args.format == "json":
            content = json.dumps(report.to_dict(), indent=2, ensure_ascii=True) + "\n"
        elif args.format == "sarif":
            from .sarif import export

            content = json.dumps(export(report), indent=2, ensure_ascii=True) + "\n"
        elif args.format == "html":
            content = render_html(report, args.diagram_focus, args.diagram_max_nodes)
        elif args.format == "mermaid":
            content = render_mermaid(report, args.diagram_focus, args.diagram_max_nodes)
        else:
            content = render_text(report)
        if _stage is not None:
            write_report(_stage / "report", content)
        elif args.output:
            if config_path and args.output.resolve() == config_path.resolve():
                raise ValueError(
                    "Report output must not overwrite the active configuration"
                )
            write_report(args.output, content)
        else:
            sys.stdout.write(content)
        if args.save_baseline:
            write_report(
                _stage / "baseline" if _stage is not None else args.save_baseline,
                baseline_content,
            )
        if any(diagnostic.code in INCOMPLETE for diagnostic in report.diagnostics):
            return 2
        return int(
            args.fail_on != "none"
            and any(
                rank[finding.priority] >= rank[args.fail_on]
                and finding.review_status != "dismissed"
                and (
                    not args.fail_on_new
                    or finding.baseline_status == "new"
                    or finding.review_expired
                )
                for finding in report.findings
            )
        )
    except (OSError, ValueError) as error:
        print(f"flowsignal: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
