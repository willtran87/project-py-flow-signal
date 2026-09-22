from __future__ import annotations

import contextlib
import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from flowsignal import scan
from flowsignal.baseline import compare, make_baseline, read_baseline
from flowsignal.cli import main
from flowsignal.config import Config
from flowsignal.diagram import graph_data, render_html, render_mermaid


class EnterpriseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def source(self, code, name="app.py"):
        path = self.root / name
        path.write_text(textwrap.dedent(code), encoding="utf-8")
        return path

    def report(self, code, config=None):
        self.source(code)
        return scan(self.root, config)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(list(map(str, args)))
        return status, out.getvalue(), err.getvalue()

    def test_configured_diagnostic_has_owner_without_becoming_log(self):
        report = self.report(
            """
            from health import record
            import requests
            def run():
                try: requests.get('url')
                except Exception:
                    record('failed')
                    return None
        """,
            Config(
                reporters=[
                    {
                        "pattern": "health.record",
                        "kind": "diagnostic",
                        "owner": "health report",
                    }
                ]
            ),
        )
        self.assertFalse(report.findings)
        self.assertFalse(report.logs)
        self.assertEqual(report.reporting_signals[0].owner, "health report")
        data = graph_data(report)
        signal = next(n for n in data["nodes"] if n["kind"] == "handler")["existing"][0]
        self.assertEqual(signal["kind"], "diagnostic")
        self.assertIn("health report", signal["text"])
        self.assertIn("DIAGNOSTIC", render_mermaid(report))

    def test_unconfigured_print_and_append_do_not_establish_coverage(self):
        report = self.report("""
            def run():
                try: fail()
                except Exception:
                    print('failed')
                    diagnostics.append('failed')
        """)
        self.assertIn("FS001", [f.rule_id for f in report.findings])
        self.assertFalse(report.reporting_signals)

    def test_expression_reporter_requires_and_respects_scope(self):
        rule = {
            "pattern": "diagnostics.append",
            "kind": "diagnostic",
            "owner": "scan report",
            "match": "expression",
            "scope": "app.run",
        }
        report = self.report(
            """
            def run():
                try: fail()
                except Exception: diagnostics.append('failed')
            def other():
                try: fail()
                except Exception: diagnostics.append('failed')
        """,
            Config(reporters=[rule]),
        )
        self.assertEqual([f.symbol for f in report.findings], ["app.py:other"])
        with self.assertRaises(ValueError):
            Config(reporters=[rule | {"scope": "*"}]).validate()

    def test_conditional_and_shadowed_reporters_do_not_cover(self):
        for signature, statement in (
            ("flag", "if flag: record('failed')"),
            ("record", "record('failed')"),
        ):
            with self.subTest(statement=statement):
                report = self.report(
                    f"""
                    from health import record
                    def run({signature}):
                        try: fail()
                        except Exception:
                            {statement}
                """,
                    Config(
                        reporters=[
                            {
                                "pattern": "health.record",
                                "kind": "diagnostic",
                                "owner": "health report",
                            }
                        ]
                    ),
                )
                self.assertIn("FS001", [f.rule_id for f in report.findings])

    def test_stderr_contract_requires_import_resolved_stderr(self):
        for imported, parameter, destination, covered in (
            ("import sys", "", "sys.stderr", True),
            ("import sys as system", "", "system.stderr", True),
            ("import sys", "sys", "sys.stderr", False),
            ("", "", "sys.stderr", False),
            ("import sys", "", "sys.stdout", False),
        ):
            with self.subTest(
                imported=imported, parameter=parameter, destination=destination
            ):
                report = self.report(
                    f"""
                    {imported}
                    def run({parameter}):
                        try: fail()
                        except Exception: print('failed', file={destination})
                """,
                    Config(
                        reporters=[
                            {
                                "pattern": "builtins.print",
                                "kind": "stderr",
                                "owner": "CLI",
                            }
                        ]
                    ),
                )
                self.assertEqual(bool(report.reporting_signals), covered)
                self.assertEqual(
                    any(f.rule_id == "FS001" for f in report.findings), not covered
                )

    def test_error_return_must_be_returned_directly(self):
        for statement, covered in (
            ("return Failure('failed')", True),
            ("Failure('failed')", False),
        ):
            with self.subTest(statement=statement):
                report = self.report(
                    f"""
                    from result import Failure
                    def run():
                        try: fail()
                        except Exception: {statement}
                """,
                    Config(
                        reporters=[
                            {
                                "pattern": "result.Failure",
                                "kind": "error_return",
                                "owner": "result consumer",
                            }
                        ]
                    ),
                )
                self.assertEqual(bool(report.reporting_signals), covered)
                self.assertEqual(
                    any(f.rule_id == "FS001" for f in report.findings), not covered
                )

    def test_deferred_reporter_and_typed_partial_handler_do_not_cover_boundary(self):
        report = self.report(
            """
            import requests
            async def report_failure(): pass
            def run():
                try: requests.get('url')
                except Exception: report_failure()
        """,
            Config(
                reporters=[
                    {
                        "pattern": "app.report_failure",
                        "kind": "diagnostic",
                        "owner": "operations",
                    }
                ]
            ),
        )
        self.assertEqual({f.rule_id for f in report.findings}, {"FS001", "FS005"})
        self.assertTrue(report.reporting_signals[0].conditional)
        report = self.report(
            """
            from health import report_failure
            import requests
            def run():
                try: requests.get('url')
                except ValueError: report_failure()
        """,
            Config(
                reporters=[
                    {
                        "pattern": "health.report_failure",
                        "kind": "diagnostic",
                        "owner": "operations",
                    }
                ]
            ),
        )
        self.assertEqual([f.rule_id for f in report.findings], ["FS005"])

    def test_property_reads_from_annotations_construction_and_self(self):
        report = self.report("""
            class Worker:
                @property
                def status(self): return 1
                def inspect(self): return self.status
            def run(worker: Worker):
                worker.status
                Worker().status
        """)
        properties = [c for c in report.calls if c.execution == "implicit_property"]
        self.assertEqual(len(properties), 3)
        self.assertTrue(all(c.target == "app.py:Worker.status" for c in properties))
        self.assertTrue(all(c.resolution == "inferred_property" for c in properties))
        self.assertIn("property getter", render_mermaid(report))

    def test_dictionary_field_element_and_values_iteration_reach_getter(self):
        report = self.report("""
            class Handler:
                @property
                def catches_all(self): return True
            class Analysis:
                handlers: dict[str, Handler]
            def first(analysis: Analysis, key):
                handler = analysis.handlers[key]
                return handler.catches_all
            def all_handlers(analysis: Analysis):
                for handler in analysis.handlers.values():
                    if handler.catches_all: return True
        """)
        properties = [c for c in report.calls if c.execution == "implicit_property"]
        self.assertEqual(len(properties), 2)
        self.assertTrue(
            all(c.target == "app.py:Handler.catches_all" for c in properties)
        )

    def test_class_access_alias_store_and_unknown_receiver_are_not_getter_calls(self):
        report = self.report("""
            class Worker:
                @property
                def status(self): return 1
            Alias = Worker
            def run(unknown):
                Worker.status
                Alias.status
                unknown.status
                worker = Worker()
                worker.status = 2
                del worker.status
        """)
        self.assertFalse(any(c.execution == "implicit_property" for c in report.calls))

    def test_shadowing_and_conflicting_receivers_do_not_invent_getter_edges(self):
        for statement in (
            "worker = unknown()\nworker.status",
            "[worker.status for worker in unknown()]",
            "match unknown():\n    case {'worker': worker}: worker.status",
            "from external import worker\nworker.status",
        ):
            with self.subTest(statement=statement):
                self.source(
                    "class Worker:\n    @property\n    def status(self): return 1\ndef run(worker: Worker):\n"
                    + textwrap.indent(statement, "    ")
                    + "\n"
                )
                report = scan(self.root)
                self.assertFalse(
                    any(c.execution == "implicit_property" for c in report.calls)
                )

    def test_ambiguous_union_decorators_and_custom_attribute_access_are_conservative(
        self,
    ):
        for modifier, parameter in (
            ("@decorate\n", "Worker"),
            ("", "Worker | Other"),
        ):
            with self.subTest(modifier=modifier, parameter=parameter):
                report = self.report(
                    modifier
                    + "class Worker:\n    @property\n    def status(self): return 1\nclass Other: pass\ndef run(worker: "
                    + parameter
                    + "):\n    return worker.status\n"
                )
                self.assertFalse(
                    any(c.execution == "implicit_property" for c in report.calls)
                )
        report = self.report("""
            class Worker:
                def __getattribute__(self, name): return 42
                @property
                def status(self): return 1
            def run(worker: Worker): return worker.status
        """)
        self.assertFalse(any(c.execution == "implicit_property" for c in report.calls))

    def test_property_returning_callable_is_not_invoked_twice(self):
        report = self.report("""
            class Worker:
                @property
                def callback(self): return lambda: 1
            def run(worker: Worker): return worker.callback()
        """)
        calls = [c for c in report.calls if c.symbol == "app.py:run"]
        self.assertEqual(sum(c.execution == "implicit_property" for c in calls), 1)
        self.assertEqual(sum(c.target is None for c in calls), 1)
        self.assertEqual(len({c.id for c in calls}), 2)

    def test_slices_class_scope_and_comprehension_shadowing_do_not_invent_getters(self):
        report = self.report("""
            class Worker:
                @property
                def status(self): return 1
            worker = Worker()
            class Container:
                worker = unknown()
                status = worker.status
            def run(items: list[Worker]):
                items[:].status
                [items[0].status for items in unknown()]
        """)
        self.assertFalse(any(c.execution == "implicit_property" for c in report.calls))

    def test_malformed_baseline_dismissal_requires_reason(self):
        document = make_baseline(self.report("import requests\nrequests.get('url')\n"))
        document["findings"][0]["review_status"] = "dismissed"
        path = self.root / "baseline.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reason"):
            read_baseline(path)

    def test_incomplete_scan_cannot_replace_saved_baseline(self):
        self.source("pass\n")
        baseline = self.root / "baseline.json"
        self.assertEqual(self.cli("scan", self.root, "--save-baseline", baseline)[0], 0)
        original = baseline.read_bytes()
        self.source("def invalid(:\n")
        self.assertEqual(self.cli("scan", self.root, "--save-baseline", baseline)[0], 2)
        self.assertEqual(baseline.read_bytes(), original)

    def test_getter_failure_can_reach_callers_reporting_handler(self):
        report = self.report("""
            import logging
            import requests
            class Worker:
                @property
                def data(self): return requests.get('url')
            def run(worker: Worker):
                try: return worker.data
                except Exception:
                    logging.exception('failed')
                    raise
        """)
        self.assertNotIn("FS005", [f.rule_id for f in report.findings])

    def test_baseline_line_movement_preserves_match_without_source_excerpts(self):
        source = "import requests\ndef run():\n    requests.get('url')\n"
        old = self.report(source)
        baseline = make_baseline(old)
        new = self.report("# comment\n\n" + source, Config(include_source=False))
        compare(new, baseline)
        self.assertEqual(new.baseline["counts"]["unchanged"], 1)
        self.assertEqual(new.findings[0].fingerprint, old.findings[0].fingerprint)
        self.assertNotEqual(new.findings[0].id, old.findings[0].id)
        self.assertEqual(new.findings[0].evidence[0].code, "")

    def test_baseline_tracks_new_resolved_and_duplicate_occurrences(self):
        old = self.report("import requests\ndef run():\n    requests.get('old')\n")
        new = self.report(
            "import requests\ndef run():\n    requests.get('new')\n    requests.get('new')\n"
        )
        compare(new, make_baseline(old))
        self.assertEqual(
            new.baseline["counts"],
            {"new": 2, "resolved": 1, "unchanged": 0, "unverified": 0, "dismissed": 0},
        )
        latest = self.report("import requests\ndef run():\n    requests.get('new')\n")
        compare(latest, make_baseline(new))
        self.assertEqual(latest.baseline["counts"]["unchanged"], 1)
        self.assertEqual(latest.baseline["counts"]["resolved"], 1)

    def test_incomplete_comparison_never_claims_resolution(self):
        baseline = make_baseline(self.report("import requests\nrequests.get('url')\n"))
        partial = self.report("def invalid(:\n")
        compare(partial, baseline)
        self.assertEqual(partial.baseline["counts"]["resolved"], 0)
        self.assertEqual(partial.baseline["counts"]["unverified"], 1)
        with self.assertRaises(ValueError):
            make_baseline(partial)

    def test_scope_change_requires_an_explicit_new_baseline(self):
        baseline = make_baseline(self.report("import requests\nrequests.get('url')\n"))
        changed = scan(self.root, Config(exclude=["vendor"]))
        with self.assertRaisesRegex(ValueError, "scope"):
            compare(changed, baseline)

    def test_cli_baseline_review_reason_new_only_gate_and_restore(self):
        self.source("import requests\ndef run():\n    requests.get('url')\n")
        baseline = self.root / "baseline.json"
        status, _, err = self.cli("scan", self.root, "--save-baseline", baseline)
        self.assertEqual((status, err), (0, ""))
        fingerprint = read_baseline(baseline)["findings"][0]["fingerprint"]
        self.assertEqual(
            self.cli(
                "scan",
                self.root,
                "--baseline",
                baseline,
                "--fail-on",
                "low",
                "--fail-on-new",
            )[0],
            0,
        )
        self.assertEqual(
            self.cli("scan", self.root, "--baseline", baseline, "--fail-on", "low")[0],
            1,
        )
        original = baseline.read_bytes()
        self.assertEqual(self.cli("review", "dismiss", baseline, fingerprint)[0], 2)
        self.assertEqual(baseline.read_bytes(), original)
        reason = 'Owned by caller <script>alert("x")</script>'
        self.assertEqual(
            self.cli("review", "dismiss", baseline, fingerprint, "--reason", reason)[0],
            0,
        )
        status, raw, err = self.cli(
            "scan",
            self.root,
            "--baseline",
            baseline,
            "--fail-on",
            "low",
            "--format",
            "json",
        )
        self.assertEqual((status, err), (0, ""))
        report = json.loads(raw)
        self.assertEqual(report["findings"][0]["review_reason"], reason)
        self.assertEqual(report["summary"]["dismissed_findings"], 1)
        live = scan(self.root)
        compare(live, read_baseline(baseline))
        html = render_html(live)
        self.assertNotIn(reason, html)
        self.assertIn("\\u003cscript\\u003e", html)
        self.source(
            "import requests\ndef run():\n    requests.get('url')\n    requests.post('other')\n"
        )
        self.assertEqual(
            self.cli(
                "scan",
                self.root,
                "--baseline",
                baseline,
                "--fail-on",
                "low",
                "--fail-on-new",
            )[0],
            1,
        )
        self.assertEqual(self.cli("review", "restore", baseline, fingerprint)[0], 0)
        self.assertEqual(
            read_baseline(baseline)["findings"][0]["review_status"], "active"
        )

    def test_invalid_baseline_and_output_paths_preserve_existing_files(self):
        self.source("pass\n")
        baseline = self.root / "baseline.json"
        baseline.write_text("{}", encoding="utf-8")
        output = self.root / "report.json"
        output.write_text("previous", encoding="utf-8")
        self.assertEqual(
            self.cli("scan", self.root, "--baseline", baseline, "--output", output)[0],
            2,
        )
        self.assertEqual(output.read_text(), "previous")
        self.assertEqual(
            self.cli(
                "scan",
                self.root,
                "--save-baseline",
                self.root / "app.py",
                "--output",
                output,
            )[0],
            2,
        )
        self.assertEqual(output.read_text(), "previous")
        self.assertEqual(self.cli("scan", self.root, "--fail-on-new")[0], 2)
        self.assertEqual(
            self.cli("scan", self.root, "--save-baseline", output, "--output", output)[
                0
            ],
            2,
        )


if __name__ == "__main__":
    unittest.main()
