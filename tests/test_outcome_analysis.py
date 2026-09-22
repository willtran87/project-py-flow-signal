from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from flowsignal import scan
from flowsignal.config import Config
from flowsignal.diagram import graph_data, render_html, render_mermaid
from flowsignal.sarif import export


class OutcomeAnalysis(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def source(self, text, name="app.py"):
        (self.root / name).write_text(textwrap.dedent(text), encoding="utf-8")

    def contract(self, outcome, **extra):
        return dict(
            pattern="app.work",
            exit="return",
            outcome=outcome,
            owner="operation-owner",
            **extra,
        )

    def test_outcome_matrix_and_existing_reporting(self):
        expected = {
            "expected_rejection": ("no_additional_log", None),
            "successful_recovery": ("metric", None),
            "degraded_recovery": ("log", "WARNING"),
            "failed_operation": ("log", "ERROR"),
            "cancellation": ("no_additional_log", None),
            "unknown": ("conditional", None),
        }
        for outcome, (kind, level) in expected.items():
            for reported in (False, True):
                with self.subTest(outcome=outcome, reported=reported):
                    self.source(
                        "import json, logging\ndef work():\n    try: return json.loads('bad')\n    except Exception:\n"
                        + ("        logging.exception('failure')\n" if reported else "")
                        + "        return None\n"
                    )
                    report = scan(
                        self.root, Config(operations=[self.contract(outcome)])
                    )
                    record = next(r for r in report.operation_outcomes if r["handler"])
                    actual_kind = (
                        "no_additional_log"
                        if reported and outcome != "unknown"
                        else kind
                    )
                    self.assertEqual(record["decision"]["kind"], actual_kind)
                    self.assertEqual(
                        record["decision"]["level"], None if reported else level
                    )
                    self.assertEqual(record["basis"], "user_declaration")
                    if not reported and outcome != "unknown":
                        advice = next(
                            f for f in report.findings if f.rule_id == "FS001"
                        ).recommendations
                        self.assertEqual(
                            [(r.kind, r.level) for r in advice], [(kind, level)]
                        )
                    self.assertIn(
                        "operationOutcomes", export(report)["runs"][0]["properties"]
                    )

    def test_unknown_conflicting_and_mixed_outcomes(self):
        self.source(
            "def work(flag):\n    try: raise ValueError()\n    except Exception:\n        if flag: return None\n        raise\n"
        )
        report = scan(self.root)
        self.assertTrue(
            all(r["outcome"] == "unknown" for r in report.operation_outcomes)
        )
        report = scan(
            self.root, Config(operations=[self.contract("successful_recovery")])
        )
        handler = [r for r in report.operation_outcomes if r["handler"]]
        self.assertEqual(
            {r["outcome"] for r in handler}, {"unknown", "successful_recovery"}
        )
        report = scan(
            self.root,
            Config(
                operations=[
                    self.contract("successful_recovery"),
                    self.contract("failed_operation"),
                ]
            ),
        )
        self.assertEqual(report.summary()["status"], "incomplete")
        self.assertIn(
            "operation_contract_conflict", {d.code for d in report.diagnostics}
        )

    def test_lifecycle_signal_choice_and_validation(self):
        self.source("def work(): return 1\n")
        for importance, level in (
            ("routine", None),
            ("important", "INFO"),
            ("critical", "INFO"),
        ):
            with self.subTest(importance=importance):
                report = scan(
                    self.root,
                    Config(
                        operations=[
                            self.contract(
                                "successful_operation",
                                scope="operation",
                                importance=importance,
                            )
                        ]
                    ),
                )
                self.assertEqual(
                    report.operation_outcomes[0]["decision"]["level"], level
                )
        for signal in ("trace", "metric", "no_additional_log"):
            report = scan(
                self.root,
                Config(
                    operations=[
                        self.contract(
                            "failed_operation", scope="operation", signal=signal
                        )
                    ]
                ),
            )
            self.assertEqual(report.operation_outcomes[0]["decision"]["kind"], signal)
        for record in (
            {},
            self.contract("made-up"),
            self.contract("unknown", importance="made-up"),
        ):
            with self.assertRaises(ValueError):
                Config(operations=[record]).validate()

    def test_callable_contexts_preserve_call_site_bindings(self):
        self.source(
            "def a(): pass\ndef b(): pass\ndef invoke(callback): callback()\ndef forward(fn): invoke(fn)\ndef main():\n    forward(a)\n    forward(b)\n"
        )
        report = scan(self.root)
        rows = report.contextual_calls
        self.assertEqual({r["callee"] for r in rows}, {"app.py:a", "app.py:b"})
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["context"], rows[1]["context"])
        self.assertTrue(all(not r["coverage_eligible"] for r in rows))
        callback = next(c for c in report.calls if c.expression == "callback")
        self.assertIsNone(callback.target)
        self.assertEqual(
            len([e for e in graph_data(report)["edges"] if e.get("basis")]), 2
        )

    def test_callable_positive_forms(self):
        forms = [
            ("def invoke(cb): cb()", "invoke(target)"),
            ("def invoke(cb): cb()", "invoke(cb=target)"),
            ("def invoke(cb=target): cb()", "invoke()"),
            ("def invoke(*, cb): cb()", "invoke(cb=target)"),
            ("def invoke(cb, /): cb()", "invoke(target)"),
            ("def invoke(cb, unused): cb()", "invoke(target, 1)"),
            ("def inner(cb): cb()\ndef invoke(cb): inner(cb)", "invoke(target)"),
            ("def inner(*, cb): cb()\ndef invoke(cb): inner(cb=cb)", "invoke(target)"),
            (
                "class C:\n    def method(self): pass\ndef invoke(cb): cb()",
                "invoke(C().method)",
            ),
            ("def invoke(cb): cb()", "invoke(imported)"),
        ]
        self.source("def target(): pass\n", "other.py")
        for helper, call in forms:
            with self.subTest(call=call, helper=helper):
                self.source(
                    "from other import target as imported\ndef target(): pass\n"
                    + helper
                    + "\ndef main(): "
                    + call
                    + "\n"
                )
                report = scan(self.root)
                self.assertTrue(report.contextual_calls)

    def test_callable_negative_forms(self):
        forms = [
            ("def invoke(cb): cb()", "invoke(unknown)"),
            ("def invoke(cb): cb()", "invoke(*values)"),
            ("def invoke(cb): cb()", "invoke(**values)"),
            ("def invoke(cb): cb()", "invoke(target, cb=target)"),
            ("def invoke(cb, required): cb()", "invoke(target)"),
            ("def invoke(cb, /): cb()", "invoke(cb=target)"),
            ("def invoke(cb):\n    cb = unknown\n    cb()", "invoke(target)"),
            ("@unknown\ndef invoke(cb): cb()", "invoke(target)"),
            ("def invoke(cb): cb()", "invoke(lambda: target())"),
            ("def invoke(*cb): cb()", "invoke(target)"),
        ]
        for helper, call in forms:
            with self.subTest(call=call, helper=helper):
                self.source(
                    "def target(): pass\n" + helper + "\ndef main(): " + call + "\n"
                )
                self.assertEqual(scan(self.root).contextual_calls, [])

    def test_registrations_are_deferred_and_configured(self):
        self.source(
            "import dispatcher\ndef callback(): pass\ndef main(): dispatcher.register(callback)\n"
        )
        self.assertFalse(scan(self.root).contextual_calls)
        for argument, call in ((0, "callback"), ("callback", "callback=callback")):
            self.source(
                "import dispatcher\ndef callback(): pass\ndef main(): dispatcher.register("
                + call
                + ")\n"
            )
            report = scan(
                self.root,
                Config(
                    registrations=[
                        {"pattern": "dispatcher.register", "argument": argument}
                    ]
                ),
            )
            self.assertEqual(
                report.contextual_calls[0]["execution"], "deferred_registration"
            )
            self.assertIn("declared deferred registration", render_mermaid(report))
        for call in ("unknown", "*values", "**values"):
            self.source(
                "import dispatcher\ndef main(): dispatcher.register(" + call + ")\n"
            )
            report = scan(
                self.root,
                Config(
                    registrations=[{"pattern": "dispatcher.register", "argument": 0}]
                ),
            )
            self.assertFalse(report.contextual_calls)
        for record in (
            {"pattern": "x", "argument": -1},
            {"pattern": "x", "argument": True},
            {"pattern": "x", "argument": "not valid"},
        ):
            with self.assertRaises(ValueError):
                Config(registrations=[record]).validate()

    def test_task_observation_and_escape_matrix(self):
        cases = {
            "task = asyncio.create_task(child())\nawait task": ("awaited", True),
            "task = asyncio.create_task(child())\nother = task\nawait other": (
                "awaited",
                True,
            ),
            "task = asyncio.create_task(child())\nreturn task": ("transferred", False),
            "task = asyncio.create_task(child())": ("retained_unobserved", False),
            "asyncio.create_task(child())": ("discarded", False),
            "await asyncio.create_task(child())": ("awaited", True),
            "task = asyncio.create_task(child())\nawait asyncio.gather(task)": (
                "awaited",
                True,
            ),
            "task = asyncio.create_task(child())\nawait asyncio.gather(task, return_exceptions=True)": (
                "exceptions_collected",
                False,
            ),
            "task = asyncio.create_task(child())\nif flag: await task": (
                "uncertain",
                False,
            ),
            "task = asyncio.create_task(child())\nconsume(task)": (
                "uncertain_escape",
                False,
            ),
            "task = asyncio.create_task(child())\nself.task = task": (
                "uncertain_escape",
                False,
            ),
            "async with asyncio.TaskGroup() as group:\n    group.create_task(child())": (
                "group_awaited",
                True,
            ),
        }
        for body, (status, observed) in cases.items():
            with self.subTest(body=body):
                self.source(
                    "import asyncio\nasync def child(): return 1\nasync def work(flag, self):\n"
                    + textwrap.indent(body, "    ")
                    + "\n"
                )
                report = scan(self.root)
                self.assertEqual(len(report.task_ownership), 1)
                self.assertEqual(report.task_ownership[0]["status"], status)
                self.assertEqual(report.task_ownership[0]["observed"], observed)
                if status == "retained_unobserved":
                    self.assertIn("FS009", {f.rule_id for f in report.findings})

    def test_retry_final_owner_and_attempt_noise(self):
        self.source(
            "import logging\ndef work():\n    for attempt in range(3):\n        try: return operation()\n        except Exception:\n            logging.error('attempt')\n            continue\n    raise RuntimeError('exhausted')\n"
        )
        report = scan(
            self.root,
            Config(
                operations=[
                    dict(
                        pattern="app.work",
                        exit="raise",
                        outcome="failed_operation",
                        owner="work-owner",
                        scope="operation",
                    )
                ]
            ),
        )
        self.assertEqual(report.retry_scopes[0]["reporting_owner"], "work-owner")
        self.assertIn("FS010", {f.rule_id for f in report.findings})
        self.assertIn("Uncertainty and next actions", render_html(report))
        self.assertTrue(report.recommendation_uncertainty)
        json.dumps(report.to_dict())

    def test_retry_structure_matrix_and_explicit_runtime_probe(self):
        import runpy
        from unittest.mock import patch

        for limit in (0, 1, 2, 3):
            for fail_until in (0, 1, 4):
                with self.subTest(limit=limit, fail_until=fail_until):
                    self.source(
                        f"import logging\ndef work():\n    for attempt in range({limit}):\n        try: return operation()\n        except Exception:\n            logging.error('attempt')\n            continue\n    raise RuntimeError('exhausted')\n"
                    )
                    report = scan(self.root)
                    self.assertTrue(report.retry_scopes)
                    self.assertIsNone(report.retry_scopes[0]["reporting_owner"])
                    attempts = []

                    def operation():
                        attempts.append(1)
                        if len(attempts) <= fail_until:
                            raise ValueError("authored failure")
                        return "recovered"

                    # Execute only this test's authored probe, never corpus/application source.
                    namespace = runpy.run_path(
                        str(self.root / "app.py"), init_globals={"operation": operation}
                    )
                    with patch("logging.error") as logged:
                        if fail_until >= limit:
                            with self.assertRaisesRegex(RuntimeError, "exhausted"):
                                namespace["work"]()
                        else:
                            self.assertEqual(namespace["work"](), "recovered")
                    self.assertEqual(logged.call_count, min(fail_until, limit))
        self.source("import retry_api\ndef work(): return retry_api.run(operation)\n")
        report = scan(
            self.root,
            Config(retries=[{"pattern": "retry_api.run", "owner": "work-owner"}]),
        )
        self.assertEqual(report.retry_scopes[0]["basis"], "declared_retry_wrapper")
        self.assertTrue(report.retry_scopes[0]["uncertain"])

    def test_declared_failure_enriches_warning_instead_of_duplicating(self):
        self.source(
            "import logging\ndef work():\n    try: operation()\n    except Exception:\n        logging.warning('failed')\n        return None\n"
        )
        report = scan(self.root, Config(operations=[self.contract("failed_operation")]))
        record = next(r for r in report.operation_outcomes if r["handler"])
        self.assertEqual(record["decision"]["kind"], "enrich_existing_log")
        self.assertEqual(record["decision"]["level"], "ERROR")

    def test_cache_retention_does_not_remove_unrelated_files(self):
        from flowsignal.cache import ScanCache

        cache_dir = self.root / "cache"
        cache = ScanCache(cache_dir, Config(max_cache_entries=2), self.root)
        keep = cache_dir / "unrelated.json"
        keep.write_text("keep", encoding="utf-8")
        for i in range(8):
            cache.write(["test", i], {"index": i})
        self.assertEqual(len(list(cache_dir.glob("flowsignal-*.json"))), 2)
        self.assertEqual(keep.read_text(encoding="utf-8"), "keep")
        self.assertEqual(cache.stats["evicted_entries"], 6)

    def test_cached_scan_reuses_checked_source_bytes(self):
        from unittest.mock import patch

        self.source("def work(): return 42\n")
        with patch(
            "flowsignal.scanner.read_snapshot",
            side_effect=AssertionError("duplicate read"),
        ):
            report = scan(self.root, cache_dir=self.root / "cache")
        self.assertEqual(report.analysis_stats["cache"]["source_files_reused"], 1)
        self.assertEqual(report.summary()["status"], "complete")

    def test_saved_gather_and_container_ownership(self):
        for expression, status in (
            (
                "pending = asyncio.gather(task, return_exceptions=True)\n    await pending",
                "exceptions_collected",
            ),
            ("pending = asyncio.gather(task)\n    await pending", "awaited"),
            ("pending = [task]\n    await pending", "uncertain_escape"),
            ("pending = {'job': task}\n    await pending", "uncertain_escape"),
        ):
            with self.subTest(expression=expression):
                self.source(
                    "import asyncio\nasync def child(): pass\nasync def work():\n    task = asyncio.create_task(child())\n    "
                    + expression
                    + "\n"
                )
                record = scan(self.root).task_ownership[0]
                self.assertEqual(record["status"], status)
                self.assertEqual(record["observed"], status == "awaited")

    def test_explicit_authored_registration_and_async_execution(self):
        import subprocess
        import sys

        self.source(
            "callbacks = []\ndef register(callback): callbacks.append(callback)\ndef dispatch(): return [callback() for callback in callbacks]\n",
            "registry.py",
        )
        self.source("def handler(): return 'called'\n", "handlers.py")
        self.source(
            "import registry\nfrom handlers import handler\ndef setup(): registry.register(handler)\n",
            "app.py",
        )
        report = scan(
            self.root,
            Config(registrations=[{"pattern": "registry.register", "argument": 0}]),
        )
        edge = next(
            r for r in report.contextual_calls if r["basis"] == "declared_registration"
        )
        self.assertTrue(edge["callee"].endswith(":handler"))
        self.assertFalse(edge["coverage_eligible"])
        # Explicitly execute only our authored fixture, never the review corpus.
        self.source(
            """
            import asyncio
            import json
            import app
            import registry
            async def child(): raise ValueError('authored failure')
            async def check():
                collected = asyncio.gather(asyncio.create_task(child()), return_exceptions=True)
                values = await collected
                assert isinstance(values[0], ValueError)
                task = asyncio.create_task(child())
                try:
                    await task
                except ValueError:
                    pass
                else:
                    raise AssertionError('await must propagate')
                try:
                    async with asyncio.TaskGroup() as group:
                        group.create_task(child())
                except ExceptionGroup as failures:
                    assert isinstance(failures.exceptions[0], ValueError)
                else:
                    raise AssertionError('group must propagate')
            app.setup()
            assert registry.dispatch() == ['called']
            asyncio.run(check())
            print(json.dumps({'registration': True, 'async': True}))
        """,
            "probe.py",
        )
        result = subprocess.run(
            [sys.executable, str(self.root / "probe.py")],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        self.assertEqual(
            json.loads(result.stdout), {"registration": True, "async": True}
        )

    def test_review_packet_validation_and_disagreements(self):
        import copy
        import hashlib

        from scripts.workflow_review import (
            RUBRIC,
            packet,
            score_reviews,
            validate_review,
        )

        self.source(
            "def work():\n    try: operation()\n    except Exception: return None\n"
        )
        manifest = self.root / "cohort.json"
        document = {
            "schema_version": "flowsignal-workflow-cohort-1",
            "rubric": RUBRIC,
            "repositories": {
                "sample": {
                    "path": ".",
                    "sha256": {
                        "app.py": hashlib.sha256(
                            (self.root / "app.py").read_bytes()
                        ).hexdigest()
                    },
                    "config": {},
                    "revision": "test-only",
                    "url": "authored-test",
                }
            },
            "cases": [
                {
                    "id": "sample/work",
                    "repository": "sample",
                    "workflow_type": "synthetic_test",
                    "symbol": "app.work",
                    "file": "app.py",
                    "line": 1,
                    "end_line": 3,
                    "split": "review_pending",
                    "tuning_used": False,
                }
            ],
        }
        manifest.write_text(json.dumps(document), encoding="utf-8")
        review = packet(manifest)
        review["reviewer"] = "synthetic-reviewer-A"
        row = review["cases"][0]
        row.update(
            status="complete",
            rationale="Importer/scorer test; not independent validation",
            findings=[["FS001", "app.work", 3]],
            judgments=[
                {
                    "symbol": "app.work",
                    "line": 3,
                    "outcomes": ["unknown"],
                    "alternatives": [
                        {"kind": "log", "level": "WARNING", "owner": None}
                    ],
                }
            ],
        )
        validate_review(review, document)
        a = self.root / "review-a.json"
        a.write_text(json.dumps(review), encoding="utf-8")
        result = score_reviews(manifest, [a])
        self.assertEqual(result["results"][0]["signal_level_owner_agreement"], 0)
        other = copy.deepcopy(review)
        other["reviewer"] = "synthetic-reviewer-B"
        other["cases"][0]["judgments"][0]["alternatives"].append(
            {"kind": "log", "level": "ERROR", "owner": None}
        )
        b = self.root / "review-b.json"
        b.write_text(json.dumps(other), encoding="utf-8")
        result = score_reviews(manifest, [a, b])
        self.assertEqual(result["results"][1]["signal_level_owner_agreement"], 1)
        self.assertEqual(len(result["disagreements"]), 1)
        self.assertFalse(result["independent_review_complete"])
        for mutate in (
            lambda d: d.update(cohort_signature="stale"),
            lambda d: d["cases"][0].update(findings=None),
            lambda d: d["cases"][0]["findings"].append(["FS001", "app.work", 3]),
            lambda d: d["cases"][0]["judgments"][0]["alternatives"].append(
                {"kind": "invalid", "level": "ERROR", "owner": None}
            ),
            lambda d: d["sources"]["sample"].update({"app.py": "modified"}),
            lambda d: d["cases"][0]["findings"][0].__setitem__(0, []),
            lambda d: d["cases"][0]["judgments"][0]["alternatives"][0].update(level=[]),
        ):
            bad = copy.deepcopy(review)
            mutate(bad)
            with self.assertRaises(ValueError):
                validate_review(bad, document)


if __name__ == "__main__":
    unittest.main()
