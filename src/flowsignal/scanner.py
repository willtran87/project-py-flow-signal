"""Bounded AST collection. Target repositories are never imported or executed."""

from __future__ import annotations

import ast
import builtins
import fnmatch
import hashlib
import io
import os
import stat
import tokenize
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_EXCLUDES, Config, matches
from .model import (
    Call,
    CoverageDecision,
    CoverageEvidence,
    Diagnostic,
    Evidence,
    Handler,
    Location,
    Log,
    Report,
    ReportingSignal,
    ResolutionGap,
    Symbol,
    stable_id,
)
from .receivers import ReceiverIndex
from .source_io import is_link, read_snapshot

BUILTIN_NAMES = frozenset(dir(builtins))


def dotted(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted(node.value)
        return f"{parent}.{node.attr}" if parent else ""
    return ""


def substitute(name: str, bindings: dict[str, str]) -> str:
    head, sep, tail = name.partition(".")
    if head in bindings:
        value = bindings[head]
        return value + (sep + tail if sep else "") if value else ""
    return name


def annotation_type(node: ast.AST | None, bindings: dict[str, str]) -> str:
    """Resolve one possible receiver type, never choose between concrete union arms."""

    def alternatives(value: ast.AST | None, depth: int = 0) -> set[str]:
        if depth > 32:
            return {""}
        if isinstance(value, ast.Constant):
            if value.value is None:
                return {"<none>"}
            if isinstance(value.value, str):
                try:
                    parsed = ast.parse(value.value, mode="eval").body
                except (SyntaxError, RecursionError):
                    return {""}
                return alternatives(parsed, depth + 1)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
            return alternatives(value.left, depth + 1) | alternatives(
                value.right, depth + 1
            )
        if isinstance(value, ast.Subscript):
            wrapper = substitute(dotted(value.value), bindings)
            if wrapper in {"typing.Optional", "typing_extensions.Optional"}:
                return alternatives(value.slice, depth + 1) | {"<none>"}
            if wrapper in {"typing.Union", "typing_extensions.Union"}:
                arms = (
                    value.slice.elts
                    if isinstance(value.slice, ast.Tuple)
                    else [value.slice]
                )
                return set().union(*(alternatives(arm, depth + 1) for arm in arms))
        return {substitute(dotted(value), bindings)}

    choices = alternatives(node) - {"<none>"}
    return next(iter(choices)) if len(choices) == 1 else ""


def outcomes(statements: list[ast.stmt]) -> set[str]:
    """Syntactic terminal outcomes; fallthrough explicitly preserves uncertainty."""
    result = {"fallthrough"}
    for statement in statements:
        if "fallthrough" not in result:
            break
        result.remove("fallthrough")
        if isinstance(statement, ast.Raise):
            current = {"raise"}
        elif isinstance(statement, ast.Return):
            current = {"return"}
        elif isinstance(statement, (ast.Break, ast.Continue)):
            current = {type(statement).__name__.lower()}
        elif isinstance(statement, ast.If):
            truth = literal_truth(statement.test)
            current = (
                outcomes(statement.body)
                if truth is True
                else outcomes(statement.orelse)
                if truth is False
                else outcomes(statement.body) | outcomes(statement.orelse)
            )
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            final = outcomes(statement.finalbody)
            branch = outcomes(statement.body)
            current = (branch - {"fallthrough"}) | (
                outcomes(statement.orelse) if "fallthrough" in branch else set()
            )
            for handler in statement.handlers:
                current |= outcomes(handler.body)
            current = (current if "fallthrough" in final else set()) | (
                final - {"fallthrough"}
            )
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            # A context manager can suppress an exception raised by its body.
            current = outcomes(statement.body)
            if "raise" in current:
                current |= {"fallthrough"}
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            current = (
                {"fallthrough"}
                | (outcomes(statement.body) & {"return", "raise"})
                | outcomes(statement.orelse)
            )
        else:
            current = {"fallthrough"}
        result |= current
    return result


def literal_truth(node: ast.AST) -> bool | None:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = literal_truth(node.operand)
        return None if value is None else not value
    return None


def contains_yield(body: list[ast.stmt]) -> bool:
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            pending.extend(ast.iter_child_nodes(node))
    return False


@dataclass
class Unit:
    path: str
    module: str
    source: str
    tree: ast.Module
    bindings: dict[str, str] = field(default_factory=dict)
    references: set[str] = field(default_factory=set)


@dataclass
class Definition:
    symbol: Symbol
    node: ast.AST
    unit: Unit
    scopes: list[str]
    class_name: str | None = None
    parent: Definition | None = None


@dataclass
class Analysis:
    config: Config
    units: list[Unit] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    logs: list[Log] = field(default_factory=list)
    reporting_signals: list[ReportingSignal] = field(default_factory=list)
    coverage: list[CoverageDecision] = field(default_factory=list)
    resolution_gaps: list[ResolutionGap] = field(default_factory=list)
    handlers: dict[str, Handler] = field(default_factory=dict)
    finalizers: list[tuple[str, Location, list[str]]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    by_name: dict[str, list[Symbol]] = field(default_factory=lambda: defaultdict(list))
    classes: set[str] = field(default_factory=set)
    class_nodes: dict[str, list[ast.ClassDef]] = field(
        default_factory=lambda: defaultdict(list)
    )
    class_units: dict[str, Unit] = field(default_factory=dict)
    receiver_index: ReceiverIndex | None = None
    source_lines: dict[str, list[str]] = field(default_factory=dict)
    binding_cache: dict[str, dict[str, str]] = field(default_factory=dict)
    reference_cache: dict[str, set[str]] = field(default_factory=dict)
    ambiguous_modules: set[str] = field(default_factory=set)
    inventory: list[dict] = field(default_factory=list)
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "source_bytes": 0,
            "ast_nodes": 0,
            "graph_steps": 0,
            "type_steps": 0,
            "resolution_context_steps": 0,
            "discovery_entries": 0,
        }
    )

    def evidence(self, location: Location, fact: str) -> Evidence:
        if location.file not in self.source_lines:
            unit = next(unit for unit in self.units if unit.path == location.file)
            self.source_lines[location.file] = unit.source.splitlines()
        lines = self.source_lines[location.file]
        code = (
            lines[location.line - 1].strip()[:300]
            if 0 < location.line <= len(lines)
            else ""
        )
        return Evidence(location, code if self.config.include_source else "", fact)

    def graph_step(self, amount: int = 1) -> bool:
        if self.stats["graph_steps"] + amount > self.config.max_graph_steps:
            if not any(item.code == "graph_work_limit" for item in self.diagnostics):
                self.diagnostics.append(
                    Diagnostic(
                        "graph_work_limit",
                        "Graph work budget reached; paths may be truncated and caller coverage is not credited beyond the limit.",
                    )
                )
            return False
        self.stats["graph_steps"] += amount
        return True


class BindingCollector(ast.NodeVisitor):
    """Keep agreed receiver types; conflicting or unknown bindings stay unresolved."""

    def __init__(
        self,
        unit: Unit,
        base: dict[str, str],
        classes: set[str],
        scope: str | None = None,
    ):
        self.unit, self.base, self.classes = unit, base, classes
        self.scope = scope or unit.module
        self.values: dict[str, list[str]] = defaultdict(list)
        self.references: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.references.add(alias.asname or alias.name.split(".")[0])
            self.values[alias.asname or alias.name.split(".")[0]].append(
                alias.name if alias.asname else alias.name.split(".")[0]
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        package = (
            self.unit.module.split(".")
            if self.unit.path.endswith("/__init__.py")
            or self.unit.path == "__init__.py"
            else self.unit.module.split(".")[:-1]
        )
        if node.level:
            prefix = package[: max(0, len(package) - node.level + 1)]
            module = ".".join(prefix + ([node.module] if node.module else []))
        else:
            module = node.module or ""
        for alias in node.names:
            if alias.name != "*":
                self.references.add(alias.asname or alias.name)
                self.values[alias.asname or alias.name].append(
                    ".".join(filter(None, [module, alias.name]))
                )

    def current(self) -> dict[str, str]:
        return self.base | {
            name: values[0] if len(set(values)) == 1 else ""
            for name, values in self.values.items()
        }

    def current_references(self, base: set[str]) -> set[str]:
        # Receiver types do not imply a class object: instance() may call __call__.
        return (base - self.values.keys()) | {
            name for name in self.references if len(self.values[name]) == 1
        }

    def value_type(self, value: ast.AST | None) -> str:
        if isinstance(value, ast.Name):
            name = self.current().get(value.id, "")
            return name if name in self.classes else ""
        if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
            choices = {self.value_type(arm) for arm in value.values}
            return next(iter(choices)) if len(choices) == 1 else ""
        if not isinstance(value, ast.Call):
            return ""
        name = substitute(dotted(value.func), self.current())
        if name in {"logging.getLogger", "logging.LoggerAdapter"}:
            return "logging.Logger"
        if name in {"structlog.get_logger", "structlog.getLogger"}:
            return "structlog.BoundLogger"
        if name in {"opentelemetry.trace.get_tracer", "trace.get_tracer"}:
            return "opentelemetry.trace.Tracer"
        if name in self.classes or f"{self.unit.module}.{name}" in self.classes:
            return name if name in self.classes else f"{self.unit.module}.{name}"
        if name and name.rsplit(".", 1)[-1][:1].isupper() and "." in name:
            return name
        return ""

    def visit_Assign(self, node: ast.Assign) -> None:
        value = self.value_type(node.value)
        for target in node.targets:
            for child in ast.walk(target):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    self.values[child.id].append(
                        value if isinstance(target, ast.Name) else ""
                    )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            annotation = annotation_type(node.annotation, self.current())
            self.values[node.target.id].append(
                self.value_type(node.value) or annotation
            )

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.values[node.target.id].append("")

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.values[node.id].append("")

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.values[node.name].append("")
        for statement in node.body:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.values[node.name].append(f"{self.scope}.{node.name}")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.values[node.name].append(f"{self.scope}.{node.name}")
        self.references.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        pass

    def visit_ListComp(self, node: ast.AST) -> None:
        # Comprehension targets have their own scope; assignment expressions can
        # bind in the enclosing scope and must conservatively invalidate a name.
        for child in ast.walk(node):
            if isinstance(child, ast.NamedExpr):
                self.visit(child.target)

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self.values[name].append(self.unit.bindings.get(name, ""))

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.values[node.name].append("")
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.values[node.name].append("")

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.values[node.rest].append("")
        self.generic_visit(node)


def scoped_bindings(analysis: Analysis, definition: Definition) -> dict[str, str]:
    """Honor enclosing function scopes before module globals, including shadows."""
    if definition.symbol.id in analysis.binding_cache:
        return dict(analysis.binding_cache[definition.symbol.id])
    base = definition.unit.bindings
    references = definition.unit.references
    if definition.parent is not None:
        base = scoped_bindings(analysis, definition.parent)
        references = analysis.reference_cache[definition.parent.symbol.id]
    collector = BindingCollector(
        definition.unit, base, analysis.classes, definition.symbol.qualified_name
    )
    if isinstance(definition.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = definition.node.args
        for arg in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *([arguments.vararg] if arguments.vararg else []),
            *([arguments.kwarg] if arguments.kwarg else []),
        ]:
            annotation = (
                None if arg in (arguments.vararg, arguments.kwarg) else arg.annotation
            )
            collector.values[arg.arg].append(annotation_type(annotation, base))
        positional = [*arguments.posonlyargs, *arguments.args]
        if (
            definition.class_name
            and positional
            and positional[0].arg in {"self", "cls"}
        ):
            collector.values[positional[0].arg] = [definition.class_name]
    for statement in definition.node.body:
        collector.visit(statement)
    bindings = collector.current()
    for name, value in list(bindings.items()):
        if f"{definition.unit.module}.{value}" in analysis.classes:
            bindings[name] = f"{definition.unit.module}.{value}"
    analysis.binding_cache[definition.symbol.id] = bindings
    analysis.reference_cache[definition.symbol.id] = collector.current_references(
        references
    )
    return dict(bindings)


def index_definitions(analysis: Analysis, unit: Unit) -> None:
    def walk(
        body: list[ast.stmt],
        names: list[str],
        scopes: list[str],
        class_name: str | None = None,
        parent: Definition | None = None,
    ) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join([unit.module, *names, node.name])
                symbol = Symbol(
                    f"{unit.path}:{'.'.join([*names, node.name])}",
                    qualname,
                    Location(unit.path, node.lineno, node.col_offset),
                    node.end_lineno or node.lineno,
                    "async_function"
                    if isinstance(node, ast.AsyncFunctionDef)
                    else "function",
                )
                if contains_yield(node.body):
                    symbol.kind = (
                        "async_generator"
                        if isinstance(node, ast.AsyncFunctionDef)
                        else "generator"
                    )
                definition = Definition(
                    symbol, node, unit, list(scopes), class_name, parent
                )
                analysis.definitions.append(definition)
                analysis.by_name[qualname].append(symbol)
                walk(
                    node.body,
                    [*names, node.name],
                    [qualname, *scopes],
                    parent=definition,
                )
            elif isinstance(node, ast.ClassDef):
                qualified = ".".join([unit.module, *names, node.name])
                analysis.classes.add(qualified)
                analysis.class_nodes[qualified].append(node)
                analysis.class_units[qualified] = unit
                walk(node.body, [*names, node.name], scopes, qualified, parent)
            else:
                # Definitions can be nested under if/try/with, without adding a scope.
                for _, value in ast.iter_fields(node):
                    if isinstance(value, list):
                        for child in value:
                            if isinstance(child, ast.ExceptHandler):
                                walk(child.body, names, scopes, class_name, parent)
                        walk(
                            [child for child in value if isinstance(child, ast.stmt)],
                            names,
                            scopes,
                            class_name,
                            parent,
                        )

    symbol = Symbol(
        f"{unit.path}:<module>",
        f"{unit.module}.<module>",
        Location(unit.path, 1),
        max(1, len(unit.source.splitlines())),
        "module",
    )
    analysis.definitions.append(Definition(symbol, unit.tree, unit, [unit.module]))
    analysis.by_name[symbol.qualified_name].append(symbol)
    walk(unit.tree.body, [], [unit.module])


BOUNDARIES = {
    "network": [
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.patch",
        "requests.delete",
        "requests.head",
        "requests.request",
        *[
            f"requests.Session.{method}"
            for method in (
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
                "request",
                "send",
            )
        ],
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "httpx.delete",
        "httpx.request",
        *[
            f"{client}.{method}"
            for client in ("httpx.Client", "httpx.AsyncClient", "aiohttp.ClientSession")
            for method in (
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
                "request",
                "send",
                "stream",
            )
        ],
        "urllib.request.urlopen",
        "socket.create_connection",
    ],
    "persistence": [
        "sqlite3.connect",
        "sqlite3.Connection.execute*",
        "sqlite3.Cursor.execute*",
        "psycopg.connect",
        "psycopg2.connect",
        "asyncpg.connect",
        "asyncpg.create_pool",
        "sqlalchemy.orm.Session.commit",
        "sqlalchemy.orm.Session.execute",
        "sqlalchemy.ext.asyncio.AsyncSession.commit",
        "sqlalchemy.ext.asyncio.AsyncSession.execute",
        "redis.Redis.get",
        "redis.Redis.set",
        "redis.Redis.execute_command",
    ],
    "serialization": [
        "json.load",
        "json.loads",
        "json.dump",
        "json.dumps",
        "yaml.safe_load",
        "yaml.safe_dump",
        "tomllib.load",
        "tomllib.loads",
        "pickle.load",
        "pickle.loads",
        "csv.reader",
        "csv.DictReader",
    ],
    "filesystem": [
        "builtins.open",
        "pathlib.Path.read_text",
        "pathlib.Path.read_bytes",
        "pathlib.Path.write_text",
        "pathlib.Path.write_bytes",
        "pathlib.Path.open",
        "pathlib.Path.unlink",
        "os.remove",
        "os.rename",
        "os.replace",
        "shutil.copy*",
        "shutil.move",
        "shutil.rmtree",
    ],
    "subprocess": [
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_call",
        "subprocess.check_output",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "os.system",
    ],
}


def boundary_for(name: str, config: Config) -> str | None:
    for record in config.boundaries:
        if fnmatch.fnmatchcase(name, record["pattern"]):
            return record["category"]
    for category, patterns in BOUNDARIES.items():
        if matches(name, patterns):
            # Constructing a client does not itself perform a network operation.
            if name.endswith((".__init__", ".close", ".aclose")):
                return None
            return category
    return None


class FactVisitor(ast.NodeVisitor):
    def __init__(self, analysis: Analysis, definition: Definition):
        self.analysis, self.definition = analysis, definition
        self.symbol, self.unit = definition.symbol, definition.unit
        self.groups: list[list[str]] = []
        self.handler: str | None = None
        self.conditional = 0
        self.handler_depth = 0
        self.traced = 0
        self.trace_failure_scopes = 0
        self.trace_evidence = []
        self.propagation_uncertain = False
        self.blocked_handler_ids: set[str] = set()
        self.parents: list[ast.AST] = []
        self.bindings = (
            dict(self.unit.bindings)
            if isinstance(definition.node, ast.Module)
            else scoped_bindings(analysis, definition)
        )
        self.references = (
            self.unit.references
            if isinstance(definition.node, ast.Module)
            else analysis.reference_cache[definition.symbol.id]
        )
        self.receiver_scope = analysis.receiver_index.scope(definition)
        self.property_shadows: set[str] = set()

    def location(self, node: ast.AST) -> Location:
        return Location(
            self.unit.path, getattr(node, "lineno", 1), getattr(node, "col_offset", 0)
        )

    def resolve(self, node: ast.AST) -> tuple[str, str | None, str]:
        raw = dotted(node)
        if isinstance(node, (ast.Name, ast.Call, ast.Attribute)) and not any(
            isinstance(part, ast.Name) and part.id in self.property_shadows
            for part in ast.walk(node)
        ):
            value = self.receiver_scope.type_of(node)
            if value and value.name.startswith("<callable>:"):
                definition = self.analysis.receiver_index.definitions[
                    value.name.removeprefix("<callable>:")
                ]
                qualified = definition.symbol.qualified_name
                if any(
                    qualified.startswith(m + ".")
                    for m in self.analysis.ambiguous_modules
                ):
                    return qualified, None, "ambiguous"
                if isinstance(node, ast.Attribute):
                    return qualified, definition.symbol.id, "inferred_receiver"
                if not (
                    isinstance(node, ast.Name)
                    and substitute(raw, self.bindings) == qualified
                ):
                    return qualified, definition.symbol.id, "inferred_callable"
        if isinstance(node, ast.Attribute) and not any(
            isinstance(part, ast.Name) and part.id in self.property_shadows
            for part in ast.walk(node.value)
        ):
            method = self.analysis.receiver_index.method(node, self.receiver_scope)
            if method:
                return (
                    method.symbol.qualified_name,
                    method.symbol.id,
                    "inferred_receiver",
                )
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
            constructor, _, _ = self.resolve(node.value.func)
            if constructor == "pathlib.Path" or constructor in self.analysis.classes:
                raw = constructor + "." + node.attr
        name = substitute(raw, self.bindings)
        if not name:
            for scope in self.definition.scopes:
                candidate = f"{scope}.{raw}"
                if len(self.analysis.by_name.get(candidate, [])) > 1:
                    return candidate, None, "ambiguous"
            return raw, None, "unresolved"
        candidates = [name]
        if raw.split(".")[0] not in self.bindings:
            candidates = [f"{scope}.{name}" for scope in self.definition.scopes] + [
                name
            ]
        for candidate in candidates:
            if any(
                candidate.startswith(module + ".")
                for module in self.analysis.ambiguous_modules
            ):
                return candidate, None, "ambiguous"
            symbols = self.analysis.by_name.get(candidate, [])
            if len(symbols) == 1:
                resolution = (
                    "inferred_receiver"
                    if raw.startswith(("self.", "cls."))
                    or raw.split(".")[0] != candidate.split(".")[0]
                    and "." in raw
                    else "lexical"
                )
                return candidate, symbols[0].id, resolution
            if len(symbols) > 1:
                return candidate, None, "ambiguous"
            classes = self.analysis.class_nodes.get(candidate, [])
            if len(classes) > 1:
                return candidate, None, "ambiguous"
            if classes and raw.split(".")[0] in self.references:
                initializers = self.analysis.by_name.get(candidate + ".__init__", [])
                if len(classes) > 1 or len(initializers) > 1:
                    return candidate, None, "ambiguous"
                cls = classes[0]
                customized = (
                    cls.decorator_list
                    or cls.keywords
                    or any(
                        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and (
                            item.name == "__new__"
                            or item.name == "__init__"
                            and item.decorator_list
                        )
                        for item in cls.body
                    )
                )
                if (
                    initializers
                    and not customized
                    and initializers[0].kind == "function"
                ):
                    return candidate, initializers[0].id, "inferred_constructor"
        if name in BUILTIN_NAMES and name not in self.bindings:
            return "builtins." + name, None, "builtin"
        if name.rpartition(".")[0] in self.analysis.classes or (
            "." in raw and self.bindings.get(raw.split(".")[0]) in self.analysis.classes
        ):
            return name, None, "unresolved"
        if raw.split(".")[0] in self.bindings and self.bindings[raw.split(".")[0]]:
            return name, None, "import_or_annotation"
        if name.startswith("pathlib.Path."):
            return name, None, "inferred_receiver"
        return name, None, "unresolved"

    def visit(self, node: ast.AST):
        self.parents.append(node)
        try:
            return super().visit(node)
        finally:
            self.parents.pop()

    def block(self, body: list[ast.stmt]) -> None:
        initial_depth = self.conditional
        try:
            for statement in body:
                self.visit(statement)
                possible = outcomes([statement])
                if "fallthrough" not in possible:
                    break
                if possible - {"fallthrough"}:
                    self.conditional += 1
        finally:
            self.conditional = initial_depth

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Defaults and decorators execute in the enclosing scope; bodies have their own symbols.
        for expression in [
            *node.decorator_list,
            *node.args.defaults,
            *[value for value in node.args.kw_defaults if value is not None],
        ]:
            self.visit(expression)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in [
            *node.decorator_list,
            *node.bases,
            *[keyword.value for keyword in node.keywords],
        ]:
            self.visit(expression)
        previous = self.bindings
        previous_references = self.references
        previous_shadows = set(self.property_shadows)
        collector = BindingCollector(
            self.unit,
            previous,
            self.analysis.classes,
            self.unit.module + "." + node.name,
        )
        for statement in node.body:
            collector.visit(statement)
        self.bindings = collector.current()
        self.references = collector.current_references(previous_references)
        self.property_shadows.update(collector.values)
        try:
            self.block(node.body)
        finally:
            self.bindings = previous
            self.references = previous_references
            self.property_shadows = previous_shadows

    def visit_ListComp(self, node: ast.ListComp | ast.SetComp | ast.DictComp) -> None:
        previous = self.bindings
        previous_shadows = set(self.property_shadows)
        self.bindings = dict(previous)
        initial_depth = self.conditional
        try:
            for generator in node.generators:
                self.visit(generator.iter)
                for target in ast.walk(generator.target):
                    if isinstance(target, ast.Name):
                        self.bindings[target.id] = ""
                        self.property_shadows.add(target.id)
                self.conditional += 1
                for condition in generator.ifs:
                    self.visit(condition)
            if isinstance(node, ast.DictComp):
                self.visit(node.key)
                self.visit(node.value)
            else:
                self.visit(node.elt)
        finally:
            self.bindings, self.conditional = previous, initial_depth
            self.property_shadows = previous_shadows

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in [
            *node.args.defaults,
            *[value for value in node.args.kw_defaults if value is not None],
        ]:
            self.visit(expression)
        self.analysis.diagnostics.append(
            Diagnostic(
                "deferred_lambda",
                "Lambda body is not resolved into a callable symbol.",
                self.unit.path,
                node.lineno,
            )
        )

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        if node.generators:
            self.visit(node.generators[0].iter)
        self.analysis.diagnostics.append(
            Diagnostic(
                "deferred_generator",
                "Only the eager outer iterable of this generator expression is scanned.",
                self.unit.path,
                node.lineno,
            )
        )

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        truth = literal_truth(node.test)
        if truth is not None:
            self.block(node.body if truth else node.orelse)
            return
        self.conditional += 1
        self.block(node.body)
        self.block(node.orelse)
        self.conditional -= 1

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.visit(node.test)
        truth = literal_truth(node.test)
        self.conditional += 1
        if truth is not False:
            self.visit(node.body)
        if truth is not True:
            self.visit(node.orelse)
        self.conditional -= 1

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        for index, value in enumerate(node.values):
            self.conditional += int(index > 0)
            self.visit(value)
            self.conditional -= int(index > 0)
            truth = literal_truth(value)
            if (
                isinstance(node.op, ast.And)
                and truth is False
                or isinstance(node.op, ast.Or)
                and truth is True
            ):
                break

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self.conditional += 1
        self.block(node.body)
        self.block(node.orelse)
        self.conditional -= 1

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self.conditional += 1
        if literal_truth(node.test) is not False:
            self.block(node.body)
        self.block(node.orelse)
        self.conditional -= 1

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        self.conditional += 1
        for case in node.cases:
            if case.guard:
                self.visit(case.guard)
            self.block(case.body)
        self.conditional -= 1

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        previous = (
            self.traced,
            self.trace_failure_scopes,
            self.propagation_uncertain,
            self.blocked_handler_ids,
            self.trace_evidence,
        )
        self.trace_evidence = list(self.trace_evidence)
        try:
            # Multiple with-items nest left to right, including evaluation of
            # later context expressions inside previously entered contexts.
            for item in node.items:
                self.visit(item.context_expr)
                name, resolution = "", "unresolved"
                if isinstance(item.context_expr, ast.Call):
                    name, _, resolution = self.resolve(item.context_expr.func)
                known = resolution not in {"unresolved", "ambiguous"}
                traced = (
                    known
                    and name.startswith("opentelemetry.")
                    and name.endswith((".start_as_current_span", ".start_span"))
                )
                non_suppressing = (
                    traced
                    or known
                    and name
                    in {
                        "builtins.open",
                        "io.open",
                        "pathlib.Path.open",
                        "contextlib.nullcontext",
                    }
                )
                if not non_suppressing:
                    self.propagation_uncertain = True
                    self.blocked_handler_ids = self.blocked_handler_ids | {
                        identity for group in self.groups for identity in group
                    }
                    # Outer spans may never receive a suppressed exception.
                    self.trace_failure_scopes = 0
                    self.trace_evidence = [
                        CoverageEvidence(
                            e.location,
                            e.symbol,
                            "outer_trace_blocked",
                            "An inner context manager may suppress the exception before this span sees it.",
                        )
                        if e.credited
                        else e
                        for e in self.trace_evidence
                    ]
                    self.trace_evidence.append(
                        CoverageEvidence(
                            Location(
                                self.unit.path,
                                item.context_expr.lineno,
                                item.context_expr.col_offset,
                            ),
                            self.symbol.id,
                            "unknown_context",
                            "Exception suppression by this context manager is unknown.",
                        )
                    )
                    self.analysis.diagnostics.append(
                        Diagnostic(
                            "context_manager_propagation",
                            "Context-manager exception suppression is unknown; outer handlers and spans are not credited across this scope.",
                            self.unit.path,
                            item.context_expr.lineno,
                        )
                    )
                self.traced += int(traced)
                enabled = False
                if traced and name.endswith(".start_as_current_span"):
                    options = {
                        keyword.arg: keyword.value
                        for keyword in item.context_expr.keywords
                    }
                    enabled = all(
                        key not in options
                        or isinstance(options[key], ast.Constant)
                        and options[key].value is True
                        for key in ("record_exception", "set_status_on_exception")
                    )
                    self.trace_failure_scopes += int(enabled and None not in options)
                    enabled = enabled and None not in options
                if traced:
                    self.trace_evidence.append(
                        CoverageEvidence(
                            Location(
                                self.unit.path,
                                item.context_expr.lineno,
                                item.context_expr.col_offset,
                            ),
                            self.symbol.id,
                            "trace_enabled" if enabled else "trace_not_credited",
                            "Span options support exception recording and error status."
                            if enabled
                            else "This span is not a current exception-recording scope, or its failure options are disabled or unknown.",
                            enabled,
                        )
                    )
            self.block(node.body)
        finally:
            (
                self.traced,
                self.trace_failure_scopes,
                self.propagation_uncertain,
                self.blocked_handler_ids,
                self.trace_evidence,
            ) = previous

    visit_AsyncWith = visit_With

    def visit_Try(self, node: ast.Try | ast.TryStar) -> None:
        group: list[str] = []
        for handler in node.handlers:
            identity = stable_id(
                self.symbol.id, "handler", handler.lineno, handler.col_offset
            )
            types = (
                handler.type.elts
                if isinstance(handler.type, ast.Tuple)
                else [handler.type]
            )
            names = [
                substitute(dotted(value), self.bindings)
                or ("*" if value is None else "<dynamic>")
                for value in types
            ]
            record = Handler(
                identity,
                self.symbol.id,
                self.location(handler),
                names,
                sorted(outcomes(handler.body)),
                conditional=self.conditional > 0,
            )
            self.analysis.handlers[identity] = record
            group.append(identity)
        self.groups.insert(0, group)
        self.block(node.body)
        self.groups.pop(0)
        for handler, identity in zip(node.handlers, group):
            previous, depth = self.handler, self.handler_depth
            self.handler, self.handler_depth = identity, self.conditional
            self.block(handler.body)
            from .reporting_paths import analyze

            analyze(self.analysis, self.analysis.handlers[identity], handler.body)
            self.handler, self.handler_depth = previous, depth
        self.block(node.orelse)
        self.block(node.finalbody)
        terminal = sorted(outcomes(node.finalbody) & {"return", "break", "continue"})
        if terminal:
            self.analysis.finalizers.append(
                (self.symbol.id, self.location(node.finalbody[0]), terminal)
            )
        if isinstance(node, ast.TryStar):
            self.analysis.diagnostics.append(
                Diagnostic(
                    "exception_group",
                    "ExceptionGroup splitting is not resolved; handler coverage remains uncertain.",
                    self.unit.path,
                    node.lineno,
                )
            )
            for identity in group:
                self.analysis.handlers[identity].types = ["<exception-group>"]

    visit_TryStar = visit_Try

    def visit_Call(self, node: ast.Call) -> None:
        # Arguments and receiver expressions execute before the outer call.
        self.generic_visit(node)
        self.record_call(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.generic_visit(node)
        getter = self.analysis.receiver_index.getter(node, self.receiver_scope)
        # A comprehension target can shadow a typed outer variable.
        shadowed = any(
            isinstance(part, ast.Name) and part.id in self.property_shadows
            for part in ast.walk(node.value)
        )
        if getter and not shadowed:
            implicit = ast.copy_location(
                ast.Call(func=node, args=[], keywords=[]), node
            )
            self.record_call(implicit, getter)

    def record_call(self, node: ast.Call, getter: Symbol | None = None) -> None:
        name, target, resolution = self.resolve(node.func)
        if getter:
            name, target, resolution = (
                getter.qualified_name,
                getter.id,
                "inferred_property",
            )
        elif self.analysis.receiver_index.getter(node.func, self.receiver_scope):
            name, target, resolution = "<property-result>", None, "unresolved"
        location = self.location(node)
        parent = self.parents[-2] if len(self.parents) > 1 else None
        boundary = (
            boundary_for(name, self.analysis.config)
            if resolution not in {"unresolved", "ambiguous"}
            else next(
                (
                    rule["category"]
                    for rule in self.analysis.config.boundaries
                    if fnmatch.fnmatchcase(name, rule["pattern"])
                ),
                None,
            )
        )
        call = Call(
            stable_id(
                self.symbol.id,
                node.lineno,
                node.col_offset,
                node.end_lineno,
                node.end_col_offset,
            ),
            self.symbol.id,
            location,
            ast.unparse(node.func)[:240]
            if self.analysis.config.include_source
            else dotted(node.func) or "<dynamic-call>",
            name,
            target,
            resolution,
            boundary,
            [list(group) for group in self.groups],
            self.handler,
            bool(self.traced),
            resolution != "unresolved"
            and name in {"asyncio.create_task", "asyncio.ensure_future"}
            and isinstance(parent, ast.Expr),
        )
        if isinstance(parent, ast.Await):
            call.execution = "awaited"
        elif target and any(
            symbol.id == target and symbol.kind == "async_function"
            for symbol in self.analysis.by_name.get(name, [])
        ):
            call.execution = "deferred_coroutine"
        elif target and any(
            symbol.id == target and symbol.kind in {"generator", "async_generator"}
            for symbol in self.analysis.by_name.get(name, [])
        ):
            call.execution = "deferred_generator"
        if getter:
            call.execution = "implicit_property"
        call.trace_failure_coverage = bool(
            self.trace_failure_scopes
        ) and not call.execution.startswith("deferred_")
        call.propagation_uncertain = self.propagation_uncertain
        call.blocked_handler_ids = sorted(self.blocked_handler_ids)
        call.trace_evidence = list(self.trace_evidence)
        self.analysis.calls.append(call)
        if resolution in {"unresolved", "ambiguous"}:
            from .uncertainty import explain

            self.analysis.resolution_gaps.append(explain(self, node.func, call))
        if self.handler:
            self.analysis.handlers[self.handler].calls.append(call.id)
        if getter:
            return
        for reporter in self.analysis.config.reporters:
            expression_match = reporter.get("match") == "expression"
            candidate = dotted(node.func) if expression_match else name
            if not expression_match and resolution in {"unresolved", "ambiguous"}:
                continue
            if not fnmatch.fnmatchcase(candidate, reporter["pattern"]) or not matches(
                self.symbol.qualified_name, [reporter.get("scope", "*")]
            ):
                continue
            if reporter["kind"] == "error_return" and not isinstance(
                parent, ast.Return
            ):
                continue
            if reporter["kind"] == "stderr":
                if name == "builtins.print":
                    destination = next(
                        (kw.value for kw in node.keywords if kw.arg == "file"), None
                    )
                    dest_name, _, dest_resolution = self.resolve(destination)
                    if dest_name != "sys.stderr" or dest_resolution in {
                        "unresolved",
                        "ambiguous",
                    }:
                        continue
                elif name != "sys.stderr.write":
                    continue
            signal = ReportingSignal(
                self.symbol.id,
                location,
                self.handler,
                reporter["kind"],
                reporter["owner"],
                reporter["pattern"],
                "configured expression contract"
                if expression_match
                else "configured resolved API contract",
                self.conditional > self.handler_depth
                or call.execution.startswith("deferred_"),
                execution=call.execution,
            )
            self.analysis.reporting_signals.append(signal)
            if self.handler:
                self.analysis.handlers[self.handler].reporting_signals.append(signal)
            break
        level = None
        for custom in self.analysis.config.loggers:
            if fnmatch.fnmatchcase(name, custom["pattern"]):
                level = custom["level"]
                break
        raw = dotted(node.func)
        logger = (
            resolution != "unresolved"
            and name.startswith(("logging.", "structlog.", "loguru.logger."))
            or raw.rpartition(".")[0] in self.analysis.config.logger_names
        )
        method = name.rsplit(".", 1)[-1]
        if logger:
            level = {
                "debug": "DEBUG",
                "info": "INFO",
                "warning": "WARNING",
                "warn": "WARNING",
                "error": "ERROR",
                "exception": "ERROR",
                "critical": "CRITICAL",
                "fatal": "CRITICAL",
            }.get(method, level)
            if method == "log":
                argument = (
                    node.args[0]
                    if node.args
                    else next(
                        (
                            keyword.value
                            for keyword in node.keywords
                            if keyword.arg == "level"
                        ),
                        None,
                    )
                )
                constant_name = substitute(dotted(argument), self.bindings)
                level = (
                    {
                        10: "DEBUG",
                        20: "INFO",
                        30: "WARNING",
                        40: "ERROR",
                        50: "CRITICAL",
                    }.get(argument.value)
                    if isinstance(argument, ast.Constant)
                    else {
                        "logging.DEBUG": "DEBUG",
                        "logging.INFO": "INFO",
                        "logging.WARNING": "WARNING",
                        "logging.WARN": "WARNING",
                        "logging.ERROR": "ERROR",
                        "logging.CRITICAL": "CRITICAL",
                        "logging.FATAL": "CRITICAL",
                    }.get(constant_name)
                )
                if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                    level = None
                    self.analysis.diagnostics.append(
                        Diagnostic(
                            "dynamic_log_level",
                            "Log level could not be resolved; this event is not credited as failure reporting.",
                            self.unit.path,
                            node.lineno,
                        )
                    )
        if level:
            exc = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "exc_info"
                ),
                None,
            )
            exception_context = (
                method == "exception"
                if exc is None
                else isinstance(exc, ast.Constant) and bool(exc.value)
            )
            if isinstance(exc, ast.Call) and not exc.args and not exc.keywords:
                context_name, _, context_resolution = self.resolve(exc.func)
                exception_context = (
                    context_name == "sys.exc_info"
                    and context_resolution not in {"unresolved", "ambiguous"}
                )
            log = Log(
                self.symbol.id,
                location,
                level,
                exception_context,
                self.handler,
                self.conditional > self.handler_depth
                or call.execution.startswith("deferred_"),
                execution=call.execution,
            )
            self.analysis.logs.append(log)
            if self.handler:
                self.analysis.handlers[self.handler].logs.append(log)


def mark_entrypoints(analysis: Analysis) -> None:
    for definition in analysis.definitions:
        symbol, node, unit = definition.symbol, definition.node, definition.unit
        if matches(symbol.id, analysis.config.entrypoints) or matches(
            symbol.qualified_name, analysis.config.entrypoints
        ):
            symbol.entrypoint, symbol.entrypoint_basis = True, "configuration"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                expression = (
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                name = substitute(dotted(expression), unit.bindings)
                if matches(
                    name,
                    [
                        "fastapi.*.get",
                        "fastapi.*.post",
                        "fastapi.*.put",
                        "fastapi.*.delete",
                        "fastapi.*.patch",
                        "fastapi.*.websocket",
                        "flask.*.route",
                        "click.command",
                        "click.group",
                        "typer.Typer.command",
                        "celery.*.task",
                        "celery.shared_task",
                    ],
                ):
                    symbol.entrypoint, symbol.entrypoint_basis = (
                        True,
                        f"decorator:{name}",
                    )
        elif isinstance(node, ast.Module):
            if Path(unit.path).name == "__main__.py" or any(
                isinstance(child, ast.Compare)
                and isinstance(child.left, ast.Name)
                and child.left.id == "__name__"
                and any(
                    isinstance(value, ast.Constant) and value.value == "__main__"
                    for value in child.comparators
                )
                for child in ast.walk(node)
            ):
                symbol.entrypoint, symbol.entrypoint_basis = True, "module_main_guard"
    for pattern in analysis.config.entrypoints:
        if not any(
            matches(item.symbol.id, [pattern])
            or matches(item.symbol.qualified_name, [pattern])
            for item in analysis.definitions
        ):
            analysis.diagnostics.append(
                Diagnostic(
                    "entrypoint_not_found",
                    f"No symbol matches configured entrypoint {pattern!r}.",
                )
            )


def excluded(relative: str, config: Config) -> bool:
    parts = relative.split("/")
    return any(part in DEFAULT_EXCLUDES for part in parts) or any(
        fnmatch.fnmatchcase(relative, pattern)
        or any(fnmatch.fnmatchcase(part, pattern) for part in parts)
        for pattern in config.exclude
    )


def scan(
    path: str | Path,
    config: Config | None = None,
    *,
    cache_dir: str | Path | None = None,
) -> Report:
    config = config or Config()
    config.validate()
    supplied = Path(path).absolute()
    if not supplied.exists():
        raise ValueError(f"Scan target does not exist: {supplied}")
    if is_link(supplied):
        raise ValueError("Scan target must not be a symbolic link")
    if supplied.is_file() and supplied.suffix != ".py":
        raise ValueError("A file scan target must be a .py source file")
    root = supplied.parent if supplied.is_file() else supplied
    root_packages = []
    package_root = root
    while (package_root / "__init__.py").is_file():
        root_packages.insert(0, package_root.name)
        if package_root.parent == package_root:
            break
        package_root = package_root.parent
    analysis = Analysis(config)
    source_roots = []
    for configured in config.source_roots:
        candidate = root / configured
        if (
            not candidate.is_dir()
            or is_link(candidate)
            or not candidate.resolve().is_relative_to(root.resolve())
        ):
            analysis.diagnostics.append(
                Diagnostic(
                    "source_root_invalid",
                    f"Source root is missing, linked, or outside the scan: {configured}",
                )
            )
        else:
            source_roots.append(candidate.relative_to(root))
    source_roots.sort(key=lambda value: len(value.parts), reverse=True)

    def eligible(candidate: Path) -> bool:
        relative = candidate.relative_to(root).as_posix()
        try:
            reason = (
                "excluded"
                if excluded(relative, config)
                else "link_skipped"
                if is_link(candidate)
                else None
            )
            if reason:
                analysis.inventory.append({"path": relative, "status": reason})
                return False
            return True
        except OSError as error:
            analysis.inventory.append({"path": relative, "status": "unreadable"})
            analysis.diagnostics.append(
                Diagnostic("source_unreadable", str(error), relative)
            )
            return False

    paths: list[Path] = []
    if supplied.is_file():
        paths = [supplied]
    else:
        folders = [root]
        discovery_stopped = False
        while folders and not discovery_stopped and len(paths) <= config.max_files:
            folder = folders.pop()
            entries = []
            try:
                if is_link(folder) or not folder.resolve().is_relative_to(
                    root.resolve()
                ):
                    raise OSError(
                        "Directory changed to a link or resolves outside the scan root"
                    )
                # Bound non-Python entries as well, before sorting or retaining
                # an arbitrarily large directory listing in memory.
                with os.scandir(folder) as listing:
                    for entry in listing:
                        if (
                            analysis.stats["discovery_entries"]
                            >= config.max_discovery_entries
                        ):
                            discovery_stopped = True
                            analysis.diagnostics.append(
                                Diagnostic(
                                    "discovery_limit",
                                    "Filesystem discovery budget reached; remaining entries and directories were not enumerated.",
                                    folder.relative_to(root).as_posix(),
                                )
                            )
                            break
                        analysis.stats["discovery_entries"] += 1
                        entries.append(entry)
            except OSError as error:
                analysis.diagnostics.append(
                    Diagnostic("directory_unreadable", str(error), str(folder))
                )
            directories = []
            for entry in sorted(entries, key=lambda item: item.name):
                candidate = folder / entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if eligible(candidate):
                            directories.append(candidate)
                    elif entry.is_symlink():
                        eligible(
                            candidate
                        )  # Record a skipped link without traversing it.
                    elif candidate.suffix == ".py" and eligible(candidate):
                        paths.append(candidate)
                        if len(paths) > config.max_files:
                            break
                except OSError as error:
                    analysis.diagnostics.append(
                        Diagnostic(
                            "source_unreadable",
                            str(error),
                            candidate.relative_to(root).as_posix(),
                        )
                    )
            folders.extend(reversed(directories))
        if len(paths) > config.max_files:
            analysis.diagnostics.append(
                Diagnostic(
                    "file_limit",
                    f"Only the first {config.max_files} Python files are scanned.",
                )
            )
            paths = paths[: config.max_files]
    cache, snapshot_key = None, None
    if cache_dir is not None:
        from .cache import ScanCache, digest

        cache = ScanCache(cache_dir, config, root)
        cache.namespace = digest([cache.namespace, root_packages])
        if not analysis.diagnostics:
            try:
                snapshot_key = cache.snapshot_key(paths, root, config)
                if snapshot_key:
                    snapshot_key.extend(
                        [list(analysis.inventory), analysis.stats["discovery_entries"]]
                    )
                cached = cache.report(snapshot_key) if snapshot_key else None
                if cached is not None:
                    return cached
            except (OSError, ValueError):
                snapshot_key = None
    for source_path in paths:
        relative = source_path.relative_to(root).as_posix()
        record = {"path": relative, "status": "unreadable"}
        analysis.inventory.append(record)
        try:
            if not stat.S_ISREG(source_path.stat().st_mode):
                analysis.diagnostics.append(
                    Diagnostic(
                        "source_unreadable",
                        "Source is not a regular file; skipped.",
                        relative,
                    )
                )
                continue
            if source_path.stat().st_size > config.max_file_bytes:
                record["status"] = "file_size_limit"
                analysis.diagnostics.append(
                    Diagnostic(
                        "file_size_limit",
                        f"Source exceeds {config.max_file_bytes} bytes; skipped.",
                        relative,
                    )
                )
                continue
            remaining = config.max_total_bytes - analysis.stats["source_bytes"]
            if source_path.stat().st_size > remaining:
                record["status"] = "total_bytes_limit"
                analysis.diagnostics.append(
                    Diagnostic(
                        "total_bytes_limit",
                        "Aggregate source-byte budget reached; remaining files were not scanned.",
                        relative,
                    )
                )
                break
            raw = read_snapshot(
                source_path, min(config.max_file_bytes, remaining), root
            )
            analysis.stats["source_bytes"] += len(raw)
            record.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            source = raw.decode(encoding)
            tree = (
                cache.parse(source, relative, record["sha256"])
                if cache
                else ast.parse(source, filename=relative)
            )
            pending = [(tree, 0)]
            limit_hit = None
            while pending:
                current, depth = pending.pop()
                analysis.stats["ast_nodes"] += 1
                if analysis.stats["ast_nodes"] > config.max_ast_nodes:
                    limit_hit = "ast_nodes_limit"
                    break
                if depth > config.max_ast_depth:
                    limit_hit = "ast_depth_limit"
                    break
                pending.extend(
                    (child, depth + 1) for child in ast.iter_child_nodes(current)
                )
            if limit_hit:
                record["status"] = limit_hit
                analysis.diagnostics.append(
                    Diagnostic(
                        limit_hit,
                        "AST analysis budget reached; this file was not analyzed.",
                        relative,
                    )
                )
                if limit_hit == "ast_nodes_limit":
                    break
                continue
            module_parts = list(Path(relative).with_suffix("").parts)
            if cache:
                cache.store_tree(relative, record["sha256"], tree)
            matched_root = next(
                (
                    prefix
                    for prefix in source_roots
                    if Path(relative).is_relative_to(prefix)
                ),
                None,
            )
            if matched_root is not None:
                module_parts = list(
                    Path(relative).relative_to(matched_root).with_suffix("").parts
                )
            elif (
                not root_packages and module_parts[0] == "src" and len(module_parts) > 1
            ):
                module_parts.pop(0)
            if matched_root is None:
                module_parts = root_packages + module_parts
            if module_parts[-1] == "__init__":
                module_parts.pop()
            if not module_parts:
                module_parts = [root.name]
            unit = Unit(relative, ".".join(module_parts), source, tree)
            staging = Analysis(config)
            index_definitions(staging, unit)
            analysis.units.append(unit)
            analysis.definitions.extend(staging.definitions)
            analysis.classes.update(staging.classes)
            analysis.class_units.update(staging.class_units)
            for name, nodes in staging.class_nodes.items():
                analysis.class_nodes[name].extend(nodes)
            for name, symbols in staging.by_name.items():
                analysis.by_name[name].extend(symbols)
            record["status"] = "analyzed"
        except (
            SyntaxError,
            UnicodeError,
            OSError,
            ValueError,
            RecursionError,
        ) as error:
            analysis.diagnostics.append(
                Diagnostic(
                    "source_unreadable",
                    str(error),
                    relative,
                    getattr(error, "lineno", None),
                )
            )
    modules: dict[str, list[str]] = defaultdict(list)
    for unit in analysis.units:
        modules[unit.module].append(unit.path)
        try:
            collector = BindingCollector(unit, {}, analysis.classes)
            for statement in unit.tree.body:
                collector.visit(statement)
            unit.bindings = collector.current()
            unit.references = collector.current_references(set())
        except RecursionError:
            analysis.diagnostics.append(
                Diagnostic(
                    "analysis_depth",
                    "Module binding analysis exceeded the stack limit.",
                    unit.path,
                )
            )
    for module, source_files in modules.items():
        if len(source_files) > 1:
            analysis.ambiguous_modules.add(module)
            analysis.diagnostics.append(
                Diagnostic(
                    "duplicate_module",
                    f"Multiple files map to module {module!r}; calls into this namespace remain ambiguous: {', '.join(source_files)}",
                )
            )
    for symbols in analysis.by_name.values():
        if len(symbols) > 1:
            for symbol in symbols:
                symbol.id += f"@{symbol.location.line}"
    mark_entrypoints(analysis)
    analysis.receiver_index = ReceiverIndex(analysis)
    for definition in analysis.definitions:
        try:
            visitor = FactVisitor(analysis, definition)
            visitor.block(definition.node.body)
        except RecursionError:
            analysis.diagnostics.append(
                Diagnostic(
                    "analysis_depth",
                    "AST nesting exceeded the analysis stack; this symbol is only partially scanned.",
                    definition.unit.path,
                    definition.symbol.location.line,
                )
            )
    from .uncertainty import attach_entrypoints

    attach_entrypoints(analysis)
    from .rules import evaluate

    findings = evaluate(analysis)
    from .baseline import fingerprint_findings

    fingerprint_findings(analysis, findings)
    if not analysis.units:
        analysis.diagnostics.append(
            Diagnostic("no_sources", "No readable Python sources were scanned.")
        )
    report = Report(
        root=str(root),
        files_scanned=len(analysis.units),
        symbols=[item.symbol for item in analysis.definitions],
        calls=analysis.calls,
        logs=analysis.logs,
        handlers=list(analysis.handlers.values()),
        findings=findings,
        diagnostics=analysis.diagnostics,
        settings=config.to_dict(),
        inventory=analysis.inventory,
        analysis_stats=analysis.stats,
        reporting_signals=analysis.reporting_signals,
        coverage=analysis.coverage,
        resolution_gaps=analysis.resolution_gaps,
    )
    from .review_queue import attach

    attach(report)
    if cache:
        if (
            snapshot_key
            and report.summary()["status"] == "complete"
            and snapshot_key[1]
            == [
                [r["path"], r.get("sha256")]
                for r in report.inventory
                if r.get("status") == "analyzed"
            ]
        ):
            cache.flush_trees()
            cache.write(snapshot_key, report.to_dict())
        report.analysis_stats["cache"] = dict(cache.stats)
    return report
