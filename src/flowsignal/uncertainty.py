"""Source-grounded unresolved-call explanations and bounded entrypoint context."""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from .model import Call, Diagnostic, ResolutionGap

if TYPE_CHECKING:
    from .scanner import Analysis, FactVisitor

REASONS = {
    "ambiguous_definition": (
        "Multiple definitions or import roots prevent selecting one target.",
        "Disambiguate import roots or duplicate definitions before relying on this edge.",
    ),
    "inference_budget": (
        "Receiver inference exhausted its work budget before this target could be established.",
        "Review type_work_limit and rerun with an appropriate max_type_steps budget.",
    ),
    "conflicting_receiver_types": (
        "The receiver has conflicting types in the supported assignment model.",
        "Inspect the assignments and use a narrower, consistent receiver contract where appropriate.",
    ),
    "callback_parameter": (
        "The callable is supplied as a function parameter; its invocation target is not established.",
        "Review call sites or runtime traces to identify callback implementations.",
    ),
    "inheritance_lookup": (
        "This call requires inherited-member or super() lookup that is not resolved by this scanner.",
        "Review the base classes and effective method resolution order.",
    ),
    "decorated_helper": (
        "A decorated helper produces this receiver; the decorator may change its return behavior.",
        "Review the decorator and helper contract before assuming the returned object's type.",
    ),
    "unknown_member_type": (
        "The receiver field has no consistent type established by the supported field analysis.",
        "Review field initialization and writes; add an accurate annotation if the contract is known.",
    ),
    "shadowed_binding": (
        "A local binding shadows an imported or enclosing name; the imported target cannot be assumed.",
        "Inspect the local binding and its call sites rather than attributing this call to the import.",
    ),
    "dynamic_callable": (
        "The callable is obtained from an expression or property result with no established target.",
        "Review the registry, factory, or property result; correlate with runtime evidence if needed.",
    ),
    "unknown_receiver": (
        "No supported receiver type establishes this member-call target.",
        "Review the receiver's origin, annotations, and factory contract.",
    ),
    "unknown_name": (
        "No unique callable definition is established for this name in the scanned scope.",
        "Check missing source roots, runtime registration, and definitions outside the selected source.",
    ),
}


def explain(visitor: FactVisitor, node: ast.expr, call: Call) -> ResolutionGap:
    from .scanner import dotted

    index = visitor.analysis.receiver_index
    scope = visitor.receiver_scope
    reason = "unknown_name"
    if call.resolution == "ambiguous":
        reason = "ambiguous_definition"
    elif index.exhausted:
        reason = "inference_budget"
    elif call.resolved_name == "<property-result>" or not isinstance(
        node, (ast.Name, ast.Attribute)
    ):
        reason = "dynamic_callable"
    elif isinstance(node, ast.Name):
        definition = visitor.definition.node
        parameters = []
        if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameters = [
                a.arg
                for a in [
                    *definition.args.posonlyargs,
                    *definition.args.args,
                    *definition.args.kwonlyargs,
                ]
            ]
        if node.id in parameters:
            reason = "callback_parameter"
        elif visitor.unit.bindings.get(node.id) and not visitor.bindings.get(node.id):
            reason = "shadowed_binding"
    else:
        receiver = node.value
        reason = "unknown_receiver"
        if isinstance(receiver, ast.Name):
            choices = {
                item for item in scope.values.get(receiver.id, []) if item is not None
            }
            if len(choices) > 1:
                reason = "conflicting_receiver_types"
            elif visitor.unit.bindings.get(receiver.id) and not visitor.bindings.get(
                receiver.id
            ):
                reason = "shadowed_binding"
        if isinstance(receiver, ast.Call):
            name = dotted(receiver.func)
            if name == "super" and "super" not in visitor.bindings:
                reason = "inheritance_lookup"
            else:
                _, target, _ = visitor.resolve(receiver.func)
                definition = index.definitions.get(target)
                if definition and getattr(definition.node, "decorator_list", []):
                    reason = "decorated_helper"
        owner = scope.type_of(receiver)
        if owner and any(
            cls.bases for cls in visitor.analysis.class_nodes.get(owner.name, [])
        ):
            reason = "inheritance_lookup"
        elif isinstance(receiver, ast.Attribute) and owner is None:
            reason = "unknown_member_type"
        if index.exhausted:
            reason = "inference_budget"
    explanation, action = REASONS[reason]
    return ResolutionGap(
        call.id,
        call.symbol,
        call.location,
        call.expression,
        reason,
        explanation,
        action,
    )


def attach_entrypoints(analysis: Analysis) -> None:
    """Potential entrypoints via known call edges, never runtime reachability proof."""
    parents = defaultdict(set)
    for call in analysis.calls:
        if call.target:
            parents[call.target].add(call.symbol)
    entries = {d.symbol.id for d in analysis.definitions if d.symbol.entrypoint}
    cache = {}
    limited = False
    for gap in analysis.resolution_gaps:
        if gap.symbol not in cache:
            queue = deque([(gap.symbol, 0)])
            seen, found = set(), set()
            truncated = False
            while queue:
                if (
                    analysis.stats["resolution_context_steps"]
                    >= analysis.config.max_resolution_steps
                ):
                    truncated = limited = True
                    break
                symbol, depth = queue.popleft()
                if symbol in seen:
                    continue
                seen.add(symbol)
                analysis.stats["resolution_context_steps"] += 1
                if symbol in entries:
                    found.add(symbol)
                    if len(found) >= 64 and queue:
                        truncated = True
                        break
                elif depth >= analysis.config.max_path_depth:
                    truncated |= bool(parents[symbol] - seen)
                else:
                    for parent in sorted(parents[symbol] - seen):
                        # Bound enqueued edges, not just popped symbols.
                        if (
                            analysis.stats["resolution_context_steps"]
                            >= analysis.config.max_resolution_steps
                        ):
                            truncated = limited = True
                            break
                        analysis.stats["resolution_context_steps"] += 1
                        queue.append((parent, depth + 1))
            cache[gap.symbol] = sorted(found), truncated
        gap.entrypoints, gap.context_truncated = cache[gap.symbol]
    if limited:
        analysis.diagnostics.append(
            Diagnostic(
                "resolution_context_limit",
                "Unresolved-call entrypoint context exhausted max_resolution_steps; affected entrypoint lists are partial.",
            )
        )
