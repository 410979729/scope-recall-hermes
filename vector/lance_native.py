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
from pathlib import Path
import subprocess
import sys
from typing import Any

_PROBE_TIMEOUT_SECONDS = 10.0
_ENVIRONMENT_MARKERS = ("site-packages", "dist-packages")
_native_import_safe: bool | None = None


def _environment_interpreter(package_file: str) -> str | None:
    """The interpreter of the environment that owns an installed ``package_file``.

    A Hermes install may boot the base interpreter and inject a venv's
    site-packages into ``sys.path``/``PYTHONPATH`` instead of booting that venv
    (no-boot-through-venv): ``sys.executable`` and ``sys.prefix`` then name the
    base installation while the dependencies -- ``jsonschema`` for
    ``contracts``, LanceDB for the worker -- live in the injected venv.  A
    module imported from a ``site-packages`` directory belongs to the nearest
    ancestor directory holding a ``pyvenv.cfg``, the marker CPython itself reads
    to resolve a prefix, so that environment's own interpreter is what the child
    has to be told about.

    The returned path is only that resolution hint; it is never executed, which
    is what keeps the base interpreter as the launched process.  ``None`` means
    no environment owns this file (a checkout or a directory plugin install) and
    the caller must keep the interpreter it is already running on.
    """
    parts = Path(package_file).resolve().parts
    installed = next((index for index, part in enumerate(parts) if part in _ENVIRONMENT_MARKERS), None)
    if installed is None:
        return None
    for parent in Path(*parts[: installed + 1]).parents:
        if (parent / "pyvenv.cfg").is_file():
            return str(parent / "Scripts" / "python.exe")
    return None


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
    env["__PYVENV_LAUNCHER__"] = _environment_interpreter(__file__) or sys.executable
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
