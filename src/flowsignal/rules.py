"""Rules reason from scanner facts; no network or model calls occur here."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING

from .model import (
    Call,
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
}


def failure_logs(handler: Handler):
    return [
        log
        for log in handler.logs
        if log.level in {"ERROR", "CRITICAL"} and not log.conditional
    ]


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
    coverage_cache: dict[str, bool] = {}

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

    def covered(call: Call) -> bool:
        handlers = effective_handlers(call)
        broad = next((handler for handler in handlers if handler.catches_all), None)
        # Typed handlers before the catch-all must also expose their failures.
        return broad is not None and all(
            any(
                log.level in {"WARNING", "ERROR", "CRITICAL"} and not log.conditional
                for log in handler.logs
            )
            for handler in handlers
        )

    def unreported_consumption(call: Call) -> bool:
        return any(
            set(handler.outcomes) != {"raise"}
            and not any(
                log.level in {"WARNING", "ERROR", "CRITICAL"} and not log.conditional
                for log in handler.logs
            )
            for handler in effective_handlers(call)
        )

    def coverage_in_callers(symbol_id: str, seen: frozenset[str] = frozenset()) -> bool:
        if symbol_id in coverage_cache:
            return coverage_cache[symbol_id]
        if symbol_id in seen or len(seen) >= analysis.config.max_path_depth:
            return False
        if not analysis.graph_step():
            return False
        incoming = callers[symbol_id]
        if not incoming or symbols[symbol_id].entrypoint:
            coverage_cache[symbol_id] = False
            return False
        for call in incoming:
            if not analysis.graph_step():
                return False
            if call.execution.startswith("deferred_"):
                coverage_cache[symbol_id] = False
                return False
            if (
                covered(call)
                or call.trace_failure_coverage
                and not unreported_consumption(call)
            ):
                continue
            # A suppressing handler blocks propagation to more distant owners.
            if call.propagation_uncertain or unreported_consumption(call):
                coverage_cache[symbol_id] = False
                return False
            if not coverage_in_callers(call.symbol, seen | {symbol_id}):
                coverage_cache[symbol_id] = False
                return False
        coverage_cache[symbol_id] = True
        return True

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
        if exits and not any(
            log.level in {"WARNING", "ERROR", "CRITICAL"} and not log.conditional
            for log in handler.logs
        ):
            emit(
                "FS001",
                handler.symbol,
                handler.location,
                f"Handler catches {', '.join(handler.types)}; syntactic outcomes: {', '.join(handler.outcomes)}. No unconditional WARNING-or-higher log is recognized in this handler.",
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
                    "A recognized exception-handler log does not explicitly retain traceback context with logger.exception() or exc_info=True.",
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
        if (
            call.boundary
            and not covered(call)
            and not (call.trace_failure_coverage and not unreported_consumption(call))
        ):
            # Do not credit callers if this operation's own handler can consume the failure.
            blocked = call.propagation_uncertain or unreported_consumption(call)
            if not blocked and coverage_in_callers(call.symbol):
                continue
            emit(
                "FS005",
                call.symbol,
                call.location,
                f"Recognized {call.boundary} operation: {call.resolved_name}. No covering trace scope or complete recognized handler logging was found along all resolved caller routes within the scan bounds.",
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
