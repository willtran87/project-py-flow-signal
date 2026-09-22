"""Command-line scanning and report publication."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

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
        location = finding.evidence[0].location
        lines.extend(
            [
                f"[{finding.priority.upper()} / {finding.confidence} confidence] {finding.rule_id}: {finding.title}",
                f"  {location.file}:{location.line}  {finding.symbol}",
            ]
        )
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
    for diagnostic in report.diagnostics:
        where = f" {diagnostic.file or ''}{':' + str(diagnostic.line) if diagnostic.line else ''}".rstrip()
        lines.append(f"Diagnostic [{diagnostic.code}]{where}: {diagnostic.message}")
    lines.extend(
        [
            "",
            "Static paths and log levels require operational context. A clean scan does not prove observability coverage.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_report(path: Path, content: str) -> None:
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
        "--format", choices=["text", "json", "html", "mermaid"], default="text"
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
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "rules":
        for identity, title in RULES.items():
            print(f"{identity}  {title}")
        return 0
    try:
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
        config.entrypoints.extend(args.entrypoint)
        config.exclude.extend(args.exclude)
        config.source_roots.extend(args.source_root)
        if args.no_source:
            config.include_source = False
        config.lifecycle_info |= args.lifecycle_info
        report = scan(target, config)
        rank = {"low": 1, "medium": 2, "high": 3}
        report.findings = [
            finding
            for finding in report.findings
            if rank[finding.confidence] >= rank[args.min_confidence]
        ]
        report.settings["min_confidence"] = args.min_confidence
        if args.format == "json":
            content = json.dumps(report.to_dict(), indent=2, ensure_ascii=True) + "\n"
        elif args.format == "html":
            content = render_html(report, args.diagram_focus, args.diagram_max_nodes)
        elif args.format == "mermaid":
            content = render_mermaid(report, args.diagram_focus, args.diagram_max_nodes)
        else:
            content = render_text(report)
        if args.output:
            if config_path and args.output.resolve() == config_path.resolve():
                raise ValueError(
                    "Report output must not overwrite the active configuration"
                )
            write_report(args.output, content)
        else:
            sys.stdout.write(content)
        if any(diagnostic.code in INCOMPLETE for diagnostic in report.diagnostics):
            return 2
        return int(
            args.fail_on != "none"
            and any(
                rank[finding.priority] >= rank[args.fail_on]
                for finding in report.findings
            )
        )
    except (OSError, ValueError) as error:
        print(f"flowsignal: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
