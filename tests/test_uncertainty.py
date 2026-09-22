from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from flowsignal import scan
from flowsignal.cli import render_text
from flowsignal.config import Config
from flowsignal.diagram import graph_data, render_html, render_mermaid


class UncertaintyTests(unittest.TestCase):
    def report(self, source, config=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.py"
            path.write_text(textwrap.dedent(source), encoding="utf-8")
            report = scan(path, config)
        self.assertEqual(
            {g.call_id for g in report.resolution_gaps},
            {c.id for c in report.calls if c.resolution in {"unresolved", "ambiguous"}},
        )
        return report

    def test_callback_and_unknown_receiver_have_actions(self):
        report = self.report(
            """
            def run(callback, client):
                callback()
                client.fetch()
        """,
            Config(entrypoints=["app.run"]),
        )
        self.assertEqual(
            [g.reason for g in report.resolution_gaps],
            ["callback_parameter", "unknown_receiver"],
        )
        for gap in report.resolution_gaps:
            self.assertEqual(gap.entrypoints, ["app.py:run"])
            self.assertFalse(gap.context_truncated)
            self.assertTrue(gap.action)

    def test_conflicting_types(self):
        report = self.report("""
            class A: pass
            class B: pass
            def run():
                client = A()
                client = B()
                client.fetch()
        """)
        self.assertEqual(
            report.resolution_gaps[-1].reason, "conflicting_receiver_types"
        )

    def test_inherited_method_and_super_are_not_external_imports(self):
        report = self.report("""
            class Base:
                def fetch(self): pass
            class Child(Base):
                def run(self):
                    self.fetch()
                    super().fetch()
        """)
        self.assertFalse(report.resolution_gaps)
        self.assertEqual(sum(c.target == "app.py:Base.fetch" for c in report.calls), 2)

    def test_decorated_helper(self):
        report = self.report("""
            from decorators import wrapped
            class Client: pass
            @wrapped
            def factory() -> Client: return Client()
            def run(): factory().fetch()
        """)
        self.assertEqual(report.resolution_gaps[-1].reason, "decorated_helper")

    def test_unknown_field_and_shadowed_import(self):
        report = self.report("""
            import requests
            class Worker:
                def run(self, requests):
                    self.client.fetch()
                    requests.get('url')
        """)
        self.assertEqual(
            [g.reason for g in report.resolution_gaps],
            ["unknown_member_type", "shadowed_binding"],
        )

    def test_duplicate_definitions(self):
        report = self.report("""
            def fetch(): pass
            def fetch(): pass
            def run(): fetch()
        """)
        self.assertEqual(report.resolution_gaps[0].reason, "ambiguous_definition")

    def test_dynamic_call_and_source_omission(self):
        report = self.report(
            """
            def run(registry): registry['secret']()
        """,
            Config(include_source=False),
        )
        self.assertEqual(report.resolution_gaps[0].reason, "dynamic_callable")
        self.assertNotIn("secret", str(report.to_dict()))

    def test_entrypoint_routes_cycles_and_unknown_frontier(self):
        report = self.report(
            """
            def leaf(client): client.fetch()
            def left(client): leaf(client)
            def right(client): leaf(client)
            def cycle(client):
                leaf(client)
                cycle(client)
        """,
            Config(entrypoints=["app.left", "app.right"]),
        )
        gap = report.resolution_gaps[0]
        self.assertEqual(gap.entrypoints, ["app.py:left", "app.py:right"])
        self.assertFalse(gap.context_truncated)
        without = self.report("def leaf(client): client.fetch()")
        self.assertEqual(without.resolution_gaps[0].entrypoints, [])

    def test_context_budget_and_depth_are_explicit(self):
        code = "def leaf(client): client.fetch()\ndef middle(client): leaf(client)\ndef run(client): middle(client)\n"
        budget = self.report(
            code, Config(entrypoints=["app.run"], max_resolution_steps=1)
        )
        self.assertEqual(budget.summary()["status"], "incomplete")
        self.assertEqual(budget.analysis_stats["resolution_context_steps"], 1)
        self.assertTrue(budget.resolution_gaps[0].context_truncated)
        depth = self.report(code, Config(entrypoints=["app.run"], max_path_depth=1))
        self.assertTrue(depth.resolution_gaps[0].context_truncated)
        self.assertEqual(depth.resolution_gaps[0].entrypoints, [])

    def test_type_budget_reason(self):
        report = self.report(
            "def run(client): client.fetch()", Config(max_type_steps=1)
        )
        self.assertEqual(report.resolution_gaps[0].reason, "inference_budget")

    def test_report_formats_preserve_explanations(self):
        report = self.report(
            "def run(callback): callback()", Config(entrypoints=["app.run"])
        )
        self.assertEqual(
            report.summary()["unresolved_reasons"], {"callback_parameter": 1}
        )
        self.assertIn("callback_parameter", render_text(report))
        self.assertIn("Unresolved:", render_mermaid(report))
        data = graph_data(report)
        self.assertEqual(len(data["resolution_gaps"]), 1)
        self.assertIn("gap-reason", render_html(report))
        self.assertEqual(
            next(n for n in data["nodes"] if n["unresolved_count"])[
                "unresolved_examples"
            ][0]["gap"]["reason"],
            "callback_parameter",
        )


if __name__ == "__main__":
    unittest.main()
