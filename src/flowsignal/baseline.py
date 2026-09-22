"""Portable structural finding baselines and explicit review decisions."""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
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


def utc_now():
    return datetime.now(timezone.utc)


def expiry(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Review expiry must be YYYY-MM-DD (expires at 00:00 UTC)")
    return datetime.combine(
        date.fromisoformat(value), datetime.min.time(), timezone.utc
    )


def history_event(fingerprint, action, owner, reason, expires, now):
    return {
        "fingerprint": fingerprint,
        "action": action,
        "owner": owner,
        "reason": reason,
        "expires": expires,
        "at": now.isoformat(),
    }


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
    history = document.get("review_history", [])
    if not isinstance(history, list) or len(history) > 100_000:
        raise ValueError("Invalid review history")
    for event in history:
        if (
            not isinstance(event, dict)
            or event.get("action") not in {"dismiss", "restore", "expired"}
            or not isinstance(event.get("fingerprint"), str)
        ):
            raise ValueError("Malformed review history event")
        if not isinstance(event.get("at"), str) or len(event["at"]) > 64:
            raise ValueError("Malformed review history timestamp")
        timestamp = datetime.fromisoformat(event["at"])
        if timestamp.tzinfo is None or not re.fullmatch(
            "[0-9a-f]{20}", event["fingerprint"]
        ):
            raise ValueError(
                "History requires a fingerprint and timezone-aware timestamp"
            )
        for key, maximum in (("owner", 200), ("reason", 2000)):
            value = event.get(key)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > maximum
            ):
                raise ValueError("Invalid history owner or reason")
        if event.get("expires") is not None:
            expiry(event["expires"])
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
        owner = entry.get("review_owner")
        if owner is not None and (
            not isinstance(owner, str) or not owner.strip() or len(owner) > 200
        ):
            raise ValueError(
                "Review owner must be a nonempty string of at most 200 characters"
            )
        if entry.get("review_expires") is not None:
            expiry(entry["review_expires"])
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
        "review_history": report.review_history,
        "findings": [
            {
                "fingerprint": finding.fingerprint,
                "rule_id": finding.rule_id,
                "symbol": finding.symbol,
                "priority": finding.priority,
                "location": asdict(finding.evidence[0].location),
                "review_status": finding.review_status,
                "review_reason": finding.review_reason,
                "review_owner": finding.review_owner,
                "review_expires": finding.review_expires,
            }
            for finding in report.findings
        ],
    }


def compare(report: Report, baseline: dict, *, now=None) -> None:
    now = now or utc_now()
    report.review_history = [
        dict(event) for event in baseline.get("review_history", [])
    ]
    history_fields = ("fingerprint", "action", "owner", "reason", "expires", "at")
    history_keys = {
        tuple(e.get(k) for k in history_fields) for e in report.review_history
    }
    if scope_for(report) != baseline["scope"]:
        raise ValueError(
            "Baseline scan scope or recognition settings differ; compare with the same settings or create a new baseline"
        )
    prior = defaultdict(deque)
    for entry in baseline["findings"]:
        prior[entry["fingerprint"]].append(entry)
    counts = {"new": 0, "unchanged": 0, "resolved": 0, "unverified": 0, "dismissed": 0}
    for finding in report.findings:
        finding.review_expired = False
        matches = prior[finding.fingerprint]
        if matches:
            old = matches.popleft()
            finding.baseline_status = "unchanged"
            finding.review_status = old["review_status"]
            finding.review_reason = old.get("review_reason")
            finding.review_owner = old.get("review_owner")
            finding.review_expires = old.get("review_expires")
            if (
                finding.review_status == "dismissed"
                and finding.review_expires
                and expiry(finding.review_expires) <= now
            ):
                finding.review_status = "active"
                finding.review_expired = True
                event = history_event(
                    finding.fingerprint,
                    "expired",
                    finding.review_owner,
                    finding.review_reason,
                    finding.review_expires,
                    expiry(finding.review_expires),
                )
                event_key = tuple(event[k] for k in history_fields)
                if event_key not in history_keys:
                    if len(report.review_history) >= 100_000:
                        raise ValueError(
                            "Review history limit reached; archive the baseline before recording expirations"
                        )
                    report.review_history.append(event)
                    history_keys.add(event_key)
        else:
            finding.baseline_status = "new"
        counts[finding.baseline_status] += 1
        counts["dismissed"] += finding.review_status == "dismissed"
    absent = [entry for entries in prior.values() for entry in entries]
    complete = report.summary()["status"] == "complete"
    key = "resolved" if complete else "unverified"
    counts[key] = len(absent)
    dismissed_symbols = {
        (old["rule_id"], old["symbol"])
        for old in absent
        if old["review_status"] == "dismissed"
    }
    report.baseline = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "counts": counts,
        "resolved": absent if complete else [],
        "unverified": [] if complete else absent,
        "expired": sorted({f.fingerprint for f in report.findings if f.review_expired}),
        "renewal_required": sorted(
            {
                f.fingerprint
                for f in report.findings
                if f.baseline_status == "new"
                and (f.rule_id, f.symbol) in dismissed_symbols
            }
        ),
        "note": "Structural matches tolerate line movement, not symbol/file renames. Absent findings in incomplete scans are unverified.",
    }


def review(
    document: dict,
    fingerprint: str,
    action: str,
    reason: str | None,
    *,
    owner: str | None = None,
    expires: str | None = None,
    now=None,
) -> None:
    now = now or utc_now()
    if action not in {"dismiss", "restore"}:
        raise ValueError("Review action must be dismiss or restore")
    if len(document.get("review_history", [])) >= 100_000:
        raise ValueError(
            "Review history limit reached; archive the baseline before further review"
        )
    if reason is not None and (
        not isinstance(reason, str) or not reason.strip() or len(reason) > 2000
    ):
        raise ValueError("Review reason must be nonempty and at most 2000 characters")
    owner = owner.strip() if isinstance(owner, str) else owner
    if owner is not None and (
        not isinstance(owner, str) or not owner or len(owner) > 200
    ):
        raise ValueError(
            "Review owner must be a nonempty string of at most 200 characters"
        )
    if action == "restore" and expires:
        raise ValueError("Restoring a finding cannot set an expiry")
    if action == "dismiss":
        expires = expires or (now + timedelta(days=30)).date().isoformat()
        if expiry(expires) <= now:
            raise ValueError("Dismissal expiry must be in the future")
    if action == "dismiss" and (
        not isinstance(reason, str) or not reason.strip() or len(reason) > 2000
    ):
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
        entry["review_owner"] = owner or "unspecified"
        entry["review_expires"] = expires if action == "dismiss" else None
    document.setdefault("review_history", []).append(
        history_event(
            fingerprint,
            action,
            owner or "unspecified",
            reason.strip() if reason else None,
            expires,
            now,
        )
    )
