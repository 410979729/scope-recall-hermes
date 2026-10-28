"""Probe children inherit their host's dependency roots and bound their own import lifetime.

Package-manager hosts inject dependency roots into sys.path at runtime, so the child needs those roots on
PYTHONPATH. Synthetic modules mark their imports to distinguish them from real wheels. Controlled children
exercise request deadlines, shared results, retry cooldowns and inherited process state without real stores.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from scope_recall.core import deadline as request_deadline
from scope_recall.vector import lance_native
from scope_recall.vector.store import LanceVectorStore, build_vector_store

_CHECKOUT = Path(__file__).resolve().parents[2]

_STUB_SOURCE = "from pathlib import Path\nPath(__file__).with_suffix('.imported').write_text('stub')\n"

_DRIVER = """\
import json, sys, types

checkout, stubs = sys.argv[1], sys.argv[2]
package = types.ModuleType("scope_recall")
package.__path__ = [checkout]
sys.modules["scope_recall"] = package
# The PM shape: the environment lands on sys.path at run time, never on PYTHONPATH.
sys.path.insert(0, stubs)
from scope_recall.vector import lance_native

lance_native._native_import_safe = None
print(json.dumps({"rehearsal": lance_native.native_import_is_safe()}))
"""


def _make_stubs(tmp_path):
    stubs = tmp_path / "stubs"
    (stubs / "lancedb").mkdir(parents=True)
    (stubs / "pyarrow").mkdir(parents=True)
    (stubs / "lancedb" / "__init__.py").write_text(_STUB_SOURCE)
    (stubs / "pyarrow" / "__init__.py").write_text(_STUB_SOURCE)
    return stubs


def test_probe_rehearses_with_the_roots_its_host_imports(tmp_path):
    stubs = _make_stubs(tmp_path)
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")}
    completed = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(_CHECKOUT), str(stubs)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    answer = json.loads(completed.stdout.strip().splitlines()[-1])
    assert answer["rehearsal"] is True, "the rehearsal child never saw the host's import roots"
    # The rehearsal must have imported THESE stubs, not a real wheel the interpreter carries.
    assert (stubs / "lancedb" / "__init__.imported").is_file()
    assert (stubs / "pyarrow" / "__init__.imported").is_file()


def test_probe_child_environment_carries_only_the_roots(monkeypatch):
    from scope_recall.vector import lance_native

    root = "/pm/env/site-packages"
    monkeypatch.setattr(lance_native, "helper_import_roots", lambda: [root])
    environment = lance_native._probe_command_environment()
    assert environment is not None
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == root

    # No host roots: the child inherits the environment untouched instead of wiping PYTHONPATH.
    monkeypatch.setattr(lance_native, "helper_import_roots", lambda: [])
    assert lance_native._probe_command_environment() is None

    # An inherited PYTHONPATH keeps its place behind the handed roots.
    monkeypatch.setattr(lance_native, "helper_import_roots", lambda: [root])
    monkeypatch.setenv("PYTHONPATH", "/pre/existing")
    environment = lance_native._probe_command_environment()
    assert environment["PYTHONPATH"].split(os.pathsep) == [root, "/pre/existing"]


@pytest.fixture
def probe(monkeypatch):
    """Isolate the process verdict and use a clock that advances only when asked."""
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(lance_native, "_native_import_safe", None)
    for name, value in (
        ("_probe_retry_at", 0.0),
        ("_probe_child", None),
        ("_probe_started_at", 0.0),
        ("_probe_retry_delay", 30.0),
    ):
        monkeypatch.setattr(lance_native, name, value, raising=False)
    monkeypatch.setattr(lance_native, "_probe_lock", threading.Lock())
    monkeypatch.setattr(lance_native, "time", SimpleNamespace(monotonic=lambda: clock.now), raising=False)
    monkeypatch.setattr(request_deadline, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(lance_native, "_probe_command_environment", lambda: None)
    return clock


def test_timeout_retries_after_backoff_and_restores_vectors(probe, monkeypatch, caplog, tmp_path):
    calls = []

    def popen(command, **options):
        calls.append(options)
        if len(calls) == 1:
            # The retry window starts when the attempt ends, rather than when it began.
            def wait(timeout):
                if calls[-1] != "killed":
                    probe.now += timeout
                    raise subprocess.TimeoutExpired(command, timeout)
                calls.append("reaped")
                return 0

            return SimpleNamespace(wait=wait, kill=lambda: calls.append("killed"))
        return SimpleNamespace(wait=lambda **kwargs: 0)

    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    monkeypatch.setattr(lance_native.importlib, "import_module", lambda name: name)
    store = LanceVectorStore(tmp_path / "TEST-lance", table_name="TEST_vectors", dimensions=2)
    with caplog.at_level(logging.WARNING, logger=lance_native.__name__):
        assert store.is_available() is False
        assert lance_native._native_import_safe is None
        probe.now += lance_native._PROBE_RETRY_SECONDS - 0.001
        assert store.is_available() is False
        assert len(calls) == 3
        probe.now += 0.001
        assert store.is_available() is True
        assert store.is_available() is True
    assert len(calls) == 4
    assert calls[1:3] == ["killed", "reaped"]
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "timed out after 10" in message
    assert message == "Native import probe timed out after 10 seconds"
    assert calls[0]["stdout"] == calls[0]["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    "error", [OSError(11, "PUBLIC cannot start child"), subprocess.SubprocessError("PUBLIC child error")]
)
def test_incomplete_probe_errors_are_logged_and_retried(probe, monkeypatch, caplog, error):
    calls = []

    def popen(command, **options):
        calls.append(command)
        if len(calls) == 1:
            raise error
        return SimpleNamespace(wait=lambda **kwargs: 0)

    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    with caplog.at_level(logging.WARNING, logger=lance_native.__name__):
        assert lance_native.native_import_is_safe() is False
        assert lance_native._native_import_safe is None
        assert lance_native.native_import_is_safe() is False
        assert len(calls) == 1
        probe.now += lance_native._PROBE_RETRY_SECONDS
        assert lance_native.native_import_is_safe() is True
    assert len(calls) == 2
    assert len(caplog.records) == 1
    assert type(error).__name__ in caplog.records[0].getMessage()


@pytest.mark.parametrize("status", [1, -4])
def test_completed_failure_is_cached(probe, monkeypatch, caplog, status):
    calls = []

    def popen(command, **options):
        calls.append(command)
        return SimpleNamespace(wait=lambda **kwargs: status)

    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    with caplog.at_level(logging.WARNING, logger=lance_native.__name__):
        assert lance_native.native_import_is_safe() is False
        probe.now += 10000
        assert lance_native.native_import_is_safe() is False
    assert lance_native._native_import_safe is False
    assert len(calls) == len(caplog.records) == 1


def test_success_is_cached_without_logging(probe, monkeypatch, caplog):
    calls = []

    def popen(command, **options):
        calls.append(command)
        return SimpleNamespace(wait=lambda **kwargs: 0)

    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    with caplog.at_level(logging.DEBUG, logger=lance_native.__name__):
        assert lance_native.native_import_is_safe() is True
        probe.now += 10000
        assert lance_native.native_import_is_safe() is True
    assert len(calls) == 1
    assert not caplog.records


def test_short_deadline_does_not_wait_for_probe_and_later_call_uses_its_answer(probe, monkeypatch, tmp_path):
    finished = threading.Event()
    calls = []

    def wait(timeout):
        if not finished.wait(timeout):
            raise subprocess.TimeoutExpired("TEST child", timeout)
        return 0

    def popen(command, **options):
        calls.append(command)
        return SimpleNamespace(wait=wait)

    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    store = LanceVectorStore(tmp_path / "TEST-lance", table_name="TEST_vectors", dimensions=2)
    monkeypatch.setattr(threading.Thread, "start", lambda self: pytest.fail("probe started a thread"))
    try:
        before = time.perf_counter()
        with request_deadline.using_request_deadline(request_deadline.RequestDeadline.from_budget(0.02)):
            with pytest.raises(RuntimeError, match="lancedb/pyarrow is not installed"):
                store.open_existing()
        assert time.perf_counter() - before < 0.5
        assert lance_native._probe_child is not None
        with request_deadline.using_request_deadline(request_deadline.RequestDeadline.from_budget(0.02)):
            assert lance_native.native_import_is_safe() is False
        assert len(calls) == 1
        lance_native._probe_lock.acquire()
        try:
            before = time.perf_counter()
            with request_deadline.using_request_deadline(request_deadline.RequestDeadline.from_budget(0.02)):
                assert lance_native.native_import_is_safe() is False
            assert time.perf_counter() - before < 0.3
        finally:
            lance_native._probe_lock.release()
    finally:
        finished.set()
    assert lance_native.native_import_is_safe() is True


def test_exhausted_deadline_starts_no_probe(probe, monkeypatch):
    monkeypatch.setattr(lance_native.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("child started"))
    with request_deadline.using_request_deadline(request_deadline.RequestDeadline.from_budget(0)):
        assert lance_native.native_import_is_safe() is False
    assert lance_native._native_import_safe is None


def test_skipping_probe_trusts_disposable_interpreter(probe, monkeypatch):
    monkeypatch.setattr(lance_native.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("child started"))
    lance_native.skip_native_probe()
    assert lance_native.native_import_is_safe() is True


@pytest.mark.parametrize("times_out", [False, True])
def test_skipping_probe_keeps_trust_when_an_inflight_probe_fails(probe, monkeypatch, times_out):
    killed = []

    def wait(timeout=None):
        if killed:
            return -9
        lance_native.skip_native_probe()
        if times_out:
            probe.now += timeout
            raise subprocess.TimeoutExpired("TEST child", timeout)
        return 1

    child = SimpleNamespace(wait=wait, kill=lambda: killed.append(True))
    monkeypatch.setattr(lance_native.subprocess, "Popen", lambda *args, **kwargs: child)
    assert lance_native.native_import_is_safe() is True
    assert bool(killed) is times_out


def test_windows_still_selects_process_store_without_probing(probe, monkeypatch, tmp_path):
    from scope_recall.vector import process_store, store

    sentinel = object()
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(process_store, "store_for", lambda *args, **kwargs: sentinel)
    monkeypatch.setattr(lance_native.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("child started"))
    assert build_vector_store("lancedb", storage_dir=tmp_path, table_name="TEST_vectors", dimensions=2) is sentinel
    assert lance_native._native_import_safe is None


def _child_is_running(pid):
    """Observe only a synthetic child's lifetime, including an unreaped Linux zombie."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            assert ctypes.get_last_error() == 87, "cannot observe the synthetic child"
            return False
        try:
            state = kernel.WaitForSingleObject(handle, 0)
            assert state in (0, 258), "cannot wait for the synthetic child"
            return state == 258
        finally:
            kernel.CloseHandle(handle)
    if not hasattr(os, "pidfd_open"):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    try:
        handle = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    except OSError as exc:
        pytest.skip(f"kernel process-exit observation is unavailable (errno={exc.errno})")
    try:
        poll = select.poll()
        poll.register(handle, select.POLLIN)
        return not poll.poll(0)
    finally:
        os.close(handle)


def _end_test_child(pid):
    """End the child created by this test, including when its assertion fails."""
    if _child_is_running(pid):
        os.kill(pid, signal.SIGTERM)
    until = time.monotonic() + 5
    while _child_is_running(pid) and time.monotonic() < until:
        time.sleep(0.01)
    assert not _child_is_running(pid), "the synthetic child did not exit"


_OBSERVES_CHILDREN = pytest.mark.skipif(
    sys.platform != "win32" and not hasattr(os, "pidfd_open"),
    reason="observing an orphan's exit requires Windows process handles or Linux pidfd_open",
)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows routes vectors to the helper and has no SIGALRM")
@pytest.mark.parametrize("host_case", ["default", "ignored-and-blocked", "hanging-sitecustomize"])
def test_host_exit_ends_its_running_probe_child(tmp_path, host_case):
    assert _child_is_running(os.getpid())
    stubs = _make_stubs(tmp_path)
    marker = stubs / "child.pid"
    # The child records its pid and hangs, in the import or in a start-up hook found on the roots it is handed.
    hang = (
        f"import os, time\nfrom pathlib import Path\nPath({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    hook = stubs / "sitecustomize.py" if host_case == "hanging-sitecustomize" else stubs / "lancedb" / "__init__.py"
    hook.write_text(hang)
    masks_alarm = "1" if host_case == "ignored-and-blocked" else "0"
    driver = (
        _DRIVER.split("lance_native._native_import_safe = None")[0]
        + """
import time
from pathlib import Path
from scope_recall.core.deadline import RequestDeadline, using_request_deadline
lance_native._PROBE_TIMEOUT_SECONDS = 1.0
if sys.argv[3] == "1":
    # A host that ignores and blocks SIGALRM hands both to its children.
    import signal
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
started = time.monotonic()
with using_request_deadline(RequestDeadline.from_budget(0.02)):
    safe = lance_native.native_import_is_safe()
waited = time.monotonic() - started
marker = Path(stubs) / 'child.pid'
until = time.monotonic() + 10
while not marker.exists() and time.monotonic() < until:
    time.sleep(0.01)
print(json.dumps({'safe': safe, 'waited': waited, 'pid': int(marker.read_text())}), flush=True)
import os
os._exit(0)
"""
    )
    child_pid = None
    try:
        host = subprocess.run(
            [sys.executable, "-c", driver, str(_CHECKOUT), str(stubs), masks_alarm],
            capture_output=True,
            text=True,
            timeout=20,
            **lance_native.python_subprocess_options(),
        )
        if marker.exists():
            child_pid = int(marker.read_text())
        assert host.returncode == 0, host.stderr
        answer = json.loads(host.stdout.strip().splitlines()[-1])
        assert answer["pid"] == child_pid
        assert answer["safe"] is False and answer["waited"] < 0.5
        until = time.monotonic() + 3
        while _child_is_running(child_pid) and time.monotonic() < until:
            time.sleep(0.01)
        assert not _child_is_running(child_pid), "the probe outlived its host"
        assert not host.stderr
    finally:
        if child_pid is None and marker.exists():
            child_pid = int(marker.read_text())
        if child_pid is not None:
            _end_test_child(child_pid)


@_OBSERVES_CHILDREN
def test_inherited_stderr_does_not_delay_a_passing_probe(probe, monkeypatch, tmp_path):
    assert _child_is_running(os.getpid())
    stubs = _make_stubs(tmp_path)
    marker = stubs / "grandchild.pid"
    (stubs / "lancedb" / "__init__.py").write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        "child = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', "
        "'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)\n"
        "Path(__file__).parents[1].joinpath('grandchild.pid').write_text(str(child.pid))\n"
    )
    monkeypatch.setattr(lance_native, "_probe_command_environment", lambda: {"PYTHONPATH": str(stubs)})
    monkeypatch.setattr(lance_native, "_PROBE_TIMEOUT_SECONDS", 2.0)
    child_pid = None
    try:
        started = time.perf_counter()
        safe = lance_native.native_import_is_safe()
        if marker.exists():
            child_pid = int(marker.read_text())
        assert safe is True
        assert time.perf_counter() - started < 1.5
        assert child_pid is not None and _child_is_running(child_pid)
    finally:
        if child_pid is None and marker.exists():
            child_pid = int(marker.read_text())
        if child_pid is not None:
            _end_test_child(child_pid)


def test_failed_child_logs_status_without_its_stderr(probe, monkeypatch, caplog, tmp_path):
    stubs = _make_stubs(tmp_path)
    secret = "TEST_token_abcdefghijklmnopqrstuvwxyz"
    (stubs / "lancedb" / "__init__.py").write_text(f"import sys\nsys.stderr.write({secret!r})\nsys.exit(1)\n")
    monkeypatch.setattr(lance_native, "_probe_command_environment", lambda: {"PYTHONPATH": str(stubs)})
    with caplog.at_level(logging.WARNING, logger=lance_native.__name__):
        assert lance_native.native_import_is_safe() is False
    assert [record.getMessage() for record in caplog.records] == ["Native import probe exited with status 1"]
    assert secret not in caplog.text


def test_start_error_logs_type_and_errno_without_exception_text(probe, monkeypatch, caplog):
    private_path = "/TEST-private/placeholder/import"

    def fail(*args, **kwargs):
        raise OSError(13, private_path)

    monkeypatch.setattr(lance_native.subprocess, "run", fail)
    monkeypatch.setattr(lance_native.subprocess, "Popen", fail)
    with caplog.at_level(logging.WARNING, logger=lance_native.__name__):
        assert lance_native.native_import_is_safe() is False
    assert [record.getMessage() for record in caplog.records] == [
        "Native import probe could not start: PermissionError (errno=13)"
    ]
    assert private_path not in caplog.text


def test_negative_posix_status_logs_signal_name(probe, monkeypatch, caplog):
    monkeypatch.setattr(lance_native, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    monkeypatch.setattr(lance_native, "python_subprocess_options", lambda: {})
    monkeypatch.setattr(lance_native.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, -4))
    monkeypatch.setattr(lance_native.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(wait=lambda **kw: -4))
    with caplog.at_level(logging.WARNING, logger=lance_native.__name__):
        assert lance_native.native_import_is_safe() is False
    assert [record.getMessage() for record in caplog.records] == ["Native import probe ended by signal SIGILL"]


def test_alarm_retries_with_doubling_cooldown_until_cap_then_restores_vectors(probe, monkeypatch, caplog, tmp_path):
    calls = []
    delays = [30, 60, 120, 240, 480, 960, 1800, 1800]
    monkeypatch.setattr(lance_native, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    monkeypatch.setattr(lance_native, "python_subprocess_options", lambda: {})
    monkeypatch.setattr(lance_native.importlib, "import_module", lambda name: name)

    def popen(command, **options):
        calls.append(command)
        return SimpleNamespace(wait=lambda **kwargs: -14 if len(calls) <= len(delays) else 0, kill=lambda: None)

    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    store = LanceVectorStore(tmp_path / "TEST-lance", table_name="TEST_vectors", dimensions=2)
    with caplog.at_level(logging.WARNING, logger=lance_native.__name__):
        for count, delay in enumerate(delays, 1):
            assert store.is_available() is False
            assert lance_native._native_import_safe is None
            probe.now += delay - 0.001
            assert store.is_available() is False
            assert len(calls) == count
            probe.now += 0.001
        assert store.is_available() is True
        assert store.is_available() is True
    assert len(calls) == len(delays) + 1
    assert len(caplog.records) == len(delays)
    assert all(record.getMessage() == "Native import probe timed out after 10 seconds" for record in caplog.records)
    assert all("signal.alarm(10)" in command[-1] for command in calls)


@pytest.mark.parametrize("outcome", ["pass", "crash", "start_error", "timeout"])
def test_probe_never_starts_a_thread(probe, monkeypatch, outcome):
    starts, children = [], []

    def forbidden_start(thread):
        starts.append(thread)
        raise RuntimeError("TEST thread start forbidden")

    def wait(timeout):
        if outcome == "timeout":
            probe.now += timeout
            raise subprocess.TimeoutExpired("TEST child", timeout)
        return 0 if outcome == "pass" else 1

    def popen(*args, **kwargs):
        children.append(kwargs)
        if outcome == "start_error":
            raise OSError(11, "TEST child start unavailable")
        return SimpleNamespace(wait=wait, kill=lambda: None)

    monkeypatch.setattr(threading.Thread, "start", forbidden_start)
    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    assert lance_native.native_import_is_safe() is (outcome == "pass")
    assert lance_native.native_import_is_safe() is (outcome == "pass")
    assert not starts
    assert len(children) == 1


def test_waiting_caller_releases_lock_for_another_deadline(probe, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls, answers, waiters = [], [], []
    monkeypatch.setattr(lance_native, "time", time)
    monkeypatch.setattr(request_deadline, "time", time)

    def wait(timeout):
        waiters.append(threading.current_thread())
        started.set()
        if not release.wait(timeout):
            raise subprocess.TimeoutExpired("TEST child", timeout)
        return 0

    def popen(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(wait=wait)

    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    caller = threading.Thread(target=lambda: answers.append(lance_native.native_import_is_safe()))
    caller.start()
    try:
        assert started.wait(1)
        assert waiters[0] is caller, "probe delegated waiting to a background thread"
        assert lance_native._probe_lock.acquire(blocking=False), "waiting caller holds the state lock"
        lance_native._probe_lock.release()
        before = time.monotonic()
        with request_deadline.using_request_deadline(request_deadline.RequestDeadline.from_budget(0.02)):
            assert lance_native.native_import_is_safe() is False
        assert time.monotonic() - before < 0.3
        assert len(calls) == 1
    finally:
        release.set()
        caller.join(5)
        assert not caller.is_alive()
    assert answers == [True]


@pytest.mark.parametrize("verdict", [None, True, False])
def test_fork_hook_replaces_lock_and_forgets_inherited_child(probe, monkeypatch, verdict):
    registrations, launches, inherited_polls = [], [], []

    class InheritedChild:
        _child_created = True

        def poll(self):
            inherited_polls.append("polled")

        def wait(self, **kwargs):
            inherited_polls.append("waited")

        def __del__(self):
            if self._child_created:
                self.poll()

    monkeypatch.setattr(os, "register_at_fork", lambda **kwargs: registrations.append(kwargs), raising=False)
    spec = importlib.util.spec_from_file_location("scope_recall.vector.TEST_probe", lance_native.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(registrations) == 1
    inherited_lock = module._probe_lock
    inherited_lock.acquire()
    module._native_import_safe = verdict
    module._probe_child = InheritedChild()
    module._probe_started_at = 100
    module._probe_retry_at = float("inf")
    module._probe_retry_delay = 1800
    registrations[0]["after_in_child"]()
    assert module._probe_lock is not inherited_lock
    assert module._probe_lock.acquire(blocking=False)
    module._probe_lock.release()
    inherited_lock.release()
    assert module._probe_child is None and module._probe_started_at == 0
    assert module._probe_retry_at == 0 and module._probe_retry_delay == 30
    assert module._native_import_safe is verdict
    assert not inherited_polls

    def popen(*args, **kwargs):
        launches.append(kwargs)
        return SimpleNamespace(wait=lambda **kwargs: 0)

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module, "_probe_command_environment", lambda: None)
    assert module.native_import_is_safe() is (verdict is not False)
    assert len(launches) == (1 if verdict is None else 0)


def test_overdue_child_is_retired_before_kill_and_reap_is_bounded(probe, monkeypatch, caplog):
    waits = []

    def wait(timeout):
        waits.append(timeout)
        probe.now += timeout
        raise subprocess.TimeoutExpired("TEST child", timeout)

    def kill():
        assert lance_native._probe_child is None
        assert lance_native._native_import_safe is None
        assert lance_native.native_import_is_safe() is False

    monkeypatch.setattr(lance_native.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(wait=wait, kill=kill))
    with caplog.at_level(logging.WARNING, logger=lance_native.__name__):
        assert lance_native.native_import_is_safe() is False
        assert lance_native.native_import_is_safe() is False
    assert len(waits) == 2 and 0 < waits[-1] <= 0.2
    assert lance_native._native_import_safe is None
    assert len(caplog.records) == 1


@pytest.mark.parametrize("start_error", [False, True])
def test_outcome_logging_does_not_hold_state_lock(probe, monkeypatch, start_error):
    messages = []

    def popen(*args, **kwargs):
        if start_error:
            raise OSError(11, "TEST child unavailable")
        return SimpleNamespace(wait=lambda **kwargs: 1)

    def warning(*args):
        assert lance_native._probe_lock.acquire(blocking=False), "logging holds the state lock"
        lance_native._probe_lock.release()
        messages.append(args)

    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    monkeypatch.setattr(lance_native._LOG, "warning", warning)
    assert lance_native.native_import_is_safe() is False
    assert lance_native.native_import_is_safe() is False
    assert len(messages) == 1


def test_set_up_time_does_not_shorten_the_import_budget(probe, monkeypatch):
    """The expiry counts from the child's creation: a slow set-up must not cut a passing import short."""

    def popen(command, **options):
        probe.now += 0.5  # resolving the import roots and creating the process

        def wait(timeout):
            # The import passes 9.9 seconds after the child exists.
            if timeout < 9.9:
                probe.now += timeout
                raise subprocess.TimeoutExpired(command, timeout)
            probe.now += 9.9
            return 0

        return SimpleNamespace(wait=wait, kill=lambda: None)

    monkeypatch.setattr(lance_native.subprocess, "Popen", popen)
    assert lance_native.native_import_is_safe() is True


def test_posix_child_arms_a_restored_alarm_before_its_start_up_hooks(monkeypatch):
    """Inherited SIGALRM state must not disarm the child's limit, and site's start-up hooks must run under it."""
    argvs = []
    monkeypatch.setattr(lance_native, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    monkeypatch.setattr(lance_native, "_probe_command_environment", lambda: None)
    monkeypatch.setattr(lance_native.subprocess, "Popen", lambda argv, **options: argvs.append(argv))
    lance_native._start_native_probe()
    argv = argvs[0]
    assert argv[1:3] == ["-S", "-c"], "site would run its start-up hooks before the alarm"
    command = argv[3]
    compile(command, "<probe>", "exec")
    order = [
        command.index(part) for part in ("SIG_DFL", "SIG_UNBLOCK", "signal.alarm(", "site.main()", "import lancedb")
    ]
    assert order == sorted(order)


def test_site_set_up_after_the_alarm_gives_the_import_path_of_a_normal_start(tmp_path):
    """Setting up site inside the command must leave the rehearsal the module search path a normal start has."""
    show = "import json, sys; print(json.dumps(sys.path))"
    roots, cwd = tmp_path / "roots", tmp_path / "cwd"
    roots.mkdir()
    cwd.mkdir()
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONPATH"] = str(roots)
    paths = []
    for argv in ([sys.executable, "-c", show], [sys.executable, "-S", "-c", "import site; site.main(); " + show]):
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env, cwd=cwd)
        assert completed.returncode == 0, completed.stderr
        found = json.loads(completed.stdout)
        # site makes the empty current-directory entry absolute when it is already present; both name the child's cwd.
        paths.append([os.path.normcase(os.path.abspath(os.path.join(cwd, path))) for path in found])
    assert paths[0] == paths[1]
