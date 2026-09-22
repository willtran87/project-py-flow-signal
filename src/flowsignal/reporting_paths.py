"""Bounded structured control-flow review of exception-handler outcomes.

This models explicit exits, not implicit exceptions from every expression or
delivery of a reporter. Unsupported effects retain an uncertain path.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .model import Handler

if TYPE_CHECKING:
    from .scanner import Analysis


@dataclass(frozen=True)
class State:
    exit: str = "fallthrough"
    signals: tuple[tuple[int, int], ...] = ()
    branches: tuple[str, ...] = ()
    uncertain: bool = False


def analyze(analysis: Analysis, handler: Handler, body: list[ast.stmt]):
    signals = {
        (s.location.line, s.location.column)
        for s in handler.logs
        if s.level in {"WARNING", "ERROR", "CRITICAL"}
        and not s.execution.startswith("deferred_")
    }
    signals.update(
        (s.location.line, s.location.column)
        for s in handler.reporting_signals
        if not s.execution.startswith("deferred_")
    )
    truncated, steps = False, 0

    def bounded(states):
        nonlocal truncated
        states = list(dict.fromkeys(states))
        if len(states) > 32:
            truncated = True
            return states[:31] + [State(exit="unknown", uncertain=True)]
        return states

    def expression(node, state):
        if node is None:
            return state
        nodes = list(ast.walk(node))
        if any(
            isinstance(
                n,
                (
                    ast.BoolOp,
                    ast.IfExp,
                    ast.Lambda,
                    ast.ListComp,
                    ast.SetComp,
                    ast.DictComp,
                    ast.GeneratorExp,
                ),
            )
            for n in nodes
        ):
            return state  # no short-circuit/deferred expression is credited unconditionally
        found = {
            (n.lineno, n.col_offset) for n in nodes if isinstance(n, ast.Call)
        } & signals
        return replace(state, signals=tuple(sorted(set(state.signals) | found)))

    def branch(state, node, label):
        return replace(
            state, branches=(*state.branches, f"L{node.lineno}: {label}")[-16:]
        )

    def block(statements, states):
        nonlocal truncated, steps
        for node in statements:
            output = []
            for state in states:
                if state.exit != "fallthrough":
                    output.append(state)
                    continue
                steps += 1
                if steps > 512 or not analysis.graph_step():
                    truncated = True
                    return [State(exit="unknown", uncertain=True)]
                if isinstance(node, ast.If):
                    truth = (
                        node.test.value if isinstance(node.test, ast.Constant) else None
                    )
                    state = expression(node.test, state)
                    if truth is not False:
                        output.extend(
                            block(node.body, [branch(state, node, "if true")])
                        )
                    if truth is not True:
                        output.extend(
                            block(node.orelse, [branch(state, node, "if false")])
                        )
                elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                    output.extend(
                        block(
                            node.orelse,
                            [branch(state, node, "loop finishes without body")],
                        )
                    )
                    for item in block(node.body, [branch(state, node, "loop body")]):
                        if item.exit == "break":
                            output.append(replace(item, exit="fallthrough"))
                        elif item.exit in {"continue", "fallthrough"}:
                            output.append(
                                replace(item, exit="fallthrough", uncertain=True)
                            )
                        else:
                            output.append(item)
                elif isinstance(node, ast.Try):
                    paths = block(node.body, [state])
                    normal = [p for p in paths if p.exit == "fallthrough"]
                    paths = [p for p in paths if p.exit != "fallthrough"] + block(
                        node.orelse, normal
                    )
                    # Exception types and implicit expression failures are unknown.
                    for candidate in node.handlers:
                        paths.extend(
                            block(
                                candidate.body,
                                [branch(state, candidate, "possible nested handler")],
                            )
                        )
                    for item in paths:
                        if node.finalbody:
                            for final in block(
                                node.finalbody, [replace(item, exit="fallthrough")]
                            ):
                                output.append(
                                    replace(final, exit=item.exit)
                                    if final.exit == "fallthrough"
                                    else final
                                )
                        else:
                            output.append(item)
                elif isinstance(
                    node, (ast.With, ast.AsyncWith, ast.TryStar, ast.Match)
                ):
                    output.append(
                        replace(
                            branch(state, node, "unsupported control effect"),
                            uncertain=True,
                        )
                    )
                elif isinstance(node, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                    state = expression(
                        getattr(node, "value", getattr(node, "exc", None)), state
                    )
                    output.append(replace(state, exit=type(node).__name__.lower()))
                else:
                    if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
                        state = expression(node.value, state)
                    output.append(state)
            states = bounded(output)
        return states

    states = block(body, [State()])
    handler.reporting_paths = [
        {
            "exit": s.exit,
            "reported": bool(s.signals) and not s.uncertain,
            "signals": [{"line": line, "column": col} for line, col in s.signals],
            "branches": list(s.branches),
            "uncertain": s.uncertain,
        }
        for s in states
    ]
    handler.paths_truncated = truncated
    handler.path_reporting = (
        bool(states)
        and not truncated
        and all(s.signals and not s.uncertain for s in states)
    )
