"""Operation semantics are declared separately from syntactic control-flow facts."""

from __future__ import annotations

import ast
import fnmatch
from collections import defaultdict
from dataclasses import asdict
from typing import TYPE_CHECKING

from .model import Diagnostic, Recommendation, stable_id

if TYPE_CHECKING:
    from .scanner import Analysis


def outcome_records(analysis: Analysis):
    from .scanner import outcomes

    records = []
    by_symbol = defaultdict(list)
    for handler in analysis.handlers.values():
        by_symbol[handler.symbol].append(handler)
    for definition in analysis.definitions:
        if isinstance(definition.node, ast.Module):
            continue
        symbol = definition.symbol
        contracts = [
            c
            for c in analysis.config.operations
            if fnmatch.fnmatchcase(symbol.qualified_name, c["pattern"])
            or fnmatch.fnmatchcase(symbol.id, c["pattern"])
        ]
        handlers = by_symbol[symbol.id]
        scopes = [(h, p) for h in handlers for p in h.reporting_paths]
        if contracts:
            scopes.extend(
                (
                    None,
                    {
                        "exit": e,
                        "branches": [],
                        "reported": False,
                        "signals": [],
                        "uncertain": False,
                    },
                )
                for e in sorted(outcomes(definition.node.body))
            )
        for handler, path in scopes:
            if not analysis.graph_step():
                break
            matches = [
                c
                for c in contracts
                if c.get("scope", "handler") == ("handler" if handler else "operation")
                and c["exit"] == path["exit"]
            ]
            location = handler.location if handler else symbol.location
            conflict = len(matches) > 1
            if conflict:
                analysis.diagnostics.append(
                    Diagnostic(
                        "operation_contract_conflict",
                        "Overlapping operation contracts match this exit; outcome remains unknown.",
                        location.file,
                        location.line,
                    )
                )
            contract = matches[0] if len(matches) == 1 else None
            uncertain = path["uncertain"] or bool(handler and handler.paths_truncated)
            value = contract["outcome"] if contract and not uncertain else "unknown"
            record = {
                "id": stable_id(
                    symbol.id, handler.id if handler else "operation", path, value
                ),
                "operation": symbol.id,
                "handler": handler.id if handler else None,
                "location": asdict(location),
                "exit": path["exit"],
                "path": path["branches"],
                "outcome": value,
                "basis": "user_declaration" if contract else "syntactic_only",
                "owner": contract["owner"] if contract else None,
                "importance": contract.get("importance", "routine")
                if contract
                else "unknown",
                "reported": path["reported"],
                "signals": path["signals"],
                "signal_levels": sorted(
                    {
                        log.level
                        for log in (handler.logs if handler else [])
                        if any(
                            s["line"] == log.location.line
                            and s["column"] == log.location.column
                            for s in path["signals"]
                        )
                    }
                ),
                "configured_reporter": bool(
                    handler
                    and any(
                        any(
                            s["line"] == r.location.line
                            and s["column"] == r.location.column
                            for s in path["signals"]
                        )
                        for r in handler.reporting_signals
                    )
                ),
                "uncertain": uncertain or conflict,
                "contract": contract,
                "assumptions": [
                    "Business meaning and ownership are user declarations, not inferred from return values or dependency type.",
                    "Reporting presence does not verify delivery.",
                ],
            }
            record["decision"] = decide(record)
            records.append(record)
    return records


def decide(record):
    outcome, contract = record["outcome"], record["contract"] or {}
    level = {"degraded_recovery": "WARNING", "failed_operation": "ERROR"}.get(outcome)
    if outcome == "successful_operation" and record["importance"] in {
        "important",
        "critical",
    }:
        level = "INFO"
    if outcome == "unknown":
        return {
            "kind": "conditional",
            "level": None,
            "reason": "Outcome is unknown; retain conditional recommendations and review business context.",
        }
    if record["reported"]:
        rank = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
        if (
            level
            and not record["configured_reporter"]
            and max((rank[v] for v in record["signal_levels"]), default=-1)
            < rank[level]
        ):
            return {
                "kind": "enrich_existing_log",
                "level": level,
                "reason": f"The declared {outcome} needs {level} at owner {record['owner']}; adjust the existing event instead of adding a duplicate.",
            }
        return {
            "kind": "no_additional_log",
            "level": None,
            "reason": "This modeled exit already has recognized reporting; enrich the existing event if needed.",
        }
    selected = contract.get("signal", "auto")
    kind = (
        selected
        if selected != "auto"
        else "log"
        if level
        else "metric"
        if outcome == "successful_recovery"
        else "no_additional_log"
    )
    if kind == "log" and level is None:
        level = "DEBUG"
    return {
        "kind": kind,
        "level": level if kind == "log" else None,
        "reason": f"Declared {outcome.replace('_', ' ')} at owner {record['owner']}; {kind.replace('_', ' ')} follows the operation contract.",
    }


def apply_decisions(findings, records):
    for finding in findings:
        # Only general outcome advice is replaced, never suppression/traceback safety advice.
        if finding.rule_id not in {"FS001", "FS003", "FS008"}:
            continue
        candidates = [
            r
            for r in records
            if r["operation"] == finding.symbol
            and r["location"] == asdict(finding.evidence[0].location)
            and r["decision"]["kind"] != "conditional"
        ]
        if not candidates:
            continue
        all_paths = [
            r
            for r in records
            if r["operation"] == finding.symbol
            and r["location"] == asdict(finding.evidence[0].location)
        ]
        recommendations = (
            [] if len(candidates) == len(all_paths) else list(finding.recommendations)
        )
        seen = set()
        for record in candidates:
            decision = record["decision"]
            key = (
                record["outcome"],
                decision["kind"],
                decision["level"],
                record["owner"],
            )
            if key in seen:
                continue
            seen.add(key)
            recommendations.append(
                Recommendation(
                    decision["kind"],
                    decision["level"],
                    f"The declared {record['outcome']} contract applies to this {record['exit']} path at owner {record['owner']}.",
                    decision["reason"],
                    finding.evidence[0].location,
                    ["operation", "outcome", "correlation_id"],
                )
            )
        finding.recommendations = recommendations
        finding.assumptions.append(
            "Outcome-specific advice uses an explicit user declaration; unknown paths retain conditional alternatives."
        )


def enrich(analysis: Analysis, findings):
    from .call_context import analyze as callable_context
    from .ownership import analyze as ownership

    records = outcome_records(analysis)
    apply_decisions(findings, records)
    tasks, retries = ownership(analysis, findings, records)
    findings.sort(
        key=lambda f: (
            {"high": 0, "medium": 1, "low": 2}[f.priority],
            f.evidence[0].location.file,
            f.evidence[0].location.line,
            f.rule_id,
        )
    )
    return {
        "operation_outcomes": records,
        "contextual_calls": callable_context(analysis),
        "task_ownership": tasks,
        "retry_scopes": retries,
    }
