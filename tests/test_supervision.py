from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowsignal.cli import main
from flowsignal.supervisor import publish
from flowsignal.worker import limit_memory


@unittest.skipUnless(
    sys.platform == "win32" or sys.platform.startswith("linux"),
    "OS memory backend requires Windows/Linux",
)
class SupervisionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "app.py"
        self.source.write_text(
            "import requests\ndef run(): requests.get('url')\n", encoding="utf-8"
        )
        self.output = self.root / "report.json"
        self.baseline = self.root / "baseline.json"

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main(["scan", str(self.source), *map(str, args)])
        return result, out.getvalue(), err.getvalue()

    def test_success_threshold_and_baseline(self):
        status, out, err = self.cli(
            "--timeout-seconds",
            30,
            "--memory-mb",
            256,
            "--format",
            "json",
            "--fail-on",
            "low",
            "--save-baseline",
            self.baseline,
        )
        self.assertEqual((status, err), (1, ""))
        report = json.loads(out)
        self.assertEqual(report["summary"]["status"], "complete")
        self.assertEqual(report["analysis_stats"]["worker_limits"]["memory_mb"], 256)
        self.assertTrue(self.baseline.is_file())
        status, out, err = self.cli(
            "--memory-mb",
            256,
            "--format",
            "json",
            "--baseline",
            self.baseline,
            "--fail-on",
            "low",
            "--fail-on-new",
        )
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(json.loads(out)["baseline"]["counts"]["unchanged"], 1)

    def test_timeout_preserves_report_and_baseline(self):
        self.output.write_bytes(b"previous report")
        self.baseline.write_bytes(b"previous baseline")
        status, out, err = self.cli(
            "--timeout-seconds",
            0.001,
            "--output",
            self.output,
            "--save-baseline",
            self.baseline,
        )
        self.assertEqual(status, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["diagnostics"][0]["code"], "worker_timeout")
        self.assertEqual(self.output.read_bytes(), b"previous report")
        self.assertEqual(self.baseline.read_bytes(), b"previous baseline")

    def test_partial_scan_still_publishes_but_cannot_save_baseline(self):
        self.source.write_text("def broken(:\n", encoding="utf-8")
        status, out, err = self.cli("--memory-mb", 256, "--format", "json")
        self.assertEqual((status, err), (2, ""))
        self.assertEqual(json.loads(out)["summary"]["status"], "incomplete")
        self.output.write_bytes(b"previous")
        self.baseline.write_bytes(b"previous")
        status, _, err = self.cli(
            "--memory-mb",
            256,
            "--output",
            self.output,
            "--save-baseline",
            self.baseline,
        )
        self.assertEqual(status, 2)
        self.assertIn("incomplete", err)
        self.assertEqual(self.output.read_bytes(), b"previous")
        self.assertEqual(self.baseline.read_bytes(), b"previous")

    def test_output_protections_apply_in_worker(self):
        config = self.root / "custom.settings"
        config.write_text("[flowsignal]\n", encoding="utf-8")
        status, _, err = self.cli(
            "--memory-mb", 256, "--config", config, "--output", config
        )
        self.assertEqual(status, 2)
        self.assertIn("overwrite", err)
        self.assertEqual(config.read_text(), "[flowsignal]\n")

    def test_limits_validate_before_launch(self):
        for flag, value in [
            ("--timeout-seconds", "nan"),
            ("--timeout-seconds", "inf"),
            ("--timeout-seconds", 0),
            ("--memory-mb", 0),
            ("--memory-mb", 2_000_000),
        ]:
            with (
                self.subTest(flag=flag, value=value),
                patch("flowsignal.supervisor.subprocess.Popen") as spawn,
            ):
                status, _, _ = self.cli(flag, value)
                self.assertEqual(status, 2)
                spawn.assert_not_called()

    def test_isolated_worker_does_not_import_target_modules(self):
        sentinel = self.root / "executed"
        (self.root / "json.py").write_text(
            f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n",
            encoding="utf-8",
        )
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            status, _, err = self.cli("--memory-mb", 256, "--format", "json")
        finally:
            os.chdir(previous)
        self.assertEqual((status, err), (0, ""))
        self.assertFalse(sentinel.exists())

    def run_render_fault(self, expression, expected, timeout=30):
        # Execute only this authored harness. Scanned application code is never run.
        package = Path(__file__).resolve().parents[1] / "src"
        harness = self.root / "harness.py"
        harness.write_text(
            f"import sys, runpy, time\nsys.path.insert(0, {str(package)!r})\nimport flowsignal.cli\n"
            f"def fault(*args):\n    {expression}\nflowsignal.cli.render_html = fault\n"
            f"runpy.run_path({str(package / 'flowsignal' / 'worker.py')!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        spawn = subprocess.Popen
        processes = []

        def launch(command, **kwargs):
            command[2] = str(harness)
            process = spawn(command, **kwargs)
            processes.append(process)
            return process

        self.output.write_bytes(b"previous report")
        self.baseline.write_bytes(b"previous baseline")
        with patch("flowsignal.supervisor.subprocess.Popen", side_effect=launch):
            status, _, err = self.cli(
                "--memory-mb",
                256,
                "--timeout-seconds",
                timeout,
                "--format",
                "html",
                "--output",
                self.output,
                "--save-baseline",
                self.baseline,
            )
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(err)["diagnostics"][0]["code"], expected)
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertEqual(self.output.read_bytes(), b"previous report")
        self.assertEqual(self.baseline.read_bytes(), b"previous baseline")
        self.assertFalse(list(self.root.glob(".flowsignal-*.tmp")))

    def test_os_memory_limit_during_rendering(self):
        self.run_render_fault(
            "return bytearray(512 * 1024 * 1024)", "worker_memory_limit"
        )

    def test_timeout_during_rendering(self):
        self.run_render_fault("time.sleep(30)", "worker_timeout", timeout=1)

    def test_worker_crash_preserves_outputs(self):
        self.run_render_fault('raise RuntimeError("authored failure")', "worker_failed")

    def test_unsupported_backend_does_not_silently_disable_limit(self):
        with patch("flowsignal.worker.sys.platform", "unsupported"):
            with self.assertRaisesRegex(OSError, "Windows and Linux"):
                limit_memory(256)

    def test_publication_failure_preserves_existing_report(self):
        self.output.write_bytes(b"previous")
        with patch("flowsignal.supervisor.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                publish(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"previous")
        self.assertFalse(list(self.root.glob(".flowsignal-*.tmp")))


if __name__ == "__main__":
    unittest.main()
