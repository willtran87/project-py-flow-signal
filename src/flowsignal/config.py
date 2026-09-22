"""Small, explicit extension points for application-specific APIs and log wrappers."""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_EXCLUDES = [
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    ".artifacts",
]
LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass
class Config:
    exclude: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    logger_names: list[str] = field(default_factory=list)
    boundaries: list[dict[str, str]] = field(default_factory=list)
    loggers: list[dict[str, str]] = field(default_factory=list)
    reporters: list[dict[str, str]] = field(default_factory=list)
    max_files: int = 10_000
    max_file_bytes: int = 2_000_000
    max_path_depth: int = 8
    max_paths: int = 5
    lifecycle_info: bool = False
    source_roots: list[str] = field(default_factory=list)
    include_source: bool = True
    max_total_bytes: int = 64_000_000
    max_ast_nodes: int = 2_000_000
    max_ast_depth: int = 120
    max_graph_steps: int = 1_000_000
    max_discovery_entries: int = 100_000

    def validate(self) -> None:
        for name in ("lifecycle_info", "include_source"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be true or false")
        for name in ("exclude", "entrypoints", "logger_names", "source_roots"):
            value = getattr(self, name)
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise ValueError(f"{name} must be a list of nonempty strings")
        for root in self.source_roots:
            if (
                Path(root).is_absolute()
                or ".." in root.replace("\\", "/").split("/")
                or ":" in root
            ):
                raise ValueError(
                    "source_roots must be relative directories within the scan root"
                )
        for name, maximum in (
            ("max_files", 100_000),
            ("max_file_bytes", 20_000_000),
            ("max_path_depth", 30),
            ("max_paths", 100),
            ("max_total_bytes", 1_000_000_000),
            ("max_ast_nodes", 20_000_000),
            ("max_ast_depth", 200),
            ("max_graph_steps", 10_000_000),
            ("max_discovery_entries", 10_000_000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        for name, required in (
            ("boundaries", {"pattern", "category"}),
            ("loggers", {"pattern", "level"}),
        ):
            records = getattr(self, name)
            if not isinstance(records, list):
                raise ValueError(f"{name} must be an array of tables")
            for record in records:
                if (
                    not isinstance(record, dict)
                    or set(record) != required
                    or any(not isinstance(v, str) or not v for v in record.values())
                ):
                    raise ValueError(
                        f"Each {name} record requires exactly {sorted(required)} as nonempty strings"
                    )
                if name == "loggers" and record["level"] not in LEVELS:
                    raise ValueError(f"Logger level must be one of {sorted(LEVELS)}")
        if not isinstance(self.reporters, list):
            raise ValueError("reporters must be an array of tables")
        for record in self.reporters:
            required = {"pattern", "kind", "owner"}
            if (
                not isinstance(record, dict)
                or not required <= record.keys()
                or record.keys() - required - {"scope", "match"}
                or any(not isinstance(v, str) or not v.strip() for v in record.values())
            ):
                raise ValueError(
                    "Each reporter requires pattern, kind, owner; optional scope and match"
                )
            if record["kind"] not in {"diagnostic", "stderr", "error_return"}:
                raise ValueError(
                    "Reporter kind must be diagnostic, stderr, or error_return"
                )
            if record.get("match", "resolved") not in {"resolved", "expression"}:
                raise ValueError("Reporter match must be resolved or expression")
            if record.get("match") == "expression" and (
                not record.get("scope") or record["scope"] == "*"
            ):
                raise ValueError(
                    "Expression reporters require a restricted symbol scope"
                )

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: Path) -> Config:
    from .source_io import read_snapshot

    document = tomllib.loads(read_snapshot(path, 1_000_000).decode("utf-8-sig"))
    tool_table = document.get("tool", {})
    if path.name == "pyproject.toml" and not isinstance(tool_table, dict):
        raise ValueError("The pyproject tool section must be a table")
    values = (
        tool_table.get("flowsignal", {})
        if path.name == "pyproject.toml"
        else document.get("flowsignal", {})
    )
    if not isinstance(values, dict):
        raise ValueError("The flowsignal configuration must be a table")
    unknown = set(values) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown FlowSignal options: {', '.join(sorted(unknown))}")
    config = Config(**values)
    config.validate()
    return config


def matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
