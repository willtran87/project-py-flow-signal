"""Positive and negative controls for research-informed resolution changes."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from flowsignal import scan
from flowsignal.diagram import graph_data


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def source(self, content, name="app.py"):
        (self.root / name).write_text(textwrap.dedent(content), encoding="utf-8")

    def report(self, content):
        self.source(content)
        return scan(self.root)

    def test_nullable_annotations_keep_one_concrete_receiver(self):
        for annotation in (
            "Client | None",
            "None | Client",
            "Optional[Client]",
            "Union[None, Client]",
            "'Optional[Client]'",
            "'Client | None'",
            "Union[Client, Client, None]",
        ):
            with self.subTest(annotation=annotation):
                report = self.report(f"""
                    from typing import Optional, Union
                    class Client:
                        def send(self): pass
                    def run(client: {annotation}):
                        client = client or Client()
                        client.send()
                """)
                call = next(c for c in report.calls if c.expression == "client.send")
                self.assertEqual(call.target, "app.py:Client.send")
                self.assertEqual(call.resolution, "inferred_receiver")

    def test_union_does_not_choose_between_types_or_unknown_arms(self):
        for annotation in (
            "Client | Other",
            "Union[Client, Other, None]",
            "Client | list[Client]",
            "Optional[list[Client]]",
            "'malformed['",
            "type[Client]",
        ):
            with self.subTest(annotation=annotation):
                report = self.report(f"""
                    from typing import Optional, Union
                    class Client:
                        def send(self): pass
                    class Other:
                        def send(self): pass
                    def run(client: {annotation}):
                        client.send()
                """)
                self.assertIsNone(report.calls[-1].target)

    def test_annotation_wrappers_respect_aliases_and_shadowing(self):
        for imported, expected in (("typing", "app.py:Client.send"), ("custom", None)):
            with self.subTest(imported=imported):
                report = self.report(f"""
                    from {imported} import Optional as Maybe
                    class Client:
                        def send(self): pass
                    def run(client: Maybe[Client]):
                        client.send()
                """)
                self.assertEqual(report.calls[-1].target, expected)

    def test_annotated_local_and_alias_preserve_receiver(self):
        report = self.report("""
            class Client:
                def send(self): pass
            def run():
                client: 'Client | None' = obtain()
                alias = client
                alias.send()
        """)
        self.assertEqual(report.calls[-1].target, "app.py:Client.send")

    def test_conflicting_fallback_and_reassignment_stay_unknown(self):
        for assignment in (
            "client = client or Other()",
            "client = client or obtain()",
            "client = None",
            "client = Other()",
            "client = client and Other()",
        ):
            with self.subTest(assignment=assignment):
                report = self.report(f"""
                    class Client:
                        def send(self): pass
                    class Other:
                        def send(self): pass
                    def run(client: Client | None):
                        {assignment}
                        client.send()
                """)
                self.assertIsNone(report.calls[-1].target)

    def test_variadic_annotations_are_not_receiver_types(self):
        report = self.report("""
            class Client:
                def send(self): pass
            def run(*args: Client, **kwargs: Client):
                args.send()
                kwargs.send()
        """)
        self.assertTrue(all(c.target is None for c in report.calls))

    def test_constructor_alias_and_diagram_show_inferred_initializer(self):
        self.source(
            """
            import ast
            class Worker(ast.NodeVisitor):
                def __init__(self): pass
        """,
            "worker.py",
        )
        self.source("""
            from worker import Worker as W
            import worker as mod
            def run():
                W()
                mod.Worker()
        """)
        report = scan(self.root)
        constructors = [
            c for c in report.calls if c.target == "worker.py:Worker.__init__"
        ]
        self.assertEqual(len(constructors), 2)
        self.assertTrue(
            all(c.resolution == "inferred_constructor" for c in constructors)
        )
        edges = graph_data(report)["edges"]
        self.assertEqual(
            sum(e["label"].startswith("possible initializer") for e in edges), 2
        )

    def test_callable_instances_and_shadowed_classes_are_not_constructors(self):
        report = self.report("""
            class Worker:
                def __init__(self): pass
                def __call__(self): pass
            def run(typed: Worker, Worker: Worker):
                typed()
                Worker()
            def other():
                instance = Worker()
                instance()
                for Worker in providers:
                    Worker()
        """)
        # The loop assignment shadows Worker throughout other(), conservatively.
        self.assertFalse(any(c.target for c in report.calls))

    def test_constructed_instance_call_does_not_repeat_initializer(self):
        report = self.report("""
            class Worker:
                def __init__(self): pass
                def __call__(self): pass
            def run():
                instance = Worker()
                instance()
        """)
        self.assertEqual(report.calls[0].target, "app.py:Worker.__init__")
        self.assertIsNone(report.calls[1].target)

    def test_customized_construction_does_not_claim_initializer(self):
        for definition in (
            "@decorate\nclass Worker:\n    def __init__(self): pass",
            "class Worker(metaclass=Meta):\n    def __init__(self): pass",
            "class Worker:\n    def __new__(cls): return object()\n    def __init__(self): pass",
            "class Worker:\n    @decorate\n    def __init__(self): pass",
            "class Worker:\n    async def __init__(self): pass",
        ):
            with self.subTest(definition=definition):
                report = self.report(definition + "\ndef run():\n    Worker()\n")
                self.assertIsNone(report.calls[-1].target)

    def test_duplicate_classes_and_inherited_initializer_stay_unresolved(self):
        report = self.report("""
            class Base:
                def __init__(self): pass
            class Child(Base): pass
            class Duplicate:
                def __init__(self): pass
            class Duplicate: pass
            def run():
                Child()
                Duplicate()
        """)
        self.assertTrue(all(c.target is None for c in report.calls))
        self.assertEqual(report.calls[-1].resolution, "ambiguous")

    def test_constructor_handler_can_own_initializer_failure(self):
        report = self.report("""
            import logging
            import requests
            class Worker:
                def __init__(self):
                    requests.get('url')
            def run():
                try:
                    return Worker()
                except Exception:
                    logging.exception('creation failed')
                    raise
        """)
        self.assertNotIn("FS005", [f.rule_id for f in report.findings])

    def test_sys_exc_info_alias_retains_traceback(self):
        for imported, expression in (
            ("import sys", "sys.exc_info()"),
            ("import sys as system", "system.exc_info()"),
            ("from sys import exc_info as context", "context()"),
        ):
            with self.subTest(expression=expression):
                report = self.report(f"""
                    import logging
                    {imported}
                    def run():
                        try: fail()
                        except Exception:
                            logging.error('failed', exc_info={expression})
                """)
                self.assertTrue(report.logs[0].exception_context)
                self.assertNotIn("FS006", [f.rule_id for f in report.findings])

    def test_shadowed_and_unknown_exc_info_do_not_hide_recommendation(self):
        for imported, parameter, expression in (
            ("import sys", "sys", "sys.exc_info()"),
            ("import custom as sys", "", "sys.exc_info()"),
            ("", "", "sys.exc_info()"),
            ("import sys", "", "sys.exc_info(1)"),
            ("import sys", "flag", "flag"),
            ("import sys", "", "False"),
        ):
            with self.subTest(
                expression=expression, imported=imported, parameter=parameter
            ):
                report = self.report(f"""
                    import logging
                    {imported}
                    def run({parameter}):
                        try: fail()
                        except Exception:
                            logging.error('failed', exc_info={expression})
                """)
                self.assertFalse(report.logs[0].exception_context)
                self.assertIn("FS006", [f.rule_id for f in report.findings])


if __name__ == "__main__":
    unittest.main()
