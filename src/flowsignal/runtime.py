"""Import externally collected call pairs without executing target code."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from .source_io import read_snapshot


def safe_file(value):
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 4096
        and "\\" not in value
        and ":" not in value
        and not any(ord(c) < 32 for c in value)
        and not PurePosixPath(value).is_absolute()
        and all(p not in {"", ".", ".."} for p in value.split("/"))
    )


def compare_trace(report, path: Path):
    try:
        document = json.loads(read_snapshot(path, 20_000_000).decode("utf-8-sig"))
    except (UnicodeError, RecursionError) as error:
        raise ValueError("Runtime trace must be bounded UTF-8 JSON") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != "flowsignal-runtime-1"
    ):
        raise ValueError(
            "Unsupported runtime trace schema; expected flowsignal-runtime-1"
        )
    files = document.get("files")
    pairs = document.get("pairs")
    if (
        not isinstance(files, dict)
        or len(files) > 10_000
        or any(
            not safe_file(p)
            or not isinstance(d, str)
            or not re.fullmatch("[0-9a-f]{64}", d)
            for p, d in files.items()
        )
    ):
        raise ValueError(
            "Runtime trace requires relative file paths and SHA-256 hashes"
        )
    if not isinstance(pairs, list) or len(pairs) > 100_000:
        raise ValueError(
            "Runtime trace pairs must be an array of at most 100000 records"
        )
    counts = {}
    for pair in pairs:
        if not isinstance(pair, dict) or set(pair) - {"caller", "callee", "count"}:
            raise ValueError("Malformed runtime pair")
        for key in ("caller", "callee"):
            symbol = pair.get(key)
            if not isinstance(symbol, str) or len(symbol) > 8192 or ":" not in symbol:
                raise ValueError("Runtime symbols must use relative-file.py:symbol IDs")
            file, name = symbol.split(":", 1)
            if file not in files or not name or any(ord(c) < 32 for c in name):
                raise ValueError(
                    "Every runtime endpoint must name a hashed source file and symbol"
                )
        count = pair.get("count", 1)
        if type(count) is not int or not 1 <= count <= 1_000_000_000:
            raise ValueError("Runtime pair count must be a positive bounded integer")
        key = pair["caller"], pair["callee"]
        counts[key] = counts.get(key, 0) + count
    hashes = {
        i["path"]: i["sha256"]
        for i in report.inventory
        if i.get("status") == "analyzed"
    }
    symbols = {s.id for s in report.symbols}
    static = {(c.symbol, c.target) for c in report.calls if c.target}
    records = []
    for (caller, callee), count in sorted(counts.items()):
        matched = all(
            hashes.get(s.split(":", 1)[0]) == files[s.split(":", 1)[0]]
            for s in (caller, callee)
        )
        status = (
            "source_mismatch"
            if not matched
            else "unmatched_symbol"
            if caller not in symbols or callee not in symbols
            else "observed_and_inferred"
            if (caller, callee) in static
            else "observed_only"
        )
        records.append(
            {"caller": caller, "callee": callee, "count": count, "status": status}
        )
    trusted = {
        (r["caller"], r["callee"])
        for r in records
        if r["status"] in {"observed_only", "observed_and_inferred"}
    }
    report.runtime = {
        "schema_version": "flowsignal-runtime-comparison-1",
        "pairs": records,
        "summary": {
            status: sum(r["status"] == status for r in records)
            for status in (
                "observed_and_inferred",
                "observed_only",
                "unmatched_symbol",
                "source_mismatch",
            )
        },
        "static_not_observed": [list(pair) for pair in sorted(static - trusted)],
        "note": "Externally supplied observations are not authenticated. Matching source hashes establish snapshot agreement, not complete execution coverage. Static findings and coverage decisions are unchanged.",
    }
