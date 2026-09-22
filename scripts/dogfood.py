"""Exercise the real CLI on FlowSignal, record execution, and inject failures.

Scanned input is never imported. Only PROBE, an authored standard-library-only
fixture below, is intentionally executed in a separate interpreter for comparison.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from flowsignal.cli import main as cli

PROBE = """import json
import logging

events = []
class Capture(logging.Handler):
    def emit(self, record):
        events.append(record.levelname)

logger = logging.getLogger("flowsignal.dogfood.probe")
logger.setLevel(logging.DEBUG)
logger.propagate = False
logger.addHandler(Capture())

def suppressed():
    try:
        raise ValueError("failure")
    finally:
        return "overridden"

def silent_recovery():
    try:
        raise TimeoutError("unavailable")
    except TimeoutError:
        return "fallback"

def reported_recovery():
    try:
        raise TimeoutError("unavailable")
    except TimeoutError:
        logger.warning("Using fallback", exc_info=True)
        return "fallback"

print(json.dumps({"suppressed": suppressed(), "silent": silent_recovery(),
                  "reported": reported_recovery(), "events": events}))
"""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def invoke(arguments: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = cli(arguments)
    return status, stdout.getvalue(), stderr.getvalue()


def run(repository: Path, output: Path) -> dict:
    source_root = repository / "src"
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    arguments = ["scan", str(source_root), "--entrypoint", "flowsignal.cli.main"]
    scan_start = time.perf_counter()
    status, _, stderr = invoke(
        arguments + ["--format", "json", "--output", str(output / "self.json")]
    )
    require(status == 0 and not stderr, "Self-scan did not complete successfully")
    scan_seconds = time.perf_counter() - scan_start
    report = json.loads((output / "self.json").read_text(encoding="utf-8"))
    require(report["summary"]["status"] == "complete", "Self-scan is incomplete")
    require(report["findings"], "Self-scan unexpectedly has no findings to review")
    checks.append("real CLI self-scan")
    process = subprocess.run(
        [sys.executable, "-m", "flowsignal", *arguments, "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    require(
        process.returncode == 0 and not process.stderr, "Module CLI subprocess failed"
    )
    child_report = json.loads(process.stdout)
    require(
        child_report["summary"] == report["summary"]
        and child_report["findings"] == report["findings"],
        "CLI subprocess disagrees with in-process scan",
    )
    checks.append("module CLI subprocess agrees with in-process results")

    # Count real Python call events in the analyzer, not arbitrary scanned code.
    observed: Counter[tuple[str, str]] = Counter()
    code_ids = {}

    def identity(frame):
        if frame.f_code in code_ids:
            return code_ids[frame.f_code]
        filename = Path(frame.f_code.co_filename)
        if not filename.is_absolute() or not filename.is_relative_to(source_root):
            code_ids[frame.f_code] = None
            return None
        value = (
            filename.relative_to(source_root).as_posix()
            + ":"
            + frame.f_code.co_qualname.replace(".<locals>.", ".")
        )
        code_ids[frame.f_code] = value
        return value

    def profile(frame, event, _arg):
        if (
            event != "call"
            or frame.f_back is None
            or not frame.f_globals.get("__name__", "").startswith("flowsignal.")
        ):
            return
        callee, caller = identity(frame), identity(frame.f_back)
        if callee and caller:
            observed[caller, callee] += 1

    prior_profile = sys.getprofile()
    sys.setprofile(profile)
    try:
        status, _, stderr = invoke(
            arguments
            + [
                "--format",
                "html",
                "--diagram-focus",
                "flowsignal.cli.main",
                "--output",
                str(output / "self.html"),
            ]
        )
    finally:
        sys.setprofile(prior_profile)
    require(status == 0 and not stderr, "Profiled HTML CLI scan failed")
    static_edges = {
        (call["symbol"], call["target"]) for call in report["calls"] if call["target"]
    }
    core_edges = {
        ("flowsignal/cli.py:main", "flowsignal/scanner.py:scan"),
        ("flowsignal/scanner.py:scan", "flowsignal/rules.py:evaluate"),
        ("flowsignal/cli.py:main", "flowsignal/diagram.py:render_html"),
        ("flowsignal/diagram.py:render_html", "flowsignal/diagram.py:graph_data"),
        ("flowsignal/cli.py:main", "flowsignal/cli.py:write_report"),
        (
            "flowsignal/scanner.py:FactVisitor.visit_ClassDef",
            "flowsignal/scanner.py:BindingCollector.__init__",
        ),
        (
            "flowsignal/scanner.py:scan",
            "flowsignal/scanner.py:BindingCollector.__init__",
        ),
        ("flowsignal/scanner.py:scan", "flowsignal/scanner.py:FactVisitor.__init__"),
        (
            "flowsignal/scanner.py:scoped_bindings",
            "flowsignal/scanner.py:BindingCollector.__init__",
        ),
        ("flowsignal/scanner.py:scan", "flowsignal/config.py:Config.validate"),
        ("flowsignal/scanner.py:scan", "flowsignal/config.py:Config.to_dict"),
        ("flowsignal/rules.py:evaluate", "flowsignal/model.py:Handler.catches_all"),
        (
            "flowsignal/rules.py:evaluate.effective_handlers",
            "flowsignal/model.py:Handler.catches_all",
        ),
    }
    core_edges.add(
        ("flowsignal/rules.py:evaluate", "flowsignal/rules.py:CoverageResult.extend")
    )
    receiver_edges = {
        ("MemberWrites.__init__", "ReturnValues.__init__"),
        ("ReceiverIndex.member", "ReturnValues.visit"),
        ("ReturnValues.visit", "ReceiverIndex.step"),
        ("ReceiverIndex.getter", "ReceiverScope.type_of"),
        ("ReceiverIndex.scope", "ReceiverScope.current"),
        ("ReceiverScope.type_of", "ReceiverIndex.member"),
        ("ReceiverScope.type_of", "ReceiverIndex.ordinary_class"),
        ("ReceiverScope.type_of", "ReceiverIndex.qualified"),
        ("ReceiverScope.visit_AnnAssign", "ReceiverIndex.annotation"),
    }
    core_edges.update(
        ("flowsignal/receivers.py:" + a, "flowsignal/receivers.py:" + b)
        for a, b in receiver_edges
    )
    core_edges.update(
        ("flowsignal/scanner.py:" + a, "flowsignal/receivers.py:" + b)
        for a, b in [
            ("FactVisitor.__init__", "ReceiverIndex.scope"),
            ("FactVisitor.record_call", "ReceiverIndex.getter"),
            ("FactVisitor.visit_Attribute", "ReceiverIndex.getter"),
        ]
    )
    require(
        core_edges <= set(observed), "A required CLI execution edge was not observed"
    )
    require(
        core_edges <= static_edges,
        "A required observed CLI edge is missing from static analysis",
    )
    checks.append(
        f"{len(core_edges)} central static call relationships corroborated by execution"
    )
    html = (output / "self.html").read_text(encoding="utf-8")
    embedded = html.split('<script id="flow-data" type="application/json">', 1)[
        1
    ].split("</script>", 1)[0]
    graph = json.loads(embedded)
    require(
        set(graph["findings"]) == {f["id"] for f in report["findings"]},
        "HTML and JSON findings disagree",
    )
    require(graph["summary"] == report["summary"], "HTML and JSON summaries disagree")
    require(graph["focus"] == "flowsignal/cli.py:main", "Incorrect graph focus")
    require(
        {(e["source"], e["target"]) for e in graph["edges"]} >= core_edges,
        "Core call edges absent from graph",
    )
    checks.append("JSON/HTML findings, counts, focus, and core graph edges agree")
    for format_name, suffix in [("text", "txt"), ("mermaid", "mmd")]:
        status, _, stderr = invoke(
            arguments
            + ["--format", format_name, "--output", str(output / f"self.{suffix}")]
        )
        require(status == 0 and not stderr, f"{format_name} CLI export failed")
    checks.append("text and Mermaid CLI exports")
    status, raw, _ = invoke(arguments + ["--format", "json", "--fail-on", "low"])
    require(
        status == 1 and json.loads(raw)["summary"]["status"] == "complete",
        "Finding threshold exit status is incorrect",
    )
    require(
        json.loads(raw)["findings"] == report["findings"],
        "Repeated self-scan findings changed",
    )
    checks.append("repeatable findings and threshold exit 1")

    failures = []
    with tempfile.TemporaryDirectory(prefix="flowsignal-dogfood-") as temporary:
        root = Path(temporary)
        target = root / "app.py"
        marker = root / "EXECUTED"
        target.write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('must not execute')\n",
            encoding="utf-8",
        )
        status, _, _ = invoke(["scan", str(target), "--format", "json"])
        require(
            status == 0 and not marker.exists(),
            "Scanned input was executed or failed to scan",
        )
        checks.append("side-effecting scanned input was never executed")

        def incomplete(name, extra=()):
            status, raw, error = invoke(
                ["scan", str(target), "--format", "json", *extra]
            )
            data = json.loads(raw)
            require(
                status == 2 and data["summary"]["status"] == "incomplete" and not error,
                name + " did not produce an incomplete report",
            )
            failures.append(
                {
                    "case": name,
                    "exit_code": status,
                    "diagnostics": data["summary"]["diagnostic_counts"],
                }
            )

        target.write_text("def invalid(:\n", encoding="utf-8")
        incomplete("malformed source")
        target.write_text("pass\n", encoding="utf-8")
        with patch(
            "flowsignal.scanner.read_snapshot",
            side_effect=PermissionError("injected read failure"),
        ):
            incomplete("source read failure")
        config = root / "limits.toml"
        config.write_text("[flowsignal]\nmax_file_bytes = 1\n", encoding="utf-8")
        incomplete("source budget exhaustion", ["--config", str(config)])
        destination = root / "report.json"
        previous = b'{"previous_report":true}'
        destination.write_bytes(previous)
        config.write_text("[flowsignal]\nunknown_setting = true\n", encoding="utf-8")
        status, _, error = invoke(
            ["scan", str(target), "--config", str(config), "--output", str(destination)]
        )
        require(
            status == 2
            and "Unknown FlowSignal options" in error
            and destination.read_bytes() == previous,
            "Invalid config changed existing output or failed silently",
        )
        failures.append(
            {
                "case": "invalid configuration",
                "exit_code": status,
                "previous_report_preserved": True,
            }
        )
        with patch(
            "flowsignal.cli.os.replace",
            side_effect=PermissionError("injected publication failure"),
        ):
            status, _, error = invoke(
                ["scan", str(target), "--output", str(destination)]
            )
        require(
            status == 2
            and "injected publication failure" in error
            and destination.read_bytes() == previous,
            "Publication failure corrupted the prior report or failed silently",
        )
        require(
            not list(root.glob(".flowsignal-*.tmp")),
            "Publication failure left temporary files",
        )
        failures.append(
            {
                "case": "publication failure",
                "exit_code": status,
                "previous_report_preserved": True,
                "temporary_files_removed": True,
            }
        )

        # This authored fixture is executed deliberately, separately from scanning.
        probe = root / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        status, raw, _ = invoke(["scan", str(probe), "--format", "json"])
        require(status == 0, "Runtime probe fixture scan failed")
        probe_scan = json.loads(raw)
        execution = subprocess.run(
            [sys.executable, "-I", "-W", "ignore::SyntaxWarning", str(probe)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        actual = json.loads(execution.stdout)
        require(
            actual
            == {
                "suppressed": "overridden",
                "silent": "fallback",
                "reported": "fallback",
                "events": ["WARNING"],
            },
            "Observed probe outcomes differed",
        )
        found = {(f["rule_id"], f["symbol"]) for f in probe_scan["findings"]}
        require(
            ("FS002", "probe.py:suppressed") in found, "Suppressed exception was missed"
        )
        require(
            ("FS001", "probe.py:silent_recovery") in found, "Silent recovery was missed"
        )
        require(
            not any(symbol == "probe.py:reported_recovery" for _, symbol in found),
            "Warning-instrumented recovery was incorrectly flagged",
        )
        checks.append(
            "three authored failure/recovery outcomes compared with real execution"
        )

    known_ids = {symbol["id"] for symbol in report["symbols"]}
    comparable = {edge for edge in observed if set(edge) <= known_ids}
    missing = comparable - static_edges
    results = {
        "python": platform.python_version(),
        "self_scan_seconds": round(scan_seconds, 3),
        "self_summary": report["summary"],
        "checks": checks,
        "injected_failures": failures,
        "runtime_probe": actual,
        "runtime_edges": {
            "observed_unique_internal_pairs": len(observed),
            "pairs_between_indexed_symbols": len(comparable),
            "pairs_present_in_static_graph": len(comparable & static_edges),
            "missing_pairs": [list(edge) for edge in sorted(missing)],
            "note": "One HTML CLI execution only; this is not whole-program precision or recall.",
        },
        "passed": True,
    }
    (output / "validation.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    (output / "runtime-edges.json").write_text(
        json.dumps(
            [
                {
                    "caller": a,
                    "callee": b,
                    "calls": count,
                    "in_static_graph": (a, b) in static_edges,
                }
                for (a, b), count in sorted(observed.items())
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".artifacts/dogfood"))
    args = parser.parse_args()
    result = run(Path(__file__).resolve().parents[1], args.output.resolve())
    print(json.dumps(result, indent=2))
