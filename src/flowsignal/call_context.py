"""Bounded callable bindings per indexed call site; never universal coverage edges."""

from __future__ import annotations

import ast
import fnmatch
from collections import defaultdict, deque
from dataclasses import asdict
from typing import TYPE_CHECKING

from .model import Diagnostic, stable_id

if TYPE_CHECKING:
    from .scanner import Analysis


def analyze(analysis: Analysis):
    index = analysis.receiver_index
    calls = {c.id: c for c in analysis.calls}
    by_symbol = defaultdict(list)
    for identity, (definition, node) in analysis.call_nodes.items():
        by_symbol[definition.symbol.id].append((calls[identity], node))
    records, seen = [], set()
    pending = deque()
    exhausted = False

    def tick():
        nonlocal exhausted
        if exhausted:
            return False
        if not analysis.graph_step() or len(records) >= 2000:
            exhausted = True
            analysis.diagnostics.append(
                Diagnostic(
                    "contextual_call_limit",
                    "Callable context work was truncated; candidates are not exhaustive.",
                )
            )
            return False
        return True

    def callable_value(node, definition, bindings):
        scope = index.scope(definition)
        if isinstance(node, ast.Name) and node.id in bindings:
            # Parameters have an initial unknown value; any write invalidates them.
            if len(scope.values.get(node.id, [])) != 1:
                return None
            return bindings[node.id]
        value = scope.type_of(node)
        if value and value.name.startswith("<callable>:"):
            target = value.name.removeprefix("<callable>:")
            candidate = index.definitions.get(target)
            if candidate and not any(
                candidate.symbol.qualified_name.startswith(m + ".")
                for m in analysis.ambiguous_modules
            ):
                return target
        return None

    def bind(call, node, caller, target, bindings):
        definition = index.definitions.get(target)
        if (
            not definition
            or not isinstance(definition.node, (ast.FunctionDef, ast.AsyncFunctionDef))
            or definition.node.decorator_list
        ):
            return None
        args = definition.node.args
        if (
            args.vararg
            or args.kwarg
            or any(isinstance(a, ast.Starred) for a in node.args)
            or any(k.arg is None for k in node.keywords)
        ):
            return None
        positional = [*args.posonlyargs, *args.args]
        implicit = bool(
            definition.class_name
            and positional
            and positional[0].arg == "self"
            and isinstance(node.func, ast.Attribute)
        )
        if definition.class_name and not implicit:
            return None  # Bound-method values passed as callbacks need separate receiver binding.
        positional = positional[1:] if implicit else positional
        if len(node.args) > len(positional):
            return None
        actual = {
            p.arg: callable_value(n, caller, bindings)
            for p, n in zip(positional, node.args)
        }
        allowed = {p.arg for p in [*args.args, *args.kwonlyargs]}
        for kw in node.keywords:
            if (
                kw.arg not in allowed
                or kw.arg in actual
                or implicit
                and kw.arg == "self"
            ):
                return None
            actual[kw.arg] = callable_value(kw.value, caller, bindings)
        defaults = (
            dict(zip([p.arg for p in positional][-len(args.defaults) :], args.defaults))
            if args.defaults
            else {}
        )
        defaults.update(
            {
                p.arg: n
                for p, n in zip(args.kwonlyargs, args.kw_defaults)
                if n is not None
            }
        )
        for p in [*positional, *args.kwonlyargs]:
            if p.arg not in actual:
                if p.arg not in defaults:
                    return None
                actual[p.arg] = callable_value(defaults[p.arg], definition, {})
        return {k: v for k, v in actual.items() if v is not None}

    def add(call, target, context, basis, execution, contract=None):
        key = (call.id, target, tuple(context), basis)
        if key in seen or not tick():
            return
        seen.add(key)
        records.append(
            {
                "id": stable_id(*key),
                "call_id": call.id,
                "caller": call.symbol,
                "callee": target,
                "location": asdict(call.location),
                "context": list(context),
                "basis": basis,
                "execution": execution,
                "contract": contract,
                "coverage_eligible": False,
                "assumptions": [
                    "Applies only to the recorded binding context; external callers and unmodeled mutation remain unknown."
                ],
            }
        )

    for call in analysis.calls:
        pair = analysis.call_nodes.get(call.id)
        if not pair or not tick():
            continue
        definition, node = pair
        if call.target:
            bound = bind(call, node, definition, call.target, {})
            if bound:
                pending.append((call.target, bound, [call.id]))
        contracts = [
            r
            for r in analysis.config.registrations
            if call.resolution not in {"unresolved", "ambiguous"}
            and fnmatch.fnmatchcase(call.resolved_name, r["pattern"])
        ]
        if len(contracts) > 1:
            analysis.diagnostics.append(
                Diagnostic(
                    "registration_contract_conflict",
                    "Multiple registration contracts match; no deferred target was selected.",
                    call.location.file,
                    call.location.line,
                )
            )
        elif contracts:
            position = contracts[0]["argument"]
            argument = (
                node.args[position]
                if type(position) is int and position < len(node.args)
                else next((k.value for k in node.keywords if k.arg == position), None)
            )
            target = callable_value(argument, definition, {})
            if (
                target
                and not any(isinstance(n, ast.Starred) for n in node.args)
                and not any(k.arg is None for k in node.keywords)
            ):
                add(
                    call,
                    target,
                    [call.id],
                    "declared_registration",
                    "deferred_registration",
                    contracts[0],
                )
            else:
                analysis.diagnostics.append(
                    Diagnostic(
                        "registration_callback_unknown",
                        "The declared registration callback cannot be resolved safely.",
                        call.location.file,
                        call.location.line,
                    )
                )
    visited = set()
    while pending and tick():
        identity, bindings, context = pending.popleft()
        signature = (identity, tuple(sorted(bindings.items())), tuple(context))
        if signature in visited:
            continue
        visited.add(signature)
        if len(context) > analysis.config.max_path_depth:
            analysis.diagnostics.append(
                Diagnostic(
                    "contextual_call_limit",
                    "Callable forwarding reached the path-depth limit.",
                )
            )
            continue
        definition = index.definitions[identity]
        for call, node in by_symbol[identity]:
            if not tick():
                break
            target = call.target or callable_value(node.func, definition, bindings)
            if not target:
                continue
            if not call.target:
                kind = index.definitions[target].symbol.kind
                execution = (
                    "deferred_coroutine"
                    if kind == "async_function" and call.execution != "awaited"
                    else "deferred_generator"
                    if kind in {"generator", "async_generator"}
                    else call.execution
                )
                add(call, target, context, "callable_argument", execution)
            if call.id not in context:
                bound = bind(call, node, definition, target, bindings)
                if bound:
                    pending.append((target, bound, [*context, call.id]))
    return sorted(
        records, key=lambda r: (r["location"]["file"], r["location"]["line"], r["id"])
    )
