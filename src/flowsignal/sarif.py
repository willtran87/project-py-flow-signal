"""SARIF 2.1.0 export; review priority is separate from instrumentation level."""

from __future__ import annotations

from dataclasses import asdict
from urllib.parse import quote

from .rules import RULES


def location(where, message=None):
    # AST columns are UTF-8 byte offsets, not SARIF character columns. Export
    # exact lines rather than silently misrepresenting non-ASCII columns.
    item = {
        "physicalLocation": {
            "artifactLocation": {"uri": quote(where.file.replace("\\", "/"), safe="/")},
            "region": {"startLine": where.line},
        }
    }
    if message:
        item["message"] = {"text": message}
    return item


def export(report):
    rules = [
        {
            "id": rule,
            "shortDescription": {"text": title},
            "fullDescription": {
                "text": title
                + ". Review source evidence and conditional advice; this is not proof of operational failure."
            },
        }
        for rule, title in RULES.items()
    ]
    indices = {rule["id"]: i for i, rule in enumerate(rules)}
    symbols = {s.id: s for s in report.symbols}
    results = []
    for finding in report.findings:
        advice = "\n".join(
            f"{r.kind}{' ' + r.level if r.level else ''}: {r.action} When: {r.condition}"
            for r in finding.recommendations
        )
        item = {
            "ruleId": finding.rule_id,
            "ruleIndex": indices[finding.rule_id],
            "level": {"high": "error", "medium": "warning", "low": "note"}[
                finding.priority
            ],
            "message": {"text": finding.title + "\n" + advice},
            "locations": [location(finding.evidence[0].location)],
            "partialFingerprints": {"flowsignalStructural/v1": finding.fingerprint},
            "properties": {
                "reviewPriority": finding.priority,
                "confidence": finding.confidence,
                "reviewStatus": finding.review_status,
                "reviewOwner": finding.review_owner,
                "reviewExpires": finding.review_expires,
                "reviewExpired": finding.review_expired,
                "assumptions": finding.assumptions,
                "recommendations": [asdict(r) for r in finding.recommendations],
            },
            "relatedLocations": [
                dict(
                    location(e.location, e.fact + ("\n" + e.code if e.code else "")),
                    id=i + 1,
                )
                for i, e in enumerate(finding.evidence)
            ],
        }
        if finding.baseline_status in {"new", "unchanged"}:
            item["baselineState"] = finding.baseline_status
        if finding.review_status == "dismissed":
            item["suppressions"] = [
                {
                    "kind": "external",
                    "status": "accepted",
                    "justification": finding.review_reason,
                }
            ]
        flows = []
        for path in finding.paths:
            steps = [
                {"location": location(symbols[s].location, s)}
                for s in path
                if s in symbols
            ]
            if steps:
                flows.append({"threadFlows": [{"locations": steps}]})
        if flows:
            item["codeFlows"] = flows
        results.append(item)
    complete = report.summary()["status"] == "complete"
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "FlowSignal",
                        "version": report.scanner_version,
                        "rules": rules,
                    }
                },
                "results": results,
                "invocations": [
                    {
                        "executionSuccessful": complete,
                        "toolExecutionNotifications": [
                            {
                                "descriptor": {"id": d.code},
                                "level": "warning",
                                "message": {"text": d.message},
                            }
                            for d in report.diagnostics
                        ],
                    }
                ],
                "properties": {
                    "scanStatus": report.summary()["status"],
                    "summary": report.summary(),
                    "limitations": report.limitations,
                    "baseline": report.baseline,
                    "runtimeComparison": report.runtime,
                    "reviewHistory": report.review_history,
                    "reviewQueue": report.review_queue,
                    "operationOutcomes": report.operation_outcomes,
                    "contextualCalls": report.contextual_calls,
                    "taskOwnership": report.task_ownership,
                    "retryScopes": report.retry_scopes,
                    "recommendationUncertainty": report.recommendation_uncertainty,
                    "handlerPaths": [
                        {
                            "symbol": h.symbol,
                            "location": asdict(h.location),
                            "paths": h.reporting_paths,
                            "truncated": h.paths_truncated,
                        }
                        for h in report.handlers
                    ],
                    "severityMeaning": "SARIF level maps review priority; recommended logging severity appears only in conditional recommendations.",
                },
            }
        ],
    }
