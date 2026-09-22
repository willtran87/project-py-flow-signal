"""Compare fresh, cold-cache, warm-cache and changed-source scans."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from benchmark_scan import workflow

from flowsignal import scan
from flowsignal.config import Config


def normalized(report):
    result = report.to_dict()
    result["analysis_stats"].pop("cache", None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 2 <= args.files <= 1000:
        parser.error("files must be between 2 and 1000")
    results = {}
    with tempfile.TemporaryDirectory(prefix="flowsignal-incremental-") as directory:
        root = Path(directory) / "source"
        root.mkdir()
        for index in range(args.files):
            (root / f"workflow_{index:05d}.py").write_bytes(workflow(index, 6).encode())
        config = Config(entrypoints=["workflow_*.run"])
        cache = Path(directory) / "cache"
        reports = {}
        for label, cached in (("fresh", False), ("cold", True), ("warm", True)):
            started = time.perf_counter()
            report = scan(root, config, cache_dir=cache if cached else None)
            results[label] = {
                "seconds": round(time.perf_counter() - started, 3),
                "cache": report.analysis_stats.get("cache"),
            }
            reports[label] = normalized(report)
        assert reports["fresh"] == reports["cold"] == reports["warm"]
        assert results["warm"]["cache"]["snapshot_reused"]
        changed = root / "workflow_00001.py"
        changed.write_bytes(workflow(2, 6).encode())
        started = time.perf_counter()
        report = scan(root, config, cache_dir=cache)
        results["changed"] = {
            "seconds": round(time.perf_counter() - started, 3),
            "cache": report.analysis_stats["cache"],
        }
        assert results["changed"]["cache"]["parsed_files_reused"] == args.files - 1
        assert normalized(report) == normalized(scan(root, config))
        assert len(report.findings) == args.files // 2 - 1
    result = {
        "files": args.files,
        "passed": True,
        "measurements": results,
        "note": "Local single-run observations. Cold caching adds serialization/I/O cost; warm snapshots reuse a full report. Any source change rebuilds all global relationships using unchanged parsed-file facts.",
    }
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
