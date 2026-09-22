"""Exercise outcome advice, callback contexts, task/retry ownership, and exports."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SOURCES = {
    "service.py": """import json, logging
def recover(payload):
    try: return json.loads(payload)
    except Exception: return {}
def degraded(payload):
    try: return json.loads(payload)
    except Exception: return {'degraded': True}
def required(payload):
    for attempt in range(3):
        try: return json.loads(payload)
        except Exception:
            logging.error('attempt')
            continue
    raise RuntimeError('exhausted')
def finished(): return 'done'
""",
    "worker.py": """import asyncio
async def child(): return 1
async def unobserved():
    task = asyncio.create_task(child())
    return None
async def observed():
    task = asyncio.create_task(child())
    await task
async def collected():
    task = asyncio.create_task(child())
    await asyncio.gather(task, return_exceptions=True)
""",
    "registry.py": """callbacks = []
def register(callback): callbacks.append(callback)
""",
    "app.py": """import registry
from service import recover, degraded, required, finished
def invoke(callback): return callback()
def success(): return 'done'
def main():
    registry.register(success)
    invoke(success)
    recover('bad')
    degraded('bad')
    return required('bad')
raise RuntimeError('This authored fixture is scan-only and must not execute')
""",
}
CONFIG = """[flowsignal]
entrypoints = ["app.main", "worker.*", "service.finished"]
lifecycle_info = true
[[flowsignal.operations]]
pattern = "service.recover"
exit = "return"
outcome = "successful_recovery"
owner = "service.recover"
[[flowsignal.operations]]
pattern = "service.degraded"
exit = "return"
outcome = "degraded_recovery"
owner = "service.degraded"
[[flowsignal.operations]]
pattern = "service.required"
exit = "raise"
scope = "operation"
outcome = "failed_operation"
owner = "service.required"
[[flowsignal.operations]]
pattern = "service.finished"
exit = "return"
scope = "operation"
outcome = "successful_operation"
importance = "important"
owner = "service.finished"
[[flowsignal.registrations]]
pattern = "registry.register"
argument = 0
"""


def validate(output, python):
    root = output / "probe"
    root.mkdir(parents=True, exist_ok=True)
    for name, text in SOURCES.items():
        (root / name).write_text(text, encoding="utf-8")
    (root / "flowsignal.toml").write_text(CONFIG, encoding="utf-8")
    for format in ("json", "html", "text", "mermaid", "sarif"):
        process = subprocess.run(
            [
                str(python),
                "-m",
                "flowsignal",
                "scan",
                str(root),
                "--format",
                format,
                "--output",
                str(output / ("report." + format)),
                "--cache-dir",
                str(output / "cache"),
                "--timeout-seconds",
                "60",
                "--memory-mb",
                "512",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert process.returncode == 0, process.stderr
    result = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert result["summary"]["status"] == "complete"
    assert {"FS009", "FS010"} <= {r["rule_id"] for r in result["findings"]}
    assert {"callable_argument", "declared_registration"} <= {
        r["basis"] for r in result["contextual_calls"]
    }
    for symbol, level in (("degraded", "WARNING"), ("finished", "INFO")):
        assert any(
            r["operation"].endswith(":" + symbol) and r["decision"]["level"] == level
            for r in result["operation_outcomes"]
        )
    assert any(
        r["operation"].endswith(":recover") and r["decision"]["kind"] == "metric"
        for r in result["operation_outcomes"]
    )
    assert {r["status"] for r in result["task_ownership"]} == {
        "awaited",
        "exceptions_collected",
        "retained_unobserved",
    }
    assert result["retry_scopes"][0]["reporting_owner"] == "service.required"
    assert any(r["issues"] for r in result["recommendation_uncertainty"])
    stress = output / "stress"
    stress.mkdir(exist_ok=True)
    (stress / "app.py").write_text(
        "import json\ndef cycle(client):\n    client."
        + "external_api_" * 20
        + "()\n    json.loads('{}')\n    return cycle(client)\n",
        encoding="utf-8",
    )
    (stress / "flowsignal.toml").write_text(
        '[flowsignal]\nmax_graph_steps = 1\ninclude_source = false\nentrypoints = ["app.cycle"]\n',
        encoding="utf-8",
    )
    partial = subprocess.run(
        [
            str(python),
            "-m",
            "flowsignal",
            "scan",
            str(stress),
            "--format",
            "html",
            "--output",
            str(output / "partial.html"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert partial.returncode == 2, partial.stderr
    validation = {
        "passed": True,
        "executed_probe": False,
        "summary": result["summary"],
        "contextual_calls": len(result["contextual_calls"]),
        "task_states": [r["status"] for r in result["task_ownership"]],
        "formats": ["json", "html", "text", "mermaid", "sarif"],
        "partial_cycle_report_exit": partial.returncode,
    }
    (output / "validation.json").write_text(
        json.dumps(validation, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/outcome-review")
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    validate(args.output.resolve(), args.python.resolve())
