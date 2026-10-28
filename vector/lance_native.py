"""Load LanceDB/PyArrow without letting a bad wheel take the host down.

Some LanceDB/PyArrow wheels terminate Python with SIGILL on CPUs without
AVX/AVX2.  A try/except around ``import lancedb`` cannot catch that, because
the process is already gone, so the import is rehearsed in a child process
first. A completed verdict is cached for the life of this interpreter;
an incomplete rehearsal can be retried after a cooldown. Request deadlines
bound the wait for the rehearsal. The child arms its own lifetime limit so
even abrupt host exit cannot leave a hanging import running indefinitely.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.deadline import RequestDeadline

_LOG = logging.getLogger(__name__)
_PROBE_TIMEOUT_SECONDS = 10.0
# A busy host gets time to settle without spawning an import child on every recall.
_PROBE_RETRY_SECONDS = 30.0
_PROBE_MAX_RETRY_SECONDS = 1800.0
_PROBE_WAIT_MARGIN_SECONDS = 0.2
_ENVIRONMENT_MARKERS = ("site-packages", "dist-packages")
_native_import_safe: bool | None = None
_probe_retry_at = 0.0
_probe_retry_delay = _PROBE_RETRY_SECONDS
_probe_lock = threading.Lock()
_probe_child: subprocess.Popen[Any] | None = None
_probe_started_at = 0.0
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
    CPython and puts the environment it built on PYTHONPATH: without it the helper dies at ``import jsonschema``
    before its first answer, and every embed and every vector search fails as ``worker_failed``.  It is handed these
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
    answer would leave nothing to say why.  This run is sent no request at all and cannot hold memory text, only an
    import or start-up traceback, of which the last 8 KiB are kept.  In UTF-8: an isolated child ignores PYTHONUTF8 and
    would write a localized error in the ANSI code page, which reads as replacement characters.
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


def _probe_command_environment() -> dict[str, str] | None:
    """The rehearsal child's environment: the parent's, plus the directories its host imports its dependencies from.

    The child inherits ``os.environ``, and on a package-manager host that carries nothing about the environment the
    package manager injected into this process's ``sys.path`` at run time (``docs/install.md`` §1) - the rehearsal
    then fails although this process imports LanceDB and PyArrow fine.  The helper receives those directories as
    arguments; the probe gets them on ``PYTHONPATH``, so it rehearses the import from where this process actually
    imports it.  ``None`` means "inherit" (the host imports its dependencies from the interpreter's own paths).
    """
    roots = helper_import_roots()
    if not roots:
        return None
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join((*roots, existing)) if existing else os.pathsep.join(roots)
    return env


def _reset_native_probe_after_fork() -> None:
    """Keep a completed verdict but discard handles and locks owned by the parent."""
    global _probe_lock, _probe_child, _probe_started_at, _probe_retry_at, _probe_retry_delay
    # Waiting on an inherited Popen handle can report a false success after ECHILD.
    _probe_lock = threading.Lock()
    if _probe_child is not None:
        # Popen's finalizer also polls; this process cannot reap the parent's child.
        vars(_probe_child)["_child_created"] = False
    _probe_child = None
    _probe_started_at = _probe_retry_at = 0.0
    _probe_retry_delay = _PROBE_RETRY_SECONDS


if _register_at_fork := getattr(os, "register_at_fork", None):
    _register_at_fork(after_in_child=_reset_native_probe_after_fork)


def _start_native_probe() -> subprocess.Popen[Any]:
    """Create a quiet rehearsal with the host's import roots and a kernel lifetime limit."""
    options = python_subprocess_options()
    environment = _probe_command_environment()
    if environment is not None:
        options.setdefault("env", dict(os.environ))
        # Windows options also carry the resolved launcher identity.
        options["env"]["PYTHONPATH"] = environment["PYTHONPATH"]
    argv = [sys.executable, "-c", "import lancedb, pyarrow"]
    if sys.platform != "win32":
        # The limit covers start-up hooks too: ``.pth`` files and ``sitecustomize`` run before ``-c``, so the child
        # starts without site, restores SIGALRM (an ignored disposition and a blocked mask both survive exec), arms
        # the alarm, and only then sets up site as a normal start would.
        argv = [
            sys.executable,
            "-S",
            "-c",
            "import signal; signal.signal(signal.SIGALRM, signal.SIG_DFL); "
            "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM}); "
            f"signal.alarm({int(_PROBE_TIMEOUT_SECONDS)}); "
            "import site; site.main(); import lancedb, pyarrow",
        ]
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)


def _delay_native_probe() -> None:
    """Start the next cooldown under the state lock, doubling it up to its cap."""
    global _probe_retry_at, _probe_retry_delay
    _probe_retry_at = time.monotonic() + _probe_retry_delay
    _probe_retry_delay = min(_probe_retry_delay * 2, _PROBE_MAX_RETRY_SECONDS)


def _wait_native_probe(
    child: subprocess.Popen[Any], started_at: float, deadline: RequestDeadline | None
) -> tuple[bool | None, str | None]:
    """Return a verdict and log outcome; neither means the caller stopped waiting early."""
    expires_at = started_at + _PROBE_TIMEOUT_SECONDS + _PROBE_WAIT_MARGIN_SECONDS
    remaining = max(0.0, expires_at - time.monotonic())
    if deadline is not None:
        remaining = min(remaining, max(0.0, deadline.remaining()))
    timeout_outcome = f"timed out after {_PROBE_TIMEOUT_SECONDS:g} seconds"
    try:
        status = child.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        if time.monotonic() < expires_at:
            return None, None
        return None, timeout_outcome
    if status < 0 and sys.platform != "win32":
        if -status == getattr(signal, "SIGALRM", 14):
            return None, timeout_outcome
        try:
            name = signal.Signals(-status).name
        except ValueError:
            name = str(-status)
        return False, f"ended by signal {name}"
    return (True, None) if status == 0 else (False, f"exited with status {status}")


def _reap_native_probe(child: subprocess.Popen[Any], deadline: RequestDeadline | None) -> None:
    """End a retired incomplete child, bounding cleanup even if an import replaced SIGALRM's handler."""
    reap_wait = _PROBE_WAIT_MARGIN_SECONDS
    if deadline is not None:
        reap_wait = min(reap_wait, max(0.0, deadline.remaining()))
    try:
        child.kill()
        child.wait(timeout=reap_wait)
    except (OSError, subprocess.TimeoutExpired):
        pass


def native_import_is_safe() -> bool:
    """Share one self-limited child, waiting and acquiring state only within the request budget.

    Calls that stop waiting leave the child for a later caller to observe. Completed verdicts stay cached;
    incomplete attempts cool down with exponential backoff. Process creation itself cannot be interrupted.
    """
    # Launcher-only imports of this module need no Core or its optional runtime dependencies.
    from ..core.deadline import current_request_deadline

    global _native_import_safe, _probe_child, _probe_started_at
    deadline = current_request_deadline()
    lock_timeout = -1.0 if deadline is None else max(0.0, deadline.remaining())
    if not _probe_lock.acquire(timeout=lock_timeout):
        return False
    outcome = None
    try:
        if _native_import_safe is not None:
            return _native_import_safe
        if _probe_child is None:
            if time.monotonic() < _probe_retry_at or (deadline is not None and deadline.exhausted()):
                return False
            try:
                _probe_child = _start_native_probe()
            except (OSError, subprocess.SubprocessError) as exc:
                _delay_native_probe()
                outcome = f"could not start: {type(exc).__name__} (errno={getattr(exc, 'errno', None)})"
            else:
                # The import's budget starts with the child, as its alarm does; resolving roots and spawning do not
                # count against it.
                _probe_started_at = time.monotonic()
        child, started_at = _probe_child, _probe_started_at
    finally:
        _probe_lock.release()
    if outcome is not None:
        _LOG.warning("Native import probe %s", outcome)
        return False
    assert child is not None
    result, outcome = _wait_native_probe(child, started_at, deadline)
    lock_timeout = -1.0 if deadline is None else max(0.0, deadline.remaining())
    if not _probe_lock.acquire(timeout=lock_timeout):
        return False
    published = False
    try:
        if _native_import_safe is None and _probe_child is child and (result is not None or outcome is not None):
            _native_import_safe = result
            _probe_child = None
            published = True
            if result is None:
                _delay_native_probe()
        safe = _native_import_safe is True
    finally:
        _probe_lock.release()
    # Update shared state before killing so another waiter cannot cache cleanup's SIGKILL as an import crash.
    if result is None and outcome is not None:
        _reap_native_probe(child, deadline)
    if published and outcome is not None:
        _LOG.warning("Native import probe %s", outcome)
    return safe


def skip_native_probe() -> None:
    """Trust the in-process import; for an interpreter that is already disposable."""
    global _native_import_safe
    with _probe_lock:
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
