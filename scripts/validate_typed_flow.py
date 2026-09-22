"""Validate typed flow and coverage explanations through the CLI without running input."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from validate_review_workflow import invoke


def run(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "examples" / "typed_workflow.py"
    for format_name, suffix in [
        ("json", "json"),
        ("html", "html"),
        ("text", "txt"),
        ("mermaid", "mmd"),
    ]:
        invoke(
            [
                "scan",
                source,
                "--entrypoint",
                "typed_workflow.run",
                "--format",
                format_name,
                "--diagram-focus",
                "typed_workflow.run",
                "--output",
                output / f"report.{suffix}",
            ]
        )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    edges = {(c["symbol"], c["target"]) for c in report["calls"] if c["target"]}
    required = {
        ("typed_workflow.py:run", "typed_workflow.py:Workflow.execute"),
        ("typed_workflow.py:Workflow.execute", "typed_workflow.py:Client.fetch"),
    }
    assert required <= edges, edges
    covered = next(
        d for d in report["coverage"] if d["symbol"].endswith(":Client.fetch")
    )
    rejected = next(d for d in report["coverage"] if d["symbol"].endswith(":refresh"))
    assert covered["status"] == "recognized"
    assert any(
        e["reason"] == "failure_log" and e["symbol"].endswith(":run")
        for e in covered["evidence"]
    )
    assert rejected["status"] == "not_established"
    assert any(e["reason"] == "conditional_log" for e in rejected["evidence"])
    result = {
        "passed": True,
        "typed_edges": len(required),
        "coverage": report["summary"]["boundary_coverage"],
        "target_executed": False,
    }
    (output / "validation.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".artifacts/typed-flow"))
    print(json.dumps(run(parser.parse_args().output), indent=2))
