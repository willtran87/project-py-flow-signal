"""Rules reason from scanner facts; no network or model calls occur here."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .model import (
    Call,
    CoverageDecision,
    CoverageEvidence,
    Diagnostic,
    Finding,
    Handler,
    Location,
    Recommendation,
    stable_id,
)

if TYPE_CHECKING:
    from .scanner import Analysis


RULES = {
    "FS001": "Possible unreported exception outcome",
    "FS002": "Finally block can suppress an active exception",
    "FS003": "Failure at an operation boundary is logged below ERROR",
    "FS004": "Rethrown failure may be logged twice",
    "FS005": "Failure-sensitive boundary lacks recognized failure instrumentation",
    "FS006": "Exception log lacks recognized traceback context",
    "FS007": "Background task handle is discarded",
    "FS008": "Configured operation has no INFO lifecycle signal",
    "FS009": "Retained task failure observation is not established",
    "FS010": "Retry attempt logs an error before the final outcome is known",
}


def failure_logs(handler: Handler):
    return [
        log
        for log in handler.logs
        if log.level in {"ERROR", "CRITICAL"}
        and (
            not log.conditional
            or handler.path_reporting
            and all(
                any(s["line"] == log.location.line for s in p["signals"])
                or any(
                    other.level in {"ERROR", "CRITICAL"}
                    and any(s["line"] == other.location.line for s in p["signals"])
                    for other in handler.logs
                )
                for p in handler.reporting_paths
            )
        )
    ]


def reports_outcome(handler: Handler) -> bool:
    if handler.path_reporting is not None:
        return handler.path_reporting
    return any(
        log.level in {"WARNING", "ERROR", "CRITICAL"} and not log.conditional
        for log in handler.logs
    ) or any(not signal.conditional for signal in handler.reporting_signals)


@dataclass
class CoverageResult:
    recognized: bool = False
    evidence: list[CoverageEvidence] = field(default_factory=list)
    truncated: bool = False

    def add(self, evidence):
        if len(self.evidence) < 64:
            self.evidence.append(evidence)
        else:
            self.truncated = True

    def extend(self, other):
        for evidence in other.evidence:
            self.add(evidence)
        self.truncated |= other.truncated


def evaluate(analysis: Analysis) -> list[Finding]:
    symbols = {
        definition.symbol.id: definition.symbol for definition in analysis.definitions
    }
    callers: dict[str, list[Call]] = defaultdict(list)
    for call in analysis.calls:
        if call.target:
            callers[call.target].append(call)
    findings: list[Finding] = []
    path_cache: dict[str, list[list[str]]] = {}
    coverage_cache: dict[str, CoverageResult] = {}

    def paths_to(symbol_id: str) -> list[list[str]]:
        if symbol_id in path_cache:
            return path_cache[symbol_id]
        queue = deque([[symbol_id]])
        result: list[list[str]] = []
        visited_states = 0
        while (
            queue and len(result) < analysis.config.max_paths and visited_states < 2_000
        ):
            reverse_path = queue.popleft()
            visited_states += 1
            current = reverse_path[-1]
            if not analysis.graph_step(len(callers[current]) + 1):
                break
            parents = sorted(
                {call.symbol for call in callers[current]} - set(reverse_path)
            )
            if (
                symbols[current].entrypoint
                or not parents
                or len(reverse_path) >= analysis.config.max_path_depth
            ):
                result.append(list(reversed(reverse_path)))
            else:
                # Bound queued states as well as popped states on dense graphs.
                room = max(0, 2_000 - visited_states - len(queue))
                queue.extend([*reverse_path, parent] for parent in parents[:room])
                if len(parents) > room and not any(
                    item.code == "path_frontier_limit" for item in analysis.diagnostics
                ):
                    analysis.diagnostics.append(
                        Diagnostic(
                            "path_frontier_limit",
                            "Caller-path examples were truncated at the bounded frontier; they are not exhaustive.",
                        )
                    )
        path_cache[symbol_id] = result
        return result

    def effective_handlers(call: Call) -> list[Handler]:
        result = []
        for group in call.handler_groups:
            for identity in group:
                if identity in call.blocked_handler_ids:
                    continue
                handler = analysis.handlers[identity]
                result.append(handler)
                if handler.catches_all:
                    # The nearest catch-all prevents assuming the outer handler runs.
                    return result
        return result

    def unreported_consumption(call: Call) -> bool:
        return any(
            set(handler.outcomes) != {"raise"} and not reports_outcome(handler)
            for handler in effective_handlers(call)
        )

    def explain_local(call: Call) -> tuple[CoverageResult, bool]:
        result = CoverageResult()

        def note(reason, message, credited=False):
            result.add(
                CoverageEvidence(
                    call.location,
                    call.symbol,
                    reason,
                    message,
                    credited,
                    call_id=call.id,
                )
            )

        if call.execution.startswith("deferred_"):
            note(
                "deferred_execution",
                "Creating deferred work does not execute its body under this handler or span.",
            )
            return result, True
        handlers = effective_handlers(call)
        broad = any(handler.catches_all for handler in handlers)
        for identity in call.blocked_handler_ids:
            handler = analysis.handlers[identity]
            result.add(
                CoverageEvidence(
                    handler.location,
                    handler.symbol,
                    "handler_blocked",
                    "An inner context manager may suppress the exception before this handler runs.",
                )
            )
        for handler in handlers:
            if handler.reporting_paths:
                result.add(
                    CoverageEvidence(
                        handler.location,
                        handler.symbol,
                        "reporting_paths",
                        f"{sum(p['reported'] for p in handler.reporting_paths)}/{len(handler.reporting_paths)} modeled handler exits report an outcome."
                        + (
                            " Path review truncated; coverage is not established."
                            if handler.paths_truncated
                            else " Implicit expression failures and delivery are not verified."
                        ),
                        bool(handler.path_reporting),
                    )
                )
            result.add(
                CoverageEvidence(
                    handler.location,
                    handler.symbol,
                    "catch_all" if handler.catches_all else "typed_handler",
                    "Catch-all candidate within the static exception model."
                    if handler.catches_all
                    else "Typed handler covers only some possible exceptions.",
                )
            )
            for log in handler.logs:
                credited = log.level in {"WARNING", "ERROR", "CRITICAL"} and (
                    not log.conditional or handler.path_reporting is True
                )
                result.add(
                    CoverageEvidence(
                        log.location,
                        log.symbol,
                        "failure_log"
                        if credited
                        else "conditional_log"
                        if log.conditional
                        else "log_below_warning",
                        f"{log.level} log participates in reporting across all modeled exits."
                        if credited and log.conditional
                        else f"{log.level} log is an unconditional outcome signal."
                        if credited
                        else f"{log.level} log is conditional or below WARNING; it cannot establish handler reporting.",
                        credited,
                    )
                )
            for signal in handler.reporting_signals:
                signal_credited = not signal.execution.startswith("deferred_") and (
                    not signal.conditional or handler.path_reporting is True
                )
                result.add(
                    CoverageEvidence(
                        signal.location,
                        signal.symbol,
                        "configured_reporter"
                        if signal_credited
                        else "conditional_reporter",
                        "Configured outcome contract is credited; delivery is not verified."
                        if signal_credited
                        else "Conditional reporter cannot establish reporting on every handler outcome.",
                        signal_credited,
                        signal.owner,
                    )
                )
            if not reports_outcome(handler):
                result.add(
                    CoverageEvidence(
                        handler.location,
                        handler.symbol,
                        "handler_unreported",
                        "No unconditional WARNING-or-higher log or configured outcome reporter is recognized.",
                    )
                )
        consumed = unreported_consumption(call)
        for item in call.trace_evidence:
            if item.credited and consumed:
                result.add(
                    CoverageEvidence(
                        item.location,
                        item.symbol,
                        "trace_consumed",
                        "An unreported handler may consume the exception before this span records it.",
                    )
                )
            else:
                result.add(item)
        if broad and all(reports_outcome(handler) for handler in handlers):
            result.recognized = True
            note(
                "handler_coverage",
                "All effective handlers report outcomes and a catch-all is present.",
                True,
            )
        elif call.trace_failure_coverage and not consumed:
            result.recognized = True
            note(
                "trace_coverage",
                "A recognized failure-recording span covers this call within the static model.",
                True,
            )
        elif consumed:
            note(
                "silent_consumption",
                "A handler may consume the failure without reporting it; more distant callers cannot be credited.",
            )
        elif call.propagation_uncertain:
            note(
                "propagation_unknown",
                "Exception propagation through an enclosing context manager is uncertain.",
            )
        else:
            note(
                "caller_review",
                "Local coverage is not established; inspect resolved caller routes.",
            )
        return result, bool(call.propagation_uncertain or consumed)

    def coverage_in_callers(
        symbol_id: str, seen: frozenset[str] = frozenset()
    ) -> CoverageResult:
        if symbol_id in coverage_cache:
            return coverage_cache[symbol_id]
        symbol = symbols[symbol_id]
        result = CoverageResult()

        def stop(reason, message):
            result.add(CoverageEvidence(symbol.location, symbol_id, reason, message))
            return result

        if symbol_id in seen:
            return stop(
                "caller_cycle",
                "A caller cycle prevents establishing a reporting owner on every route.",
            )
        if len(seen) >= analysis.config.max_path_depth:
            return stop(
                "caller_depth_limit",
                "Caller review reached the configured depth limit.",
            )
        if not analysis.graph_step():
            return stop(
                "graph_work_limit", "Caller review exhausted the graph work budget."
            )
        incoming = callers[symbol_id]
        if not incoming or symbol.entrypoint:
            return stop(
                "entrypoint" if symbol.entrypoint else "no_known_callers",
                "An entrypoint ends caller coverage review."
                if symbol.entrypoint
                else "No resolved caller establishes a reporting owner; external callers remain unknown.",
            )
        for call in incoming:
            if not analysis.graph_step():
                return stop(
                    "graph_work_limit", "Caller review exhausted the graph work budget."
                )
            local, blocked = explain_local(call)
            result.extend(local)
            if local.recognized:
                continue
            if blocked:
                return result
            parent = coverage_in_callers(call.symbol, seen | {symbol_id})
            result.extend(parent)
            if not parent.recognized:
                return result
        result.recognized = True
        result.add(
            CoverageEvidence(
                symbol.location,
                symbol_id,
                "all_known_callers",
                "All resolved caller routes inspected within the bounds establish static coverage; unresolved and external callers remain unknown.",
                True,
            )
        )
        # Negative results depend on the current route's cycle/depth bounds.
        coverage_cache[symbol_id] = result
        return result

    def emit(
        rule: str,
        symbol_id: str,
        location: Location,
        fact: str,
        recommendations: list[Recommendation],
        *,
        priority: str = "medium",
        confidence: str = "medium",
        assumptions: list[str] | None = None,
        additional=None,
    ) -> None:
        evidence = [analysis.evidence(location, fact)]
        if additional:
            evidence.extend(additional)
        findings.append(
            Finding(
                stable_id(rule, symbol_id, location.line, location.column),
                rule,
                RULES[rule],
                priority,
                confidence,
                symbol_id,
                evidence,
                recommendations,
                assumptions or [],
                paths_to(symbol_id),
            )
        )

    def log_advice(
        location: Location, level: str, condition: str, action: str
    ) -> Recommendation:
        return Recommendation(
            "log",
            level,
            condition,
            action,
            location,
            ["operation", "exception_type", "correlation_id"],
        )

    for handler in analysis.handlers.values():
        symbol = symbols[handler.symbol]
        exits = set(handler.outcomes) - {"raise"}
        if exits and not reports_outcome(handler):
            emit(
                "FS001",
                handler.symbol,
                handler.location,
                f"Handler catches {', '.join(handler.types)}; syntactic outcomes: {', '.join(handler.outcomes)}. No unconditional WARNING-or-higher log or configured outcome reporter is recognized in this handler.",
                [
                    log_advice(
                        handler.location,
                        "WARNING",
                        "The exception causes unexpected degradation and the operation can continue.",
                        "Record the degraded outcome at the handler or its designated reporting boundary.",
                    ),
                    log_advice(
                        handler.location,
                        "ERROR",
                        "Required processing cannot complete and no outer operation boundary records that failure.",
                        "Record one operation failure with the original exception context.",
                    ),
                ],
                priority="medium" if handler.catches_all else "low",
                confidence="medium" if handler.catches_all else "low",
                assumptions=[
                    "Returning, continuing, or falling through does not prove successful recovery.",
                    "An expected miss or routine control-flow exception may need DEBUG, a metric, or no additional log.",
                    "Diagnostic wrappers and outcome checks in callers may already provide coverage.",
                ],
            )
        if (
            symbol.entrypoint
            and set(handler.outcomes) == {"raise"}
            and handler.logs
            and not any(not signal.conditional for signal in handler.reporting_signals)
            and not any(log.level in {"ERROR", "CRITICAL"} for log in handler.logs)
        ):
            emit(
                "FS003",
                handler.symbol,
                handler.location,
                "This recognized entrypoint logs below ERROR in a handler whose explicit terminal outcomes all raise.",
                [
                    log_advice(
                        handler.location,
                        "ERROR",
                        "The exception represents an unsuccessful operation and the host framework does not already record it.",
                        "Report the failed operation once at its owning boundary.",
                    )
                ],
                assumptions=[
                    "Expected client errors, cancellations, and validation failures may not warrant ERROR.",
                    "The framework may already own exception reporting.",
                ],
            )
        if set(handler.outcomes) == {"raise"} and failure_logs(handler):
            for call in callers[handler.symbol]:
                if call.execution.startswith("deferred_"):
                    continue
                outer = next(
                    (
                        candidate
                        for candidate in effective_handlers(call)
                        if candidate.catches_all and failure_logs(candidate)
                    ),
                    None,
                )
                if outer:
                    emit(
                        "FS004",
                        handler.symbol,
                        handler.location,
                        "This handler logs at ERROR or higher and raises; a resolved caller also has a catch-all handler with an ERROR-or-higher log.",
                        [
                            Recommendation(
                                "no_additional_log",
                                None,
                                "Both handlers record the same propagated exception in one failed operation.",
                                "Choose one reporting owner; preserve exception propagation and use tracing or DEBUG for inner context.",
                                handler.location,
                            )
                        ],
                        additional=[
                            analysis.evidence(
                                outer.location,
                                f"Potential outer reporting owner: {outer.symbol}.",
                            )
                        ],
                        assumptions=[
                            "The two handlers may catch different exception types or execute under different runtime conditions."
                        ],
                    )
                    break
        for log in handler.logs:
            if (
                log.level in {"WARNING", "ERROR", "CRITICAL"}
                and not log.exception_context
            ):
                emit(
                    "FS006",
                    handler.symbol,
                    log.location,
                    "A recognized exception-handler log does not explicitly retain traceback context with logger.exception(), exc_info=True, or sys.exc_info().",
                    [
                        Recommendation(
                            "enrich_existing_log",
                            log.level,
                            "The traceback is needed to diagnose this failure and is not already attached by the logging wrapper.",
                            "Preserve exception context on the existing event; include safe operation and correlation fields.",
                            log.location,
                            ["exception_type", "operation", "correlation_id"],
                        )
                    ],
                    priority="low",
                    assumptions=[
                        "Some expected recoverable failures do not need a traceback.",
                        "Custom processors may already attach exception context.",
                    ],
                )

    for symbol_id, location, terminal in analysis.finalizers:
        emit(
            "FS002",
            symbol_id,
            location,
            f"A finally block has a possible explicit {', '.join(terminal)} outcome, which can replace an in-flight exception.",
            [
                log_advice(
                    location,
                    "ERROR",
                    "An in-flight failure prevents required processing from completing.",
                    "Preserve the original exception for the operation's reporting owner; review the finally exit before adding a log.",
                )
            ],
            priority="high",
            confidence="high",
            assumptions=[
                "This is a syntactic suppression possibility, not evidence that an exception occurs at runtime."
            ],
        )

    for call in analysis.calls:
        decision = None
        if call.boundary:
            result, blocked = explain_local(call)
            if not result.recognized and not blocked:
                parent = coverage_in_callers(call.symbol)
                result.extend(parent)
                result.recognized = parent.recognized
            decision = CoverageDecision(
                call.id,
                call.symbol,
                call.location,
                "recognized" if result.recognized else "not_established",
                result.evidence,
                result.truncated,
            )
            analysis.coverage.append(decision)
        if decision and decision.status == "not_established":
            emit(
                "FS005",
                call.symbol,
                call.location,
                f"Recognized {call.boundary} operation: {call.resolved_name}. No covering trace scope or complete recognized handler reporting was found along all resolved caller routes within the scan bounds.",
                [
                    Recommendation(
                        "trace",
                        None,
                        "This boundary's latency or failure is operationally useful to diagnose.",
                        "Create or reuse a span around the operation and record its outcome; consider a failure counter and latency histogram for frequent calls.",
                        call.location,
                        ["operation", "dependency", "outcome"],
                    ),
                    log_advice(
                        call.location,
                        "ERROR",
                        "The failure reaches the owning operation boundary and makes that operation unsuccessful.",
                        "Record one final failure at the owner, after retries and fallback decisions; do not log every attempt as ERROR.",
                    ),
                ],
                priority="low" if call.boundary == "serialization" else "medium",
                assumptions=[
                    "Auto-instrumentation and host-framework exception reporting are outside this static model.",
                    "Caller routes are incomplete when dynamic dispatch or external callers are unresolved.",
                    "Expected failures or successful recovery may need WARNING, DEBUG, metrics, or no additional log.",
                ],
            )
        if call.discarded_task:
            emit(
                "FS007",
                call.symbol,
                call.location,
                "The return value of a recognized asyncio task-creation call is discarded as an expression statement.",
                [
                    log_advice(
                        call.location,
                        "ERROR",
                        "The background operation fails and its exception is not already reported by the task or event-loop handler.",
                        "Establish task completion/error reporting, retain task ownership, and correlate failures with the initiating operation.",
                    )
                ],
                confidence="high",
                assumptions=[
                    "The event loop may already report an unhandled exception; task-specific correlation and lifecycle ownership still need review."
                ],
            )

    if analysis.config.lifecycle_info:
        for symbol in symbols.values():
            if symbol.entrypoint_basis == "configuration" and not any(
                log.symbol == symbol.id and log.level == "INFO" for log in analysis.logs
            ):
                emit(
                    "FS008",
                    symbol.id,
                    symbol.location,
                    "This explicitly configured operation has no recognized INFO event; lifecycle review was enabled by configuration.",
                    [
                        log_advice(
                            symbol.location,
                            "INFO",
                            "An important expected lifecycle transition or successful business operation completes.",
                            "Consider one concise lifecycle event at successful completion, with safe operation identifiers; avoid routine per-call logging.",
                        )
                    ],
                    priority="low",
                    confidence="low",
                    assumptions=[
                        "The importance and volume of this operation require project context.",
                        "Existing metrics, traces, or caller events may already make an additional log unnecessary.",
                    ],
                )
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        findings,
        key=lambda finding: (
            order[finding.priority],
            finding.evidence[0].location.file,
            finding.evidence[0].location.line,
            finding.rule_id,
            finding.id,
        ),
    )
