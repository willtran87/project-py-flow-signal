"""Portable structural finding baselines and explicit review decisions."""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path

from .model import Report, stable_id
from .source_io import read_snapshot

SCHEMA = "flowsignal-baseline-1"
FINGERPRINT_VERSION = "structural-1-rules-1"
SCOPE_KEYS = (
    "exclude",
    "entrypoints",
    "source_roots",
    "logger_names",
    "boundaries",
    "loggers",
    "reporters",
    "lifecycle_info",
    "min_confidence",
)


def fingerprint_findings(analysis, findings):
    anchors = {}
    for unit in analysis.units:
        for node in ast.walk(unit.tree):
            if isinstance(node, (ast.stmt, ast.Call, ast.ExceptHandler, ast.Attribute)):
                key = (unit.path, node.lineno, node.col_offset)
                # Calls are more specific than their enclosing expression statement.
                if key not in anchors or isinstance(
                    node, (ast.Call, ast.ExceptHandler)
                ):
                    anchors[key] = node
    for finding in findings:
        location = finding.evidence[0].location
        node = anchors.get((location.file, location.line, location.column))
        shape = (
            ast.dump(node, include_attributes=False)
            if node
            else finding.evidence[0].fact
        )
        advice = [
            (r.kind, r.level, r.condition, r.action) for r in finding.recommendations
        ]
        finding.fingerprint = stable_id(
            FINGERPRINT_VERSION,
            finding.rule_id,
            finding.symbol,
            shape,
            finding.priority,
            finding.confidence,
            json.dumps(advice, sort_keys=True),
        )


def scope_for(report: Report):
    return {
        key: report.settings.get(key, "low" if key == "min_confidence" else None)
        for key in SCOPE_KEYS
    }


def read_baseline(path: Path) -> dict:
    try:
        document = json.loads(read_snapshot(path, 20_000_000).decode("utf-8-sig"))
    except (UnicodeError, RecursionError) as error:
        raise ValueError("Baseline is not a supported UTF-8 JSON document") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA
        or document.get("fingerprint_version") != FINGERPRINT_VERSION
    ):
        raise ValueError(
            "Unsupported baseline schema or fingerprint version; create a new baseline"
        )
    if not isinstance(document.get("scope"), dict) or set(document["scope"]) != set(
        SCOPE_KEYS
    ):
        raise ValueError("Baseline scope is missing or malformed")
    entries = document.get("findings")
    if not isinstance(entries, list):
        raise ValueError("Baseline findings must be an array")
    for entry in entries:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(k), str) or not entry[k]
            for k in ("fingerprint", "rule_id", "symbol", "priority")
        ):
            raise ValueError("Malformed baseline finding")
        if not re.fullmatch("[0-9a-f]{20}", entry["fingerprint"]):
            raise ValueError("Invalid baseline fingerprint")
        if entry.get("review_status") not in {"active", "dismissed"}:
            raise ValueError("Baseline review_status must be active or dismissed")
        reason = entry.get("review_reason")
        if reason is not None and (
            not isinstance(reason, str) or not reason.strip() or len(reason) > 2000
        ):
            raise ValueError(
                "Review reasons must be nonempty strings of at most 2000 characters"
            )
        if entry["review_status"] == "dismissed" and reason is None:
            raise ValueError("Dismissed findings require a review reason")
        location = entry.get("location")
        if (
            not isinstance(location, dict)
            or not isinstance(location.get("file"), str)
            or type(location.get("line")) is not int
            or location["line"] < 1
        ):
            raise ValueError("Baseline finding requires a source location")
    return document


def make_baseline(report: Report) -> dict:
    if report.summary()["status"] != "complete":
        raise ValueError("Cannot save a baseline from an incomplete scan")
    return {
        "schema_version": SCHEMA,
        "fingerprint_version": FINGERPRINT_VERSION,
        "scope": scope_for(report),
        "findings": [
            {
                "fingerprint": finding.fingerprint,
                "rule_id": finding.rule_id,
                "symbol": finding.symbol,
                "priority": finding.priority,
                "location": asdict(finding.evidence[0].location),
                "review_status": finding.review_status,
                "review_reason": finding.review_reason,
            }
            for finding in report.findings
        ],
    }


def compare(report: Report, baseline: dict) -> None:
    if scope_for(report) != baseline["scope"]:
        raise ValueError(
            "Baseline scan scope or recognition settings differ; compare with the same settings or create a new baseline"
        )
    prior = defaultdict(deque)
    for entry in baseline["findings"]:
        prior[entry["fingerprint"]].append(entry)
    counts = {"new": 0, "unchanged": 0, "resolved": 0, "unverified": 0, "dismissed": 0}
    for finding in report.findings:
        matches = prior[finding.fingerprint]
        if matches:
            old = matches.popleft()
            finding.baseline_status = "unchanged"
            finding.review_status = old["review_status"]
            finding.review_reason = old.get("review_reason")
        else:
            finding.baseline_status = "new"
        counts[finding.baseline_status] += 1
        counts["dismissed"] += finding.review_status == "dismissed"
    absent = [entry for entries in prior.values() for entry in entries]
    complete = report.summary()["status"] == "complete"
    key = "resolved" if complete else "unverified"
    counts[key] = len(absent)
    report.baseline = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "counts": counts,
        "resolved": absent if complete else [],
        "unverified": [] if complete else absent,
        "note": "Structural matches tolerate line movement, not symbol/file renames. Absent findings in incomplete scans are unverified.",
    }


def review(document: dict, fingerprint: str, action: str, reason: str | None) -> None:
    if action == "dismiss" and (not reason or not reason.strip() or len(reason) > 2000):
        raise ValueError(
            "Dismissal requires a nonempty reason of at most 2000 characters"
        )
    matches = [
        entry for entry in document["findings"] if entry["fingerprint"] == fingerprint
    ]
    if not matches:
        raise ValueError("Fingerprint was not found in the baseline")
    for entry in matches:
        entry["review_status"] = "dismissed" if action == "dismiss" else "active"
        entry["review_reason"] = reason.strip() if action == "dismiss" else None
