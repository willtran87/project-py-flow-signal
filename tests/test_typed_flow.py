from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from flowsignal import scan
from flowsignal.baseline import make_baseline
from flowsignal.config import Config


class TypedFlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def source(self, code, name="app.py"):
        (self.root / name).write_text(textwrap.dedent(code), encoding="utf-8")

    def targets(self, code, config=None):
        self.source(code)
        self.report = scan(self.root, config)
        return [
            c.resolved_name
            for c in self.report.calls
            if c.target and c.resolved_name.endswith(".run")
        ]

    def test_typed_and_tuple_initialized_fields(self):
        targets = self.targets("""
            class Client:
                def run(self): pass
            class Workflow:
                def __init__(self, client: Client):
                    self.client, self.label = client, 'workflow'
                def execute(self): self.client.run()
        """)
        self.assertEqual(targets, ["app.Client.run"])

    def test_constructed_field_and_helper_chain(self):
        targets = self.targets("""
            class Client:
                def run(self): pass
            class Workflow:
                def __init__(self): self.client = Client()
            def create(): return Workflow()
            def main(): create().client.run()
        """)
        self.assertEqual(targets, ["app.Client.run"])

    def test_imported_helper_and_alias(self):
        self.source(
            """
            class Client:
                def run(self): pass
            def create(): return Client()
        """,
            "factory.py",
        )
        self.assertEqual(
            self.targets("""
            from factory import create as build
            def main():
                client = build()
                client.run()
        """),
            ["factory.Client.run"],
        )

    def test_method_return_and_annotation_contract(self):
        self.assertEqual(
            self.targets("""
            class Client:
                def run(self): pass
            class Factory:
                def create(self) -> Client: return opaque()
            def main(factory: Factory): factory.create().run()
        """),
            ["app.Client.run"],
        )

    def test_awaited_helper_only(self):
        self.assertEqual(
            self.targets("""
            class Client:
                def run(self): pass
            async def create(): return Client()
            def generate():
                yield Client()
            async def main():
                good = await create()
                good.run()
                create().run()
                generate().run()
        """),
            ["app.Client.run"],
        )

    def test_conflicting_unknown_and_deleted_fields(self):
        for mutation in [
            "self.client = Other()",
            "self.client = opaque()",
            "del self.client",
            "self.client += opaque()",
        ]:
            with self.subTest(mutation=mutation):
                self.assertEqual(
                    self.targets(f"""
                    class Client:
                        def run(self): pass
                    class Other:
                        def run(self): pass
                    class Workflow:
                        def __init__(self): self.client = Client()
                        def change(self): {mutation}
                        def execute(self): self.client.run()
                """),
                    [],
                )

    def test_ambiguous_return_body_overrides_annotation(self):
        self.assertEqual(
            self.targets("""
            class Client:
                def run(self): pass
            class Other: pass
            def create(flag) -> Client:
                if flag: return Client()
                return Other()
            def main(): create(True).run()
        """),
            [],
        )

    def test_nested_return_does_not_pollute_helper(self):
        self.assertEqual(
            self.targets("""
            class Client:
                def run(self): pass
            def create():
                def inner(): return opaque()
                return Client()
            def main(): create().run()
        """),
            ["app.Client.run"],
        )

    def test_decorated_and_shadowed_helpers(self):
        self.assertEqual(
            self.targets("""
            class Client:
                def run(self): pass
            @wrapper
            def create() -> Client: return Client()
            def plain(): return Client()
            def main(plain):
                plain().run()
                create().run()
        """),
            [],
        )

    def test_recursive_helpers_terminate(self):
        self.assertEqual(
            self.targets("""
            def first(): return second()
            def second(): return first()
            def main(): first().run()
        """),
            [],
        )
        self.assertEqual(self.report.summary()["status"], "complete")

    def test_type_budget_marks_scan_incomplete(self):
        self.targets(
            """
            class Client:
                def run(self): pass
            def main(client: Client): client.run()
        """,
            Config(max_type_steps=1),
        )
        self.assertEqual(self.report.summary()["status"], "incomplete")
        self.assertEqual(self.report.analysis_stats["type_steps"], 1)
        self.assertEqual(
            sum(d.code == "type_work_limit" for d in self.report.diagnostics), 1
        )
        with self.assertRaisesRegex(ValueError, "incomplete"):
            make_baseline(self.report)


if __name__ == "__main__":
    unittest.main()
