"""Isolated scan-stage, peak-memory, and cache-equivalence measurements."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from flowsignal import scan
from flowsignal.config import Config
from flowsignal.diagram import render_html


def peak_bytes():
    if os.name != "nt":
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == "darwin" else 1024
        )

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_working_set",
                "working_set",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    function = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not function(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.peak_working_set


def worker(request):
    config = Config(**request["config"])
    started = time.perf_counter()
    report = scan(request["root"], config, cache_dir=request.get("cache"), measure=True)
    scan_seconds = time.perf_counter() - started
    timings = dict(report.analysis_stats["timings"])
    cache = report.analysis_stats.get("cache")
    started = time.perf_counter()
    payload = report.to_dict()
    payload["analysis_stats"].pop("timings", None)
    payload["analysis_stats"].pop("cache", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    timings["serialization"] = time.perf_counter() - started
    started = time.perf_counter()
    html_size = len(render_html(report))
    timings["rendering"] = time.perf_counter() - started
    return {
        "scan_seconds": scan_seconds,
        "timings": timings,
        "peak_memory_bytes": peak_bytes(),
        "summary": report.summary(),
        "cache": cache,
        "semantic_digest": fingerprint,
        "html_bytes": html_size,
        "settings": config.to_dict(),
    }


def run(root, config, cache=None):
    request = {
        "root": str(root),
        "config": config,
        "cache": str(cache) if cache else None,
    }
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=900,
    )
    if process.returncode:
        raise RuntimeError(process.stderr)
    return json.loads(process.stdout)


def suite(source, config, repeats, modes, synthetic=0, shape="many"):
    measurements = {}
    with tempfile.TemporaryDirectory(prefix="flowsignal-workloads-") as temporary:
        home = Path(temporary)
        root = home / "source"
        if source:
            shutil.copytree(source, root)
        else:
            root.mkdir()
            if shape == "dense":
                body = "import json\n"
                for i in range(40):
                    body += f"def task_{i}():\n"
                    body += "".join(f"    task_{j}()\n" for j in range(40))
                    body += "    return json.loads('{}')\n"
                (root / "dense.py").write_text(body, encoding="utf-8")
            elif shape == "long":
                body = "import json\n" + "".join(
                    f"def task_{i}():\n    value = '{{}}'\n    return json.loads(value)\n"
                    for i in range(2000)
                )
                (root / "long.py").write_text(body, encoding="utf-8")
            else:
                (root / "shared.py").write_text(
                    "def work(): return 1\n", encoding="utf-8"
                )
                for i in range(synthetic - 1):
                    body = "from shared import work\nimport json\ndef run():\n    work()\n    return json.loads('{}')\n"
                    (root / f"flow_{i:05d}.py").write_text(body, encoding="utf-8")
        leaf = next(iter(sorted(root.rglob("*.py"))))
        shared = (
            root / "shared.py"
            if synthetic
            else next(
                (
                    p
                    for p in root.rglob("*.py")
                    if p.name in {"helpers.py", "util.py", "_utils.py"}
                ),
                leaf,
            )
        )
        originals = {p: p.read_bytes() for p in {leaf, shared}}
        extra = root / "flowsignal_added_probe.py"
        for repeat in range(repeats):
            for path, raw in originals.items():
                path.write_bytes(raw)
            if extra.exists():
                extra.unlink()
            cache = home / ("cache-" + str(repeat))
            fresh = run(root, config)
            measurements.setdefault("fresh", []).append(fresh)
            if modes == "fresh":
                print(
                    f"  repetition {repeat + 1}/{repeats}: fresh {fresh['scan_seconds']:.3f}s",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            for label in ("cold", "warm"):
                result = run(root, config, cache)
                if result["semantic_digest"] != fresh["semantic_digest"]:
                    raise AssertionError("Cache semantic mismatch: " + label)
                measurements.setdefault(label, []).append(result)
            scenarios = [
                "leaf_edit",
                "shared_edit",
                "config_change",
                "add_file",
                "delete_file",
            ]
            for label in scenarios:
                current = dict(config)
                if label == "leaf_edit":
                    leaf.write_bytes(originals[leaf] + b"\n# leaf source changed\n")
                elif label == "shared_edit":
                    shared.write_bytes(
                        originals[shared] + b"\n# shared source changed\n"
                    )
                elif label == "config_change":
                    current["include_source"] = False
                elif label == "add_file":
                    extra.write_text(
                        "import json\ndef probe(): return json.loads('bad')\n",
                        encoding="utf-8",
                    )
                elif label == "delete_file":
                    extra.unlink()
                result, check = run(root, current, cache), run(root, current)
                if result["semantic_digest"] != check["semantic_digest"]:
                    raise AssertionError("Changed scan differs from fresh: " + label)
                result["fresh_comparison_seconds"] = check["scan_seconds"]
                measurements.setdefault(label, []).append(result)
            print(
                f"  repetition {repeat + 1}/{repeats}: all modes equivalent",
                file=sys.stderr,
                flush=True,
            )
    summaries = {}
    for label, rows in measurements.items():
        seconds = [r["scan_seconds"] for r in rows]
        summaries[label] = {
            "median_seconds": statistics.median(seconds),
            "range_seconds": [min(seconds), max(seconds)],
            "peak_memory_bytes": max(r["peak_memory_bytes"] for r in rows),
            "complete_runs": sum(r["summary"]["status"] == "complete" for r in rows),
            "samples": rows,
        }
    fresh_time = summaries["fresh"]["median_seconds"]
    return {
        "modes": summaries,
        "targets": {
            "warm_at_most_70_percent": summaries["warm"]["median_seconds"]
            <= fresh_time * 0.7
            if "warm" in summaries
            else None,
            "leaf_change_at_most_110_percent": statistics.median(
                r["scan_seconds"] / r["fresh_comparison_seconds"]
                for r in measurements["leaf_edit"]
            )
            <= 1.1
            if "leaf_edit" in measurements
            else None,
        },
        "equivalence": "passed for measured cached modes"
        if modes != "fresh"
        else "not measured",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument(
        "--manifest", type=Path, default=Path("benchmarks/workflow-cohort.json")
    )
    parser.add_argument("--sizes", type=int, nargs="*", default=[1000, 10000])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--skip-repositories", action="store_true")
    parser.add_argument(
        "--shapes",
        action="store_true",
        help="Also measure dense/cyclic and long-file inputs",
    )
    parser.add_argument("--modes", choices=["all", "fresh"], default="all")
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/outcome-review/scale.json")
    )
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(json.load(sys.stdin))))
        return
    if not 1 <= args.repeats <= 20 or any(not 2 <= n <= 10000 for n in args.sizes):
        parser.error("repeats must be 1-20; sizes must be 2-10000")
    from workflow_review import load_cohort

    import flowsignal

    package = Path(flowsignal.__file__).parent
    implementation = hashlib.sha256()
    for path in sorted([*package.glob("*.py"), *package.glob("templates/*.html")]):
        implementation.update(path.relative_to(package).as_posix().encode())
        implementation.update(path.read_bytes())
    result = {
        "implementation_sha256": implementation.hexdigest(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "repeats": args.repeats,
        "mode_selection": args.modes,
        "workloads": {},
        "note": "Isolated subprocesses, sequential measurements; setup is excluded. Peak working set/RSS includes serialization and HTML rendering. Timings are observations, not CI assertions. Selected public libraries are not representative enterprise applications.",
    }
    jobs = []
    if not args.skip_repositories:
        manifest, _ = load_cohort(args.manifest)
        jobs.extend(
            (name, args.manifest.parent / r["path"], r["config"], 0, "many")
            for name, r in manifest["repositories"].items()
        )
    jobs.extend((f"synthetic-{n}", None, {}, n, "many") for n in args.sizes)
    if args.shapes:
        jobs.extend(
            [
                ("dense-cyclic", None, {"max_graph_steps": 2000}, 0, "dense"),
                ("long-file", None, {}, 0, "long"),
            ]
        )
    if not jobs:
        parser.error("Select repositories, synthetic sizes, or --shapes")
    for name, source, config, size, shape in jobs:
        print("Measuring " + name, file=sys.stderr, flush=True)
        result["workloads"][name] = suite(
            source, config, args.repeats, args.modes, size, shape
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"output": str(args.output), "workloads": list(result["workloads"])},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
