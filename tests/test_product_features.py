from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

from flowsignal import scan
from flowsignal.baseline import compare, make_baseline, read_baseline, review
from flowsignal.diagram import graph_data
from flowsignal.runtime import compare_trace
from flowsignal.sarif import export


class ProductFeatureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def report(self, code):
        (self.root / "app.py").write_text(textwrap.dedent(code), encoding="utf-8")
        return scan(self.root)

    def boundary(self):
        return self.report("import requests\ndef fetch(): return requests.get('url')\n")

    def test_review_expiry_history_and_sarif_suppression(self):
        report = self.boundary()
        baseline = make_baseline(report)
        fingerprint = report.findings[0].fingerprint
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        review(
            baseline,
            fingerprint,
            "dismiss",
            "Owned by gateway",
            owner="platform",
            expires="2030-02-01",
            now=now,
        )
        compare(report, baseline, now=now)
        result = export(report)["runs"][0]["results"][0]
        self.assertEqual(result["suppressions"][0]["status"], "accepted")
        self.assertEqual(result["properties"]["reviewOwner"], "platform")
        compare(report, baseline, now=datetime(2030, 2, 1, tzinfo=timezone.utc))
        self.assertTrue(report.findings[0].review_expired)
        self.assertEqual(report.findings[0].review_status, "active")
        self.assertNotIn("suppressions", export(report)["runs"][0]["results"][0])
        self.assertEqual(
            [e["action"] for e in report.review_history], ["dismiss", "expired"]
        )
        saved = make_baseline(report)
        compare(report, saved, now=datetime(2030, 3, 1, tzinfo=timezone.utc))
        self.assertEqual(len(report.review_history), 2)

    def test_changed_finding_requires_renewal_and_retains_history(self):
        report = self.boundary()
        baseline = make_baseline(report)
        review(
            baseline, report.findings[0].fingerprint, "dismiss", "Gateway", owner="team"
        )
        changed = self.report(
            "import requests\ndef fetch(): return requests.post('url')\n"
        )
        compare(changed, baseline)
        self.assertEqual(changed.findings[0].review_status, "active")
        self.assertEqual(
            changed.baseline["renewal_required"], [changed.findings[0].fingerprint]
        )
        self.assertEqual(len(make_baseline(changed)["review_history"]), 1)

    def test_review_validation_and_legacy_baseline(self):
        report = self.boundary()
        baseline = make_baseline(report)
        fingerprint = report.findings[0].fingerprint
        for kwargs in (
            {"owner": 2},
            {"expires": "2020-01-01"},
            {"expires": "tomorrow"},
        ):
            with self.assertRaises(ValueError):
                review(
                    copy.deepcopy(baseline), fingerprint, "dismiss", "reason", **kwargs
                )
        review(baseline, fingerprint, "dismiss", "reason")
        self.assertIsNotNone(baseline["findings"][0]["review_expires"])
        path = self.root / "baseline.json"
        baseline["review_history"][0]["at"] = "not a timestamp"
        path.write_text(json.dumps(baseline), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_baseline(path)
        baseline.pop("review_history")
        for entry in baseline["findings"]:
            entry.pop("review_owner")
            entry.pop("review_expires")
        path.write_text(json.dumps(baseline), encoding="utf-8")
        compare(report, read_baseline(path))
        self.assertEqual(report.findings[0].review_status, "dismissed")

    def trace(self, pairs, digest=None):
        document = {
            "schema_version": "flowsignal-runtime-1",
            "files": {
                "app.py": digest
                or hashlib.sha256((self.root / "app.py").read_bytes()).hexdigest()
            },
            "pairs": pairs,
        }
        path = self.root / "trace.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_runtime_comparison_preserves_static_conclusions(self):
        report = self.report(
            "def work(): pass\ndef run(callback):\n    work()\n    callback()\ndef other(): pass\n"
        )
        before = report.to_dict()
        pairs = [
            {"caller": "app.py:run", "callee": "app.py:work", "count": 2},
            {"caller": "app.py:run", "callee": "app.py:other"},
            {"caller": "app.py:run", "callee": "app.py:missing"},
        ]
        compare_trace(report, self.trace([*pairs, pairs[0]]))
        self.assertEqual(
            report.runtime["summary"],
            {
                "observed_and_inferred": 1,
                "observed_only": 1,
                "unmatched_symbol": 1,
                "source_mismatch": 0,
            },
        )
        self.assertEqual(
            next(
                p["count"]
                for p in report.runtime["pairs"]
                if p["callee"].endswith(":work")
            ),
            4,
        )
        self.assertEqual(report.to_dict()["calls"], before["calls"])
        self.assertEqual(report.to_dict()["coverage"], before["coverage"])
        graph = graph_data(report)
        self.assertEqual(len([e for e in graph["edges"] if e["kind"] == "runtime"]), 2)
        compare_trace(report, self.trace(pairs, "0" * 64))
        self.assertEqual(report.runtime["summary"]["source_mismatch"], 3)
        self.assertFalse(
            any(e["kind"] == "runtime" for e in graph_data(report)["edges"])
        )

    def test_runtime_rejects_invalid_inputs_without_execution(self):
        marker = self.root / "executed"
        report = self.report(f"open({str(marker)!r}, 'w').close()\ndef work(): pass\n")
        for pairs in (
            [{"caller": "app.py:work", "callee": "app.py:work", "count": True}],
            [{"caller": "../app.py:work", "callee": "app.py:work"}],
            ["invalid"],
        ):
            with self.assertRaises(ValueError):
                compare_trace(report, self.trace(pairs))
        self.assertFalse(marker.exists())

    def test_reporting_path_includes_owner_and_stops_at_barrier(self):
        for statement, recognized in (
            ("logging.exception('failed')", True),
            ("return None", False),
        ):
            report = self.report(
                f"import requests, logging\ndef fetch(): return requests.get('url')\ndef middle(): return fetch()\ndef run():\n    try: return middle()\n    except Exception: {statement}\n"
            )
            node = next(
                n for n in graph_data(report)["nodes"] if n["kind"] == "boundary"
            )
            path = node["coverage_path"]
            self.assertTrue(
                {"app.py:fetch", "app.py:middle", "app.py:run"} <= set(path["nodes"])
            )
            self.assertEqual(bool(path["owners"]), recognized)
            self.assertEqual(bool(path["barriers"]), not recognized)
            self.assertEqual(node["coverage"]["status"] == "recognized", recognized)

    def test_inheritance_constructor_and_tuple_resolution(self):
        report = self.report("""
            class Client:
                def run(self): pass
            class Parent:
                def ping(self): pass
            class Child(Parent):
                def __init__(self, client): self.client = client
                def go(self):
                    super().ping()
                    self.client.run()
            def pair(): return Child(Client()), True
            def main():
                child, ok = pair()
                child.ping()
                child.go()
        """)
        pairs = {(c.symbol, c.target) for c in report.calls if c.target}
        self.assertTrue(
            {
                ("app.py:Child.go", "app.py:Parent.ping"),
                ("app.py:Child.go", "app.py:Client.run"),
                ("app.py:main", "app.py:Parent.ping"),
                ("app.py:main", "app.py:Child.go"),
            }
            <= pairs
        )

    def test_ambiguous_constructor_and_multiple_inheritance_stay_unknown(self):
        report = self.report("""
            class A:
                def run(self): pass
            class B:
                def run(self): pass
            class Child(A, B): pass
            class Wrapper:
                def __init__(self, client): self.client = client
                def execute(self): self.client.run()
            def main():
                a = Wrapper(A())
                b = Wrapper(B())
                Child().run()
        """)
        self.assertFalse(
            any(c.target for c in report.calls if c.expression.endswith(".run"))
        )

    def test_sarif_locations_fingerprints_and_severity_meaning(self):
        report = self.boundary()
        document = export(report)
        self.assertEqual(document["version"], "2.1.0")
        result = document["runs"][0]["results"][0]
        self.assertEqual(
            result["partialFingerprints"]["flowsignalStructural/v1"],
            report.findings[0].fingerprint,
        )
        self.assertEqual(
            result["locations"][0]["physicalLocation"]["region"], {"startLine": 2}
        )
        self.assertIn(
            "review priority", document["runs"][0]["properties"]["severityMeaning"]
        )
        self.assertTrue(result["properties"]["recommendations"])

    def test_constructor_inputs_are_consistent_and_bound_conservatively(self):
        template = """
class Client:
    def run(self): pass
class Box:
    def __init__(self, client): self.client = client
    def go(self): self.client.run()
def build(unknown):
    %s
"""
        for construction, expected in [
            ("Box(client=Client())", True),
            ("Box(Client()); Box(Client())", True),
            ("Box(Client()); Box(unknown)", False),
            ("Box(*unknown)", False),
            ("Box(**unknown)", False),
            ("Box(Client(), client=Client())", False),
        ]:
            with self.subTest(construction=construction):
                report = self.report(template % construction)
                self.assertEqual(
                    any(c.target == "app.py:Client.run" for c in report.calls), expected
                )

    def test_tuple_returns_reject_conflicting_element_types(self):
        report = self.report("""
            class A:
                def run(self): pass
            class B:
                def run(self): pass
            def pair(flag) -> tuple[A, bool]:
                if flag: return A(), True
                return B(), False
            def main():
                value, ok = pair(True)
                value.run()
        """)
        self.assertFalse(
            any(c.target for c in report.calls if c.expression == "value.run")
        )

    def test_inherited_method_masks_and_unknown_bases_stay_unknown(self):
        for body in (
            "run = None",
            "def __getattribute__(self, name): return None",
            "def __init__(self): self.run = None",
        ):
            report = self.report(
                f"class A:\n    def run(self): pass\nclass B(A):\n    {body}\ndef main():\n    b = B()\n    b.run()\n"
            )
            self.assertFalse(
                any(c.target for c in report.calls if c.expression == "b.run")
            )
        report = self.report(
            "from external import Base\nclass Child(Base): pass\ndef main(): Child().run()\n"
        )
        self.assertFalse(
            any(c.target for c in report.calls if c.expression.endswith(".run"))
        )

    def test_benchmark_reports_misses_and_does_not_drop_unknown_labels(self):
        from scripts.benchmark_accuracy import evaluate, score

        result = score([("a", "b"), ("a", "missing")], [("a", "b"), ("a", "extra")])
        self.assertEqual(
            (
                result["true_positive"],
                result["false_positive"],
                result["false_negative"],
            ),
            (1, 1, 1),
        )
        manifest = Path(__file__).resolve().parents[1] / "benchmarks/manifest.json"
        result = evaluate(manifest)
        self.assertGreater(result["totals"]["evaluation"]["edges"]["false_negative"], 0)
        self.assertNotIn("findings", result["totals"]["evaluation"])


if __name__ == "__main__":
    unittest.main()
