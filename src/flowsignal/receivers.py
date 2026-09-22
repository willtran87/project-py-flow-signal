"""Bounded receiver evidence from annotations, fields, helpers and property reads."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model import Diagnostic

if TYPE_CHECKING:
    from .scanner import Analysis, Definition


@dataclass(frozen=True)
class ReceiverType:
    name: str
    arguments: tuple[ReceiverType | None, ...] = ()


class ReceiverIndex:
    def __init__(self, analysis: Analysis):
        self.analysis = analysis
        self.scopes = {}
        self.fields = {}
        self.properties = {}
        self.returns = {}
        self.writes = {}
        self.exhausted = False
        self.constructions = None
        self.constructor_types = {}
        self.method_masks = {}
        self.definitions = {d.symbol.id: d for d in analysis.definitions}
        self.methods = {}
        for definition in analysis.definitions:
            if definition.class_name:
                self.methods.setdefault(definition.class_name, []).append(definition)
        self.modules = {
            d.unit.path: d
            for d in analysis.definitions
            if isinstance(d.node, ast.Module)
        }

    def step(self):
        if self.exhausted:
            return False
        if self.analysis.stats["type_steps"] >= self.analysis.config.max_type_steps:
            self.exhausted = True
            self.analysis.diagnostics.append(
                Diagnostic(
                    "type_work_limit",
                    "Receiver and return inference exhausted its work budget; remaining type-dependent edges are unresolved.",
                )
            )
            return False
        self.analysis.stats["type_steps"] += 1
        return True

    def qualified(self, node, bindings):
        from .scanner import dotted, substitute

        return substitute(dotted(node), bindings)

    def annotation(self, node, bindings, depth=0):
        if node is None or depth > 32 or not self.step():
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
                "typing.Tuple": "tuple",
                "collections.abc.Sequence": "list",
                "collections.abc.Iterable": "list",
            }
            name = aliases.get(name, name.removeprefix("builtins."))
            if name in {"dict", "list", "set", "tuple"}:
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
        if not self.step():
            return None
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
        declared = (
            self.annotation(matches[0].annotation, unit.bindings)
            if len(matches) == 1
            else None
        )
        if owner not in self.writes:
            collector = MemberWrites(self)
            for definition in self.methods.get(owner, []):
                if definition.class_name == owner and isinstance(
                    definition.node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    collector.definition = definition
                    for statement in definition.node.body:
                        collector.visit(statement)
            self.writes[owner] = collector.writes
        choices = set()
        for definition, value, annotation in self.writes[owner].get(attribute, []):
            scope = self.scope(definition)
            actual = scope.type_of(value)
            if actual is None and annotation is not None:
                actual = self.annotation(annotation, scope.bindings)
            choices.add(actual)
        choices.discard(ReceiverType("<none>"))
        if self.exhausted or len(choices) > 1 or None in choices:
            return None
        inferred = next(iter(choices)) if choices else declared
        if declared is not None and inferred != declared:
            return None
        self.fields[key] = inferred
        return self.fields[key]

    def getter(self, node, scope: ReceiverScope):
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

    def lookup_method(self, owner, attribute, *, base_only=False):
        """Single-inheritance lookup, stopping at unknown bases or ambiguity."""
        seen = set()
        for depth in range(32):
            if owner in seen or not self.step() or not self.ordinary_class(owner):
                return None
            seen.add(owner)
            if self.analysis.by_name.get(owner + ".__getattribute__"):
                return None
            if not (base_only and depth == 0):
                if owner not in self.method_masks:
                    masks = set()
                    for item in ast.walk(self.analysis.class_nodes[owner][0]):
                        if not self.step():
                            return None
                        if (
                            isinstance(item, ast.Attribute)
                            and isinstance(item.ctx, (ast.Store, ast.Del))
                            and isinstance(item.value, ast.Name)
                            and item.value.id in {"self", "cls"}
                        ):
                            masks.add(item.attr)
                    self.method_masks[owner] = masks
                if attribute in self.method_masks[owner]:
                    return None
                candidates = self.analysis.by_name.get(owner + "." + attribute, [])
                if candidates:
                    if len(candidates) != 1:
                        return None
                    definition = self.definitions[candidates[0].id]
                    if (
                        isinstance(
                            definition.node, (ast.FunctionDef, ast.AsyncFunctionDef)
                        )
                        and not definition.node.decorator_list
                    ):
                        return definition
                    return None
                # A field/property declaration can mask an inherited method.
                node = self.analysis.class_nodes[owner][0]
                if any(
                    isinstance(n, ast.AnnAssign)
                    and isinstance(n.target, ast.Name)
                    and n.target.id == attribute
                    or isinstance(n, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == attribute for t in n.targets
                    )
                    for n in node.body
                ):
                    return None
            bases = self.analysis.class_nodes[owner][0].bases
            if len(bases) != 1:
                return None
            owner = self.qualified(bases[0], self.analysis.class_units[owner].bindings)
        return None

    def method(self, node, scope: ReceiverScope):
        if not isinstance(node, ast.Attribute):
            return None
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "super"
            and "super" not in scope.bindings
            and not node.value.args
            and not node.value.keywords
        ):
            owner = scope.current().get("self")
            if owner:
                return self.lookup_method(owner.name, node.attr, base_only=True)
            return None
        owner = scope.type_of(node.value)
        if owner is None or not self.ordinary_class(owner.name):
            return None
        if self.analysis.by_name.get(owner.name + ".__getattribute__"):
            return None
        return self.lookup_method(owner.name, node.attr)

    def constructor_parameters(self, definition):
        identity = definition.symbol.id
        if identity in self.constructor_types:
            return self.constructor_types[identity]
        self.constructor_types[identity] = {}
        if self.constructions is None:
            from .scanner import dotted, scoped_bindings

            self.constructions = {}
            for caller in self.definitions.values():
                bindings = (
                    caller.unit.bindings
                    if isinstance(caller.node, ast.Module)
                    else scoped_bindings(self.analysis, caller)
                )
                collector = CallSites(self)
                for statement in caller.node.body:
                    collector.visit(statement)
                for call in collector.calls:
                    name = self.qualified(call.func, bindings)
                    target, offset = None, 0
                    references = (
                        caller.unit.references
                        if isinstance(caller.node, ast.Module)
                        else self.analysis.reference_cache[caller.symbol.id]
                    )
                    if (
                        name in self.analysis.classes
                        and dotted(call.func).split(".")[0] in references
                    ):
                        target = self.lookup_method(name, "__init__")
                    elif (
                        isinstance(call.func, ast.Attribute)
                        and call.func.attr == "__init__"
                    ):
                        if (
                            isinstance(call.func.value, ast.Call)
                            and dotted(call.func.value.func) == "super"
                            and "super" not in bindings
                            and not call.func.value.args
                            and not call.func.value.keywords
                            and caller.class_name
                        ):
                            target = self.lookup_method(
                                caller.class_name, "__init__", base_only=True
                            )
                        elif name.rpartition(".")[0] in self.analysis.classes:
                            target = self.lookup_method(
                                name.rpartition(".")[0], "__init__"
                            )
                            offset = 1
                    if target:
                        self.constructions.setdefault(target.symbol.id, []).append(
                            (caller, call, offset)
                        )
        args = definition.node.args
        positional = [*args.posonlyargs, *args.args][1:]
        defaults = (
            dict(zip([a.arg for a in positional][-len(args.defaults) :], args.defaults))
            if args.defaults
            else {}
        )
        defaults.update(
            {
                a.arg: v
                for a, v in zip(args.kwonlyargs, args.kw_defaults)
                if v is not None
            }
        )
        choices = {a.arg: set() for a in [*positional, *args.kwonlyargs]}
        for caller, call, offset in self.constructions.get(identity, []):
            scope = self.scope(caller)
            values = {a.arg: v for a, v in zip(positional, call.args[offset:])}
            keywords = {k.arg: k.value for k in call.keywords}
            invalid = (
                any(isinstance(a, ast.Starred) for a in call.args)
                or None in keywords
                or bool(set(keywords) & {a.arg for a in args.posonlyargs})
                or bool(set(values) & set(keywords))
                or len(call.args[offset:]) > len(positional)
                or bool(set(keywords) - set(choices))
            )
            values.update(keywords)
            for name in choices:
                value = (
                    None
                    if invalid
                    else scope.type_of(values[name])
                    if name in values
                    else self.scope(definition).type_of(defaults.get(name))
                )
                choices[name].add(value)
        result = {
            name: next(iter(values))
            for name, values in choices.items()
            if len(values) == 1 and None not in values
        }
        if not self.exhausted:
            self.constructor_types[identity] = result
        return self.constructor_types[identity]

    def callee(self, node, scope: ReceiverScope):
        method = self.method(node, scope)
        if method:
            return method
        name = self.qualified(node, scope.bindings)
        candidates = self.analysis.by_name.get(name, [])
        if len(candidates) != 1:
            return None
        definition = self.definitions[candidates[0].id]
        if (
            not isinstance(definition.node, (ast.FunctionDef, ast.AsyncFunctionDef))
            or definition.node.decorator_list
        ):
            return None
        # Attribute calls on inferred instances must resolve through method(),
        # not by treating a receiver type as a class/module reference.
        from .scanner import dotted

        if (
            isinstance(node, ast.Attribute)
            and dotted(node).split(".")[0] not in scope.references
        ):
            return None
        return definition

    def returned(self, definition: Definition):
        identity = definition.symbol.id
        if identity in self.returns:
            return self.returns[identity]
        self.returns[identity] = None  # recursive helpers stay conservative
        if not self.step():
            return None
        annotation = self.annotation(definition.node.returns, definition.unit.bindings)
        scope = self.scope(definition)
        collector = ReturnValues(self)
        for statement in definition.node.body:
            collector.visit(statement)
        choices = {
            scope.type_of(value) if value is not None else ReceiverType("<none>")
            for value in collector.values
        }
        choices.discard(ReceiverType("<none>"))
        if self.exhausted:
            return None
        known = next(iter(choices - {None}), None)
        if annotation is not None:

            def compatible(actual, declared):
                return (
                    actual is None
                    or actual.name == declared.name
                    and len(actual.arguments) == len(declared.arguments)
                    and all(
                        d is None or compatible(a, d)
                        for a, d in zip(actual.arguments, declared.arguments)
                    )
                )

            result = (
                annotation
                if all(compatible(choice, annotation) for choice in choices)
                else None
            )
        else:
            result = known if None not in choices and len(choices) <= 1 else None
        self.returns[identity] = result
        return result

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

    def scope(self, definition: Definition) -> ReceiverScope:
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
        self.scopes[identity] = (
            scope  # break helper/scope cycles without executing code
        )
        if isinstance(definition.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = definition.node.args
            inferred = (
                self.constructor_parameters(definition)
                if definition.node.name == "__init__"
                and definition.class_name
                and not definition.node.decorator_list
                else {}
            )
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                scope.add(
                    arg.arg,
                    self.annotation(arg.annotation, bindings) or inferred.get(arg.arg),
                )
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
    def __init__(self, index: ReceiverIndex, bindings, references, base):
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
        if depth > 32 or not self.index.step():
            return None
        if isinstance(node, ast.Constant) and node.value is None:
            return ReceiverType("<none>")
        if isinstance(node, ast.Constant) and type(node.value) in {
            bool,
            int,
            str,
            float,
        }:
            return ReceiverType(type(node.value).__name__)
        if isinstance(node, ast.Tuple):
            return ReceiverType(
                "tuple", tuple(self.type_of(item, depth + 1) for item in node.elts)
            )
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
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            definition = self.index.callee(node.value.func, self)
            if definition and definition.symbol.kind == "async_function":
                return self.index.returned(definition)
            return None
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
            definition = self.index.callee(node.func, self)
            if definition and definition.symbol.kind == "function":
                return self.index.returned(definition)
        return None

    def visit_Assign(self, node):
        value = self.type_of(node.value)
        for target in node.targets:
            self.assign(target, value)

    def assign(self, target, value):
        if isinstance(target, ast.Name):
            self.add(target.id, value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            items = (
                value.arguments
                if value
                and value.name == "tuple"
                and len(value.arguments) == len(target.elts)
                else [None] * len(target.elts)
            )
            for item, typ in zip(target.elts, items):
                self.assign(item, typ)
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


class ReturnValues(ast.NodeVisitor):
    def __init__(self, index):
        self.index, self.values = index, []

    def visit(self, node):
        if self.index.step():
            super().visit(node)

    def visit_Return(self, node):
        self.values.append(node.value)

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef


class MemberWrites(ReturnValues):
    def __init__(self, index):
        super().__init__(index)
        self.writes = {}
        self.definition = None

    def record(self, target, value, annotation=None):
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            self.writes.setdefault(target.attr, []).append(
                (self.definition, value, annotation)
            )
        elif isinstance(target, (ast.Tuple, ast.List)):
            values = (
                value.elts
                if isinstance(value, (ast.Tuple, ast.List))
                and len(value.elts) == len(target.elts)
                else [None] * len(target.elts)
            )
            for item, expression in zip(target.elts, values):
                self.record(item, expression)

    def visit_Assign(self, node):
        for target in node.targets:
            self.record(target, node.value)

    def visit_AnnAssign(self, node):
        self.record(node.target, node.value, node.annotation)

    def visit_AugAssign(self, node):
        self.record(node.target, None)

    def visit_Delete(self, node):
        for target in node.targets:
            self.record(target, None)


class CallSites(ReturnValues):
    def __init__(self, index):
        super().__init__(index)
        self.calls = []

    def visit_Call(self, node):
        self.calls.append(node)
        self.generic_visit(node)

    def visit_Return(self, node):
        self.generic_visit(node)
