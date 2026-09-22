"""Exercise reporter contracts, property edges and baseline review through the CLI.

The authored input is scanned, never imported or executed. Outputs include a
small interactive report for the separate browser check.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

from flowsignal.cli import main

SOURCE = """import requests
from diagnostic_api import publish

class Snapshot:
    @property
    def payload(self):
        return requests.get("https://example.invalid/snapshot")

def fetch_snapshot(snapshot: Snapshot):
    try:
        return snapshot.payload
    except Exception:
        publish("Snapshot retrieval failed")
        return None

def legacy_health():
    return requests.get("https://example.invalid/health")

def retired_probe():
    return requests.head("https://example.invalid/retired")
"""
CONFIG = """[flowsignal]
entrypoints = ["app.fetch_snapshot"]
[[flowsignal.reporters]]
pattern = "diagnostic_api.publish"
kind = "diagnostic"
owner = "Workflow diagnostic report"
"""


def invoke(arguments, expected=0):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = main([str(arg) for arg in arguments])
    if status != expected or err.getvalue():
        raise RuntimeError(
            f"CLI returned {status}, expected {expected}: {err.getvalue()}"
        )
    return out.getvalue()


def run(output: Path):
    target = output / "input"
    target.mkdir(parents=True, exist_ok=True)
    source = target / "app.py"
    source.write_text(SOURCE, encoding="utf-8")
    (target / "flowsignal.toml").write_text(CONFIG, encoding="utf-8")
    baseline = output / "baseline.json"
    invoke(
        [
            "scan",
            target,
            "--format",
            "json",
            "--output",
            output / "before.json",
            "--save-baseline",
            baseline,
        ]
    )
    before = json.loads((output / "before.json").read_text(encoding="utf-8"))
    fingerprint = next(
        f["fingerprint"]
        for f in before["findings"]
        if f["symbol"] == "app.py:legacy_health"
    )
    reason = "Health probe failures are already handled by the deployment monitor."
    invoke(["review", "dismiss", baseline, fingerprint, "--reason", reason])
    current = (
        "# Harmless line movement must retain the review decision.\n\n"
        + SOURCE.replace(
            'return requests.head("https://example.invalid/retired")', "return None"
        )
        + '\ndef new_delivery():\n    return requests.post("https://example.invalid/delivery")\n'
    )
    source.write_text(current, encoding="utf-8")
    for format_name, suffix in (
        ("json", "json"),
        ("html", "html"),
        ("mermaid", "mmd"),
        ("text", "txt"),
    ):
        invoke(
            [
                "scan",
                target,
                "--baseline",
                baseline,
                "--fail-on",
                "low",
                "--fail-on-new",
                "--format",
                format_name,
                "--diagram-focus",
                "app.fetch_snapshot",
                "--output",
                output / f"report.{suffix}",
            ],
            expected=1,
        )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    expected = {
        "new": 1,
        "unchanged": 1,
        "resolved": 1,
        "unverified": 0,
        "dismissed": 1,
    }
    assert report["baseline"]["counts"] == expected, report["baseline"]
    assert report["summary"]["reporting_signals"] == 1
    assert any(
        c["target"] == "app.py:Snapshot.payload"
        and c["execution"] == "implicit_property"
        for c in report["calls"]
    )
    assert not any(
        f["symbol"] in {"app.py:fetch_snapshot", "app.py:Snapshot.payload"}
        for f in report["findings"]
    )
    assert (
        next(f for f in report["findings"] if f["review_status"] == "dismissed")[
            "review_reason"
        ]
        == reason
    )
    result = {
        "passed": True,
        "baseline_counts": expected,
        "reporter_owner": "Workflow diagnostic report",
        "property_getter": "app.py:Snapshot.payload",
        "new_finding_gate_exit": 1,
        "review_survives_line_movement": True,
        "target_executed": False,
    }
    (output / "validation.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/review-workflow")
    )
    args = parser.parse_args()
    print(json.dumps(run(args.output.resolve()), indent=2))
