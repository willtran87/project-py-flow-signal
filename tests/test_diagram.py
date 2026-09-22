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
from flowsignal.config import Config
from flowsignal.diagram import graph_data, render_html, render_mermaid, select_view


class DiagramTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def report(self, source, config=None):
        (self.root / "app.py").write_text(textwrap.dedent(source), encoding="utf-8")
        return scan(self.root, config)

    def test_existing_logs_and_recommendations_are_separate(self):
        report = self.report(
            """
            import logging
            import requests
            def run():
                logging.info('started')
                requests.get('url')
        """,
            Config(entrypoints=["app.run"]),
        )
        data = graph_data(report)
        self.assertEqual(data["focus"], "app.py:run")
        function = next(node for node in data["nodes"] if node["id"] == "app.py:run")
        boundary = next(node for node in data["nodes"] if node["kind"] == "boundary")
        self.assertEqual(function["existing"][0]["level"], "INFO")
        self.assertEqual(boundary["existing"], [])
        self.assertEqual(data["findings"][boundary["findings"][0]]["rule_id"], "FS005")
        self.assertTrue(
            any(
                edge["source"] == function["id"] and edge["target"] == boundary["id"]
                for edge in data["edges"]
            )
        )

    def test_incomplete_scan_status_travels_with_diagrams(self):
        report = self.report("def broken(:\n")
        self.assertEqual(graph_data(report)["summary"]["status"], "incomplete")
        self.assertIn("INCOMPLETE SCAN: review diagnostics", render_mermaid(report))
        self.assertIn('id="scan-status"', render_html(report))

    def test_exception_logs_stay_at_the_handler_and_can_need_enrichment(self):
        report = self.report("""
            import logging
            def run():
                try:
                    operation()
                except Exception:
                    logging.error('failed')
                    raise
        """)
        data = graph_data(report)
        handler = next(node for node in data["nodes"] if node["kind"] == "handler")
        self.assertEqual(handler["existing"][0]["level"], "ERROR")
        self.assertEqual(data["findings"][handler["findings"][0]]["rule_id"], "FS006")
        self.assertIn(":::mixed", render_mermaid(report))

    def test_deferred_calls_have_no_invented_handler_edge(self):
        report = self.report("""
            import asyncio
            async def worker():
                pass
            async def run():
                try:
                    asyncio.create_task(worker())
                except Exception:
                    pass
        """)
        data = graph_data(report)
        self.assertTrue(
            any(
                edge["kind"] == "deferred" and edge["target"] == "app.py:worker"
                for edge in data["edges"]
            )
        )
        self.assertFalse(
            any(
                edge["kind"] == "exception" and edge["source"] == "app.py:worker"
                for edge in data["edges"]
            )
        )

    def test_handler_edges_are_explicit_candidates(self):
        report = self.report("""
            import requests
            def run():
                try:
                    requests.get('url')
                except Exception:
                    pass
        """)
        data = graph_data(report)
        edge = next(edge for edge in data["edges"] if edge["kind"] == "exception")
        self.assertIn("candidate", edge["label"])
        self.assertTrue(edge["source"].startswith("call:"))
        self.assertTrue(edge["target"].startswith("handler:"))

    def test_custom_internal_boundary_preserves_implementation_path(self):
        report = self.report(
            "def gateway():\n    pass\ndef run():\n    gateway()\n",
            Config(boundaries=[{"pattern": "app.gateway", "category": "network"}]),
        )
        data = graph_data(report, "app.run")
        view = select_view(data)
        self.assertIn("app.py:gateway", {node["id"] for node in view["nodes"]})
        self.assertTrue(
            any(
                edge["label"] == "resolved implementation"
                and edge["target"] == "app.py:gateway"
                for edge in view["edges"]
            )
        )

    def test_unknown_calls_do_not_become_guessed_edges(self):
        data = graph_data(
            self.report("def run(client):\n    client.fetch()\n"), "app.run"
        )
        node = next(node for node in data["nodes"] if node["id"] == "app.py:run")
        self.assertEqual(node["unresolved_count"], 1)
        self.assertEqual(node["unresolved_examples"][0]["expression"], "client.fetch")
        self.assertEqual(data["edges"], [])

    def test_view_limits_cycles_and_disconnected_nodes_are_explicit(self):
        report = self.report(
            "def a():\n    b()\ndef b():\n    a()\ndef disconnected():\n    pass\n"
        )
        data = graph_data(report, "app.a", 1)
        view = select_view(data)
        self.assertEqual(len(view["nodes"]), 1)
        self.assertEqual(view["omitted"], 1)
        self.assertEqual(view["outside"], 2)
        ids = {node["id"] for node in view["nodes"]}
        self.assertTrue(
            all(
                edge["source"] in ids and edge["target"] in ids
                for edge in view["edges"]
            )
        )

    def test_callers_can_be_excluded_from_focused_view(self):
        report = self.report("def a():\n    b()\ndef b():\n    pass\n")
        data = graph_data(report, "app.b")
        self.assertEqual(len(select_view(data)["nodes"]), 2)
        self.assertEqual(len(select_view(data, include_callers=False)["nodes"]), 1)

    def test_html_embeds_source_as_inert_json(self):
        report = self.report("""
            import logging
            def run():
                try:
                    work()
                except Exception:
                    logging.error('</script><script>window.injected = true</script>')
        """)
        html = render_html(report)
        self.assertNotIn("</script><script>window.injected", html)
        embedded = html.split('<script id="flow-data" type="application/json">')[
            1
        ].split("</script>")[0]
        data = json.loads(embedded)
        self.assertIn(
            "</script><script>window.injected",
            next(iter(data["findings"].values()))["evidence"][0]["code"],
        )
        self.assertNotIn("<script src=", html)
        self.assertNotIn("__FLOW_DATA__", html)

    def test_mermaid_escapes_labels_and_keeps_recommendation_conditions(self):
        report = self.report("import requests\ndef run():\n    requests.get('url')\n")
        report.calls[-1].resolved_name = 'evil["]<script> & name'
        diagram = render_mermaid(report, "app.run")
        self.assertNotIn("<script>", diagram)
        self.assertIn("#34;", diagram)
        self.assertIn(" WHEN ", diagram)
        self.assertIn("omitted by view limit", diagram)
        self.assertEqual(diagram, render_mermaid(report, "app.run"))

    def test_focus_and_limits_are_validated(self):
        report = self.report("def run():\n    pass\n")
        with self.assertRaises(ValueError):
            graph_data(report, "app.missing")
        with self.assertRaises(ValueError):
            graph_data(report, max_nodes=0)
        with self.assertRaises(ValueError):
            graph_data(report, max_nodes=201)

    def test_empty_report_is_renderable(self):
        report = scan(self.root)
        self.assertEqual(select_view(graph_data(report))["nodes"], [])
        self.assertIn("No readable symbols", render_html(report))
        self.assertIn("0 shown", render_mermaid(report))

    def test_cli_exports_html_and_mermaid_without_changing_exit_semantics(self):
        self.report(
            "def run():\n    try:\n        work()\n    finally:\n        return None\n"
        )
        for format_name, filename in [
            ("html", "report.html"),
            ("mermaid", "report.mmd"),
        ]:
            output = self.root / filename
            self.assertEqual(
                main(
                    [
                        "scan",
                        str(self.root),
                        "--format",
                        format_name,
                        "--output",
                        str(output),
                        "--fail-on",
                        "high",
                    ]
                ),
                1,
            )
            self.assertTrue(output.is_file())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "scan",
                        str(self.root),
                        "--format",
                        "html",
                        "--diagram-focus",
                        "missing",
                    ]
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
