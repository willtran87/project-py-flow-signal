"""Evidence-backed graph projection shared by offline HTML and Mermaid reports."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import asdict
from importlib.resources import files

from .model import Location, Report, stable_id


def graph_data(report: Report, focus: str | None = None, max_nodes: int = 60) -> dict:
    if not 1 <= max_nodes <= 200:
        raise ValueError("diagram-max-nodes must be between 1 and 200")
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    anchors: dict[tuple[str, int, int], str] = {}

    def node(identity, title, kind, symbol, location, **extra):
        record = {
            "id": identity,
            "title": title,
            "kind": kind,
            "symbol": symbol,
            "location": asdict(location),
            "existing": [],
            "findings": [],
            "unresolved_count": 0,
            "unresolved_examples": [],
            **extra,
        }
        nodes[identity] = record
        return record

    def anchor(symbol: str, location: Location, identity: str):
        anchors[(symbol, location.line, location.column)] = identity

    def edge(identity, source, target, kind, label, location, **extra):
        if source in nodes and target in nodes:
            edges.append(
                {
                    "id": identity,
                    "source": source,
                    "target": target,
                    "kind": kind,
                    "label": label,
                    "location": asdict(location),
                    **extra,
                }
            )

    for symbol in report.symbols:
        node(
            symbol.id,
            symbol.id.split(":", 1)[-1],
            "symbol",
            symbol.id,
            symbol.location,
            qualified_name=symbol.qualified_name,
            symbol_kind=symbol.kind,
            entrypoint=symbol.entrypoint,
            entrypoint_basis=symbol.entrypoint_basis,
        )
    for handler in report.handlers:
        identity = "handler:" + handler.id
        node(
            identity,
            "except " + ", ".join(handler.types),
            "handler",
            handler.symbol,
            handler.location,
            outcomes=handler.outcomes,
            conditional=handler.conditional,
        )
        anchor(handler.symbol, handler.location, identity)
        edge(
            "scope:" + handler.id,
            handler.symbol,
            identity,
            "scope",
            "contains handler (not execution order)",
            handler.location,
        )
    for call in report.calls:
        if call.boundary or call.discarded_task:
            identity = "call:" + call.id
            node(
                identity,
                call.resolved_name or call.expression,
                "task" if call.discarded_task else "boundary",
                call.symbol,
                call.location,
                boundary=call.boundary,
                execution=call.execution,
                expression=call.expression,
            )
            anchor(call.symbol, call.location, identity)
    for log in report.logs:
        identity = "handler:" + log.handler if log.handler else log.symbol
        if identity not in nodes:
            identity = log.symbol
        if identity in nodes:
            nodes[identity]["existing"].append(
                {
                    "kind": "log",
                    "level": log.level,
                    "location": asdict(log.location),
                    "text": f"{log.level} log"
                    + (" with exception context" if log.exception_context else ""),
                    "conditional": log.conditional,
                }
            )
            anchor(log.symbol, log.location, identity)
    for signal in report.reporting_signals:
        identity = "handler:" + signal.handler if signal.handler else signal.symbol
        if identity in nodes:
            nodes[identity]["existing"].append(
                {
                    "kind": signal.kind,
                    "level": None,
                    "location": asdict(signal.location),
                    "text": f"Configured {signal.kind} reporter owned by {signal.owner}",
                    "conditional": signal.conditional,
                    "basis": signal.basis,
                }
            )
    for call in report.calls:
        call_node = "call:" + call.id
        destination = call_node if call_node in nodes else call.target
        if destination:
            deferred = call.execution.startswith("deferred_")
            kind = "deferred" if deferred else "call"
            label = {
                "awaited": "await",
                "deferred_coroutine": "creates coroutine",
                "deferred_generator": "creates generator",
                "implicit_property": "property getter",
            }.get(call.execution, "call")
            if call.resolution == "inferred_constructor":
                label = "possible initializer"
            edge(
                call.id,
                call.symbol,
                destination,
                kind,
                f"{label} at L{call.location.line}",
                call.location,
                resolution=call.resolution,
                execution=call.execution,
                expression=call.expression,
            )
        if call_node in nodes and call.target:
            edge(
                call.id + ":implementation",
                call_node,
                call.target,
                kind,
                "resolved implementation",
                call.location,
                resolution=call.resolution,
                execution=call.execution,
            )
        if call.traced and call.symbol in nodes:
            trace_owner = call_node if call_node in nodes else call.symbol
            nodes[trace_owner]["existing"].append(
                {
                    "kind": "trace",
                    "level": None,
                    "location": asdict(call.location),
                    "text": "Call inside a recognized trace scope",
                    "conditional": False,
                }
            )
        if call.resolution in {"unresolved", "ambiguous"} and call.symbol in nodes:
            owner = nodes[call.symbol]
            owner["unresolved_count"] += 1
            if len(owner["unresolved_examples"]) < 12:
                owner["unresolved_examples"].append(
                    {
                        "expression": call.expression,
                        "location": asdict(call.location),
                        "resolution": call.resolution,
                    }
                )
        # Lexical handlers are candidates, never asserted exception propagation.
        # A coroutine/generator creation edge cannot transfer its later body failures.
        if destination and not call.execution.startswith("deferred_"):
            for group in call.handler_groups:
                for handler_id in group:
                    edge(
                        f"exception:{call.id}:{handler_id}",
                        destination,
                        "handler:" + handler_id,
                        "exception",
                        f"handler candidate for call at L{call.location.line}",
                        call.location,
                        caller=call.symbol,
                    )
                # Only the nearest lexical try is drawn; do not jump over recovery.
                if group:
                    break
    findings = {}
    for finding in report.findings:
        findings[finding.id] = asdict(finding)
        location = finding.evidence[0].location
        identity = anchors.get(
            (finding.symbol, location.line, location.column), finding.symbol
        )
        if identity in nodes:
            nodes[identity]["findings"].append(finding.id)
    choices = [node for node in nodes.values() if node["kind"] == "symbol"]
    choices.sort(
        key=lambda item: (not item["entrypoint"], item["qualified_name"], item["id"])
    )
    if focus:
        matching = [
            item for item in choices if focus in {item["id"], item["qualified_name"]}
        ]
        if len(matching) != 1:
            raise ValueError(
                "diagram-focus must identify exactly one symbol by ID or qualified name"
            )
        initial = matching[0]["id"]
    else:
        ranked = sorted(
            choices,
            key=lambda item: (
                not item["entrypoint"],
                not bool(item["findings"]),
                item["symbol_kind"] == "module",
                item["qualified_name"],
                item["id"],
            ),
        )
        # Prefer a known beginning of a finding path to show its caller context.
        initial = next((item["id"] for item in ranked if item["entrypoint"]), None)
        if initial is None:
            initial = next(
                (
                    path[0]
                    for finding in report.findings
                    for path in finding.paths
                    if path and path[0] in nodes
                ),
                None,
            )
        initial = initial or (ranked[0]["id"] if ranked else None)
    return {
        "schema_version": "flowsignal-diagram-1",
        "root": report.root,
        "nodes": list(nodes.values()),
        "edges": edges,
        "findings": findings,
        "focus": initial,
        "max_nodes": max_nodes,
        "summary": report.summary(),
        "diagnostics": [asdict(item) for item in report.diagnostics],
        "limitations": report.limitations,
        "baseline": report.baseline,
    }


def select_view(
    data: dict,
    focus: str | None = None,
    max_nodes: int | None = None,
    include_callers: bool = True,
) -> dict:
    """A bounded neighborhood, with local instrumentation kept beside each symbol."""
    focus = focus or data["focus"]
    maximum = max_nodes if max_nodes is not None else data["max_nodes"]
    if not 1 <= maximum <= 200:
        raise ValueError("diagram-max-nodes must be between 1 and 200")
    by_id = {node["id"]: node for node in data["nodes"]}
    if focus is None:
        return {"nodes": [], "edges": [], "omitted": 0, "outside": 0}
    if focus not in by_id:
        raise ValueError("Diagram focus is not present in the report")
    outgoing, incoming, owned = defaultdict(list), defaultdict(list), defaultdict(list)
    for node in data["nodes"]:
        if node["kind"] != "symbol":
            owned[node["symbol"]].append(node["id"])
    for edge in data["edges"]:
        source, target = by_id[edge["source"]], by_id[edge["target"]]
        if edge["kind"] in {"call", "deferred"} and target["kind"] == "symbol":
            owner = source["symbol"]
            outgoing[owner].append(target["id"])
            incoming[target["id"]].append(owner)
    reached, order, pending = set(), [], deque([focus])
    while pending:
        identity = pending.popleft()
        if identity in reached:
            continue
        reached.add(identity)
        order.append(identity)
        for child in owned[identity]:
            if child not in reached:
                reached.add(child)
                order.append(child)
        adjacent = outgoing[identity] + (incoming[identity] if include_callers else [])
        pending.extend(sorted(set(adjacent) - reached))
    chosen = set(order[:maximum])
    return {
        "nodes": [by_id[identity] for identity in order[:maximum]],
        "edges": [
            edge
            for edge in data["edges"]
            if edge["source"] in chosen and edge["target"] in chosen
        ],
        "omitted": max(0, len(order) - maximum),
        "outside": len(by_id) - len(order),
    }


def _mermaid_text(text: str) -> str:
    # Mermaid's decimal entities preserve text without permitting markup/directives.
    return "".join(
        character
        if character.isascii() and (character.isalnum() or character in " .,:_/-()")
        else f"#{ord(character)};"
        for character in text
    )


def render_mermaid(
    report: Report, focus: str | None = None, max_nodes: int = 60
) -> str:
    data = graph_data(report, focus, max_nodes)
    view = select_view(data)
    lines = [
        "flowchart LR",
        f"    %% Scan status: {data['summary']['status']}; completion does not establish complete coverage.",
        "    %% Possible static paths; signal presence does not prove coverage.",
        "    %% Solid: calls. Dotted: deferred execution or candidate handlers.",
        f"    %% {len(view['nodes'])} shown; {view['omitted']} omitted by view limit; {view['outside']} outside this neighborhood.",
    ]
    if data["summary"]["status"] == "incomplete":
        lines.append(
            '    scan_status["INCOMPLETE SCAN: review diagnostics before interpreting missing findings"]:::suggested'
        )
    if report.baseline:
        lines.append(
            "    %% Baseline changes: "
            + json.dumps(report.baseline["counts"], sort_keys=True)
        )
    for node in view["nodes"]:
        labels = [
            node["title"],
            f"{node['location']['file']}:{node['location']['line']}",
        ]
        if node["existing"]:
            labels.append(
                "EXISTING: "
                + ", ".join(
                    sorted(
                        {
                            signal["level"] or signal["kind"].upper()
                            for signal in node["existing"]
                        }
                    )
                )
            )
        if node["findings"]:
            states = {
                data["findings"][identity]["review_status"]
                for identity in node["findings"]
            }
            labels.append("Review state: " + ", ".join(sorted(states)))
            suggestions = sorted(
                {
                    recommendation["level"] or recommendation["kind"].replace("_", " ")
                    for finding_id in node["findings"]
                    for recommendation in data["findings"][finding_id][
                        "recommendations"
                    ]
                }
            )
            labels.append("REVIEW: " + ", ".join(suggestions))
        if node["unresolved_count"]:
            labels.append(f"{node['unresolved_count']} unresolved calls")
        identity = "n" + stable_id(node["id"])
        style = (
            "mixed"
            if node["existing"] and node["findings"]
            else "suggested"
            if node["findings"]
            else "existing"
            if node["existing"]
            else "neutral"
        )
        label = "<br/>".join(_mermaid_text(part) for part in labels)
        lines.append(f'    {identity}["{label}"]:::{style}')
    for edge in view["edges"]:
        source, target = (
            "n" + stable_id(edge["source"]),
            "n" + stable_id(edge["target"]),
        )
        connector = "-->" if edge["kind"] == "call" else "-.->"
        lines.append(
            f'    {source} {connector}|"{_mermaid_text(edge["label"])}"| {target}'
        )
    lines.extend(
        [
            "    classDef existing fill:#e2f5ef,stroke:#14735a,color:#173d32",
            "    classDef suggested fill:#fff3d9,stroke:#a36516,color:#573812",
            "    classDef mixed fill:#e2f5ef,stroke:#a36516,stroke-width:3px,color:#173d32",
            "    classDef neutral fill:#f1f4f8,stroke:#74849b,color:#29394f",
        ]
    )
    # Conditions must travel with a standalone diagram; levels are alternatives,
    # not simultaneous instructions to log a single outcome at multiple levels.
    for node in view["nodes"]:
        for identity in node["findings"]:
            finding = data["findings"][identity]
            if finding["review_reason"]:
                reason = f"{finding['rule_id']} {finding['review_status']}: {finding['review_reason']}"
                lines.append("    %% " + reason.replace("\n", " ").replace("\r", " "))
            for recommendation in finding["recommendations"]:
                description = f"{finding['rule_id']} at {node['location']['file']}:{node['location']['line']}: {recommendation['level'] or recommendation['kind']} WHEN {recommendation['condition']}"
                lines.append(
                    "    %% " + description.replace("\n", " ").replace("\r", " ")
                )
    return "\n".join(lines) + "\n"


def render_html(report: Report, focus: str | None = None, max_nodes: int = 60) -> str:
    payload = json.dumps(
        graph_data(report, focus, max_nodes), ensure_ascii=True, separators=(",", ":")
    )
    payload = (
        payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )
    template = (
        files("flowsignal")
        .joinpath("templates/report.html")
        .read_text(encoding="utf-8")
    )
    return template.replace("__FLOW_DATA__", payload)
