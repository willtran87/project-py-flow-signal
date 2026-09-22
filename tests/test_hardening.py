from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from flowsignal import scan
from flowsignal.cli import main
from flowsignal.config import Config, load_config
from flowsignal.diagram import render_html
from flowsignal.source_io import read_snapshot


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def source(self, content, name="app.py"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    def report(self, source, config=None):
        self.source(source)
        return scan(self.root, config)

    def rules(self, report):
        return {finding.rule_id for finding in report.findings}

    def test_closure_parameter_masks_module_import(self):
        report = self.report("""
            import requests
            def outer(requests):
                def inner():
                    return requests.get('url')
                return inner()
        """)
        self.assertFalse(any(call.boundary for call in report.calls))
        self.assertEqual(
            next(
                call for call in report.calls if call.expression == "requests.get"
            ).resolution,
            "unresolved",
        )

    def test_nested_local_function_wins_over_module_function(self):
        report = self.report("""
            def helper():
                pass
            def outer():
                def helper():
                    pass
                def inner():
                    helper()
                inner()
        """)
        call = next(
            call for call in report.calls if call.symbol == "app.py:outer.inner"
        )
        self.assertEqual(call.target, "app.py:outer.helper")

    def test_duplicate_outer_names_preserve_actual_lexical_parent(self):
        report = self.report("""
            import requests
            def outer(requests):
                def inner():
                    requests.get('url')
            def outer(requests):
                def inner():
                    requests.get('url')
        """)
        self.assertFalse(any(call.boundary for call in report.calls))

    def test_comprehension_target_does_not_leak_or_use_outer_import(self):
        report = self.report("""
            import requests
            def run(clients):
                values = [requests.get('local') for requests in clients]
                return requests.get('external')
        """)
        calls = [call for call in report.calls if call.expression == "requests.get"]
        self.assertEqual([call.boundary for call in calls], [None, "network"])

    def test_class_namespace_masks_module_import(self):
        report = self.report("""
            import requests
            class Container:
                requests = object()
                data = requests.get('local')
        """)
        self.assertFalse(any(call.boundary for call in report.calls))

    def test_builtin_calls_are_not_reported_as_unknown_dispatch(self):
        report = self.report("def run(value):\n    return str(len(value))\n")
        self.assertTrue(all(call.resolution == "builtin" for call in report.calls))
        self.assertEqual(report.summary()["unresolved_calls"], 0)

    def test_imported_client_preparation_is_not_network_io(self):
        report = self.report("""
            import requests
            import httpx
            def run():
                session = requests.Session()
                client = httpx.Client()
                session.prepare_request(request)
                client.build_request('GET', 'url')
        """)
        self.assertFalse(any(call.boundary for call in report.calls))

    def test_trace_with_disabled_error_recording_does_not_hide_gap(self):
        report = self.report("""
            from opentelemetry import trace
            import requests
            tracer = trace.get_tracer(__name__)
            def run():
                with tracer.start_as_current_span('work', record_exception=False):
                    requests.get('url')
        """)
        call = next(call for call in report.calls if call.boundary)
        self.assertTrue(call.traced)
        self.assertFalse(call.trace_failure_coverage)
        self.assertIn("FS005", self.rules(report))

    def test_start_span_alone_does_not_prove_error_capture(self):
        report = self.report("""
            from opentelemetry import trace
            import requests
            tracer = trace.get_tracer(__name__)
            def run():
                with tracer.start_span('work'):
                    requests.get('url')
        """)
        self.assertIn("FS005", self.rules(report))

    def test_exception_consumed_inside_trace_remains_reviewable(self):
        report = self.report("""
            from opentelemetry import trace
            import requests
            tracer = trace.get_tracer(__name__)
            def run():
                with tracer.start_as_current_span('work'):
                    try:
                        requests.get('url')
                    except Exception:
                        return None
        """)
        self.assertIn("FS005", self.rules(report))

    def test_unknown_trace_options_do_not_prove_error_capture(self):
        report = self.report("""
            from opentelemetry import trace
            import requests
            tracer = trace.get_tracer(__name__)
            def run(options):
                with tracer.start_as_current_span('work', **options):
                    requests.get('url')
        """)
        self.assertIn("FS005", self.rules(report))

    def test_nonlogging_error_constant_does_not_satisfy_failure_logging(self):
        report = self.report("""
            import logging
            def run(custom):
                try:
                    action()
                except Exception:
                    logging.log(custom.ERROR, 'failed')
                    return None
        """)
        self.assertEqual(report.logs, [])
        self.assertIn("FS001", self.rules(report))
        self.assertTrue(
            any(item.code == "dynamic_log_level" for item in report.diagnostics)
        )

    def test_keyword_logging_level_is_recognized(self):
        report = self.report(
            "import logging\nlogging.log(level=logging.ERROR, msg='failed')\n"
        )
        self.assertEqual(report.logs[0].level, "ERROR")

    def test_monorepo_roots_resolve_imports_without_execution(self):
        self.source(
            "from toolkit.client import fetch\ndef run():\n    return fetch()\n",
            "services/api/src/api/jobs.py",
        )
        self.source(
            "def fetch():\n    pass\n", "libraries/shared/src/toolkit/client.py"
        )
        report = scan(
            self.root, Config(source_roots=["services/api/src", "libraries/shared/src"])
        )
        call = next(call for call in report.calls if call.expression == "fetch")
        self.assertEqual(call.target, "libraries/shared/src/toolkit/client.py:fetch")

    def test_duplicate_modules_do_not_create_false_internal_edges(self):
        self.source("def one():\n    pass\n", "a/pkg/client.py")
        self.source("def two():\n    pass\n", "b/pkg/client.py")
        self.source("from pkg.client import one\none()\n")
        report = scan(self.root, Config(source_roots=["a", "b"]))
        call = next(call for call in report.calls if call.expression == "one")
        self.assertIsNone(call.target)
        self.assertEqual(call.resolution, "ambiguous")
        self.assertEqual(report.summary()["status"], "incomplete")

    def test_missing_or_escaping_source_root_is_visible(self):
        self.source("pass\n")
        report = scan(self.root, Config(source_roots=["missing"]))
        self.assertEqual(report.summary()["status"], "incomplete")
        with self.assertRaises(ValueError):
            scan(self.root, Config(source_roots=["../elsewhere"]))

    def test_total_source_budget_stops_with_explicit_status(self):
        self.source("pass\n", "a.py")
        self.source("pass\n", "b.py")
        report = scan(self.root, Config(max_total_bytes=6))
        self.assertEqual(report.files_scanned, 1)
        self.assertEqual(
            report.analysis_stats["source_bytes"],
            len((self.root / "a.py").read_bytes()),
        )
        self.assertEqual(report.summary()["status"], "incomplete")

    def test_ast_node_budget_is_global(self):
        self.source("x = 1\n", "a.py")
        self.source("x = 2\n", "b.py")
        report = scan(self.root, Config(max_ast_nodes=6))
        self.assertEqual(report.files_scanned, 1)
        self.assertTrue(
            any(item.code == "ast_nodes_limit" for item in report.diagnostics)
        )

    def test_deep_ast_is_skipped_before_recursive_visitors(self):
        report = self.report(
            "value = " + "[" * 45 + "0" + "]" * 45 + "\n", Config(max_ast_depth=20)
        )
        self.assertEqual(report.files_scanned, 0)
        self.assertTrue(
            any(item.code == "ast_depth_limit" for item in report.diagnostics)
        )

    def test_graph_budget_retains_findings_and_reports_incompleteness(self):
        report = self.report(
            "import requests\ndef inner():\n    requests.get('url')\ndef outer():\n    inner()\n",
            Config(max_graph_steps=1),
        )
        self.assertIn("FS005", self.rules(report))
        self.assertEqual(report.summary()["status"], "incomplete")
        self.assertLessEqual(report.analysis_stats["graph_steps"], 1)

    def test_inventory_binds_analyzed_source_bytes(self):
        path = self.source("pass\n")
        report = scan(self.root)
        record = next(item for item in report.inventory if item["path"] == "app.py")
        self.assertEqual(record["status"], "analyzed")
        self.assertEqual(
            record["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertEqual(report.to_dict(), scan(self.root).to_dict())

    def test_dense_cyclic_call_graph_obeys_global_work_budget(self):
        functions = []
        for index in range(40):
            calls = [f"    task_{other}()" for other in range(40)]
            functions.append(
                f"def task_{index}():\n"
                + "\n".join(calls)
                + "\n    requests.get('url')\n"
            )
        report = self.report(
            "import requests\n" + "\n".join(functions),
            Config(max_graph_steps=2000),
        )
        self.assertEqual(report.summary()["status"], "incomplete")
        self.assertLessEqual(report.analysis_stats["graph_steps"], 2000)
        self.assertEqual(sum(f.rule_id == "FS005" for f in report.findings), 40)
        self.assertEqual(
            sum(d.code == "graph_work_limit" for d in report.diagnostics), 1
        )

    def test_snapshot_rejects_replacement_between_stat_and_open(self):
        path = self.source("original = 1\n")
        replacement = self.source("replacement = 2\n", "replacement.py")
        original_open = os.open

        def swapped_open(target, flags):
            os.replace(replacement, path)
            return original_open(target, flags)

        with patch("flowsignal.source_io.os.open", side_effect=swapped_open):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                read_snapshot(path, 1000, self.root)

    def test_snapshot_checks_size_and_root_and_rejects_links(self):
        path = self.source("pass\n")
        with self.assertRaises(ValueError):
            read_snapshot(path, 1)
        child = self.root / "child"
        child.mkdir()
        with self.assertRaises(ValueError):
            read_snapshot(path, 1000, child)
        link = self.root / "link.py"
        try:
            link.symlink_to(path)
        except OSError:
            self.skipTest("Symbolic-link creation is unavailable for this account")
        with self.assertRaises(ValueError):
            read_snapshot(link, 1000)

    def test_config_input_is_bounded_and_regular(self):
        path = self.root / "flowsignal.toml"
        path.write_bytes(b"#" * 1_000_001)
        with self.assertRaises(ValueError):
            load_config(path)

    def test_source_omission_removes_literals_from_call_expressions_and_evidence(self):
        report = self.report(
            """
            import logging
            def run():
                try:
                    Client('SECRET_SENTINEL').invoke()
                except Exception:
                    logging.error('SECRET_SENTINEL')
                    return None
        """,
            Config(include_source=False),
        )
        self.assertNotIn("SECRET_SENTINEL", json.dumps(report.to_dict()))
        self.assertNotIn("SECRET_SENTINEL", render_html(report))

    def test_cli_incomplete_status_cannot_be_hidden_by_confidence_filter(self):
        self.source("def broken(:\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                ["scan", str(self.root), "--format", "json", "--min-confidence", "high"]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(output.getvalue())["summary"]["status"], "incomplete"
        )


if __name__ == "__main__":
    unittest.main()
