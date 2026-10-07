"""Load LanceDB/PyArrow without letting a bad wheel take the host down.

Some LanceDB/PyArrow wheels terminate Python with SIGILL on CPUs without
AVX/AVX2.  A try/except around ``import lancedb`` cannot catch that, because
the process is already gone, so the import is rehearsed in a child process
first and the verdict cached for the life of this interpreter.  A failed
rehearsal lets the runtime fall back to the SQLite store instead of crashing.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

_PROBE_TIMEOUT_SECONDS = 10.0
_ENVIRONMENT_MARKERS = ("site-packages", "dist-packages")
_native_import_safe: bool | None = None
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _PACKAGE_ROOT / "_lance_worker.py"
#: The helper's imports from outside the standard library, by top-level name: its start-up reaches ``jsonschema``
#: (``core/__init__`` composes Core), then LanceDB and PyArrow, which load numpy.  What these import in turn is
#: looked for beside them, where the installer that installed them put it.
_HELPER_DEPENDENCIES = ("jsonschema", "lancedb", "pyarrow", "numpy")
_START_FAILURE_BYTES = 8192


def helper_import_roots() -> list[str]:
    """The directories this process imports the helper's dependencies from, in its own ``sys.path`` order.

    The helper runs isolated (``-I``) and sees no PYTHONPATH.  Hermes Desktop's package manager boots a bundled
    CPython and puts the environment it built on PYTHONPATH (#176): the helper died at ``import jsonschema`` before
    its first answer, and every embed and every vector search failed as ``worker_failed``.  It is handed these
    directories and nothing else of this process's path: not its working directory, not a host's source tree, never
    this package's own directory, whose top-level names (``packaging``, ``core``, ``tests``) ``-I`` keeps off the
    path.  ``find_spec`` finds a top-level module without running it.
    """
    holders: dict[str, str] = {}
    for name in _HELPER_DEPENDENCIES:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec is None:
            continue
        locations = list(spec.submodule_search_locations or ()) or ([spec.origin] if spec.has_location else [])
        for location in locations:
            holder = os.path.dirname(os.path.abspath(location))
            holders.setdefault(os.path.normcase(holder), holder)
    holders.pop(os.path.normcase(str(_PACKAGE_ROOT)), None)
    # This process's order first (PYTHONPATH ahead of site-packages, as it imports them), then a directory a finder
    # reached off ``sys.path``; each directory once, compared as Windows compares paths.
    roots: dict[str, str] = {}
    for entry in (*(os.path.abspath(entry) for entry in sys.path if entry), *holders.values()):
        if os.path.normcase(entry) in holders:
            roots.setdefault(os.path.normcase(entry), entry)
    return list(roots.values())


def helper_command(*arguments: str) -> list[str]:
    """The helper's command line: isolated, its arguments, then the directories it imports from first."""
    return [sys.executable, "-I", "-B", str(_WORKER), *arguments, *helper_import_roots()]


def helper_start_failure(timeout: float) -> str | None:
    """What the helper's start-up writes to stderr when it fails, run once more; ``None`` when it starts.

    The helper's own stderr is discarded (``process_store._spawn_helper``), so a helper that ended before its first
    answer left nothing to say why.  This run is sent no request at all and cannot hold memory text, only an import
    or start-up traceback, of which the last 8 KiB are kept.  In UTF-8: an isolated child ignores PYTHONUTF8 and
    wrote a localized error in the ANSI code page, which read as replacement characters (review of 3.4.10).
    """
    command = helper_command("--probe")
    command[1:1] = ["-X", "utf8"]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(0.001, min(float(timeout), _PROBE_TIMEOUT_SECONDS)),
            check=False,
            **python_subprocess_options(),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if completed.returncode == 0:
        return None
    return completed.stderr[-_START_FAILURE_BYTES:].decode("utf-8", errors="replace")


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
                # The import this process is about to make, with this process's path: only the in-process store
                # rehearses it (off Windows), and it imports from everywhere this process does.  The isolated
                # helper's start-up sees only the directories handed to it, and failed layouts this import serves
                # (review of 3.4.10).
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


__all__ = [
    "helper_command",
    "helper_import_roots",
    "helper_start_failure",
    "native_import_is_safe",
    "native_modules",
    "python_subprocess_options",
    "skip_native_probe",
]
