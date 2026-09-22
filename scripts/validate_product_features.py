"""Exercise CLI exports/reviews using an explicitly authored, safe runtime probe.

Only this script executes the probe below. The scanner and runtime importer never
execute a scanned repository. No arbitrary source path is accepted for execution.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import runpy
import subprocess
import sys
from collections import Counter
from pathlib import Path

PROBE = """import json, logging
def parse_reported(payload):
    return json.loads(payload)
def parse_unreported(payload):
    return json.loads(payload)
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
"""


def validate(output, python):
    output.mkdir(parents=True, exist_ok=True)
    source = output / "probe"
    source.mkdir(exist_ok=True)
    fixture = source / "demo.py"
    fixture.write_bytes(PROBE.encode())
    namespace = runpy.run_path(str(fixture), run_name="__flowsignal_authored_probe__")
    pairs = Counter()

    def profile(frame, event, argument):
        if event != "call" or not frame.f_back:
            return
        if frame.f_code.co_filename == str(fixture) == frame.f_back.f_code.co_filename:
            pairs[
                (
                    "demo.py:" + frame.f_back.f_code.co_qualname,
                    "demo.py:" + frame.f_code.co_qualname,
                )
            ] += 1

    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        with contextlib.redirect_stderr(io.StringIO()):
            assert namespace["exercise"]() == "done"
    finally:
        sys.setprofile(previous)
    trace = {
        "schema_version": "flowsignal-runtime-1",
        "files": {"demo.py": hashlib.sha256(fixture.read_bytes()).hexdigest()},
        "pairs": [
            {"caller": a, "callee": b, "count": n}
            for (a, b), n in sorted(pairs.items())
        ],
    }
    trace_path = output / "trace.json"
    trace_path.write_text(json.dumps(trace, indent=2), encoding="utf-8")

    def cli(*args, expected=0):
        process = subprocess.run(
            [str(python), "-m", "flowsignal", *map(str, args)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert process.returncode == expected, process.stderr or process.stdout
        return process

    baseline_path = output / "baseline.json"
    common = ["scan", source, "--runtime-trace", trace_path]
    cli(
        *common,
        "--format",
        "json",
        "--output",
        output / "report.json",
        "--save-baseline",
        baseline_path,
    )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["runtime"]["summary"]["observed_only"] == 1
    assert report["runtime"]["summary"]["observed_and_inferred"] >= 5
    assert not report["runtime"]["summary"]["source_mismatch"]
    fingerprint = next(
        f["fingerprint"] for f in report["findings"] if f["rule_id"] == "FS005"
    )
    cli(
        "review",
        "dismiss",
        baseline_path,
        fingerprint,
        "--reason",
        "Temporary acceptance while parsing ownership is assigned",
        "--owner",
        "workflow-platform",
    )
    for format, extension in [
        ("html", "html"),
        ("sarif", "sarif"),
        ("text", "txt"),
        ("mermaid", "mmd"),
    ]:
        cli(
            *common,
            "--baseline",
            baseline_path,
            "--format",
            format,
            "--output",
            output / ("report." + extension),
            "--timeout-seconds",
            "30",
            "--memory-mb",
            "512",
        )
    sarif = json.loads((output / "report.sarif").read_text(encoding="utf-8"))
    assert any(r.get("suppressions") for r in sarif["runs"][0]["results"])
    cli("review", "restore", baseline_path, fingerprint, "--owner", "workflow-platform")
    cli(
        *common,
        "--baseline",
        baseline_path,
        "--format",
        "json",
        "--output",
        output / "restored.json",
    )
    restored = json.loads((output / "restored.json").read_text(encoding="utf-8"))
    assert [e["action"] for e in restored["review_history"]] == ["dismiss", "restore"]
    # An expired dismissal is active even with --fail-on-new.
    document = json.loads(baseline_path.read_text(encoding="utf-8"))
    for item in document["findings"]:
        item["review_status"], item["review_reason"], item["review_expires"] = (
            "dismissed",
            "Expired acceptance",
            "2020-01-01",
        )
    baseline_path.write_text(json.dumps(document), encoding="utf-8")
    cli(
        *common,
        "--baseline",
        baseline_path,
        "--fail-on",
        "low",
        "--fail-on-new",
        "--output",
        output / "expired.txt",
        expected=1,
    )
    print(
        json.dumps(
            {
                "passed": True,
                "runtime": report["runtime"]["summary"],
                "formats": ["json", "text", "html", "mermaid", "sarif"],
                "checks": [
                    "authored probe observations",
                    "supervised exports",
                    "dismiss/restore history",
                    "expiry reopens new-only gate",
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/product-features")
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    validate(args.output.resolve(), args.python.resolve())
