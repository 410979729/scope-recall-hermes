"""Owned process-tree watchdog and optional finite worker supervisor."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .validation import utc_now
from .worker_entry import load_config, persist_worker_status
from .worker_launch import detached_creationflags, reap_process, taskkill_tree, validate_wake_arguments

#: Seconds an owned child may outlive the deadline it was handed.  A child that
#: honours that deadline has already written its receipt and exited; one still
#: running after the grace is hung, and its tree is killed.
KILL_GRACE_SECONDS = 5.0


class _OwnedWindowsJob:
    """Kill-on-close Job Object for one watchdog-owned process tree."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Io(ctypes.Structure):
            _fields_ = [("values", ctypes.c_ulonglong * 6)]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        # ctypes defaults to a C int result, which truncates HANDLE on Win64.
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        ok = kernel.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            raise ctypes.WinError(error)
        self.handle = handle
        self._kernel = kernel

    def assign(self, process: subprocess.Popen[str]) -> bool:
        if os.name != "nt":
            return True  # Popen created a dedicated Unix session below.
        if self.handle is None:
            raise OSError("worker_job_not_open")
        import ctypes

        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise OSError("worker_process_handle_unavailable")
        if not self._kernel.AssignProcessToJobObject(self.handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        return True

    def close(self) -> None:
        if self.handle is not None:
            self._kernel.CloseHandle(self.handle)
            self.handle = None


def _kill_tree(process: subprocess.Popen[str], job: _OwnedWindowsJob | None = None) -> None:
    if job is not None:
        job.close()
    if os.name == "nt":
        taskkill_tree(process)
    else:
        # The group belongs exclusively to this worker.  Reap it even after
        # the parent has exited, including descendants that ignore SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    reap_process(process)


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _degraded(gap: str) -> dict[str, object]:
    return {"status": "degraded", "processed": 0, "items": [], "capability_gaps": [gap]}


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _wait_for_prior_worker(pid: int, deadline: float) -> bool:
    """Wait only; never signal the prior watchdog or acquire its ownership."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only.
        if not handle:
            if ctypes.get_last_error() == 87:  # Already exited.
                return True
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result = kernel.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000)))
            if result not in (0, 258):
                raise OSError("worker_predecessor_wait_failed")
            return result == 0
        finally:
            kernel.CloseHandle(handle)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # A parent may not yet have reaped its finished watchdog.
        proc_stat = Path(f"/proc/{pid}/stat")
        try:
            if proc_stat.exists() and proc_stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except (OSError, IndexError):
            pass
        time.sleep(min(.02, max(0, deadline - time.monotonic())))
    return False


def _spawn_worker(config_path: Path, python_executable: Path, deadline: float) -> subprocess.Popen[str]:
    # A monotonic reading means nothing in another process: hand the child
    # the same instant in epoch seconds, as an argument only it receives.
    deadline_epoch = time.time() + (deadline - time.monotonic())
    command = [
        str(python_executable),
        "-B",
        str(Path(__file__).with_name("_worker_bootstrap.py")),
        str(config_path),
        repr(deadline_epoch),
    ]
    return subprocess.Popen(
        command,
        cwd=str(Path(__file__).resolve().parents[1]),
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=detached_creationflags(),
        start_new_session=(os.name != "nt"),
    )


def _release_worker(child: subprocess.Popen[str]) -> None:
    """The stdlib-only bootstrap cannot import or run the worker until
    ownership is established.  EOF also aborts it if this owner dies."""
    assert child.stdin is not None
    child.stdin.write("\x01")
    child.stdin.flush()
    child.stdin.close()
    child.stdin = None


def _wait_for_exit(child: subprocess.Popen[str], deadline: float) -> bool:
    while child.poll() is None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _relay_output(stdout: str, stderr: str, result_sink: dict | None) -> None:
    if result_sink is not None and stdout.strip():
        try:
            value = json.loads(stdout.strip().splitlines()[-1])
            if isinstance(value, dict):
                result_sink.update(value)
        except (ValueError, IndexError):
            pass
    if stdout.strip():
        sys.stdout.write(stdout)
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()


def _run_once(config_path: Path, python_executable: Path, *, cleanup_config: bool,
        after_pid: int | None = None, delay_seconds: float = 0.0,
        timeout_seconds: float | None = None, result_sink: dict | None = None) -> int:
    started_at = utc_now()
    config = None
    child: subprocess.Popen[str] | None = None
    job: _OwnedWindowsJob | None = None
    tree_stopped = False

    def save(payload, exit_code) -> None:
        if result_sink is not None:
            result_sink.update(payload)
        if config is not None:
            try:
                persist_worker_status(config, payload, started_at=started_at,
                                      exit_code=exit_code, worker_pid=child.pid if child is not None else None)
            except (OSError, ValueError):
                pass

    def report(payload, exit_code) -> int:
        save(payload, exit_code)
        _emit(payload)
        return exit_code

    try:
        config = load_config(config_path)
        validate_wake_arguments(after_pid, delay_seconds)
        # One detached delayed wake survives host shutdown. It never spawns
        # another wake; its ordinary daily/model budgets remain unchanged.
        if delay_seconds:
            time.sleep(delay_seconds)
        budget = float(config.drain_seconds)
        if timeout_seconds is not None:
            budget = min(budget, timeout_seconds)
        # One deadline bounds the whole pass, a wait for the predecessor
        # included.  The child is handed it and finishes inside it.
        deadline = time.monotonic() + budget
        if after_pid is not None and not _wait_for_prior_worker(after_pid, deadline):
            started_at = utc_now()
            return report(_degraded("worker_followup_wait_timeout"), 124)
        started_at = utc_now()
        job = _OwnedWindowsJob()
        child = _spawn_worker(config_path, python_executable, deadline)
        if not job.assign(child):
            raise OSError("worker_job_assignment_failed")
        _release_worker(child)
        if not _wait_for_exit(child, deadline + KILL_GRACE_SECONDS):
            _kill_tree(child, job)
            tree_stopped = True
            payload = {**_degraded("worker_watchdog_timeout"), "owner_id": config.owner_id,
                       "installation_id": config.binding.installation_id}
            return report(payload, 124)
        # Descendants may have inherited the pipes and outlived their parent.
        # Stop the owned tree before reading EOF, rather than waiting on them.
        _kill_tree(child, job)
        tree_stopped = True
        stdout, stderr = child.communicate(timeout=5.0)
        _relay_output(stdout, stderr, result_sink)
        if child.returncode and not stdout.strip():
            save({"status": "degraded", "capability_gaps": ["worker_process_failed"]}, int(child.returncode))
        return int(child.returncode or 0)
    except Exception as exc:
        return report(_degraded(f"watchdog_error:{type(exc).__name__}"), 1)
    finally:
        if child is not None and not tree_stopped:
            _kill_tree(child, job)
        elif job is not None:
            job.close()
        if cleanup_config:
            _unlink_quietly(config_path)


def run(config_path: Path, python_executable: Path, *, cleanup_config: bool,
        after_pid: int | None = None, delay_seconds: float = 0.0) -> int:
    passes = 0
    try:
        config = load_config(config_path)
        if not config.supervisor_enabled:
            return _run_once(config_path, python_executable, cleanup_config=False,
                             after_pid=after_pid, delay_seconds=delay_seconds)
        validate_wake_arguments(after_pid, delay_seconds)
        from .scheduling import supervise
        predecessor = after_pid
        latest = {}

        def drain_once(remaining):
            nonlocal predecessor, passes
            payload = {}
            # Preserve the process protocol: one final compact JSON receipt,
            # not an unbounded stream or a pipe inherited by sleeping helpers.
            with redirect_stdout(StringIO()):
                code = _run_once(config_path, python_executable, cleanup_config=False,
                                 after_pid=predecessor, timeout_seconds=remaining, result_sink=payload)
            passes += 1
            predecessor = None
            latest.clear()
            latest.update(payload)
            return code, payload

        code = supervise(config_path, drain_once, delay_seconds=delay_seconds)
        _emit(latest or {'status': 'coalesced', 'processed': 0, 'items': [], 'capability_gaps': []})
        return code
    except Exception as exc:
        # Scheduling reads the bound database before the first pass, so a
        # missing or unreadable one would surface as an opaque watchdog_error.
        # When no pass ever started, run one bounded pass directly: the worker
        # names the real cause.  The delay is dropped; the wake already passed.
        if not passes:
            return _run_once(config_path, python_executable, cleanup_config=False,
                             after_pid=after_pid, delay_seconds=0.0)
        _emit(_degraded(f'watchdog_error:{type(exc).__name__}'))
        return 1
    finally:
        if cleanup_config:
            _unlink_quietly(config_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scope_recall.runtime.worker_watchdog")
    parser.add_argument("--config", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--cleanup-config", action="store_true")
    parser.add_argument("--after-pid", type=int)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)
    config_raw = Path(args.config).expanduser()
    python_raw = Path(args.python).expanduser()
    if not config_raw.is_absolute() or not python_raw.is_absolute():
        raise SystemExit("absolute paths required")
    config_path = config_raw.resolve()
    python_executable = python_raw.resolve()
    if os.name != "nt":
        # WorkerProcess.terminate() signals the watchdog's process group.
        # Run the finally block to reap the child's separate process group.
        def terminate_owned(_signum: int, _frame: object) -> None:
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, terminate_owned)
    return run(config_path, python_executable, cleanup_config=bool(args.cleanup_config),
               after_pid=args.after_pid, delay_seconds=args.delay_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
