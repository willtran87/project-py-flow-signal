"""Private worker bootstrap; launched by absolute path with isolated Python (-I)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_job = None


def limit_memory(megabytes: int) -> str:
    """Apply an OS-enforced allocation limit before loading or scanning input."""
    global _job
    size = megabytes * 1024 * 1024
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IO),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
        limits.process_memory = size
        if not kernel.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(handle)
            raise error
        _job = handle  # Keep the job alive for the lifetime of this worker.
        return "windows_job_committed_memory"
    if sys.platform.startswith("linux"):
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        effective = min(
            [size]
            + [value for value in (soft, hard) if value != resource.RLIM_INFINITY]
        )
        resource.setrlimit(resource.RLIMIT_AS, (effective, effective))
        return "linux_address_space"
    raise OSError(
        "Enforced worker memory limits are supported on Windows and Linux only"
    )


def main():
    stage = Path(sys.argv[1])
    request = json.loads((stage / "request.json").read_text(encoding="utf-8"))
    try:
        backend = limit_memory(request["memory_mb"])
    except (OSError, ValueError):
        os.write(
            2, b"Worker memory limit could not be installed; scan was not started.\n"
        )
        return 71
    try:
        # -I excludes the target working directory and PYTHONPATH. Use this
        # installation's package, never a package from the scanned checkout.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from flowsignal.cli import main as cli

        status = cli(
            request["argv"],
            _stage=stage,
            _worker_limits={
                "timeout_seconds": request["timeout_seconds"],
                "memory_mb": request["memory_mb"],
                "memory_backend": backend,
            },
        )
        (stage / "result.json").write_text(
            json.dumps({"exit_code": status}), encoding="utf-8"
        )
        return 0
    except MemoryError:
        os.write(2, b"Worker allocation failed under the enforced memory limit.\n")
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
