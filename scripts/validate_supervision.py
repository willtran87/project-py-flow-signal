"""Dogfood supervised scans, uncertainty exports and timeout preservation."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

from flowsignal.cli import main


def invoke(arguments, expected=0):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        status = main(list(map(str, arguments)))
    if status != expected:
        raise RuntimeError(
            f"Expected exit {expected}, got {status}: {errors.getvalue()}"
        )
    return output.getvalue(), errors.getvalue()


def run(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "examples" / "uncertain_workflow.py"
    arguments = [
        "scan",
        source,
        "--entrypoint",
        "uncertain_workflow.run",
        "--entrypoint",
        "uncertain_workflow.scheduled",
        "--memory-mb",
        "256",
        "--timeout-seconds",
        "30",
    ]
    for format_name, suffix in [
        ("json", "json"),
        ("html", "html"),
        ("text", "txt"),
        ("mermaid", "mmd"),
    ]:
        _, errors = invoke(
            [
                *arguments,
                "--format",
                format_name,
                "--output",
                output / f"report.{suffix}",
            ]
        )
        assert not errors, errors
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["unresolved_reasons"] == {
        "callback_parameter": 1,
        "inheritance_lookup": 1,
        "unknown_receiver": 2,
    }
    for gap in report["resolution_gaps"]:
        expected = (
            []
            if gap["symbol"].endswith(":detached")
            else ["uncertain_workflow.py:run", "uncertain_workflow.py:scheduled"]
        )
        assert gap["entrypoints"] == expected, gap
    baseline = output / "baseline.json"
    invoke(
        [
            *arguments,
            "--save-baseline",
            baseline,
            "--output",
            output / "complete.json",
            "--format",
            "json",
        ]
    )
    before_report, before_baseline = (
        (output / "report.html").read_bytes(),
        baseline.read_bytes(),
    )
    _, errors = invoke(
        [
            "scan",
            source,
            "--timeout-seconds",
            ".001",
            "--output",
            output / "report.html",
            "--save-baseline",
            baseline,
        ],
        expected=2,
    )
    assert json.loads(errors)["diagnostics"][0]["code"] == "worker_timeout"
    assert before_report == (output / "report.html").read_bytes()
    assert before_baseline == baseline.read_bytes()
    result = {
        "passed": True,
        "unresolved_reasons": report["summary"]["unresolved_reasons"],
        "worker_limits": report["analysis_stats"]["worker_limits"],
        "timeout_exit": 2,
        "report_preserved": True,
        "baseline_preserved": True,
        "target_executed": False,
    }
    (output / "validation.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/supervised-review")
    )
    print(json.dumps(run(parser.parse_args().output), indent=2))
