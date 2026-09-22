"""Measure labeled accuracy without importing or executing corpus source files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from flowsignal import scan
from flowsignal.config import Config


def score(expected, actual):
    expected, actual = set(map(tuple, expected)), set(map(tuple, actual))
    tp, fp, fn = len(expected & actual), len(actual - expected), len(expected - actual)
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "unexpected": sorted(actual - expected),
        "missed": sorted(expected - actual),
    }


def evaluate(manifest_path):
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if document.get("schema_version") != "flowsignal-accuracy-1":
        raise ValueError("Unsupported accuracy manifest")
    cases = []
    for case in document["cases"]:
        root = (manifest_path.parent / case["path"]).resolve()
        if not root.is_relative_to(manifest_path.parent.resolve()):
            raise ValueError("Corpus path must stay inside the manifest directory")
        for name, digest in case["sha256"].items():
            source = (root / name).resolve()
            if (
                not source.is_relative_to(root)
                or hashlib.sha256(source.read_bytes()).hexdigest() != digest
            ):
                raise ValueError(f"Corpus snapshot changed: {case['id']}/{name}")
        report = scan(root, Config(**case.get("config", {})))
        if report.summary()["status"] != "complete":
            raise ValueError(f"Incomplete corpus scan: {case['id']}")
        names = {
            s.id: s.qualified_name.removesuffix(".<module>") for s in report.symbols
        }
        actual = {
            "edges": {
                (names[c.symbol], names[c.target]) for c in report.calls if c.target
            },
            "findings": {
                (f.rule_id, names[f.symbol], f.evidence[0].location.line)
                for f in report.findings
            },
            "owners": {
                (names[d.symbol], names[e.symbol])
                for d in report.coverage
                for e in d.evidence
                if d.status == "recognized"
                and e.credited
                and e.reason in {"failure_log", "configured_reporter", "trace_coverage"}
            },
        }
        labels = dict(case)
        if "upstream_graph" in case:
            graph = json.loads(
                (root / case["upstream_graph"]).read_text(encoding="utf-8")
            )
            labels["edges"] = [
                (caller, callee)
                for caller, targets in graph.items()
                for callee in targets
            ]
        measured = {
            kind: score(labels[kind], actual[kind]) for kind in actual if kind in labels
        }
        by_rule = {}
        if "findings" in labels:
            rule_ids = {row[0] for row in [*labels["findings"], *actual["findings"]]}
            by_rule = {
                rule: score(
                    [r for r in labels["findings"] if r[0] == rule],
                    [r for r in actual["findings"] if r[0] == rule],
                )
                for rule in sorted(rule_ids)
            }
        cases.append(
            {
                "id": case["id"],
                "split": case["split"],
                "source": case["source"],
                "metrics": measured,
                "findings_by_rule": by_rule,
                "unlabeled": sorted(set(actual) - set(measured)),
            }
        )
    totals = {}
    for split in sorted({case["split"] for case in cases}):
        totals[split] = {}
        for kind in ("edges", "findings", "owners"):
            metrics = [
                c["metrics"][kind]
                for c in cases
                if c["split"] == split and kind in c["metrics"]
            ]
            if not metrics:
                continue
            tp, fp, fn = (
                sum(m[k] for m in metrics)
                for k in ("true_positive", "false_positive", "false_negative")
            )
            totals[split][kind] = {
                "labeled_cases": len(metrics),
                "true_positive": tp,
                "false_positive": fp,
                "false_negative": fn,
                "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
            }
    return {
        "schema_version": "flowsignal-accuracy-results-1",
        "totals": totals,
        "cases": cases,
        "note": "Small fixed corpus, not enterprise accuracy. Evaluation has independent call labels only; unlabeled findings/owners are not scored. Missing expected symbols count as misses. Pairs are unique, not call-site counts. No target code was executed.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "benchmarks/manifest.json",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check",
        type=Path,
        help="Fail on per-case increases in false positives or misses against saved results",
    )
    args = parser.parse_args()
    result = evaluate(args.manifest)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(json.dumps(result["totals"], indent=2))
    if args.check:
        previous = json.loads(args.check.read_text(encoding="utf-8"))
        expected = {c["id"]: c for c in previous["cases"]}
        if set(expected) != {c["id"] for c in result["cases"]}:
            raise SystemExit(
                "Corpus membership changed; explicitly review the benchmark baseline"
            )
        for case in result["cases"]:
            old = expected[case["id"]]
            if old["split"] != case["split"] or set(old["metrics"]) != set(
                case["metrics"]
            ):
                raise SystemExit("Benchmark split or labels changed")
            for kind, metric in case["metrics"].items():
                if any(
                    metric[key] > old["metrics"][kind][key]
                    for key in ("false_positive", "false_negative")
                ):
                    raise SystemExit(f"Accuracy regression: {case['id']} {kind}")


if __name__ == "__main__":
    main()
