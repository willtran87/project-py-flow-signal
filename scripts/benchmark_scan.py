"""Reproducible general-Python workload; timings are local observations, not an SLA."""

from __future__ import annotations

import argparse
import json
import platform
import tempfile
import time
from collections import Counter
from pathlib import Path

from flowsignal import scan
from flowsignal.config import Config


def workflow(index: int, depth: int) -> str:
    lines = ["import logging", "import requests", ""]
    for step in range(depth):
        operation = (
            "requests.get('https://example.invalid', timeout=5)"
            if step == depth - 1
            else f"step_{step + 1}()"
        )
        lines.extend([f"def step_{step}():", f"    return {operation}", ""])
    lines.append("def run():")
    if index % 2 == 0:
        lines.extend(
            [
                "    try:",
                "        return step_0()",
                "    except Exception:",
                "        logging.exception('Operation failed')",
                "        raise",
            ]
        )
    else:
        lines.append("    return step_0()")
    return "\n".join(lines) + "\n"


def measure(target: Path, config: Config) -> dict:
    start = time.perf_counter()
    report = scan(target, config)
    elapsed = time.perf_counter() - start
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "scan_seconds": round(elapsed, 3),
        "summary": report.summary(),
        "analysis_stats": report.analysis_stats,
        "rules": dict(sorted(Counter(f.rule_id for f in report.findings).items())),
        "diagnostics": dict(
            sorted(Counter(d.code for d in report.diagnostics).items())
        ),
        "settings": config.to_dict(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        help="Scan an existing target instead of the synthetic fixture",
    )
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--depth", type=int, default=6)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.files <= 10_000 or not 1 <= args.depth <= 6:
        parser.error(
            "files must be 1–10000 and depth 1–6 (within the default path budget)"
        )
    if args.target:
        result = measure(args.target, Config())
        result["target"] = str(args.target.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="flowsignal-benchmark-") as temporary:
            root = Path(temporary)
            for index in range(args.files):
                (root / f"workflow_{index:05d}.py").write_text(
                    workflow(index, args.depth), encoding="utf-8", newline="\n"
                )
            result = measure(root, Config(entrypoints=["workflow_*.run"]))
        result["fixture"] = {"files": args.files, "depth": args.depth}
        expected = args.files // 2
        result["expectations_passed"] = (
            result["summary"]["status"] == "complete"
            and result["summary"]["files_scanned"] == args.files
            and result["summary"]["unresolved_calls"] == 0
            and result["rules"] == ({"FS005": expected} if expected else {})
        )
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if result.get("expectations_passed") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
