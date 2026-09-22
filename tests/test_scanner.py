from __future__ import annotations

import contextlib
import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from flowsignal import scan
from flowsignal.cli import main
from flowsignal.config import Config, load_config


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def source(self, content, name="app.py"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    def report(self, content, config=None):
        self.source(content)
        return scan(self.root, config)

    def rules(self, report):
        return [finding.rule_id for finding in report.findings]

    def test_import_aliases_and_transitive_path(self):
        self.source(
            "from requests import get as fetch\ndef load():\n    return fetch('url')\n",
            "src/shop/client.py",
        )
        self.source(
            "from .client import load\ndef checkout():\n    return load()\n",
            "src/shop/api.py",
        )
        report = scan(self.root, Config(entrypoints=["shop.api.checkout"]))
        finding = next(item for item in report.findings if item.rule_id == "FS005")
        self.assertEqual(
            finding.paths, [["src/shop/api.py:checkout", "src/shop/client.py:load"]]
        )
        self.assertEqual(
            next(call for call in report.calls if call.boundary).resolved_name,
            "requests.get",
        )

    def test_package_root_initializer_resolves_relative_import(self):
        self.source("from .helpers import work\nwork()\n", "package/__init__.py")
        self.source("def work():\n    pass\n", "package/helpers.py")
        report = scan(self.root / "package")
        self.assertEqual(report.calls[0].target, "helpers.py:work")

    def test_generator_creation_does_not_observe_iteration_failures(self):
        report = self.report("""
            import requests
            import logging
            def values():
                yield requests.get('url')
            def work():
                try:
                    return values()
                except Exception:
                    logging.exception('construction failed')
                    raise
        """)
        self.assertIn("FS005", self.rules(report))
        self.assertEqual(
            next(call for call in report.calls if call.target).execution,
            "deferred_generator",
        )

    def test_silent_handler_provides_conditional_levels(self):
        report = self.report("""
            def work():
                try:
                    perform()
                except Exception:
                    return None
        """)
        finding = next(item for item in report.findings if item.rule_id == "FS001")
        self.assertEqual(
            [item.level for item in finding.recommendations], ["WARNING", "ERROR"]
        )
        self.assertTrue(all(item.condition for item in finding.recommendations))
        self.assertIn("does not prove", finding.assumptions[0])

    def test_outer_owner_covers_inner_boundary(self):
        report = self.report("""
            import requests
            import logging
            log = logging.getLogger(__name__)
            def inner():
                return requests.get('url')
            def main():
                try:
                    return inner()
                except Exception:
                    log.exception('operation failed')
                    raise
        """)
        self.assertNotIn("FS005", self.rules(report))
        self.assertNotIn("FS006", self.rules(report))

    def test_suppression_prevents_crediting_outer_logging(self):
        report = self.report("""
            import requests
            import logging
            def inner():
                return requests.get('url')
            def middle():
                try:
                    return inner()
                except Exception:
                    return None
            def outer():
                try:
                    return middle()
                except Exception:
                    logging.exception('failed')
                    raise
        """)
        self.assertIn("FS005", self.rules(report))
        self.assertIn("FS001", self.rules(report))

    def test_conditional_log_does_not_cover_every_handler_path(self):
        report = self.report("""
            import requests
            import logging
            def work(verbose):
                try:
                    return requests.get('url')
                except Exception:
                    if verbose:
                        logging.exception('failed')
                    return None
        """)
        self.assertIn("FS001", self.rules(report))
        self.assertIn("FS005", self.rules(report))

    def test_early_handler_return_does_not_credit_later_log(self):
        report = self.report("""
            import requests
            import logging
            def work(ignore):
                try:
                    return requests.get('url')
                except Exception:
                    if ignore:
                        return None
                    logging.exception('failed')
                    raise
        """)
        self.assertIn("FS001", self.rules(report))
        self.assertIn("FS005", self.rules(report))

    def test_mixed_handler_recovery_does_not_credit_outer_log(self):
        report = self.report("""
            import requests
            import logging
            def inner(recover):
                try:
                    return requests.get('url')
                except Exception:
                    if recover:
                        return None
                    raise
            def outer():
                try:
                    return inner(True)
                except Exception:
                    logging.exception('failed')
                    raise
        """)
        self.assertIn("FS005", self.rules(report))

    def test_typed_handler_does_not_claim_complete_coverage(self):
        report = self.report("""
            import requests
            import logging
            def work():
                try:
                    return requests.get('url')
                except ValueError:
                    logging.exception('failed')
                    raise
        """)
        self.assertIn("FS005", self.rules(report))

    def test_specific_suppressing_handler_before_logged_catch_all(self):
        report = self.report("""
            import requests
            import logging
            def work():
                try:
                    return requests.get('url')
                except ValueError:
                    return None
                except Exception:
                    logging.exception('failed')
                    raise
        """)
        self.assertIn("FS005", self.rules(report))

    def test_rethrow_duplicate_has_two_source_locations(self):
        report = self.report("""
            import logging
            def inner():
                try:
                    perform()
                except Exception:
                    logging.exception('inner')
                    raise
            def outer():
                try:
                    inner()
                except Exception:
                    logging.exception('outer')
                    raise
        """)
        finding = next(item for item in report.findings if item.rule_id == "FS004")
        self.assertEqual(len(finding.evidence), 2)
        self.assertEqual(finding.recommendations[0].kind, "no_additional_log")

    def test_shadowed_import_and_builtin_do_not_create_boundaries(self):
        report = self.report("""
            import requests
            def work(requests, open, logging):
                requests.get('url')
                open('path')
                logging.error('not necessarily a logger')
        """)
        self.assertFalse(any(call.boundary for call in report.calls))
        self.assertEqual(report.logs, [])

    def test_reassigned_import_is_unresolved(self):
        report = self.report(
            "import requests\nrequests = unknown\nrequests.get('url')\n"
        )
        self.assertFalse(any(call.boundary for call in report.calls))

    def test_unreachable_calls_and_logs_are_excluded(self):
        report = self.report("""
            import requests
            import logging
            def work():
                if False:
                    requests.get('never')
                False and requests.get('never')
                return 1
                logging.error('unreachable')
                requests.get('never')
        """)
        self.assertEqual(report.calls, [])

    def test_finally_return_is_high_priority(self):
        report = self.report("""
            def work():
                try:
                    risky()
                finally:
                    return None
        """)
        finding = next(item for item in report.findings if item.rule_id == "FS002")
        self.assertEqual(finding.priority, "high")
        self.assertEqual(finding.confidence, "high")

    def test_nested_function_return_is_not_a_finally_exit(self):
        report = self.report("""
            def work():
                try:
                    risky()
                finally:
                    def helper():
                        return None
        """)
        self.assertNotIn("FS002", self.rules(report))

    def test_nested_functions_have_correct_edges(self):
        report = self.report("""
            def outer():
                def inner():
                    pass
                inner()
        """)
        call = next(call for call in report.calls if call.expression == "inner")
        self.assertEqual(call.target, "app.py:outer.inner")

    def test_method_and_annotation_resolution(self):
        report = self.report("""
            class Service:
                def load(self):
                    pass
                def run(self):
                    return self.load()
            def work(service: Service):
                return service.run()
        """)
        self.assertEqual(
            {call.target for call in report.calls},
            {"app.py:Service.load", "app.py:Service.run"},
        )

    def test_custom_boundaries_and_logging_wrappers(self):
        report = self.report(
            """
            from company.gateway import fetch
            from company.telemetry import failed
            def work():
                try:
                    fetch()
                except Exception:
                    failed('failed')
                    raise
        """,
            Config(
                boundaries=[
                    {"pattern": "company.gateway.fetch", "category": "network"}
                ],
                loggers=[{"pattern": "company.telemetry.failed", "level": "ERROR"}],
            ),
        )
        self.assertTrue(any(call.boundary == "network" for call in report.calls))
        self.assertNotIn("FS005", self.rules(report))
        self.assertEqual(report.logs[0].level, "ERROR")

    def test_existing_trace_scope_avoids_extra_boundary_log(self):
        report = self.report("""
            from opentelemetry import trace
            import requests
            tracer = trace.get_tracer(__name__)
            def work():
                with tracer.start_as_current_span('fetch'):
                    return requests.get('url')
        """)
        self.assertNotIn("FS005", self.rules(report))

    def test_discarded_task_but_not_retained_task(self):
        report = self.report("""
            import asyncio
            async def work():
                task = asyncio.create_task(run())
                await task
                asyncio.create_task(run())
        """)
        self.assertEqual(self.rules(report).count("FS007"), 1)

    def test_task_creation_handler_does_not_cover_deferred_execution(self):
        report = self.report("""
            import asyncio
            import logging
            import requests
            async def worker():
                requests.get('url')
            async def main():
                try:
                    asyncio.create_task(worker())
                except Exception:
                    logging.exception('failed to schedule')
                    raise
        """)
        self.assertIn("FS005", self.rules(report))
        call = next(call for call in report.calls if call.target == "app.py:worker")
        self.assertEqual(call.execution, "deferred_coroutine")

    def test_awaited_failure_can_be_reported_by_caller(self):
        report = self.report("""
            import logging
            import requests
            async def worker():
                requests.get('url')
            async def main():
                try:
                    return await worker()
                except Exception:
                    logging.exception('operation failed')
                    raise
        """)
        self.assertNotIn("FS005", self.rules(report))
        call = next(call for call in report.calls if call.target == "app.py:worker")
        self.assertEqual(call.execution, "awaited")

    def test_lifecycle_info_requires_explicit_opt_in(self):
        self.source("def run():\n    return 1\n")
        self.assertNotIn(
            "FS008", self.rules(scan(self.root, Config(entrypoints=["app.run"])))
        )
        report = scan(self.root, Config(entrypoints=["app.run"], lifecycle_info=True))
        finding = next(item for item in report.findings if item.rule_id == "FS008")
        self.assertEqual(finding.recommendations[0].level, "INFO")

    def test_low_level_failure_only_at_recognized_entrypoint(self):
        self.source("""
            import logging
            def run():
                try:
                    risky()
                except Exception:
                    logging.info('failed')
                    raise
        """)
        self.assertNotIn("FS003", self.rules(scan(self.root)))
        self.assertIn(
            "FS003", self.rules(scan(self.root, Config(entrypoints=["app.run"])))
        )

    def test_syntax_error_is_reported_without_losing_other_files(self):
        self.source("def broken(:\n", "broken.py")
        self.source("import requests\nrequests.get('url')\n")
        report = scan(self.root)
        self.assertEqual(report.files_scanned, 1)
        self.assertTrue(
            any(item.code == "source_unreadable" for item in report.diagnostics)
        )
        self.assertIn("FS005", self.rules(report))

    def test_target_code_is_never_executed(self):
        marker = self.root / "executed.txt"
        self.source(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('must not run')\n"
        )
        scan(self.root)
        self.assertFalse(marker.exists())

    def test_default_and_custom_exclusions(self):
        for name in [".venv/lib.py", "vendor/lib.py", "app.py"]:
            self.source("import requests\nrequests.get('url')\n", name)
        report = scan(self.root, Config(exclude=["vendor"]))
        self.assertEqual(report.files_scanned, 1)

    def test_limits_report_incomplete_scan(self):
        self.source("pass\n", "a.py")
        self.source("pass\n", "b.py")
        report = scan(self.root, Config(max_files=1))
        self.assertEqual(report.files_scanned, 1)
        self.assertTrue(any(item.code == "file_limit" for item in report.diagnostics))
        report = scan(self.root, Config(max_file_bytes=1))
        self.assertEqual(report.files_scanned, 0)
        self.assertTrue(
            any(item.code == "file_size_limit" for item in report.diagnostics)
        )

    def test_recursive_graph_is_bounded_and_reports_are_deterministic(self):
        self.source(
            "import requests\ndef a():\n    b()\ndef b():\n    a()\n    requests.get('url')\n"
        )
        first = scan(self.root).to_dict()
        self.assertEqual(first, scan(self.root).to_dict())
        self.assertTrue(
            all(
                len(path) <= 8
                for finding in first["findings"]
                for path in finding["paths"]
            )
        )

    def test_duplicate_definitions_remain_ambiguous(self):
        report = self.report("def work():\n    pass\ndef work():\n    pass\nwork()\n")
        self.assertEqual(
            len({symbol.id for symbol in report.symbols}), len(report.symbols)
        )
        self.assertIsNone(report.calls[0].target)
        self.assertEqual(report.calls[0].resolution, "ambiguous")

    def test_chained_calls_have_distinct_graph_identities(self):
        report = self.report("""
            from pathlib import Path
            def work():
                try:
                    return Path('file').absolute().read_text()
                except Exception:
                    return None
        """)
        ids = {call.id for call in report.calls}
        self.assertEqual(len(ids), len(report.calls))
        self.assertEqual(len(report.calls), 3)
        self.assertTrue(
            all(
                identity in ids
                for handler in report.handlers
                for identity in handler.calls
            )
        )

    def test_json_cli_and_priority_exit_status(self):
        self.source(
            "def work():\n    try:\n        fail()\n    finally:\n        return None\n"
        )
        output = self.root / "report.json"
        code = main(
            [
                "scan",
                str(self.root),
                "--format",
                "json",
                "--output",
                str(output),
                "--fail-on",
                "high",
            ]
        )
        self.assertEqual(code, 1)
        data = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], "flowsignal-report-1")
        self.assertEqual(data["findings"][0]["rule_id"], "FS002")

    def test_incomplete_cli_does_not_report_success(self):
        self.source("def broken(:\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["scan", str(self.root)]), 2)

    def test_pyproject_config_and_validation(self):
        config = self.root / "pyproject.toml"
        config.write_text(
            '[tool.flowsignal]\nentrypoints = ["app.run"]\nlifecycle_info = true\n',
            encoding="utf-8",
        )
        self.assertTrue(load_config(config).lifecycle_info)
        config.write_text("[tool.flowsignal]\nmax_files = 0\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(config)

    def test_unknown_config_and_unmatched_entrypoint_are_visible(self):
        self.source("pass\n")
        report = scan(self.root, Config(entrypoints=["app.missing"]))
        self.assertTrue(
            any(item.code == "entrypoint_not_found" for item in report.diagnostics)
        )
        config = self.root / "flowsignal.toml"
        config.write_text("[flowsignal]\nmax_filez = 10\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(config)


if __name__ == "__main__":
    unittest.main()
