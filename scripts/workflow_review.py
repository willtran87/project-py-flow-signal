"""Prepare and score source-only, independently labeled workflow review packets."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path

from flowsignal import scan
from flowsignal.config import OUTCOMES, Config
from flowsignal.rules import RULES
from flowsignal.source_io import read_snapshot

RUBRIC = {
    "version": "workflow-rubric-1",
    "finding": "Complete scoped findings are (rule, qualified symbol, line). Empty lists explicitly label absence.",
    "judgment": "Each judgment identifies symbol, line, acceptable outcomes, and alternatives of {kind, level, owner}. Record explicit no_additional_log when appropriate.",
    "agreement": "All predicted choices at a matched site must belong to the acceptable alternatives, with at least one prediction. Extra severity alternatives fail agreement. Outcome sets must be nonempty subsets of acceptable outcomes. Conditional/unknown outcomes are abstentions unless explicitly acceptable.",
    "independence": "Review full scoped source before seeing scanner output. Record disagreements and adjudicate explicitly. Identity is declared, not authenticated. Unreviewed and abstained scenarios remain unscored.",
}
KINDS = {"log", "trace", "metric", "enrich_existing_log", "no_additional_log"}
SELECTION = {
    "flask": (
        "https://github.com/pallets/flask",
        "src/flask",
        "service",
        {
            "app": [
                "Flask.run",
                "Flask.full_dispatch_request",
                "Flask.finalize_request",
                "Flask.wsgi_app",
                "Flask.make_response",
                "Flask.test_request_context",
            ],
            "config": [
                "Config.from_prefixed_env",
                "Config.from_pyfile",
                "Config.from_file",
            ],
            "sessions": ["SecureCookieSessionInterface.open_session"],
        },
    ),
    "billiard": (
        "https://github.com/celery/billiard",
        "billiard",
        "background",
        {
            "pool": [
                "Worker.workloop",
                "Worker.after_fork",
                "TaskHandler.body",
                "TimeoutHandler.on_soft_timeout",
                "TimeoutHandler.on_hard_timeout",
                "ResultHandler._process_result",
                "ApplyResult.safe_apply_callback",
            ],
            "queues": ["Queue.get", "Queue._feed"],
            "process": ["BaseProcess._bootstrap"],
        },
    ),
    "tenacity": (
        "https://github.com/jd/tenacity",
        "tenacity",
        "callback_retry",
        {
            "__init__": ["Retrying.__call__"],
            "asyncio/__init__": [
                "AsyncRetrying.__call__",
                "AsyncRetrying._run_retry",
                "AsyncRetrying._run_wait",
                "AsyncRetrying._run_stop",
                "AsyncRetrying.iter",
                "AsyncRetrying.__anext__",
            ],
            "asyncio/retry": [
                "retry_if_exception.__call__",
                "retry_if_result.__call__",
                "retry_any.__call__",
            ],
        },
    ),
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


def prepare(checkouts, manifest):
    if manifest.exists():
        raise ValueError(
            "Cohort already exists; use a new manifest path to explicitly revise membership"
        )
    repos, cases = {}, []
    for name, (url, package, kind, modules) in SELECTION.items():
        root = checkouts / name
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        target = manifest.parent / "corpus" / "workflow_review" / name
        hashes = {}
        for source in sorted((root / package).rglob("*.py")):
            relative = source.relative_to(root)
            data = read_snapshot(source, 2_000_000, root)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            hashes[relative.as_posix()] = hashlib.sha256(data).hexdigest()
        licenses = [
            p
            for p in root.iterdir()
            if p.is_file()
            and p.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
        ]
        if not licenses:
            raise ValueError("Source license required: " + name)
        for source in licenses:
            shutil.copyfile(source, target / source.name)
            hashes[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
        repos[name] = {
            "url": url,
            "revision": revision,
            "path": target.relative_to(manifest.parent).as_posix(),
            "sha256": hashes,
            "config": {"source_roots": ["src"]} if name == "flask" else {},
        }
        for module, selectors in modules.items():
            relative = package + "/" + module + ".py"
            parsed = ast.parse((root / relative).read_bytes())
            found = {}

            def visit(node, prefix=""):
                for child in getattr(node, "body", []):
                    if isinstance(child, ast.ClassDef):
                        visit(child, prefix + child.name + ".")
                    elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found[prefix + child.name] = child

            visit(parsed)
            module_name = (name + "." + module.replace("/", ".")).removesuffix(
                ".__init__"
            )
            for selector in selectors:
                node = found[selector]
                cases.append(
                    {
                        "id": name + "/" + module + "/" + selector,
                        "repository": name,
                        "workflow_type": kind,
                        "symbol": module_name + "." + selector,
                        "file": relative,
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                        "split": "review_pending",
                        "tuning_used": False,
                    }
                )
    write(
        manifest,
        {
            "schema_version": "flowsignal-workflow-cohort-1",
            "rubric": RUBRIC,
            "repositories": repos,
            "cases": cases,
            "selection": "Ten explicitly named source scopes per project, selected before labeling. Not a random or representative enterprise sample. Complete Python package snapshots are included for context; dependencies are not executed.",
        },
    )


def load_cohort(path):
    doc = json.loads(read_snapshot(path, 20_000_000))
    if (
        doc.get("schema_version") != "flowsignal-workflow-cohort-1"
        or doc.get("rubric") != RUBRIC
    ):
        raise ValueError("Unsupported cohort or changed scoring rubric")
    if len(doc["cases"]) != len({c["id"] for c in doc["cases"]}):
        raise ValueError("Duplicate cohort IDs")
    sources = {}
    for name, repo in doc["repositories"].items():
        root = (path.parent / repo["path"]).resolve()
        if not root.is_relative_to(path.parent.resolve()):
            raise ValueError("Repository snapshot escapes corpus")
        sources[name] = {}
        for relative, expected in repo["sha256"].items():
            source = root / relative
            raw = read_snapshot(source, 2_000_000, root)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Source snapshot changed: " + relative)
            sources[name][relative] = raw.decode("utf-8")
        if {p.relative_to(root).as_posix() for p in root.rglob("*.py")} != {
            n for n in repo["sha256"] if n.endswith(".py")
        }:
            raise ValueError("Python source membership changed")
    return doc, sources


def packet(path):
    cohort, sources = load_cohort(path)
    return {
        "schema_version": "flowsignal-workflow-review-1",
        "cohort_signature": digest(cohort),
        "rubric": RUBRIC,
        "reviewer": "",
        "rule_catalog": RULES,
        "role": "independent",
        "sources": sources,
        "provenance": cohort["repositories"],
        "cases": [
            dict(c, status="pending", rationale="", findings=None, judgments=None)
            for c in cohort["cases"]
        ],
    }


def validate_review(document, cohort):
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != "flowsignal-workflow-review-1"
        or document.get("cohort_signature") != digest(cohort)
        or document.get("rubric") != RUBRIC
    ):
        raise ValueError("Stale source/configuration/cohort or changed rubric")
    if document.get("provenance") != cohort["repositories"] or not isinstance(
        document.get("sources"), dict
    ):
        raise ValueError("Review source provenance changed")
    for name, repository in cohort["repositories"].items():
        sources = document["sources"].get(name)
        if not isinstance(sources, dict) or set(sources) != set(repository["sha256"]):
            raise ValueError("Review source membership changed")
        for path, expected_hash in repository["sha256"].items():
            if (
                not isinstance(sources[path], str)
                or hashlib.sha256(sources[path].encode("utf-8")).hexdigest()
                != expected_hash
            ):
                raise ValueError("Review packet source was modified")
    reviewer = document.get("reviewer")
    if (
        not isinstance(reviewer, str)
        or not reviewer.strip()
        or len(reviewer) > 200
        or document.get("role") not in {"independent", "adjudication"}
    ):
        raise ValueError("Declared reviewer and valid role required")
    expected = {c["id"]: c for c in cohort["cases"]}
    reviews = {}
    if not isinstance(document.get("cases"), list):
        raise ValueError("Review cases must be a list")
    for row in document["cases"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or row.get("id") not in expected
            or row["id"] in reviews
        ):
            raise ValueError("Unknown or duplicate review case")
        if any(row.get(k) != v for k, v in expected[row["id"]].items()):
            raise ValueError("Review scope changed")
        if row.get("status") not in {"pending", "complete", "abstain"}:
            raise ValueError("Review status required")
        if row["status"] != "pending" and (
            not isinstance(row.get("rationale"), str)
            or not row["rationale"].strip()
            or len(row["rationale"]) > 4000
        ):
            raise ValueError("Completed/abstained review requires rationale")
        if row["status"] != "complete":
            if row.get("findings") is not None or row.get("judgments") is not None:
                raise ValueError("Pending or abstained cases must remain unlabeled")
        else:
            if (
                not isinstance(row.get("findings"), list)
                or not isinstance(row.get("judgments"), list)
                or max(len(row["findings"]), len(row["judgments"])) > 1000
            ):
                raise ValueError("Complete bounded labels required")
            keys = set()
            for label in row["findings"]:
                if (
                    not isinstance(label, list)
                    or len(label) != 3
                    or not isinstance(label[0], str)
                    or label[0] not in RULES
                    or label[1] != row["symbol"]
                    or type(label[2]) is not int
                    or not row["line"] <= label[2] <= row["end_line"]
                    or tuple(label) in keys
                ):
                    raise ValueError("Invalid or duplicate scoped finding")
                keys.add(tuple(label))
            keys = set()
            for label in row["judgments"]:
                if (
                    not isinstance(label, dict)
                    or set(label) != {"symbol", "line", "outcomes", "alternatives"}
                    or label["symbol"] != row["symbol"]
                    or type(label["line"]) is not int
                    or not row["line"] <= label["line"] <= row["end_line"]
                    or label["line"] in keys
                ):
                    raise ValueError("Invalid or duplicate judgment site")
                keys.add(label["line"])
                if (
                    not isinstance(label["outcomes"], list)
                    or not label["outcomes"]
                    or any(not isinstance(value, str) for value in label["outcomes"])
                    or not set(label["outcomes"]) <= OUTCOMES
                ):
                    raise ValueError("Acceptable outcomes required")
                choices = label["alternatives"]
                if not isinstance(choices, list) or not 1 <= len(choices) <= 10:
                    raise ValueError("Bounded acceptable choices required")
                for choice in choices:
                    if (
                        not isinstance(choice, dict)
                        or set(choice) != {"kind", "level", "owner"}
                        or not isinstance(choice["kind"], str)
                        or choice["kind"] not in KINDS
                        or not (
                            choice["level"] is None or isinstance(choice["level"], str)
                        )
                        or choice["level"]
                        not in {None, "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
                        or not (
                            choice["owner"] is None
                            or isinstance(choice["owner"], str)
                            and 0 < len(choice["owner"]) <= 500
                        )
                    ):
                        raise ValueError("Invalid acceptable signal/level/owner")
                if len({digest(c) for c in choices}) != len(choices):
                    raise ValueError("Duplicate acceptable choices")
        reviews[row["id"]] = row
    if set(reviews) != set(expected):
        raise ValueError("Review packet membership changed")
    return reviews


def interval(success, count):
    if not count:
        return None
    z, p = 1.96, success / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    radius = (
        z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    )
    return [max(0, center - radius), min(1, center + radius)]


def score_reviews(manifest, paths):
    cohort, _ = load_cohort(manifest)
    packets = [json.loads(read_snapshot(p, 20_000_000)) for p in paths]
    reviews = [validate_review(p, cohort) for p in packets]
    if len({p.get("reviewer") for p in packets}) != len(packets):
        raise ValueError("Reviewers must have distinct declared identities")
    reports = {}
    results = []
    for document, review in zip(packets, reviews):
        groups = {}
        outcome_ok = choice_ok = judged = 0
        matched = matched_choice_ok = 0
        component_ok = {"signal": 0, "level": 0, "owner": 0}
        abstentions = pending = completed = 0
        details = []
        for case in cohort["cases"]:
            row = review[case["id"]]
            if row["status"] != "complete":
                abstentions += row["status"] == "abstain"
                pending += row["status"] == "pending"
                continue
            completed += 1
            name = case["repository"]
            if name not in reports:
                repo = cohort["repositories"][name]
                reports[name] = scan(
                    manifest.parent / repo["path"], Config(**repo["config"])
                )
                if reports[name].summary()["status"] != "complete":
                    raise ValueError("Incomplete source scan cannot be scored: " + name)
            report = reports[name]
            names = {s.id: s.qualified_name for s in report.symbols}
            relevant = [f for f in report.findings if names[f.symbol] == case["symbol"]]
            actual = {
                (f.rule_id, case["symbol"], f.evidence[0].location.line)
                for f in relevant
            }
            expected = set(map(tuple, row["findings"]))
            for rule in {r[0] for r in actual | expected}:
                a, e = (
                    {r for r in actual if r[0] == rule},
                    {r for r in expected if r[0] == rule},
                )
                for group in (
                    "all",
                    "rule/" + rule,
                    "workflow/" + case["workflow_type"],
                ):
                    counts = groups.setdefault(group, {"tp": 0, "fp": 0, "fn": 0})
                    counts["tp"] += len(a & e)
                    counts["fp"] += len(a - e)
                    counts["fn"] += len(e - a)
            decisions = []
            for label in row["judgments"]:
                records = [
                    r
                    for r in report.operation_outcomes
                    if names[r["operation"]] == label["symbol"]
                    and r["location"]["line"] == label["line"]
                ]
                predicted_outcomes = {r["outcome"] for r in records} or {"unknown"}
                owners = {r["owner"] for r in records} or {None}
                choices = {
                    (a.kind, a.level, owner)
                    for f in relevant
                    if f.evidence[0].location.line == label["line"]
                    for a in f.recommendations
                    for owner in owners
                }
                if not choices:
                    choices = {
                        (r["decision"]["kind"], r["decision"]["level"], r["owner"])
                        for r in records
                        if r["decision"]["kind"] != "conditional"
                    }
                accepted = {
                    (c["kind"], c["level"], c["owner"]) for c in label["alternatives"]
                }
                same_outcome = predicted_outcomes <= set(label["outcomes"])
                same_choice = bool(choices) and choices <= accepted
                outcome_ok += same_outcome
                choice_ok += same_choice
                matched += bool(choices)
                matched_choice_ok += same_choice
                for index, component in enumerate(component_ok):
                    component_ok[component] += bool(choices) and {
                        c[index] for c in choices
                    } <= {c[index] for c in accepted}
                judged += 1
                decisions.append(
                    {
                        "line": label["line"],
                        "outcome_agrees": same_outcome,
                        "signal_level_owner_agree": same_choice,
                        "predicted_outcomes": sorted(predicted_outcomes),
                        "predicted_choices": sorted(choices, key=str),
                        "acceptable": label["alternatives"],
                    }
                )
            details.append(
                {
                    "case": case["id"],
                    "missed": sorted(expected - actual),
                    "unexpected": sorted(actual - expected),
                    "judgments": decisions,
                }
            )
        for counts in groups.values():
            tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
            counts.update(
                precision=tp / (tp + fp) if tp + fp else None,
                recall=tp / (tp + fn) if tp + fn else None,
                precision_interval=interval(tp, tp + fp),
                recall_interval=interval(tp, tp + fn),
            )
        results.append(
            {
                "reviewer": document["reviewer"],
                "role": document["role"],
                "completed": completed,
                "abstained": abstentions,
                "pending": pending,
                "finding_metrics": groups,
                "judgments": judged,
                "matched_judgments": matched,
                "unmatched_judgments": judged - matched,
                "matched_choice_agreement": matched_choice_ok / matched
                if matched
                else None,
                "matched_choice_interval": interval(matched_choice_ok, matched),
                "outcome_agreement": outcome_ok / judged if judged else None,
                "signal_level_owner_agreement": choice_ok / judged if judged else None,
                "outcome_interval": interval(outcome_ok, judged),
                "choice_interval": interval(choice_ok, judged),
                "component_agreement": {
                    key: {
                        "agreed": value,
                        "denominator": judged,
                        "rate": value / judged if judged else None,
                        "interval": interval(value, judged),
                        "matched_denominator": matched,
                        "matched_rate": value / matched if matched else None,
                        "matched_interval": interval(value, matched),
                    }
                    for key, value in component_ok.items()
                },
                "cases": details,
            }
        )

    def canonical_labels(row):
        judgments = []
        for judgment in row["judgments"]:
            judgments.append(
                dict(
                    judgment,
                    outcomes=sorted(judgment["outcomes"]),
                    alternatives=sorted(judgment["alternatives"], key=digest),
                )
            )
        return digest([sorted(row["findings"]), sorted(judgments, key=digest)])

    disagreements = []
    overlap = set()
    pair_overlaps = []
    independent = [r for d, r in zip(packets, reviews) if d["role"] == "independent"]
    for i, left in enumerate(independent):
        for j, right in enumerate(independent[i + 1 :], i + 1):
            shared = []
            for identity in left:
                a, b = left[identity], right[identity]
                if a["status"] == b["status"] == "complete":
                    overlap.add(identity)
                    shared.append(identity)
                    if canonical_labels(a) != canonical_labels(b):
                        disagreements.append(
                            {
                                "case": identity,
                                "review_pair": [i, j],
                                "left": {k: a[k] for k in ("findings", "judgments")},
                                "right": {k: b[k] for k in ("findings", "judgments")},
                            }
                        )
            pair_overlaps.append({"review_pair": [i, j], "cases": sorted(shared)})
    adjudicated = {
        identity
        for document, review in zip(packets, reviews)
        if document["role"] == "adjudication"
        for identity, row in review.items()
        if row["status"] == "complete"
    }
    unresolved = [d for d in disagreements if d["case"] not in adjudicated]
    independent_complete = (
        len(independent) >= 2
        and any(len(pair["cases"]) >= 10 for pair in pair_overlaps)
        and all(
            any(r[c["id"]]["status"] == "complete" for r in independent)
            for c in cohort["cases"]
        )
    )
    return {
        "schema_version": "flowsignal-workflow-accuracy-1",
        "cohort_signature": digest(cohort),
        "results": results,
        "shared_independent_scenarios": len(overlap),
        "review_pair_overlap": pair_overlaps,
        "independent_review_complete": independent_complete,
        "disagreements": disagreements,
        "adjudicated_scenarios": sorted(adjudicated),
        "unadjudicated_disagreements": len(unresolved),
        "adjudication_complete": independent_complete and not unresolved,
        "note": "Declared human labels are not authenticated. Wilson intervals describe this selected sample only; scenarios may be correlated. No label means unscored, not success. Adjudication is reported separately.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("benchmarks/workflow-cohort.json")
    )
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--reviews", type=Path, nargs="*")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.prepare, args.manifest)
    if args.packet:
        write(args.packet, packet(args.manifest))
    if args.reviews is not None:
        result = score_reviews(args.manifest, args.reviews)
        if args.output:
            write(args.output, result)
        print(json.dumps(result, indent=2))
    if not args.prepare and not args.packet and args.reviews is None:
        cohort, _ = load_cohort(args.manifest)
        print(
            json.dumps(
                {
                    "cases": len(cohort["cases"]),
                    "repositories": len(cohort["repositories"]),
                    "labels": "pending",
                    "signature": digest(cohort),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
