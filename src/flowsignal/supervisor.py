"""Bounded worker supervision and streaming, atomic report publication."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def publish(source: Path, destination: Path) -> None:
    from .cli import validate_output_path

    validate_output_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".flowsignal-", suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            with source.open("rb") as stream:
                shutil.copyfileobj(stream, output, length=64 * 1024)
        validate_output_path(destination)
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def run(argv: list[str], args) -> int:
    timeout = args.timeout_seconds if args.timeout_seconds is not None else 300.0
    memory = args.memory_mb if args.memory_mb is not None else 1024
    if not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ValueError(
            "--timeout-seconds must be finite, greater than zero, and at most 86400"
        )
    if not 32 <= memory <= 1_048_576:
        raise ValueError("--memory-mb must be between 32 and 1048576 MiB")
    from .cli import validate_output_path

    for path in (args.output, args.save_baseline):
        if path:
            validate_output_path(path)
    with tempfile.TemporaryDirectory(prefix="flowsignal-worker-") as directory:
        stage = Path(directory)
        (stage / "request.json").write_text(
            json.dumps({"argv": argv, "memory_mb": memory, "timeout_seconds": timeout}),
            encoding="utf-8",
        )
        reason = None
        started = time.monotonic()
        with (stage / "stderr.txt").open("wb") as errors:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("worker.py")),
                    str(stage),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
                if time.monotonic() - started > timeout:
                    reason = "worker_timeout"
            except subprocess.TimeoutExpired:
                reason = "worker_timeout"
            except KeyboardInterrupt:
                reason = "worker_cancelled"
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
        if reason is None and process.returncode:
            reason = {70: "worker_memory_limit", 71: "worker_limit_unavailable"}.get(
                process.returncode, "worker_failed"
            )
        if reason is None:
            try:
                with (stage / "result.json").open(encoding="utf-8") as stream:
                    result = json.loads(stream.read(4096))
                status = result["exit_code"]
                if type(status) is not int or status not in {0, 1, 2}:
                    raise ValueError("Invalid worker status")
                if status in {0, 1} and not (stage / "report").is_file():
                    raise ValueError("Missing staged report")
                if (
                    args.save_baseline
                    and status in {0, 1}
                    and not (stage / "baseline").is_file()
                ):
                    raise ValueError("Missing staged baseline")
            except (OSError, ValueError, KeyError, TypeError):
                reason = "worker_failed"
        if reason:
            messages = {
                "worker_timeout": "Scan/render exceeded the worker wall-time limit.",
                "worker_memory_limit": "An allocation failed under the worker memory limit.",
                "worker_limit_unavailable": "The OS memory limit could not be installed; Windows or Linux support is required and the scan was not started.",
                "worker_cancelled": "The supervised scan was cancelled.",
                "worker_failed": "The worker terminated unexpectedly or did not produce a valid completion record.",
            }
            failure = {
                "schema_version": "flowsignal-worker-failure-1",
                "status": "incomplete",
                "diagnostics": [
                    {
                        "code": reason,
                        "message": messages[reason]
                        + " Existing report and baseline destinations were preserved.",
                    }
                ],
                "worker_limits": {"timeout_seconds": timeout, "memory_mb": memory},
            }
            print(json.dumps(failure), file=sys.stderr)
            return 2
        with (stage / "stderr.txt").open("rb") as stream:
            error = stream.read(8192).decode("utf-8", errors="replace")
        if error:
            sys.stderr.write(error)
        if args.save_baseline and status == 2:
            return status
        # No publication occurs before the worker completes successfully.
        # Ordinary incomplete scans may still carry partial reports (exit 2).
        if (stage / "report").is_file():
            if args.output:
                publish(stage / "report", args.output)
            else:
                with (stage / "report").open(encoding="utf-8") as stream:
                    shutil.copyfileobj(stream, sys.stdout, length=64 * 1024)
        if args.save_baseline and status in {0, 1}:
            publish(stage / "baseline", args.save_baseline)
        return status
