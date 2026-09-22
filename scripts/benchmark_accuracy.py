"""Measure labeled accuracy without importing or executing corpus source files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from flowsignal import scan
from flowsignal.config import Config
from flowsignal.rules import RULES
from flowsignal.source_io import read_snapshot


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


def review_packet(manifest_path):
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = []
    for case in document["cases"]:
        if case["split"] != "review_pending":
            continue
        root = (manifest_path.parent / case["path"]).resolve()
        if not root.is_relative_to(manifest_path.parent.resolve()):
            raise ValueError("Review source must stay within the corpus")
        sources = {}
        for name, expected in case["sha256"].items():
            path = (root / name).resolve()
            if not path.is_relative_to(root):
                raise ValueError("Review source escapes case")
            raw = read_snapshot(path, 2_000_000, root)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Review source snapshot changed")
            sources[name] = raw.decode("utf-8")
        cases.append(
            {
                "id": case["id"],
                "sha256": case["sha256"],
                "config": case.get("config", {}),
                "sources": sources,
                "completed": False,
                "reviewer": "",
                "rationale": "",
                "findings": None,
                "owners": None,
            }
        )
    return {
        "schema_version": "flowsignal-label-review-1",
        "rules": RULES,
        "cases": cases,
        "instructions": "Review source independently of scanner output. Fill complete lists: findings [[rule_id, qualified_symbol, line], ...], owners [[boundary_function, reporting_function], ...]. Empty lists explicitly label absence. Supply reviewer, rationale, and completed=true. Identity is declared, not authenticated. Do not change source hashes/config. Unsupported business assumptions belong in rationale.",
    }


def reviewed_labels(path, cases):
    if path is None:
        return {}
    document = json.loads(read_snapshot(path, 20_000_000))
    if document.get("schema_version") != "flowsignal-label-review-1" or not isinstance(
        document.get("cases"), list
    ):
        raise ValueError("Unsupported label review")
    manifest = {c["id"]: c for c in cases}
    reviews = {}
    for review in document["cases"]:
        case = manifest.get(review.get("id"))
        if (
            not case
            or case["split"] != "review_pending"
            or review.get("sha256") != case["sha256"]
            or review.get("config") != case.get("config", {})
        ):
            raise ValueError(
                "Reviewed source or configuration does not match the pending corpus case"
            )
        if review["id"] in reviews or review.get("completed") is not True:
            raise ValueError("Review must be completed and unique")
        for key, maximum in (("reviewer", 200), ("rationale", 4000)):
            if (
                not isinstance(review.get(key), str)
                or not review[key].strip()
                or len(review[key]) > maximum
            ):
                raise ValueError("Review requires a declared reviewer and rationale")
        for key, length in (("findings", 3), ("owners", 2)):
            if not isinstance(review.get(key), list) or len(review[key]) > 10_000:
                raise ValueError(
                    "Review requires complete bounded finding and owner labels"
                )
            for label in review[key]:
                if (
                    not isinstance(label, list)
                    or len(label) != length
                    or any(
                        not isinstance(v, str) or not v or len(v) > 8192
                        for v in label[:2]
                    )
                ):
                    raise ValueError("Malformed review label")
                if key == "findings" and (
                    label[0] not in RULES or type(label[2]) is not int or label[2] < 1
                ):
                    raise ValueError(
                        "Review finding requires a known rule and source line"
                    )
        reviews[review["id"]] = review
    return reviews


def evaluate(manifest_path, review_path=None):
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if document.get("schema_version") != "flowsignal-accuracy-1":
        raise ValueError("Unsupported accuracy manifest")
    reviews = reviewed_labels(review_path, document["cases"])
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
        if {p.relative_to(root).as_posix() for p in root.rglob("*.py")} != {
            n for n in case["sha256"] if n.endswith(".py")
        }:
            raise ValueError(
                "Corpus Python membership differs from the pinned snapshot"
            )
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
        if case["id"] in reviews:
            labels.update({k: reviews[case["id"]][k] for k in ("findings", "owners")})
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
                "feature": case.get("feature", "unspecified"),
                "reviewer": reviews.get(case["id"], {}).get("reviewer"),
                "label_signature": hashlib.sha256(
                    json.dumps(
                        [
                            case["sha256"],
                            case.get("config", {}),
                            {k: labels[k] for k in actual if k in labels},
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                "source": case["source"],
                "metrics": measured,
                "contextual_call_metrics": score(
                    labels["edges"],
                    actual["edges"]
                    | {
                        (names[r["caller"]], names[r["callee"]])
                        for r in report.contextual_calls
                        if r["basis"] == "callable_argument"
                    },
                )
                if "edges" in labels
                else None,
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
    by_feature = {}
    contextual_totals = {}
    for split in totals:
        metrics = [
            c["contextual_call_metrics"]
            for c in cases
            if c["split"] == split and c["contextual_call_metrics"] is not None
        ]
        if metrics:
            tp, fp, fn = (
                sum(m[k] for m in metrics)
                for k in ("true_positive", "false_positive", "false_negative")
            )
            contextual_totals[split] = {
                "true_positive": tp,
                "false_positive": fp,
                "false_negative": fn,
                "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
            }
    for case in cases:
        if "edges" in case["metrics"]:
            key = case["split"] + "/" + case["feature"]
            counts = by_feature.setdefault(
                key,
                {k: 0 for k in ("true_positive", "false_positive", "false_negative")},
            )
            for k in counts:
                counts[k] += case["metrics"]["edges"][k]
    for counts in by_feature.values():
        tp, fp, fn = (
            counts[k] for k in ("true_positive", "false_positive", "false_negative")
        )
        counts.update(
            precision=tp / (tp + fp) if tp + fp else None,
            recall=tp / (tp + fn) if tp + fn else None,
        )
    return {
        "schema_version": "flowsignal-accuracy-results-1",
        "totals": totals,
        "contextual_call_totals": contextual_totals,
        "cases": cases,
        "call_features": by_feature,
        "independently_reviewed_cases": sum(bool(c["reviewer"]) for c in cases),
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
        "--export-review",
        type=Path,
        help="Write a source-only packet without scanner judgments for independent labeling",
    )
    parser.add_argument(
        "--review-file",
        type=Path,
        help="Score completed independently reviewed labels matching source/config snapshots",
    )
    parser.add_argument(
        "--check",
        type=Path,
        help="Fail on per-case increases in false positives or misses against saved results",
    )
    args = parser.parse_args()
    if args.export_review:
        args.export_review.parent.mkdir(parents=True, exist_ok=True)
        args.export_review.write_text(
            json.dumps(review_packet(args.manifest), indent=2) + "\n", encoding="utf-8"
        )
    result = evaluate(args.manifest, args.review_file)
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
            if old.get("label_signature") != case["label_signature"]:
                raise SystemExit(
                    "Benchmark labels, source, or configuration changed; review baseline explicitly"
                )
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
            if old.get("contextual_call_metrics") and any(
                case["contextual_call_metrics"][k] > old["contextual_call_metrics"][k]
                for k in ("false_positive", "false_negative")
            ):
                raise SystemExit(f"Contextual call accuracy regression: {case['id']}")


if __name__ == "__main__":
    main()
