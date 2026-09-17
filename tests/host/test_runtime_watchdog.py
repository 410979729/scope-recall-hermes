"""Real process-tree lifecycle checks for the owned runtime watchdog."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scope_recall.adapters.codex import install_codex_scope_recall
from scope_recall.adapters.runtime_wiring import write_ephemeral_worker_config
from scope_recall.runtime.worker_entry import FINALIZE_MARGIN_SECONDS
from scope_recall.runtime.worker_launch import launch_worker
from scope_recall.runtime.worker_watchdog import KILL_GRACE_SECONDS, _OwnedWindowsJob, _kill_tree
from scope_recall.runtime import worker_watchdog


def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if handle:
        kernel.CloseHandle(handle)
        return True
    return False


def test_owned_watchdog_kills_real_child_tree_after_abrupt_parent_exit(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import pathlib,subprocess,sys,time; "
        "assert sys.stdin.buffer.read(1)==b'\\x01'; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid), encoding='ascii'); "
        "__import__('os')._exit(17)"
    )
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if os.name == "nt":
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    parent = subprocess.Popen(
        [sys.executable, "-c", script, str(pid_file)],
        stdin=subprocess.PIPE,
        creationflags=flags,
        start_new_session=(os.name != "nt"),
    )
    job = _OwnedWindowsJob()
    try:
        assert job.assign(parent)
        assert parent.stdin is not None
        parent.stdin.write(b"\x01")
        parent.stdin.close()
        deadline = time.monotonic() + 5.0
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 5.0
        while parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert parent.poll() == 17
        _kill_tree(parent, job)
        assert parent.poll() is not None
        deadline = time.monotonic() + 5.0
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(child_pid)
    finally:
        _kill_tree(parent, job)


def test_bootstrap_exits_without_running_worker_when_owner_pipe_closes(tmp_path: Path):
    bootstrap = Path(worker_watchdog.__file__).with_name("_worker_bootstrap.py")
    worker = subprocess.Popen(
        [sys.executable, "-I", "-B", str(bootstrap), str(tmp_path / "missing.json")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    stdout, stderr = worker.communicate(input=b"", timeout=5)
    assert worker.returncode == 125
    assert stdout == stderr == b""


def test_bootstrap_with_a_handed_deadline_still_waits_for_the_release_token(tmp_path: Path):
    bootstrap = Path(worker_watchdog.__file__).with_name("_worker_bootstrap.py")
    for extra in ([repr(time.time() + 60)], [repr(time.time() + 60), "TEST-unexpected"]):
        worker = subprocess.Popen(
            [sys.executable, "-I", "-B", str(bootstrap), str(tmp_path / "missing.json"), *extra],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        stdout, stderr = worker.communicate(input=b"", timeout=5)
        assert worker.returncode == 125
        assert stdout == stderr == b""


# The real bootstrap and worker entry, with only the Core drain replaced: "busy"
# uses the whole window it is given, "hung" never returns.
_OWNED_CHILD = """import json, runpy, sys, time
from pathlib import Path
import scope_recall.core.worker as worker

record, mode = Path(sys.argv[1]), sys.argv[2]
sys.argv = sys.argv[3:]


def drain(*args, remaining_seconds, **kwargs):
    now = time.time()
    record.write_text(json.dumps({"handed": float(sys.argv[2]), "drain_ends": now + remaining_seconds}))
    time.sleep(remaining_seconds if mode == "busy" else 600)
    return worker.WorkerReceipt(0, 0, 0, 0, 0, 0, 0, True, ())


worker.drain_worker = drain
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _owned_children(monkeypatch, tmp_path: Path, mode: str) -> tuple[Path, list]:
    wrapper = tmp_path / "TEST-owned-child.py"
    wrapper.write_text(_OWNED_CHILD, encoding="utf-8")
    record = tmp_path / f"TEST-{mode}-drain.json"
    children = []
    real_popen = subprocess.Popen

    def spawn(command, **kwargs):
        if len(command) > 2 and str(command[2]).endswith("_worker_bootstrap.py"):
            command = [*command[:2], str(wrapper), str(record), mode, *command[2:]]
        children.append(real_popen(command, **kwargs))
        return children[-1]

    monkeypatch.setattr(worker_watchdog.subprocess, "Popen", spawn)
    return record, children


def test_busy_child_finishes_inside_the_window_it_was_handed(tmp_path: Path, monkeypatch, capsys):
    """The watchdog's deadline starts before the wait for a predecessor and the
    spawn; the child restarted a full drain budget after its own start-up.  A
    busy child was killed with 124 before its receipt, and the page it had
    reserved for the day was never refunded."""
    config_path, _ = _runtime_payload(tmp_path, drain_seconds=120.0)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    window = 8.0  # The supervisor's, which is tighter than drain_seconds here.
    payload.update(supervisor_seconds=window, supervisor_max_drains=1)
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    data = Path(payload["binding"]["data_directory"])
    predecessor = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(2.5)"],
                                   creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    record, children = _owned_children(monkeypatch, tmp_path, "busy")
    try:
        started = time.time()
        code = worker_watchdog.run(config_path, Path(sys.executable), cleanup_config=False,
                                   after_pid=predecessor.pid)
        finished = time.time()
    finally:
        if predecessor.poll() is None:
            predecessor.kill()
        predecessor.wait(timeout=5)
    assert code == 0
    drained = json.loads(record.read_text(encoding="utf-8"))
    # The supervisor's remaining window, not restarted after the 2.5 s wait.
    # The slack covers the supervisor's own synced state writes before the pass.
    assert drained["handed"] <= started + window + 1.0
    clock_reads = .1  # Epoch/monotonic conversions, a few 15.6 ms Windows ticks.
    assert drained["drain_ends"] <= drained["handed"] - FINALIZE_MARGIN_SECONDS + clock_reads
    assert finished < drained["handed"] + KILL_GRACE_SECONDS  # It exited; nothing was killed.
    receipt = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert receipt["status"] == "idle" and receipt["daily_queue_used"] == 0
    status = json.loads((data / "runtime-worker-status.json").read_text(encoding="utf-8"))
    assert status["exit_code"] == 0 and status["status"] == "idle"
    # The whole page reserved before the drain was refunded after it.
    assert json.loads((data / "runtime-worker-day.json").read_text(encoding="utf-8"))["used"] == 0
    assert children and all(child.poll() is not None for child in children)


def test_hung_child_is_still_killed_with_124_after_the_grace(tmp_path: Path, monkeypatch, capsys):
    drain_seconds = 5.0
    config_path, _ = _runtime_payload(tmp_path, drain_seconds=drain_seconds)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["supervisor_enabled"] = False
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    data = Path(payload["binding"]["data_directory"])
    record, children = _owned_children(monkeypatch, tmp_path, "hung")
    started = time.monotonic()
    assert worker_watchdog.run(config_path, Path(sys.executable), cleanup_config=False) == 124
    assert time.monotonic() - started >= drain_seconds + KILL_GRACE_SECONDS
    assert record.exists()  # It hung inside the drain, not before it.
    assert json.loads(capsys.readouterr().out)["capability_gaps"] == ["worker_watchdog_timeout"]
    status = json.loads((data / "runtime-worker-status.json").read_text(encoding="utf-8"))
    assert status["exit_code"] == 124 and status["capability_gaps"] == ["worker_watchdog_timeout"]
    assert children and all(child.poll() is not None for child in children)
    # A killed pass keeps the page it reserved: a hung worker gets no free retries.
    assert json.loads((data / "runtime-worker-day.json").read_text(encoding="utf-8"))["used"] == 32


def test_assignment_failure_aborts_real_blocked_worker(tmp_path: Path, monkeypatch, capsys):
    config_path, _ = _runtime_payload(tmp_path, drain_seconds=30.0)
    children = []
    real_popen = subprocess.Popen

    def record_child(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    class FailedOwnership:
        def assign(self, _process):
            raise OSError("TEST_job_assignment_failed")

        def close(self):
            pass

    monkeypatch.setattr(worker_watchdog, "_OwnedWindowsJob", FailedOwnership)
    monkeypatch.setattr(worker_watchdog.subprocess, "Popen", record_child)
    assert worker_watchdog.run(config_path, Path(sys.executable), cleanup_config=False) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["capability_gaps"] == ["watchdog_error:OSError"]
    assert config_path.exists()
    assert children and all(child.poll() is not None for child in children)


def test_detached_trailing_wake_survives_short_host_shutdown(tmp_path: Path):
    config_path, _ = _runtime_payload(tmp_path, drain_seconds=5.0)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload['worker_min_interval_seconds'] = 1.0
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    marker = tmp_path / 'host-receipt.json'
    script = '''import json,os,sys,time
from pathlib import Path
from scope_recall.runtime.worker_entry import load_config
from scope_recall.adapters.hermes.runtime_wiring import attach_trusted_host_runtime
path=Path(sys.argv[1]); config=load_config(path)
host=attach_trusted_host_runtime(config_path=path,expected_binding=config.binding,
    session_id=config.session_id,allowed_scope_ids=config.allowed_scope_ids)
host._last_worker_launch=time.monotonic()
host.maybe_launch_bounded_worker(session_id=config.session_id,allowed_scope_ids=config.allowed_scope_ids)
host.maybe_launch_bounded_worker(session_id=config.session_id,allowed_scope_ids=config.allowed_scope_ids)
active,tail=host._owned_worker,host._trailing_worker
Path(sys.argv[2]).write_text(json.dumps({'pids':[active.pid,tail.pid],
    'configs':[str(active.config_path),str(tail.config_path)]}),encoding='utf-8')
host.close()
os._exit(0)
'''
    env = os.environ.copy()
    root = str(Path(worker_watchdog.__file__).resolve().parents[1])
    env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')
    parent = subprocess.Popen([sys.executable, '-B', '-c', script, str(config_path), str(marker)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=int(getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
    out, err = parent.communicate(timeout=5)
    assert parent.returncode == 0, (out, err)
    receipt = json.loads(marker.read_text(encoding='utf-8'))
    deadline = time.monotonic() + 8
    while any(_alive(pid) for pid in receipt['pids']) and time.monotonic() < deadline:
        time.sleep(.02)
    assert not any(_alive(pid) for pid in receipt['pids'])
    assert all(not Path(path).exists() for path in receipt['configs'])
    status = json.loads((Path(payload['binding']['data_directory'])/'runtime-worker-status.json').read_text())
    assert status['exit_code'] == 0 and status['status'] == 'idle'


def _runtime_payload(tmp_path: Path, *, drain_seconds: float) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    config, _core = install_codex_scope_recall(tmp_path / "install", project_root=project, test_mode=True)
    path = tmp_path / "runtime.json"
    binding = config.to_binding()
    path.write_text(
        json.dumps(
            {
                "binding": {
                    "agent_id": binding.agent_id,
                    "installation_id": binding.installation_id,
                    "data_directory": str(binding.data_directory),
                    "scope_ids": sorted(binding.scope_ids),
                    "test_mode": binding.test_mode,
                },
                "session_id": "TEST-watchdog",
                "allowed_scope_ids": sorted(binding.scope_ids),
                "auxiliary": {"external_embedding": False, "external_consolidation": False},
                "drain_seconds": drain_seconds,
            }
        ),
        encoding="utf-8",
    )
    return path, config.config_path


def test_worker_config_cleanup_is_explicit_and_bounded(tmp_path: Path):
    persistent, _installation = _runtime_payload(tmp_path / "persistent", drain_seconds=30.0)
    normal = launch_worker(persistent, python_executable=sys.executable)
    assert normal.wait(timeout=30.0) == 0
    normal.communicate(timeout=5.0)
    assert persistent.exists()

    timed, _installation = _runtime_payload(tmp_path / "timeout", drain_seconds=0.001)
    timeout_worker = launch_worker(timed, python_executable=sys.executable)
    assert timeout_worker.wait(timeout=30.0) == 124
    timeout_worker.communicate(timeout=5.0)
    data_dir = Path(json.loads(timed.read_text(encoding="utf-8"))["binding"]["data_directory"])
    status = json.loads((data_dir / "runtime-worker-status.json").read_text(encoding="utf-8"))
    assert status["exit_code"] == 124
    assert status["capability_gaps"] == ["worker_watchdog_timeout"]
    assert status["worker_pid"] > 0 and status["finished_at"] >= status["started_at"]
    assert timed.exists()

    ephemeral = write_ephemeral_worker_config(
        persistent,
        session_id="TEST-ephemeral",
        allowed_scope_ids=frozenset(json.loads(persistent.read_text(encoding="utf-8"))["allowed_scope_ids"]),
    )
    owned = launch_worker(ephemeral, python_executable=sys.executable, cleanup_config=True)
    assert owned.wait(timeout=30.0) == 0
    owned.communicate(timeout=5.0)
    assert persistent.exists()
    assert not ephemeral.exists()
