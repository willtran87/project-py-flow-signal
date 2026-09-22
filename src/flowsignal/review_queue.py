"""Bounded workflow context for findings and unresolved-call review."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict


def attach(report):
    from .path_uncertainty import attach as uncertainty

    uncertainty(report)
    symbols = {s.id: s for s in report.symbols}
    incoming = defaultdict(set)
    for call in report.calls:
        if call.target:
            incoming[call.target].add(call.symbol)
    remaining = report.settings.get("max_resolution_steps", 100_000)
    contexts = {}

    def context(symbol):
        nonlocal remaining
        if symbol in contexts:
            return contexts[symbol]
        pending, seen, found = deque([(symbol, 0)]), set(), set()
        partial = False
        while pending:
            current, depth = pending.popleft()
            if current in seen:
                continue
            if remaining <= 0:
                partial = True
                break
            remaining -= 1
            seen.add(current)
            if current in symbols and symbols[current].entrypoint:
                found.add(current)
                if len(found) >= 64:
                    partial = bool(pending)
                    break
            elif depth >= report.settings.get("max_path_depth", 8):
                partial |= bool(incoming[current])
            else:
                for parent in sorted(incoming[current]):
                    if remaining <= 0:
                        partial = True
                        break
                    remaining -= 1
                    pending.append((parent, depth + 1))
        contexts[symbol] = sorted(found), partial
        return contexts[symbol]

    coverage = {
        (d.symbol, d.location.line, d.location.column): d for d in report.coverage
    }
    signal_owners = defaultdict(set)
    for decision in report.coverage:
        for evidence in decision.evidence:
            if evidence.credited and evidence.reason in {
                "failure_log",
                "configured_reporter",
                "trace_coverage",
            }:
                signal_owners[
                    (evidence.symbol, evidence.location.line, evidence.location.column)
                ].add(evidence.owner or evidence.symbol)
    rows = []
    for finding in report.findings:
        location = finding.evidence[0].location
        decision = coverage.get((finding.symbol, location.line, location.column))
        owners = (
            sorted(
                {
                    e.owner or e.symbol
                    for e in decision.evidence
                    if e.credited
                    and e.reason
                    in {"failure_log", "configured_reporter", "trace_coverage"}
                }
            )
            if decision
            else []
        )
        owners = sorted(
            set(owners)
            | signal_owners[(finding.symbol, location.line, location.column)]
        )
        entrypoints, partial = context(finding.symbol)
        states = ["finding", finding.review_status]
        if finding.baseline_status == "new":
            states.append("new")
        if finding.review_expired:
            states.append("expired")
        if decision and decision.status != "recognized":
            states.append("uncovered")
        rows.append(
            {
                "id": "finding:" + finding.id,
                "finding_id": finding.id,
                "kind": "finding",
                "title": finding.title,
                "rule_id": finding.rule_id,
                "priority": finding.priority,
                "symbol": finding.symbol,
                "location": asdict(location),
                "entrypoints": entrypoints,
                "owners": owners,
                "review_owner": finding.review_owner,
                "states": states,
                "context_truncated": partial,
                "node": "call:" + decision.call_id if decision else finding.symbol,
            }
        )
    for gap in report.resolution_gaps:
        entrypoints, partial = context(gap.symbol)
        rows.append(
            {
                "id": "gap:" + gap.call_id,
                "kind": "unresolved",
                "title": gap.explanation,
                "rule_id": gap.reason,
                "priority": "unrated",
                "symbol": gap.symbol,
                "location": asdict(gap.location),
                "entrypoints": entrypoints,
                "owners": [],
                "review_owner": None,
                "states": ["unresolved"],
                "context_truncated": partial,
                "node": gap.symbol,
            }
        )
    rows.sort(
        key=lambda r: (
            {"high": 0, "medium": 1, "low": 2, "unrated": 3}[r["priority"]],
            r["location"]["file"],
            r["location"]["line"],
            r["id"],
        )
    )
    report.review_queue = {
        "items": rows,
        "context_steps": report.settings.get("max_resolution_steps", 100_000)
        - remaining,
        "context_truncated": any(r["context_truncated"] for r in rows),
        "note": "Potential entrypoints follow known call edges. Empty or partial context does not prove a finding is unreachable. Reporting owners are static evidence; review owners are human declarations.",
    }
