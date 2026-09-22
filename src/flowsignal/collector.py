"""Explicit test-run profiling. Importing this module never starts collection."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from collections import Counter
from pathlib import Path
from types import CodeType

from .source_io import read_snapshot


class collect:
    """Use ``with collect(root, output, run_id='test-name'): selected_tests()``.

    Records Python profile call events on this thread and threads created inside
    the context. Async resumes may produce additional events; these are not
    guaranteed invocation counts. Existing profilers are never replaced.
    """

    def __init__(
        self,
        root,
        output,
        *,
        run_id,
        scenario="",
        max_pairs=100_000,
        max_events=1_000_000,
    ):
        if (
            not isinstance(run_id, str)
            or not run_id.strip()
            or len(run_id) > 200
            or not isinstance(scenario, str)
            or len(scenario) > 500
        ):
            raise ValueError(
                "Collector requires a run ID (1-200 characters) and scenario up to 500 characters"
            )
        if (
            type(max_pairs) is not int
            or not 1 <= max_pairs <= 100_000
            or type(max_events) is not int
            or not 1 <= max_events <= 10_000_000
        ):
            raise ValueError("Invalid collection limits")
        self.root, self.output = Path(root).resolve(), Path(output).absolute()
        self.run_id, self.scenario = run_id, scenario
        self.max_pairs, self.max_events = max_pairs, max_events
        self.files, self.codes, self.pairs = {}, {}, Counter()
        self.endpoints = {}
        self.events, self.truncated, self.closed = 0, False, False
        self.issues = set()
        self.started = False

    def endpoint(self, frame):
        if frame.f_code in self.endpoints:
            return self.endpoints[frame.f_code]
        result = self.resolve_endpoint(frame)
        if len(self.endpoints) < 100_000:
            self.endpoints[frame.f_code] = result
        else:
            self.truncated = True
        return result

    def resolve_endpoint(self, frame):
        filename = frame.f_code.co_filename
        if filename.startswith("<"):
            return None
        path = Path(filename).resolve()
        if not path.is_relative_to(self.root) or path.suffix != ".py":
            return None
        relative = path.relative_to(self.root).as_posix()
        if relative not in self.codes:
            if len(self.codes) >= 10_000:
                self.truncated = True
                return None
            try:
                raw = read_snapshot(path, 2_000_000, self.root)
                compiled = compile(raw, filename, "exec", dont_inherit=True)
                pending, codes = [compiled], {}
                while pending:
                    code = pending.pop()
                    codes[(code.co_qualname, code.co_firstlineno)] = code
                    pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
                self.files[relative] = hashlib.sha256(raw).hexdigest()
                self.codes[relative] = codes
            except (OSError, ValueError, SyntaxError, RecursionError):
                self.codes[relative] = {}
                self.issues.add("unreadable_or_uncompilable_source:" + relative)
        candidate = self.codes[relative].get(
            (frame.f_code.co_qualname, frame.f_code.co_firstlineno)
        )
        if candidate != frame.f_code:
            self.issues.add("code_snapshot_mismatch:" + relative)
            return None
        name = frame.f_code.co_qualname.replace(".<locals>.", ".")
        return relative + ":" + name

    def profile(self, frame, event, argument):
        if self.closed or event != "call" or frame.f_back is None:
            return
        self.events += 1
        if self.events > self.max_events:
            self.truncated = True
            return
        try:
            caller, callee = self.endpoint(frame.f_back), self.endpoint(frame)
            if caller and callee:
                pair = caller, callee
                if pair in self.pairs or len(self.pairs) < self.max_pairs:
                    self.pairs[pair] += 1
                else:
                    self.truncated = True
        except (OSError, ValueError, RecursionError):
            self.issues.add("collection_error")

    def __enter__(self):
        if self.started:
            raise ValueError("A collector instance can only be used once")
        if sys.getprofile() is not None or threading.getprofile() is not None:
            raise ValueError("An existing profiler is active; collection did not start")
        if not self.root.is_dir():
            raise ValueError("Collection root must be a directory")
        from .cli import validate_output_path

        validate_output_path(self.output)
        self.started = True
        threading.setprofile(self.profile)
        sys.setprofile(self.profile)
        return self

    def __exit__(self, error_type, error, traceback):
        self.closed = True
        if sys.getprofile() == self.profile:
            sys.setprofile(None)
        if threading.getprofile() == self.profile:
            threading.setprofile(None)
        try:
            self.write_trace()
        except (OSError, ValueError, RecursionError) as write_error:
            if error is None:
                raise
            error.add_note(f"FlowSignal trace could not be written: {write_error}")
        return False

    def write_trace(self):
        changed = set()
        for name, expected in self.files.items():
            try:
                if (
                    hashlib.sha256(
                        read_snapshot(self.root / name, 2_000_000, self.root)
                    ).hexdigest()
                    != expected
                ):
                    changed.add(name)
            except (OSError, ValueError):
                changed.add(name)
        self.issues.update("source_changed:" + name for name in changed)
        pairs = [
            {"caller": a, "callee": b, "count": n}
            for (a, b), n in sorted(self.pairs.items())
            if a.split(":", 1)[0] not in changed and b.split(":", 1)[0] not in changed
        ]
        document = {
            "schema_version": "flowsignal-runtime-1",
            "files": self.files,
            "pairs": pairs,
            "run": {
                "id": self.run_id,
                "scenario": self.scenario,
                "collector": "flowsignal-profile-1",
                "complete": not self.truncated and not self.issues,
                "python": sys.version.split()[0],
            },
            "collection_issues": sorted(self.issues)[:100],
            "collection_truncated": self.truncated,
        }
        text = json.dumps(document, indent=2, ensure_ascii=True) + "\n"
        if len(text) > 20_000_000:
            raise ValueError(
                "Collected trace exceeds import limit; narrow the selected tests"
            )
        from .cli import write_report

        write_report(self.output, text)
