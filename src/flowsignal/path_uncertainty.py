"""Connect uncertainty to bounded displayed caller paths, without inventing order."""

from collections import defaultdict
from dataclasses import asdict


def attach(report):
    gaps, outcomes = defaultdict(list), defaultdict(list)
    for gap in report.resolution_gaps:
        gaps[gap.symbol].append(gap)
    for outcome in report.operation_outcomes:
        outcomes[outcome["operation"]].append(outcome)
    records = []
    remaining = report.settings.get("max_resolution_steps", 100_000)
    for finding in report.findings:
        issues = []
        truncated = False
        for path in finding.paths or [[finding.symbol]]:
            for symbol in path:
                if remaining <= 0 or len(issues) >= 32:
                    truncated = True
                    break
                remaining -= 1
                if gaps[symbol]:
                    gap = gaps[symbol][0]
                    issues.append(
                        {
                            "category": "unsupported_semantics",
                            "path": path,
                            "symbol": symbol,
                            "location": asdict(gap.location),
                            "call_id": gap.call_id,
                            "reason": gap.reason,
                            "message": gap.explanation,
                            "action": gap.action,
                            "scope": "First function with a known gap on this displayed caller path; execution order inside that function is not established.",
                        }
                    )
                    break
        for outcome in outcomes[finding.symbol][:16]:
            if (
                outcome["outcome"] == "unknown"
                or outcome["basis"] == "user_declaration"
            ):
                issues.append(
                    {
                        "category": "user_declaration"
                        if outcome["contract"]
                        else "unknown_outcome",
                        "symbol": finding.symbol,
                        "location": outcome["location"],
                        "path": [finding.symbol],
                        "reason": outcome["outcome"],
                        "message": "Business outcome is a declaration."
                        if outcome["contract"]
                        else "Code exits do not establish business success or degradation.",
                        "action": "Verify the scoped operation outcome/owner contract against the required business behavior.",
                    }
                )
        for diagnostic in report.diagnostics:
            if "limit" in diagnostic.code and (
                not diagnostic.file
                or diagnostic.file == finding.evidence[0].location.file
            ):
                issues.append(
                    {
                        "category": "analysis_budget",
                        "symbol": finding.symbol,
                        "location": asdict(finding.evidence[0].location),
                        "path": [finding.symbol],
                        "reason": diagnostic.code,
                        "message": diagnostic.message,
                        "action": "Narrow the scan or increase the relevant explicit budget, then rescan before interpreting absence.",
                    }
                )
                break
        if not report.runtime:
            issues.append(
                {
                    "category": "missing_runtime_observation",
                    "symbol": finding.symbol,
                    "location": asdict(finding.evidence[0].location),
                    "path": [finding.symbol],
                    "reason": "not_collected",
                    "message": "No runtime observations were imported; static findings remain independently valid review candidates.",
                    "action": "Optionally collect explicitly selected tests to corroborate specific call pairs; absence will not prove unreachability.",
                }
            )
        records.append(
            {
                "finding_id": finding.id,
                "issues": issues[:32],
                "truncated": truncated or len(issues) > 32,
            }
        )
    report.recommendation_uncertainty = records
