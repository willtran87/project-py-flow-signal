from __future__ import annotations

import contextlib
import io
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from flowsignal import scan
from flowsignal.cli import main, write_report
from flowsignal.config import Config


class EnterpriseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def source(self, source, name="app.py"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def report(self, source):
        self.source(source)
        return scan(self.root)

    def boundaries(self, report):
        return [f for f in report.findings if f.rule_id == "FS005"]

    def test_suppression_blocks_outer_handler_credit(self):
        report = self.report("""
            import contextlib, logging, requests
            def run():
                try:
                    with contextlib.suppress(Exception):
                        requests.get('url')
                except Exception:
                    logging.exception('failed')
        """)
        self.assertEqual(len(self.boundaries(report)), 1)
        call = next(c for c in report.calls if c.boundary == "network")
        self.assertTrue(call.propagation_uncertain)
        self.assertTrue(call.blocked_handler_ids)
        self.assertEqual(report.summary()["propagation_uncertain_calls"], 1)

    def test_unknown_context_blocks_transitive_caller_credit(self):
        report = self.report("""
            import requests, logging
            def fetch():
                requests.get('url')
            def worker():
                with custom_context():
                    fetch()
            def run():
                try:
                    worker()
                except Exception:
                    logging.exception('failed')
        """)
        self.assertEqual(len(self.boundaries(report)), 1)

    def test_handler_inside_unknown_context_still_reports(self):
        report = self.report("""
            import requests, logging
            def run():
                with custom_context():
                    try:
                        requests.get('url')
                    except Exception:
                        logging.exception('failed')
        """)
        self.assertEqual(self.boundaries(report), [])

    def test_nested_trace_position_matters(self):
        prefix = "import contextlib, requests\nfrom opentelemetry import trace\ntracer = trace.get_tracer(__name__)\n"
        outer = self.report(
            prefix
            + """
def run():
    with tracer.start_as_current_span('work'):
        with contextlib.suppress(Exception):
            requests.get('url')
"""
        )
        inner = self.report(
            prefix
            + """
def run():
    with contextlib.suppress(Exception):
        with tracer.start_as_current_span('work'):
            requests.get('url')
"""
        )
        self.assertEqual(len(self.boundaries(outer)), 1)
        self.assertEqual(self.boundaries(inner), [])

    def test_multiple_context_items_follow_nesting_order(self):
        prefix = "import contextlib, requests\nfrom opentelemetry import trace\ntracer = trace.get_tracer(__name__)\n"
        for items, expected in [
            ("tracer.start_as_current_span('work'), contextlib.suppress(Exception)", 1),
            ("contextlib.suppress(Exception), tracer.start_as_current_span('work')", 0),
        ]:
            with self.subTest(items=items):
                report = self.report(
                    prefix
                    + f"def run():\n    with {items}:\n        requests.get('url')\n"
                )
                self.assertEqual(len(self.boundaries(report)), expected)

    def test_context_factory_call_uses_outer_scope(self):
        report = self.report("""
            import requests, logging
            def run():
                try:
                    with unknown_context(requests.get('url')):
                        pass
                except Exception:
                    logging.exception('failed')
        """)
        self.assertEqual(self.boundaries(report), [])

    def test_file_and_null_contexts_preserve_outer_reporting(self):
        report = self.report("""
            import requests, logging
            from contextlib import nullcontext
            def run():
                try:
                    with open('file') as stream, nullcontext():
                        requests.get('url')
                except Exception:
                    logging.exception('failed')
        """)
        self.assertEqual(self.boundaries(report), [])
        self.assertFalse(any(c.propagation_uncertain for c in report.calls))

    def test_unknown_scope_restores_reporting_after_exit(self):
        report = self.report("""
            import requests, logging
            def run():
                try:
                    with unknown_context():
                        pass
                    requests.get('url')
                except Exception:
                    logging.exception('failed')
        """)
        self.assertEqual(self.boundaries(report), [])

    def test_async_context_is_conservatively_modeled(self):
        report = self.report("""
            import logging
            import httpx
            async def run(client: httpx.AsyncClient):
                try:
                    async with custom_context():
                        await client.get('url')
                except Exception:
                    logging.exception('failed')
        """)
        self.assertEqual(len(self.boundaries(report)), 1)

    def test_suppression_does_not_produce_false_duplicate_logging(self):
        report = self.report("""
            import logging, contextlib
            def inner():
                try:
                    operation()
                except Exception:
                    logging.exception('inner')
                    raise
            def run():
                try:
                    with contextlib.suppress(Exception):
                        inner()
                except Exception:
                    logging.exception('outer')
        """)
        self.assertFalse(any(f.rule_id == "FS004" for f in report.findings))

    def test_discovery_budget_counts_non_python_files(self):
        for index in range(12):
            self.source("data", f"data{index}.txt")
        report = scan(self.root, Config(max_discovery_entries=5))
        self.assertEqual(report.analysis_stats["discovery_entries"], 5)
        self.assertEqual(report.summary()["status"], "incomplete")
        self.assertEqual(report.summary()["diagnostic_counts"]["discovery_limit"], 1)

    def test_exact_discovery_limit_is_complete(self):
        self.source("pass", "a.py")
        self.source("pass", "b.py")
        report = scan(self.root, Config(max_discovery_entries=2))
        self.assertEqual(report.files_scanned, 2)
        self.assertEqual(report.summary()["status"], "complete")

    def test_discovery_budget_spans_directories(self):
        self.source("pass", "a/one.py")
        self.source("pass", "b/two.py")
        report = scan(self.root, Config(max_discovery_entries=3))
        self.assertEqual(report.files_scanned, 1)
        self.assertEqual(report.analysis_stats["discovery_entries"], 3)
        self.assertEqual(report.summary()["status"], "incomplete")

    def test_excluded_tree_is_not_enumerated(self):
        self.source("pass")
        for index in range(10):
            self.source("pass", f".venv/{index}.py")
        report = scan(self.root, Config(max_discovery_entries=2))
        self.assertEqual(report.files_scanned, 1)
        self.assertEqual(report.summary()["status"], "complete")

    def test_protected_output_does_not_modify_existing_files(self):
        for name in [
            "app.py",
            "app.pyi",
            "pyproject.toml",
            "pipeline.yml",
            "package.json",
            "requirements.txt",
        ]:
            with self.subTest(name=name):
                path = self.source("original", name)
                with self.assertRaises(ValueError):
                    write_report(path, "report")
                self.assertEqual(path.read_text(), "original")

    def test_custom_active_configuration_cannot_be_overwritten(self):
        self.source("pass")
        config = self.source("[flowsignal]\n", "settings.custom")
        with contextlib.redirect_stderr(io.StringIO()):
            result = main(
                [
                    "scan",
                    str(self.root),
                    "--config",
                    str(config),
                    "--output",
                    str(config),
                ]
            )
        self.assertEqual(result, 2)
        self.assertEqual(config.read_text(), "[flowsignal]\n")

    def test_failed_publication_keeps_previous_report_and_removes_temporary(self):
        path = self.source("previous", "report.json")
        with patch("flowsignal.cli.os.replace", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                write_report(path, "replacement")
        self.assertEqual(path.read_text(), "previous")
        self.assertEqual(list(self.root.glob(".flowsignal-*.tmp")), [])

    def test_resolution_counts_keep_import_assumptions_separate(self):
        report = self.report("""
            import requests
            def run():
                len([])
                requests.get('url')
                unknown()
            run()
        """)
        self.assertEqual(
            report.summary()["resolution_counts"],
            {"builtin": 1, "import_or_annotation": 1, "lexical": 1, "unresolved": 1},
        )

    def test_match_captures_shadow_imports_and_builtins(self):
        for pattern in [
            "{'client': requests}",
            "[*requests]",
            "{**requests}",
            "requests",
        ]:
            with self.subTest(pattern=pattern):
                report = self.report(
                    "import requests\ndef run(value):\n    match value:\n        case "
                    + pattern
                    + ":\n            requests.get('url')\n"
                )
                self.assertEqual(self.boundaries(report), [])
                self.assertEqual(report.calls[-1].resolution, "unresolved")
        report = self.report(
            "def run(value):\n    match value:\n        case open:\n            open('file')\n"
        )
        self.assertEqual(report.calls[0].resolution, "unresolved")

    def test_lambda_defaults_are_eager_but_body_is_deferred(self):
        report = self.report("""
            import requests
            def run():
                return lambda x=requests.get('default'), *, y=requests.post('keyword'): requests.delete('body')
        """)
        self.assertEqual(
            [c.resolved_name for c in report.calls], ["requests.get", "requests.post"]
        )
        self.assertEqual(len(self.boundaries(report)), 2)
        self.assertEqual(report.summary()["diagnostic_counts"], {"deferred_lambda": 1})


if __name__ == "__main__":
    unittest.main()
