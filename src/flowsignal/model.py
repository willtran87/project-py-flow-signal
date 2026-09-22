"""Public report records. Log level, review priority, and confidence are separate."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Location:
    file: str
    line: int
    column: int = 0


@dataclass
class Evidence:
    location: Location
    code: str
    fact: str


@dataclass
class Recommendation:
    kind: str
    level: str | None
    condition: str
    action: str
    location: Location
    context_fields: list[str] = field(default_factory=list)


@dataclass
class Finding:
    id: str
    rule_id: str
    title: str
    priority: str
    confidence: str
    symbol: str
    evidence: list[Evidence]
    recommendations: list[Recommendation]
    assumptions: list[str] = field(default_factory=list)
    paths: list[list[str]] = field(default_factory=list)
    origin: str = "deterministic"
    fingerprint: str = ""
    baseline_status: str | None = None
    review_status: str = "active"
    review_reason: str | None = None
    review_owner: str | None = None
    review_expires: str | None = None
    review_expired: bool = False


@dataclass
class Diagnostic:
    code: str
    message: str
    file: str | None = None
    line: int | None = None


INCOMPLETE_CODES = frozenset(
    {
        "source_unreadable",
        "directory_unreadable",
        "file_limit",
        "file_size_limit",
        "analysis_depth",
        "no_sources",
        "entrypoint_not_found",
        "total_bytes_limit",
        "ast_nodes_limit",
        "ast_depth_limit",
        "graph_work_limit",
        "type_work_limit",
        "resolution_context_limit",
        "source_root_invalid",
        "duplicate_module",
        "discovery_limit",
    }
)


@dataclass
class Symbol:
    id: str
    qualified_name: str
    location: Location
    end_line: int
    kind: str
    entrypoint: bool = False
    entrypoint_basis: str | None = None


@dataclass
class CoverageEvidence:
    location: Location
    symbol: str
    reason: str
    message: str
    credited: bool = False
    owner: str | None = None
    call_id: str | None = None


@dataclass
class CoverageDecision:
    call_id: str
    symbol: str
    location: Location
    status: str
    evidence: list[CoverageEvidence]
    truncated: bool = False


@dataclass
class Call:
    id: str
    symbol: str
    location: Location
    expression: str
    resolved_name: str
    target: str | None = None
    resolution: str = "unresolved"
    boundary: str | None = None
    handler_groups: list[list[str]] = field(default_factory=list)
    enclosing_handler: str | None = None
    traced: bool = False
    discarded_task: bool = False
    execution: str = "synchronous_or_unknown"
    trace_failure_coverage: bool = False
    propagation_uncertain: bool = False
    blocked_handler_ids: list[str] = field(default_factory=list)
    trace_evidence: list[CoverageEvidence] = field(default_factory=list)


@dataclass
class Log:
    symbol: str
    location: Location
    level: str
    exception_context: bool
    handler: str | None
    conditional: bool = False
    execution: str = "immediate"


@dataclass
class ReportingSignal:
    symbol: str
    location: Location
    handler: str | None
    kind: str
    owner: str
    pattern: str
    basis: str
    conditional: bool = False
    execution: str = "immediate"


@dataclass
class Handler:
    id: str
    symbol: str
    location: Location
    types: list[str]
    outcomes: list[str]
    logs: list[Log] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    conditional: bool = False
    reporting_signals: list[ReportingSignal] = field(default_factory=list)
    path_reporting: bool | None = None
    reporting_paths: list[dict] = field(default_factory=list)
    paths_truncated: bool = False

    @property
    def catches_all(self) -> bool:
        return any(
            name
            in {
                "*",
                "Exception",
                "BaseException",
                "builtins.Exception",
                "builtins.BaseException",
            }
            for name in self.types
        )


LIMITATIONS = [
    "Static paths are possibilities, not proof of runtime execution or business impact.",
    "Dynamic dispatch, reflection, monkey-patching, star imports, callback invocation, and dependency injection may remain unresolved.",
    "Call relationships and lexical exception scopes are bounded evidence, not a complete path-sensitive control-flow graph.",
    "Exception types from callees are not inferred; typed handlers may cover only some failures.",
    "Logging and tracing wrappers need configuration when their behavior cannot be recognized.",
    "Runtime logger levels, filters, exporters, framework exception handlers, and log delivery are not verified.",
    "Business outcomes and expected versus degraded fallback behavior require project context; log levels are conditional recommendations.",
    "Generator execution timing, ExceptionGroup splitting, implicit failures, and context-manager suppression are not modeled fully.",
    "Configured outcome reporters are user-declared contracts; successful publication, consumption and delivery are not verified.",
    "Property edges infer supported read-only getters from receiver annotations and simple assignments; inherited descriptors, setters and dynamic attribute hooks remain limited.",
    "Receiver fields, constructor arguments and fixed-arity tuple returns use compatible indexed evidence. Single known inheritance chains and zero-argument super are supported conservatively; multiple inheritance, runtime mutation, callable returns and external factories remain limited. Annotations and unseen constructor sites are assumptions.",
    "Boundary coverage explains the same bounded decision as FS005. Recognized means static reporting evidence, not proven delivery or coverage of unresolved callers; explanations retain at most 64 evidence items.",
]


@dataclass
class ResolutionGap:
    call_id: str
    symbol: str
    location: Location
    expression: str
    reason: str
    explanation: str
    action: str
    entrypoints: list[str] = field(default_factory=list)
    context_truncated: bool = False


@dataclass
class Report:
    root: str
    files_scanned: int
    symbols: list[Symbol]
    calls: list[Call]
    logs: list[Log]
    handlers: list[Handler]
    findings: list[Finding]
    diagnostics: list[Diagnostic]
    settings: dict[str, Any]
    inventory: list[dict[str, Any]] = field(default_factory=list)
    analysis_stats: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "flowsignal-report-1"
    scanner_version: str = "0.1.0"
    limitations: list[str] = field(default_factory=lambda: list(LIMITATIONS))
    reporting_signals: list[ReportingSignal] = field(default_factory=list)
    baseline: dict[str, Any] | None = None
    coverage: list[CoverageDecision] = field(default_factory=list)
    resolution_gaps: list[ResolutionGap] = field(default_factory=list)
    review_history: list[dict[str, Any]] = field(default_factory=list)
    runtime: dict[str, Any] | None = None
    review_queue: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "files_scanned": self.files_scanned,
            "symbols": len(self.symbols),
            "calls": len(self.calls),
            "resolved_internal_calls": sum(
                call.target is not None for call in self.calls
            ),
            "unresolved_calls": sum(
                call.resolution in {"unresolved", "ambiguous"} for call in self.calls
            ),
            "ambiguous_calls": sum(
                call.resolution == "ambiguous" for call in self.calls
            ),
            "unresolved_reasons": dict(
                sorted(Counter(g.reason for g in self.resolution_gaps).items())
            ),
            "resolution_counts": dict(
                sorted(Counter(call.resolution for call in self.calls).items())
            ),
            "propagation_uncertain_calls": sum(
                call.propagation_uncertain for call in self.calls
            ),
            "diagnostic_counts": dict(
                sorted(Counter(item.code for item in self.diagnostics).items())
            ),
            "status": "incomplete"
            if any(item.code in INCOMPLETE_CODES for item in self.diagnostics)
            else "complete",
            "findings": len(self.findings),
            "diagnostics": len(self.diagnostics),
            "reporting_signals": len(self.reporting_signals),
            "boundary_coverage": dict(
                sorted(Counter(c.status for c in self.coverage).items())
            ),
            "dismissed_findings": sum(
                f.review_status == "dismissed" for f in self.findings
            ),
            "baseline": self.baseline["counts"] if self.baseline else None,
        }

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["summary"] = self.summary()
        return result


def stable_id(*parts: object) -> str:
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()[:20]
