from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from flowsignal import scan
from flowsignal.cli import render_text
from flowsignal.config import Config
from flowsignal.diagram import graph_data, render_html, render_mermaid


class CoverageExplanationTests(unittest.TestCase):
    def report(self, code, config=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.py"
            path.write_text(textwrap.dedent(code), encoding="utf-8")
            report = scan(Path(directory), config)
        decisions = {d.call_id: d for d in report.coverage}
        self.assertEqual(set(decisions), {c.id for c in report.calls if c.boundary})
        findings = {
            (f.symbol, f.evidence[0].location.line)
            for f in report.findings
            if f.rule_id == "FS005"
        }
        for decision in decisions.values():
            self.assertEqual(
                (decision.symbol, decision.location.line) in findings,
                decision.status == "not_established",
            )
            self.assertTrue(decision.evidence)
            self.assertLessEqual(len(decision.evidence), 64)
        self.assertEqual(len(decisions), 1)
        self.decision = report.coverage[0]
        self.reasons = {e.reason for e in self.decision.evidence}
        return report

    def test_unconditional_error_has_source_evidence(self):
        report = self.report(
            """
            import requests, logging
            def main():
                try: requests.get('secret-url')
                except Exception: logging.exception('failed')
        """,
            Config(include_source=False),
        )
        self.assertEqual(self.decision.status, "recognized")
        item = next(e for e in self.decision.evidence if e.reason == "failure_log")
        self.assertEqual(item.location.line, 5)
        self.assertTrue(item.credited)
        self.assertNotIn("secret-url", str(report.to_dict()))
        self.assertIn("Boundary coverage: recognized", render_text(report))
        self.assertIn("Coverage: recognized", render_mermaid(report))
        self.assertIn("Coverage decision", render_html(report))
        self.assertEqual(
            next(n for n in graph_data(report)["nodes"] if n["kind"] == "boundary")[
                "coverage"
            ]["status"],
            "recognized",
        )

    def test_conditional_warning_is_rejected(self):
        self.report("""
            import requests, logging
            def main(enabled):
                try: requests.get('url')
                except Exception:
                    if enabled: logging.warning('failed')
        """)
        self.assertEqual(self.decision.status, "not_established")
        self.assertTrue(
            {"conditional_log", "handler_unreported", "silent_consumption"}
            <= self.reasons
        )

    def test_low_level_log_is_rejected(self):
        self.report("""
            import requests, logging
            def main():
                try: requests.get('url')
                except Exception: logging.info('failed')
        """)
        self.assertIn("log_below_warning", self.reasons)

    def test_typed_handler_is_partial(self):
        self.report("""
            import requests, logging
            def main():
                try: requests.get('url')
                except ValueError: logging.exception('failed')
        """)
        self.assertEqual(self.decision.status, "not_established")
        self.assertIn("typed_handler", self.reasons)
        self.assertIn("no_known_callers", self.reasons)

    def test_configured_reporter_owner_and_condition(self):
        for conditional in [False, True]:
            with self.subTest(conditional=conditional):
                statement = (
                    "if enabled: record('failed')"
                    if conditional
                    else "record('failed')"
                )
                self.report(
                    f"""
                    import requests
                    from health import record
                    def main(enabled):
                        try: requests.get('url')
                        except Exception:
                            {statement}
                """,
                    Config(
                        reporters=[
                            {
                                "pattern": "health.record",
                                "kind": "diagnostic",
                                "owner": "Workflow health",
                            }
                        ]
                    ),
                )
                evidence = next(e for e in self.decision.evidence if e.owner)
                self.assertEqual(evidence.owner, "Workflow health")
                self.assertEqual(evidence.credited, not conditional)
                self.assertEqual(
                    self.decision.status,
                    "not_established" if conditional else "recognized",
                )

    def test_all_known_callers_report(self):
        self.report("""
            import requests, logging
            def fetch(): requests.get('url')
            def owner():
                try: fetch()
                except Exception: logging.exception('failed')
        """)
        self.assertEqual(self.decision.status, "recognized")
        self.assertIn("all_known_callers", self.reasons)
        self.assertTrue(
            any(
                e.symbol.endswith(":owner") and e.reason == "failure_log"
                for e in self.decision.evidence
            )
        )

    def test_one_uncovered_caller_blocks_coverage(self):
        self.report("""
            import requests, logging
            def fetch(): requests.get('url')
            def owner():
                try: fetch()
                except Exception: logging.exception('failed')
            def unowned(): fetch()
        """)
        self.assertEqual(self.decision.status, "not_established")
        self.assertIn("no_known_callers", self.reasons)

    def test_silent_consumption_blocks_outer_owner(self):
        self.report("""
            import requests, logging
            def fetch():
                try: requests.get('url')
                except Exception: return None
            def owner():
                try: fetch()
                except Exception: logging.exception('failed')
        """)
        self.assertIn("silent_consumption", self.reasons)
        self.assertNotIn("failure_log", self.reasons)

    def test_trace_options_and_source(self):
        for method, options, recognized in [
            ("start_as_current_span", "", True),
            ("start_as_current_span", ", record_exception=False", False),
            ("start_as_current_span", ", **options", False),
            ("start_span", "", False),
        ]:
            with self.subTest(method=method, options=options):
                self.report(f"""
                    import requests
                    from opentelemetry import trace
                    tracer = trace.get_tracer(__name__)
                    def main(options):
                        with tracer.{method}('span'{options}):
                            requests.get('url')
                """)
                self.assertEqual(
                    self.decision.status,
                    "recognized" if recognized else "not_established",
                )
                reason = "trace_enabled" if recognized else "trace_not_credited"
                item = next(e for e in self.decision.evidence if e.reason == reason)
                self.assertEqual(item.location.line, 6)

    def test_unknown_context_blocks_outer_span_and_handler(self):
        self.report("""
            import requests, logging
            from opentelemetry import trace
            tracer = trace.get_tracer(__name__)
            def main():
                with tracer.start_as_current_span('span'):
                    try:
                        with unknown(): requests.get('url')
                    except Exception: logging.exception('failed')
        """)
        self.assertTrue(
            {
                "handler_blocked",
                "outer_trace_blocked",
                "unknown_context",
                "propagation_unknown",
            }
            <= self.reasons
        )
        self.assertEqual(self.decision.status, "not_established")

    def test_cycle_and_depth_limit(self):
        self.report("""
            import requests
            def first():
                requests.get('url')
                second()
            def second(): first()
        """)
        self.assertIn("caller_cycle", self.reasons)
        self.report(
            """
            import requests
            def first(): requests.get('url')
            def second(): first()
            def third(): second()
        """,
            Config(max_path_depth=1),
        )
        self.assertIn("caller_depth_limit", self.reasons)

    def test_graph_budget_is_explained(self):
        report = self.report(
            """
            import requests
            def first(): requests.get('url')
            def second(): first()
        """,
            Config(max_graph_steps=1),
        )
        self.assertIn("graph_work_limit", self.reasons)
        self.assertEqual(report.summary()["status"], "incomplete")

    def test_deferred_boundary_is_not_credited(self):
        self.report(
            """
            import logging
            async def boundary(): pass
            def main():
                try: boundary()
                except Exception: logging.exception('failed')
        """,
            Config(boundaries=[{"pattern": "app.boundary", "category": "network"}]),
        )
        self.assertEqual(self.decision.status, "not_established")
        self.assertEqual(self.reasons, {"deferred_execution"})

    def test_explanation_cap_does_not_change_verdict(self):
        code = 'import requests, logging\ndef main():\n    try: requests.get("url")\n    except Exception:\n'
        code += '        logging.error("failed")\n' * 80
        self.report(code)
        self.assertEqual(self.decision.status, "recognized")
        self.assertEqual(len(self.decision.evidence), 64)
        self.assertTrue(self.decision.truncated)


if __name__ == "__main__":
    unittest.main()
