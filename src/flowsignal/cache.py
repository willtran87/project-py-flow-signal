"""Opt-in local JSON cache. Cache directories must be trusted application state.

No pickle, target imports, or executable bytecode is stored or loaded. Source
snapshots, configuration, Python minor version, and scanner implementation all
participate in keys. Global relationships rebuild whenever a source changes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile
import types
from dataclasses import fields, is_dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from .model import Report
from .source_io import is_link, read_snapshot


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


@lru_cache(maxsize=64)
def field_types(typ):
    hints = get_type_hints(typ)
    return [(f.name, hints[f.name]) for f in fields(typ)]


def restore(value, typ):
    if value is None or typ is Any or typ in (str, int, bool, float):
        return value
    origin, args = get_origin(typ), get_args(typ)
    if origin in (Union, types.UnionType):
        return restore(value, next(t for t in args if t is not type(None)))
    if origin is list:
        return [restore(v, args[0]) for v in value]
    if origin is dict:
        return {k: restore(v, args[1]) for k, v in value.items()}
    if is_dataclass(typ):
        return typ(
            **{
                name: restore(value[name], hint)
                for name, hint in field_types(typ)
                if name in value
            }
        )
    return value


def encode(node):
    if isinstance(node, ast.AST):
        return {
            "_ast": type(node).__name__,
            "fields": {k: encode(v) for k, v in ast.iter_fields(node)},
            "attributes": {
                k: getattr(node, k) for k in node._attributes if hasattr(node, k)
            },
        }
    if isinstance(node, list):
        return [encode(n) for n in node]
    if isinstance(node, bytes):
        return {"_bytes": node.hex()}
    if isinstance(node, complex):
        return {"_complex": [node.real, node.imag]}
    if node is Ellipsis:
        return {"_ellipsis": True}
    return node


def decode(value, depth=0):
    if depth > 150:
        raise ValueError("Cache AST depth exceeded")
    if isinstance(value, list):
        return [decode(v, depth + 1) for v in value]
    if isinstance(value, dict):
        if set(value) == {"_bytes"}:
            return bytes.fromhex(value["_bytes"])
        if set(value) == {"_complex"}:
            return complex(*value["_complex"])
        if set(value) == {"_ellipsis"}:
            return Ellipsis
        if set(value) != {"_ast", "fields", "attributes"}:
            raise ValueError("Invalid AST record")
        cls = getattr(ast, value["_ast"], None)
        if (
            not isinstance(cls, type)
            or not issubclass(cls, ast.AST)
            or set(value["fields"]) != set(cls._fields)
            or set(value["attributes"]) - set(cls._attributes)
        ):
            raise ValueError("Invalid AST type or fields")
        return cls(
            **{k: decode(v, depth + 1) for k, v in value["fields"].items()},
            **value["attributes"],
        )
    return value


class ScanCache:
    def __init__(self, directory, config, root):
        self.directory = Path(directory).absolute()
        if any(
            is_link(p) for p in [self.directory, *self.directory.parents] if p.exists()
        ):
            raise ValueError("Cache directory must not traverse links")
        self.directory.mkdir(parents=True, exist_ok=True)
        implementation = hashlib.sha256()
        for path in sorted(Path(__file__).parent.glob("*.py")):
            implementation.update(path.name.encode())
            implementation.update(path.read_bytes())
        self.namespace = digest(
            [
                "flowsignal-cache-1",
                implementation.hexdigest(),
                list(sys.version_info[:2]),
                config.to_dict(),
                str(root),
            ]
        )
        self.stats = {
            "parsed_files_reused": 0,
            "parsed_files_built": 0,
            "snapshot_reused": False,
            "invalid_entries": 0,
            "write_failures": 0,
        }
        self.trees = None
        self.used_paths = set()
        self.trees_changed = False

    def read(self, key):
        path = self.directory / (digest([self.namespace, key]) + ".json")
        if not path.exists():
            return None
        try:
            document = json.loads(read_snapshot(path, 64_000_000, self.directory))
            if document["checksum"] != digest(document["payload"]):
                raise ValueError("Cache checksum mismatch")
            return document["payload"]
        except (ValueError, OSError, KeyError, TypeError, RecursionError):
            self.stats["invalid_entries"] += 1
            return None

    def write(self, key, payload):
        name = None
        try:
            text = json.dumps(
                {"checksum": digest(payload), "payload": payload}, ensure_ascii=True
            )
            if len(text) > 64_000_000:
                self.stats["write_failures"] += 1
                return
            destination = self.directory / (digest([self.namespace, key]) + ".json")
            if destination.exists() and is_link(destination):
                raise ValueError("Linked cache entry")
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory, delete=False
            ) as stream:
                name = stream.name
                stream.write(text)
            os.replace(name, destination)
        except (ValueError, OSError, RecursionError):
            self.stats["write_failures"] += 1
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def parse(self, source, relative, source_hash):
        self.last_miss = False
        if self.trees is None:
            self.trees = self.read(["parsed-files"]) or {}
            if not isinstance(self.trees, dict):
                self.stats["invalid_entries"] += 1
                self.trees = {}
        self.used_paths.add(relative)
        record = self.trees.get(relative)
        payload = (
            record.get("tree")
            if isinstance(record, dict) and record.get("source_hash") == source_hash
            else None
        )
        if payload is not None:
            try:
                tree = decode(payload)
                if not isinstance(tree, ast.Module):
                    raise ValueError("Expected module")
                self.stats["parsed_files_reused"] += 1
                return tree
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                self.stats["invalid_entries"] += 1
        tree = ast.parse(source, filename=relative)
        self.stats["parsed_files_built"] += 1
        self.last_miss = True
        return tree

    def store_tree(self, relative, source_hash, tree):
        if self.last_miss:
            self.trees[relative] = {"source_hash": source_hash, "tree": encode(tree)}
            self.trees_changed = True

    def flush_trees(self):
        if self.trees is not None and (
            self.trees_changed or set(self.trees) != self.used_paths
        ):
            self.write(
                ["parsed-files"],
                {
                    path: self.trees[path]
                    for path in sorted(self.used_paths)
                    if path in self.trees
                },
            )

    def snapshot_key(self, paths, root, config):
        snapshots, total = [], 0
        for path in paths:
            raw = read_snapshot(path, config.max_file_bytes, root)
            total += len(raw)
            if total > config.max_total_bytes:
                return None
            snapshots.append(
                [path.relative_to(root).as_posix(), hashlib.sha256(raw).hexdigest()]
            )
        return ["report", snapshots]

    def report(self, key):
        document = self.read(key)
        if document is not None:
            try:
                report = restore(document, Report)
                report.summary()
                self.stats["snapshot_reused"] = True
                report.analysis_stats["cache"] = dict(self.stats)
                return report
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                self.stats["invalid_entries"] += 1
        return None
