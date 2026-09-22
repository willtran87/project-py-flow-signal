"""Bounded receiver evidence for explicit annotations, containers and property reads."""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class ReceiverType:
    name: str
    arguments: tuple[ReceiverType | None, ...] = ()


class ReceiverIndex:
    def __init__(self, analysis):
        self.analysis = analysis
        self.scopes = {}
        self.fields = {}
        self.properties = {}
        self.definitions = {d.symbol.id: d for d in analysis.definitions}
        self.modules = {
            d.unit.path: d
            for d in analysis.definitions
            if isinstance(d.node, ast.Module)
        }

    def qualified(self, node, bindings):
        from .scanner import dotted, substitute

        return substitute(dotted(node), bindings)

    def annotation(self, node, bindings, depth=0):
        if node is None or depth > 32:
            return None
        if isinstance(node, ast.Constant):
            if node.value is None:
                return ReceiverType("<none>")
            if isinstance(node.value, str):
                try:
                    node = ast.parse(node.value, mode="eval").body
                except (SyntaxError, RecursionError):
                    return None
                return self.annotation(node, bindings, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            choices = {
                self.annotation(node.left, bindings, depth + 1),
                self.annotation(node.right, bindings, depth + 1),
            } - {ReceiverType("<none>")}
            return next(iter(choices)) if len(choices) == 1 else None
        if isinstance(node, ast.Subscript):
            name = self.qualified(node.value, bindings)
            args = (
                node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            )
            types = tuple(self.annotation(arg, bindings, depth + 1) for arg in args)
            if name in {"typing.Optional", "typing_extensions.Optional"}:
                return types[0] if len(types) == 1 else None
            if name in {"typing.Union", "typing_extensions.Union"}:
                choices = set(types) - {ReceiverType("<none>")}
                return next(iter(choices)) if len(choices) == 1 else None
            aliases = {
                "typing.Dict": "dict",
                "typing.List": "list",
                "typing.Set": "set",
                "typing.Sequence": "list",
                "typing.Iterable": "list",
                "collections.abc.Sequence": "list",
                "collections.abc.Iterable": "list",
            }
            name = aliases.get(name, name.removeprefix("builtins."))
            if name in {"dict", "list", "set"}:
                return ReceiverType(name, types)
            return None
        name = self.qualified(node, bindings)
        if name in self.analysis.classes or name in {"str", "int", "bool", "float"}:
            return ReceiverType(name)
        return None

    def member(self, owner, attribute):
        key = (owner, attribute)
        if key in self.fields:
            return self.fields[key]
        self.fields[key] = None
        nodes = self.analysis.class_nodes.get(owner, [])
        if (
            len(nodes) != 1
            or not self.ordinary_class(owner)
            or self.analysis.by_name.get(owner + ".__getattribute__")
            or self.analysis.by_name.get(owner + "." + attribute)
        ):
            return None
        unit = self.analysis.class_units[owner]
        matches = [
            n
            for n in nodes[0].body
            if isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == attribute
        ]
        if len(matches) == 1:
            self.fields[key] = self.annotation(matches[0].annotation, unit.bindings)
        return self.fields[key]

    def getter(self, node, scope):
        if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
            return None
        owner = scope.type_of(node.value)
        if owner is None:
            return None
        key = (owner.name, node.attr)
        if key in self.properties:
            return self.properties[key]
        self.properties[key] = None
        candidates = self.analysis.by_name.get(owner.name + "." + node.attr, [])
        classes = self.analysis.class_nodes.get(owner.name, [])
        if len(candidates) != 1 or len(classes) != 1:
            return None
        definition = self.definitions[candidates[0].id]
        method = definition.node
        if not isinstance(method, ast.FunctionDef) or len(method.decorator_list) != 1:
            return None
        decorator = self.qualified(method.decorator_list[0], definition.unit.bindings)
        if decorator == "property" and "property" not in definition.unit.bindings:
            decorator = "builtins.property"
        if decorator != "builtins.property" or not self.ordinary_class(owner.name):
            return None
        if self.analysis.by_name.get(owner.name + ".__getattribute__"):
            return None
        self.properties[key] = candidates[0]
        return candidates[0]

    def ordinary_class(self, name):
        nodes = self.analysis.class_nodes.get(name, [])
        if len(nodes) != 1 or nodes[0].keywords:
            return False
        bindings = self.analysis.class_units[name].bindings
        return all(
            self.qualified(d.func if isinstance(d, ast.Call) else d, bindings)
            == "dataclasses.dataclass"
            for d in nodes[0].decorator_list
        )

    def scope(self, definition):
        from .scanner import scoped_bindings

        identity = definition.symbol.id
        if identity in self.scopes:
            return self.scopes[identity]
        parent = definition.parent
        if parent is None and not isinstance(definition.node, ast.Module):
            parent = self.modules[definition.unit.path]
        base = self.scope(parent).current() if parent else {}
        bindings = (
            definition.unit.bindings
            if isinstance(definition.node, ast.Module)
            else scoped_bindings(self.analysis, definition)
        )
        references = (
            definition.unit.references
            if isinstance(definition.node, ast.Module)
            else self.analysis.reference_cache[identity]
        )
        scope = ReceiverScope(self, bindings, references, base)
        if isinstance(definition.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = definition.node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                scope.add(arg.arg, self.annotation(arg.annotation, bindings))
            for arg in [args.vararg, args.kwarg]:
                if arg:
                    scope.add(arg.arg, None)
            positional = [*args.posonlyargs, *args.args]
            if (
                definition.class_name
                and positional
                and positional[0].arg == "self"
                and not definition.node.decorator_list
            ):
                scope.values["self"] = [ReceiverType(definition.class_name)]
            # @property itself is an instance method; other decorators stay conservative.
            elif (
                definition.class_name
                and positional
                and positional[0].arg == "self"
                and all(
                    self.qualified(d, bindings) in {"property", "builtins.property"}
                    for d in definition.node.decorator_list
                )
            ):
                scope.values["self"] = [ReceiverType(definition.class_name)]
        for statement in definition.node.body:
            scope.visit(statement)
        self.scopes[identity] = scope
        return scope


class ReceiverScope(ast.NodeVisitor):
    def __init__(self, index, bindings, references, base):
        self.index, self.bindings, self.references, self.base = (
            index,
            bindings,
            references,
            base,
        )
        self.values = {}

    def add(self, name, value):
        self.values.setdefault(name, []).append(value)

    def current(self):
        return self.base | {
            name: values[0] if len(set(values)) == 1 else None
            for name, values in self.values.items()
        }

    def type_of(self, node, depth=0):
        if depth > 32:
            return None
        if isinstance(node, ast.Name):
            return self.current().get(node.id)
        if isinstance(node, ast.Attribute):
            owner = self.type_of(node.value, depth + 1)
            return self.index.member(owner.name, node.attr) if owner else None
        if isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Slice):
                return None
            owner = self.type_of(node.value, depth + 1)
            if owner and owner.name in {"dict", "list"} and owner.arguments:
                return owner.arguments[-1]
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            choices = {self.type_of(value, depth + 1) for value in node.values}
            return next(iter(choices)) if len(choices) == 1 else None
        if isinstance(node, ast.Call):
            from .scanner import dotted

            name = self.index.qualified(node.func, self.bindings)
            if (
                dotted(node.func).split(".")[0] in self.references
                and self.index.ordinary_class(name)
                and not self.index.analysis.by_name.get(name + ".__new__")
            ):
                return ReceiverType(name)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "values"
                and not node.args
                and not node.keywords
            ):
                owner = self.type_of(node.func.value, depth + 1)
                if owner and owner.name == "dict" and len(owner.arguments) == 2:
                    return ReceiverType("list", (owner.arguments[1],))
        return None

    def visit_Assign(self, node):
        value = self.type_of(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.add(target.id, value)
            else:
                self.visit(target)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name):
            self.add(
                node.target.id,
                self.type_of(node.value)
                or self.index.annotation(node.annotation, self.bindings),
            )

    def visit_For(self, node):
        iterable = self.type_of(node.iter)
        if isinstance(node.target, ast.Name):
            item = (
                iterable.arguments[0]
                if iterable
                and iterable.name in {"list", "set", "dict"}
                and iterable.arguments
                else None
            )
            self.add(node.target.id, item)
        else:
            self.visit(node.target)
        for statement in [*node.body, *node.orelse]:
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_Import(self, node):
        for alias in node.names:
            self.add(alias.asname or alias.name.split(".")[0], None)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.add(alias.asname or alias.name, None)

    def visit_Global(self, node):
        for name in node.names:
            self.add(name, None)

    visit_Nonlocal = visit_Global

    def visit_MatchAs(self, node):
        if node.name:
            self.add(node.name, None)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.add(node.rest, None)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.add(node.id, None)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.add(node.name, None)
        for statement in node.body:
            self.visit(statement)

    def visit_FunctionDef(self, node):
        self.add(node.name, None)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        pass

    def visit_ListComp(self, node):
        for child in ast.walk(node):
            if isinstance(child, ast.NamedExpr):
                self.visit(child.target)

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp
