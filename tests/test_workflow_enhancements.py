from __future__ import annotations

import json
import runpy
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from flowsignal import scan
from flowsignal.collector import collect
from flowsignal.config import Config
from flowsignal.runtime import compare_runs, compare_trace


class WorkflowEnhancements(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.root = self.home / "source"
        self.root.mkdir()
        self.cache = self.home / "cache"

    def source(self, code, name="app.py"):
        path = self.root / name
        path.write_text(textwrap.dedent(code), encoding="utf-8")
        return path

    def targets(self, code):
        self.source(code)
        return {(c.symbol, c.target) for c in scan(self.root).calls if c.target}

    def test_callable_aliases_returns_tuple_and_bound_method(self):
        pairs = self.targets("""
            def one(): pass
            def two(): pass
            class C:
                def work(self): pass
            def factory(): return C().work
            def main():
                a, b = one, two
                a(); b()
                f = factory()
                f()
        """)
        self.assertTrue(
            {
                ("app.py:main", "app.py:one"),
                ("app.py:main", "app.py:two"),
                ("app.py:main", "app.py:C.work"),
            }
            <= pairs
        )

    def test_conflicting_callable_shadow_and_decorator_stay_unknown(self):
        for assignment in ("f = one; f = two", "f = one; f = unknown"):
            self.source(
                f"def one(): pass\ndef two(): pass\ndef main(unknown):\n    {assignment}\n    f()\n"
            )
            report = scan(self.root)
            self.assertIsNone(
                next(c.target for c in report.calls if c.expression == "f")
            )
        self.source("@decorate\ndef one(): pass\ndef main():\n    f = one\n    f()\n")
        self.assertIsNone(
            next(c.target for c in scan(self.root).calls if c.expression == "f")
        )

    def handler(self, body):
        self.source(
            "import requests, logging\ndef main(flag):\n    try: requests.get('url')\n    except Exception:\n"
            + textwrap.indent(textwrap.dedent(body), "        ")
        )
        return scan(self.root)

    def test_all_branches_collectively_report(self):
        report = self.handler("""\
if flag:
    logging.exception('first')
else:
    logging.warning('fallback', exc_info=True)
""")
        self.assertTrue(report.handlers[0].path_reporting)
        self.assertEqual(len(report.handlers[0].reporting_paths), 2)
        self.assertNotIn("FS005", {f.rule_id for f in report.findings})

    def test_early_return_cannot_borrow_other_branch_reporting(self):
        report = self.handler("""\
if flag: return None
logging.exception('failed')
""")
        self.assertFalse(report.handlers[0].path_reporting)
        self.assertEqual(
            {p["reported"] for p in report.handlers[0].reporting_paths}, {True, False}
        )
        self.assertIn("FS005", {f.rule_id for f in report.findings})

    def test_finally_reports_return_and_raise_exits(self):
        report = self.handler("""\
try:
    if flag: return None
    raise
finally:
    logging.exception('failed')
""")
        self.assertTrue(report.handlers[0].path_reporting)
        self.assertEqual(
            {p["exit"] for p in report.handlers[0].reporting_paths}, {"return", "raise"}
        )

    def test_loop_and_unknown_context_remain_uncertain(self):
        for body in (
            "for item in flag:\n    logging.exception('failed')\n",
            "with flag:\n    logging.exception('failed')\n",
        ):
            report = self.handler(body)
            self.assertFalse(report.handlers[0].path_reporting)
            self.assertIn("FS005", {f.rule_id for f in report.findings})

    def test_path_limit_is_explicit_and_not_credited(self):
        report = self.handler(
            "\n".join(f"if flag: value = {i}" for i in range(7))
            + "\nlogging.exception('failed')\n"
        )
        self.assertTrue(report.handlers[0].paths_truncated)
        self.assertFalse(report.handlers[0].path_reporting)

    def normalize(self, report):
        document = report.to_dict()
        document["analysis_stats"].pop("cache", None)
        return document

    def test_warm_cache_equivalence_and_changed_dependency(self):
        self.source("from dependency import work\ndef main(): return work()\n")
        self.source("def work(): return 1\n", "dependency.py")
        cold = scan(self.root, cache_dir=self.cache)
        warm = scan(self.root, cache_dir=self.cache)
        self.assertTrue(warm.analysis_stats["cache"]["snapshot_reused"])
        self.assertEqual(self.normalize(cold), self.normalize(warm))
        self.source(
            "import requests\ndef work(): return requests.get('url')\n", "dependency.py"
        )
        changed = scan(self.root, cache_dir=self.cache)
        self.assertFalse(changed.analysis_stats["cache"]["snapshot_reused"])
        self.assertEqual(changed.analysis_stats["cache"]["parsed_files_reused"], 1)
        self.assertEqual(self.normalize(changed), self.normalize(scan(self.root)))
        self.assertTrue(changed.findings)

    def test_cache_config_new_file_deleted_file_and_corruption(self):
        self.source("def work(): return 1\n")
        scan(self.root, cache_dir=self.cache)
        changed = scan(
            self.root, Config(entrypoints=["app.work"]), cache_dir=self.cache
        )
        self.assertFalse(changed.analysis_stats["cache"]["snapshot_reused"])
        self.source("def extra(): pass\n", "extra.py")
        self.assertEqual(scan(self.root, cache_dir=self.cache).files_scanned, 2)
        (self.root / "extra.py").unlink()
        self.assertEqual(scan(self.root, cache_dir=self.cache).files_scanned, 1)
        for path in self.cache.glob("*.json"):
            path.write_text("broken", encoding="utf-8")
        report = scan(self.root, cache_dir=self.cache)
        self.assertGreater(report.analysis_stats["cache"]["invalid_entries"], 0)
        self.assertEqual(self.normalize(report), self.normalize(scan(self.root)))

    def test_cache_never_executes_target(self):
        marker = self.home / "executed"
        self.source(f"open({str(marker)!r}, 'w').close()\n")
        scan(self.root, cache_dir=self.cache)
        scan(self.root, cache_dir=self.cache)
        self.assertFalse(marker.exists())

    def test_cache_preserves_exclusions_and_current_discovery_metadata(self):
        self.source("def work(): return 1\n")
        (self.root / ".venv").mkdir()
        scan(self.root, cache_dir=self.cache)
        warm = scan(self.root, cache_dir=self.cache)
        self.assertTrue(warm.analysis_stats["cache"]["snapshot_reused"])
        self.assertEqual(self.normalize(warm), self.normalize(scan(self.root)))
        (self.root / "notes.txt").write_text("new non-Python file", encoding="utf-8")
        changed = scan(self.root, cache_dir=self.cache)
        self.assertFalse(changed.analysis_stats["cache"]["snapshot_reused"])
        self.assertEqual(self.normalize(changed), self.normalize(scan(self.root)))
        (self.root / "node_modules").mkdir()
        changed = scan(self.root, cache_dir=self.cache)
        self.assertEqual(self.normalize(changed), self.normalize(scan(self.root)))

    def probe(self):
        path = self.source(
            "def first(): return 1\ndef second(): return 2\ndef main(other=False):\n    first()\n    if other: second()\n"
        )
        return runpy.run_path(str(path))

    def test_collector_and_repeated_run_comparison(self):
        namespace = self.probe()
        a, b = self.home / "a.json", self.home / "b.json"
        with collect(self.root, a, run_id="first"):
            namespace["main"]()
        with collect(self.root, b, run_id="second", scenario="alternate branch"):
            namespace["main"](True)
        self.assertIsNone(sys.getprofile())
        report = scan(self.root)
        compare_trace(report, b)
        compare_runs(report, a)
        self.assertEqual(
            report.runtime["run_comparison"]["added"],
            [("app.py:main", "app.py:second")],
        )
        self.assertEqual(report.runtime["run"]["id"], "second")
        self.assertEqual(report.runtime["summary"]["observed_and_inferred"], 2)

    def test_collector_rejects_stale_loaded_code_and_changed_source(self):
        namespace = self.probe()
        self.source("def first(): return 99\ndef main(other=False): return first()\n")
        output = self.home / "trace.json"
        with collect(self.root, output, run_id="stale"):
            namespace["main"]()
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(document["run"]["complete"])
        self.assertEqual(document["pairs"], [])
        namespace = self.probe()
        with collect(self.root, output, run_id="changed"):
            namespace["main"]()
            self.source("def first(): return 3\n")
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["pairs"], [])
        self.assertTrue(
            any(i.startswith("source_changed:") for i in document["collection_issues"])
        )

    def test_collector_preserves_active_profiler_and_exception(self):
        def existing(frame, event, argument):
            pass

        sys.setprofile(existing)
        try:
            with self.assertRaises(ValueError):
                with collect(self.root, self.home / "trace.json", run_id="busy"):
                    self.fail("Should not enter")
            self.assertIs(sys.getprofile(), existing)
        finally:
            sys.setprofile(None)
        namespace = self.probe()
        with self.assertRaisesRegex(RuntimeError, "original"):
            with collect(self.root, self.home / "trace.json", run_id="failed"):
                namespace["main"]()
                raise RuntimeError("original")
        self.assertIsNone(sys.getprofile())

    def test_review_queue_context_and_unresolved_items(self):
        self.source(
            "import requests\ndef fetch(): return requests.get('url')\ndef main(callback):\n    fetch()\n    callback()\n"
        )
        report = scan(self.root, Config(entrypoints=["app.main"]))
        items = report.review_queue["items"]
        self.assertTrue(any("uncovered" in r["states"] for r in items))
        self.assertTrue(any("unresolved" in r["states"] for r in items))
        self.assertTrue(all(r["entrypoints"] == ["app.py:main"] for r in items))
        self.assertTrue(all(r["node"] for r in items))

    def test_collector_write_failure_preserves_test_exception_and_rejects_reuse(self):
        from unittest.mock import patch

        collector = collect(self.root, self.home / "trace.json", run_id="failed")
        with patch("flowsignal.cli.write_report", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(RuntimeError, "test failed") as caught:
                with collector:
                    raise RuntimeError("test failed")
        self.assertIn("disk failure", caught.exception.__notes__[0])
        self.assertIsNone(sys.getprofile())
        with self.assertRaisesRegex(ValueError, "only be used once"):
            with collector:
                self.fail("Cannot reuse collector")

    def test_independent_review_packet_requires_matching_completed_labels(self):
        from scripts.benchmark_accuracy import evaluate, review_packet

        manifest = Path(__file__).resolve().parents[1] / "benchmarks/manifest.json"
        packet = review_packet(manifest)
        self.assertIsNone(packet["cases"][0]["findings"])
        path = self.home / "review.json"
        path.write_text(json.dumps(packet), encoding="utf-8")
        with self.assertRaises(ValueError):
            evaluate(manifest, path)
        case = packet["cases"][0]
        # Synthetic labels exercise import validation only, not actual adjudication.
        case.update(
            completed=True,
            reviewer="synthetic-test-reviewer",
            rationale="Importer test, not production evidence",
            findings=[],
            owners=[],
        )
        path.write_text(json.dumps(packet), encoding="utf-8")
        self.assertEqual(evaluate(manifest, path)["independently_reviewed_cases"], 1)
        case["sha256"]["storage.py"] = "0" * 64
        path.write_text(json.dumps(packet), encoding="utf-8")
        with self.assertRaises(ValueError):
            evaluate(manifest, path)


if __name__ == "__main__":
    unittest.main()
