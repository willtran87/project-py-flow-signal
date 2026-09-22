"""Validate the six workflow enhancements with an explicitly authored safe probe."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import runpy
import subprocess
import sys
import time
from pathlib import Path

from flowsignal import scan
from flowsignal.baseline import make_baseline
from flowsignal.collector import collect
from flowsignal.config import Config

PROBE = """import json, logging
def parse_reported(payload): return json.loads(payload)
def parse_unreported(payload): return json.loads(payload)
def run():
    try: return parse_reported('invalid')
    except Exception: logging.exception('parse failed')
def quiet():
    try: return parse_unreported('invalid')
    except Exception: return None
def callback(): return 'done'
def invoke(fn): return fn()
def exercise():
    run()
    quiet()
    return invoke(callback)
def branching(flag):
    try: json.loads('invalid')
    except Exception:
        if flag: logging.warning('expected fallback')
        else: logging.exception('failed operation')
"""


def validate(output, python):
    output.mkdir(parents=True, exist_ok=True)
    root = output / "probe"
    root.mkdir(exist_ok=True)
    source = root / "demo.py"
    source.write_bytes(PROBE.encode())
    namespace = runpy.run_path(str(source), run_name="__flowsignal_authored_probe__")
    for name, alternate in (("first", False), ("second", True)):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            collect(
                root,
                output / f"{name}.json",
                run_id=name,
                scenario="explicit authored smoke test",
            ),
        ):
            namespace["exercise"]()
            if alternate:
                namespace["branching"](True)
                namespace["branching"](False)
    config = Config(entrypoints=["demo.exercise", "demo.branching"])
    measurements = {}
    for name in ("cold", "warm"):
        started = time.perf_counter()
        report = scan(root, config, cache_dir=output / "cache")
        measurements[name] = {
            "seconds": round(time.perf_counter() - started, 4),
            **report.analysis_stats["cache"],
        }
    assert measurements["warm"]["snapshot_reused"]
    assert any(
        h.path_reporting and len(h.reporting_paths) == 2 for h in report.handlers
    )
    baseline = make_baseline(report)
    # The authored baseline deliberately simulates one new finding and expired acceptance.
    baseline["findings"].pop()
    for finding in baseline["findings"]:
        finding.update(
            review_status="dismissed",
            review_reason="Demo acceptance expired",
            review_owner="workflow-team",
            review_expires="2020-01-01",
        )
    (output / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    for format in ("json", "html", "sarif", "text", "mermaid"):
        process = subprocess.run(
            [
                str(python),
                "-m",
                "flowsignal",
                "scan",
                str(root),
                "--entrypoint",
                "demo.exercise",
                "--entrypoint",
                "demo.branching",
                "--cache-dir",
                str(output / "cli-cache"),
                "--runtime-trace",
                str(output / "second.json"),
                "--previous-runtime-trace",
                str(output / "first.json"),
                "--baseline",
                str(output / "baseline.json"),
                "--format",
                format,
                "--output",
                str(output / ("report." + format)),
                "--timeout-seconds",
                "30",
                "--memory-mb",
                "512",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert process.returncode == 0, process.stderr
    result = json.loads((output / "report.json").read_text(encoding="utf-8"))
    states = {s for r in result["review_queue"]["items"] for s in r["states"]}
    assert {"new", "expired", "uncovered", "unresolved"} <= states
    assert result["runtime"]["run"]["id"] == "second"
    assert result["runtime"]["summary"]["observed_only"] == 1
    assert result["runtime"]["run_comparison"]["retained"]
    validation = {
        "passed": True,
        "cache": measurements,
        "queue_states": sorted(states),
        "runtime": result["runtime"]["summary"],
        "run_comparison": result["runtime"]["run_comparison"],
        "formats": ["json", "html", "sarif", "text", "mermaid"],
    }
    (output / "validation.json").write_text(
        json.dumps(validation, indent=2), encoding="utf-8"
    )
    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/workflow-review")
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    validate(args.output.resolve(), args.python.resolve())
