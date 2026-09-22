"""Bounded explicit retry structure and local async handle ownership."""

from __future__ import annotations

import ast
import fnmatch
from copy import deepcopy
from dataclasses import asdict
from typing import TYPE_CHECKING

from .model import Finding, Location, Recommendation, stable_id


def local_nodes(node):
    pending = list(ast.iter_child_nodes(node))
    while pending:
        item = pending.pop()
        yield item
        if not isinstance(
            item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            pending.extend(ast.iter_child_nodes(item))


if TYPE_CHECKING:
    from .scanner import Analysis


def analyze(analysis: Analysis, findings, outcomes):
    tasks, retries = [], []
    calls = {
        id(node): c
        for c in analysis.calls
        if c.id in analysis.call_nodes
        for _, node in [analysis.call_nodes[c.id]]
    }

    def emit(rule, definition, location, fact, kind, level, action):
        titles = {
            "FS009": "Retained task failure observation is not established",
            "FS010": "Retry attempt logs an error before the final outcome is known",
        }
        findings.append(
            Finding(
                stable_id(rule, definition.symbol.id, location.line, location.column),
                rule,
                titles[rule],
                "medium",
                "medium",
                definition.symbol.id,
                [analysis.evidence(location, fact)],
                [
                    Recommendation(
                        kind,
                        level,
                        "This work belongs to the declared operation and no equivalent owner already reports its outcome.",
                        action,
                        location,
                        ["operation", "outcome", "correlation_id"],
                    )
                ],
                [
                    "Local ownership and retry structure are bounded evidence, not proof of business failure or reporting delivery."
                ],
                [[definition.symbol.id]],
            )
        )

    for call in analysis.calls:
        matching = [
            r
            for r in analysis.config.retries
            if call.resolution not in {"unresolved", "ambiguous"}
            and fnmatch.fnmatchcase(call.resolved_name, r["pattern"])
        ]
        if matching:
            retries.append(
                {
                    "id": stable_id(call.id, "retry-contract"),
                    "operation": call.symbol,
                    "location": asdict(call.location),
                    "basis": "declared_retry_wrapper",
                    "supported": False,
                    "uncertain": True,
                    "attempt_handlers": [],
                    "final_exits": [],
                    "owner_candidates": sorted({r["owner"] for r in matching}),
                    "reporting_owner": matching[0]["owner"]
                    if len(matching) == 1
                    else None,
                    "note": "Wrapper retry behavior is declared, not inspected; attempt counts, recovery, and reporting delivery remain unknown.",
                }
            )
    for definition in analysis.definitions:
        if not isinstance(definition.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(local_nodes(definition.node))
        if not analysis.graph_step(len(nodes)):
            break
        parents = {
            id(child): parent
            for parent in [definition.node, *nodes]
            for child in ast.iter_child_nodes(parent)
        }
        for loop in (n for n in nodes if isinstance(n, (ast.For, ast.While))):
            descendants = list(local_nodes(loop))
            candidate_handlers = [
                n
                for n in descendants
                if isinstance(n, ast.ExceptHandler)
                and any(isinstance(t, ast.Continue) for t in local_nodes(n))
            ]
            if not candidate_handlers:
                continue
            supported = (
                isinstance(loop, ast.For)
                and isinstance(loop.iter, ast.Call)
                and isinstance(loop.iter.func, ast.Name)
                and loop.iter.func.id == "range"
                and "range" not in analysis.receiver_index.scope(definition).bindings
                and all(
                    isinstance(a, ast.Constant) and type(a.value) is int
                    for a in loop.iter.args
                )
                and not loop.iter.keywords
                and 1 <= len(loop.iter.args) <= 3
            )
            nested = any(
                isinstance(
                    n,
                    (
                        ast.For,
                        ast.While,
                        ast.AsyncFor,
                        ast.With,
                        ast.AsyncWith,
                        ast.TryStar,
                    ),
                )
                for n in descendants
            )
            breaks = any(isinstance(n, ast.Break) for n in descendants)
            retry_location = Location(
                definition.unit.path, loop.lineno, loop.col_offset
            )
            owners = sorted(
                {
                    r["owner"]
                    for r in outcomes
                    if r["operation"] == definition.symbol.id
                    and r["outcome"] == "failed_operation"
                    and not r["uncertain"]
                    and r["owner"]
                }
            )
            parent = parents.get(id(loop))
            siblings = (
                next(
                    (
                        v
                        for _, v in ast.iter_fields(parent)
                        if isinstance(v, list) and loop in v
                    ),
                    [],
                )
                if parent
                else []
            )
            after = siblings[siblings.index(loop) + 1 :] if loop in siblings else []
            terminal = [
                n
                for n in [*loop.orelse, *after]
                if isinstance(n, (ast.Raise, ast.Return))
            ]
            record = {
                "id": stable_id(definition.symbol.id, loop.lineno, "retry"),
                "operation": definition.symbol.id,
                "location": asdict(retry_location),
                "basis": "explicit_continue_handler",
                "supported": supported and not nested and not breaks,
                "uncertain": not supported or nested or breaks or not terminal,
                "attempt_handlers": [h.lineno for h in candidate_handlers],
                "final_exits": [
                    {"line": n.lineno, "exit": type(n).__name__.lower()}
                    for n in terminal
                ],
                "owner_candidates": owners,
                "reporting_owner": owners[0]
                if len(owners) == 1
                and supported
                and not nested
                and not breaks
                and terminal
                else None,
                "note": "Continue-based retry-shaped control flow does not establish attempt failure, successful recovery, or wrapper behavior. Final ownership is declared.",
            }
            retries.append(record)
            for handler in candidate_handlers:
                for log in analysis.logs:
                    if (
                        log.symbol == definition.symbol.id
                        and handler.lineno <= log.location.line <= handler.end_lineno
                        and log.level in {"ERROR", "CRITICAL"}
                    ):
                        emit(
                            "FS010",
                            definition,
                            log.location,
                            "An ERROR-or-higher event appears in a handler with a possible continue to another attempt.",
                            "enrich_existing_log",
                            log.level,
                            "Distinguish attempt outcome from final operation outcome; use a metric, trace, or DEBUG for expected attempts and report final failure once at its owner.",
                        )

        if not isinstance(definition.node, ast.AsyncFunctionDef):
            continue
        records = {}
        steps = 0
        truncated = False

        def expression(node, state, groups):
            if node is None:
                return set(), False
            if isinstance(node, ast.Name):
                return state["env"].get(node.id, (set(), False))
            if isinstance(node, ast.Await):
                ids, collected = expression(node.value, state, groups)
                for identity in ids:
                    state["states"][identity] = (
                        "exceptions_collected" if collected else "awaited"
                    )
                    records[identity]["observation_sites"].add(node.lineno)
                return set(), False
            if isinstance(node, ast.Call):
                call = calls.get(id(node))
                name = (
                    call.resolved_name
                    if call and call.resolution not in {"unresolved", "ambiguous"}
                    else ""
                )
                group = (
                    node.func.value.id
                    if isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.attr == "create_task"
                    and node.func.value.id in groups
                    else None
                )
                if name in {"asyncio.create_task", "asyncio.ensure_future"} or group:
                    if not call:
                        return set(), False
                    record = records.setdefault(
                        call.id,
                        {
                            "call_id": call.id,
                            "operation": definition.symbol.id,
                            "location": asdict(call.location),
                            "child": None,
                            "group": group,
                            "observation_sites": set(),
                        },
                    )
                    child = calls.get(id(node.args[0])) if node.args else None
                    record["child"] = child.target if child else None
                    state["states"][call.id] = (
                        "group_owned" if group else "retained_unobserved"
                    )
                    return {call.id}, False
                ids = set()
                collected = False
                for value in [*node.args, *(k.value for k in node.keywords)]:
                    children, collects = expression(value, state, groups)
                    ids.update(children)
                    collected |= collects
                if name == "asyncio.gather":
                    # Only an enclosing await observes the aggregate.
                    collected |= any(
                        k.arg == "return_exceptions"
                        and not (
                            isinstance(k.value, ast.Constant) and k.value.value is False
                        )
                        for k in node.keywords
                    )
                    return ids, collected
                for identity in ids:
                    state["states"][identity] = "uncertain_escape"
                return set(), False
            if isinstance(
                node,
                (
                    ast.Lambda,
                    ast.ListComp,
                    ast.SetComp,
                    ast.DictComp,
                    ast.GeneratorExp,
                    ast.BoolOp,
                    ast.IfExp,
                ),
            ):
                for child in ast.walk(node):
                    if isinstance(child, ast.Name):
                        for identity in state["env"].get(child.id, (set(), False))[0]:
                            state["states"][identity] = "uncertain_escape"
                return set(), False
            ids = set()
            for child in ast.iter_child_nodes(node):
                children, _ = expression(child, state, groups)
                ids.update(children)
            for identity in ids:
                state["states"][identity] = "uncertain_escape"
            return set(), False

        def block(body, states, groups):
            nonlocal steps, truncated
            for node in body:
                output = []
                for state in states:
                    if state["terminal"]:
                        output.append(state)
                        continue
                    steps += 1
                    if steps > 512 or not analysis.graph_step():
                        truncated = True
                        return states
                    if isinstance(node, ast.If):
                        expression(node.test, state, groups)
                        output.extend(block(node.body, [deepcopy(state)], groups))
                        output.extend(block(node.orelse, [deepcopy(state)], groups))
                    elif isinstance(node, ast.Try):
                        branches = block(node.body, [deepcopy(state)], groups)
                        normal = [s for s in branches if not s["terminal"]]
                        branches = [s for s in branches if s["terminal"]] + block(
                            node.orelse, normal, groups
                        )
                        for handler in node.handlers:
                            branches.extend(
                                block(handler.body, [deepcopy(state)], groups)
                            )
                        for s in branches:
                            terminal = s["terminal"]
                            s["terminal"] = False
                            final = block(node.finalbody, [s], groups)
                            for t in final:
                                t["terminal"] |= terminal
                            output.extend(final)
                    elif isinstance(node, ast.AsyncWith):
                        active = set(groups)
                        created = set()
                        for item in node.items:
                            call = calls.get(id(item.context_expr))
                            if (
                                call
                                and call.resolved_name == "asyncio.TaskGroup"
                                and call.resolution not in {"unresolved", "ambiguous"}
                                and isinstance(item.optional_vars, ast.Name)
                            ):
                                # Rebinding the group inside the block invalidates attribution.
                                if not any(
                                    isinstance(n, ast.Name)
                                    and isinstance(n.ctx, ast.Store)
                                    and n.id == item.optional_vars.id
                                    for stmt in node.body
                                    for n in ast.walk(stmt)
                                ):
                                    active.add(item.optional_vars.id)
                                    created.add(item.optional_vars.id)
                        inner = block(node.body, [state], active)
                        for s in inner:
                            for identity in s["states"]:
                                if records[identity]["group"] in created:
                                    s["states"][identity] = "group_awaited"
                                    records[identity]["observation_sites"].add(
                                        node.end_lineno
                                    )
                        output.extend(inner)
                    elif isinstance(
                        node,
                        (
                            ast.For,
                            ast.While,
                            ast.AsyncFor,
                            ast.With,
                            ast.TryStar,
                            ast.Match,
                        ),
                    ):
                        # Unsupported effects cannot promote any existing task to observed.
                        for identity in state["states"]:
                            state["states"][identity] = "uncertain_control"
                        nested = block(
                            getattr(node, "body", []), [deepcopy(state)], groups
                        )
                        for s in nested:
                            for identity in s["states"]:
                                s["states"][identity] = "uncertain_control"
                        output.extend([state, *nested])
                    else:
                        value = getattr(node, "value", getattr(node, "exc", None))
                        ids, collected = expression(value, state, groups)
                        if isinstance(node, (ast.Assign, ast.AnnAssign)):
                            targets = (
                                node.targets
                                if isinstance(node, ast.Assign)
                                else [node.target]
                            )
                            for target in targets:
                                if isinstance(target, ast.Name):
                                    state["env"][target.id] = (ids, collected)
                                else:
                                    for identity in ids:
                                        state["states"][identity] = "uncertain_escape"
                        elif isinstance(node, ast.Return):
                            for identity in ids:
                                state["states"][identity] = "transferred"
                        elif isinstance(node, ast.Expr):
                            for identity in ids:
                                if state["states"][identity] != "group_owned":
                                    state["states"][identity] = "discarded"
                        if isinstance(node, (ast.Return, ast.Raise)):
                            state["terminal"] = True
                        output.append(state)
                if len(output) > 32:
                    truncated = True
                    output = output[:32]
                states = output
            return states

        states = block(
            definition.node.body, [{"env": {}, "states": {}, "terminal": False}], set()
        )
        for identity, record in records.items():
            alternatives = sorted(
                {s["states"][identity] for s in states if identity in s["states"]}
            )
            observed = (
                bool(alternatives)
                and all(s in {"awaited", "group_awaited"} for s in alternatives)
                and not truncated
            )
            record.update(
                states=alternatives,
                status=alternatives[0]
                if len(alternatives) == 1 and not truncated
                else "uncertain",
                observed=observed,
                truncated=truncated,
                observation_sites=sorted(record["observation_sites"]),
                reporting_owner=None,
                note="Observation or transfer does not prove reporting delivery. Implicit expression failures and external ownership remain unknown.",
            )
            if observed:
                owners = sorted(
                    {
                        r["owner"]
                        for r in outcomes
                        if r["operation"] == definition.symbol.id
                        and r["owner"]
                        and not r["uncertain"]
                    }
                )
                record["reporting_owner"] = owners[0] if len(owners) == 1 else None
            tasks.append(record)
            if (
                not observed
                and alternatives
                and alternatives != ["transferred"]
                and not calls[id(analysis.call_nodes[identity][1])].discarded_task
            ):
                location = Location(**record["location"])
                emit(
                    "FS009",
                    definition,
                    location,
                    "Task ownership alternatives: " + ", ".join(alternatives),
                    "trace",
                    None,
                    "Establish task completion/failure observation or an explicit ownership transfer; report final failure at the operation owner only if the required operation fails.",
                )
    return tasks, retries
