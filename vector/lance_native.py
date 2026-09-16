"""Load LanceDB/PyArrow without letting a bad wheel take the host down.

Some LanceDB/PyArrow wheels terminate Python with SIGILL on CPUs without
AVX/AVX2.  A try/except around ``import lancedb`` cannot catch that, because
the process is already gone, so the import is rehearsed in a child process
first and the verdict cached for the life of this interpreter.  A failed
rehearsal lets the runtime fall back to the SQLite store instead of crashing.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from typing import Any

_PROBE_TIMEOUT_SECONDS = 10.0
_native_import_safe: bool | None = None


def python_subprocess_options() -> dict[str, Any]:
    """Keep Windows venv identity without launching its redirector process.

    Windows venv executables (including uv's) may start a second Python
    process.  Terminating the outer redirector does not kill that interpreter,
    which can keep anonymous pipes open forever during timeout cleanup.
    CPython's launcher environment preserves the venv with the base executable.
    """
    if sys.platform != "win32":
        return {}
    env = dict(os.environ)
    env["__PYVENV_LAUNCHER__"] = sys.executable
    return {
        "executable": getattr(sys, "_base_executable", None) or sys.executable,
        "env": env,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def native_import_is_safe() -> bool:
    """Whether ``import lancedb, pyarrow`` survives in a child process."""
    global _native_import_safe
    if _native_import_safe is None:
        try:
            completed = subprocess.run(
                [sys.executable, "-c", "import lancedb, pyarrow"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
                **python_subprocess_options(),
            )
            _native_import_safe = completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _native_import_safe = False
    return _native_import_safe


def skip_native_probe() -> None:
    """Trust the in-process import; for an interpreter that is already disposable."""
    global _native_import_safe
    _native_import_safe = True


def native_modules() -> tuple[Any, Any] | None:
    """``(lancedb, pyarrow)`` once the probe passed, else ``None``."""
    if not native_import_is_safe():
        return None
    try:
        return importlib.import_module("lancedb"), importlib.import_module("pyarrow")
    except Exception:
        return None


__all__ = ["native_import_is_safe", "native_modules", "python_subprocess_options", "skip_native_probe"]
