"""Owned process-tree watchdog and optional finite worker supervisor."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .worker_entry import load_config


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
        try:
            if process.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5.0,
                )
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.terminate()
    else:
        # The group belongs exclusively to this worker.  Reap it even after
        # the parent has exited, including descendants that ignore SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
        process.wait(timeout=5.0)


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


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


def _run_once(config_path: Path, python_executable: Path, *, cleanup_config: bool,
        after_pid: int | None = None, delay_seconds: float = 0.0,
        timeout_seconds: float | None = None, result_sink: dict | None = None) -> int:
    from .worker_entry import _now, persist_worker_status
    started_at = _now()
    config = None

    def save(payload, exit_code):
        if result_sink is not None:
            result_sink.update(payload)
        if config is not None:
            try:
                persist_worker_status(config, payload, started_at=started_at,
                                      exit_code=exit_code, worker_pid=child.pid if child is not None else None)
            except (OSError, ValueError):
                pass
    child: subprocess.Popen[str] | None = None
    job: _OwnedWindowsJob | None = None
    tree_stopped = False
    try:
        config = load_config(config_path)
        if after_pid is not None and (type(after_pid) is not int or not 1 <= after_pid <= 0xFFFFFFFF):
            raise ValueError("worker_after_pid")
        if type(delay_seconds) not in (int, float) or not math.isfinite(delay_seconds) or not 0 <= delay_seconds <= 3600:
            raise ValueError("worker_delay_seconds")
        # One detached delayed wake survives host shutdown. It never spawns
        # another wake; its ordinary daily/model budgets remain unchanged.
        if delay_seconds:
            time.sleep(delay_seconds)
        deadline = time.monotonic() + min(float(config.drain_seconds), timeout_seconds if timeout_seconds is not None else float(config.drain_seconds))
        if after_pid is not None and not _wait_for_prior_worker(after_pid, deadline):
            started_at = _now()
            payload = {"status": "degraded", "processed": 0, "items": [],
                       "capability_gaps": ["worker_followup_wait_timeout"]}
            save(payload, 124)
            _emit(payload)
            return 124
        started_at = _now()
        job = _OwnedWindowsJob()
        command = [
            str(python_executable),
            "-B",
            str(Path(__file__).with_name("_worker_bootstrap.py")),
            str(config_path),
        ]
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if os.name == "nt":
            creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        child = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=os.environ.copy(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
        # The stdlib-only bootstrap cannot import or run the worker until
        # ownership is established.  EOF also aborts it if this owner dies.
        if not job.assign(child):
            raise OSError("worker_job_assignment_failed")
        assert child.stdin is not None
        child.stdin.write("\x01")
        child.stdin.flush()
        child.stdin.close()
        child.stdin = None
        while child.poll() is None:
            if time.monotonic() >= deadline:
                _kill_tree(child, job)
                tree_stopped = True
                payload = {
                        "status": "degraded",
                        "owner_id": config.owner_id,
                        "installation_id": config.binding.installation_id,
                        "processed": 0,
                        "items": [],
                        "capability_gaps": ["worker_watchdog_timeout"],
                    }
                save(payload, 124)
                _emit(payload)
                return 124
            time.sleep(0.02)
        # Descendants may have inherited the pipes and outlived their parent.
        # Stop the owned tree before reading EOF, rather than waiting on them.
        _kill_tree(child, job)
        tree_stopped = True
        stdout, stderr = child.communicate(timeout=5.0)
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
        if child.returncode and not stdout.strip():
            save({"status": "degraded", "capability_gaps": ["worker_process_failed"]}, int(child.returncode))
        return int(child.returncode or 0)
    except Exception as exc:
        payload = {
                "status": "degraded",
                "processed": 0,
                "items": [],
                "capability_gaps": [f"watchdog_error:{type(exc).__name__}"],
            }
        save(payload, 1)
        _emit(payload)
        return 1
    finally:
        if child is not None and not tree_stopped:
            _kill_tree(child, job)
        elif job is not None:
            job.close()
        if cleanup_config:
            try:
                config_path.unlink(missing_ok=True)
            except OSError:
                pass


def run(config_path: Path, python_executable: Path, *, cleanup_config: bool,
        after_pid: int | None = None, delay_seconds: float = 0.0) -> int:
    started: set[bool] = set()
    try:
        config = load_config(config_path)
        if not config.supervisor_enabled:
            return _run_once(config_path, python_executable, cleanup_config=False,
                             after_pid=after_pid, delay_seconds=delay_seconds)
        if after_pid is not None and (type(after_pid) is not int or not 1 <= after_pid <= 0xFFFFFFFF):
            raise ValueError('worker_after_pid')
        if type(delay_seconds) not in (int, float) or not math.isfinite(delay_seconds) or not 0 <= delay_seconds <= 3600:
            raise ValueError('worker_delay_seconds')
        from .scheduling import supervise
        predecessor = after_pid
        latest = {}

        def drain_once(remaining):
            nonlocal predecessor
            payload = {}
            # Preserve the process protocol: one final compact JSON receipt,
            # not an unbounded stream or a pipe inherited by sleeping helpers.
            with redirect_stdout(StringIO()):
                code = _run_once(config_path, python_executable, cleanup_config=False,
                                 after_pid=predecessor, timeout_seconds=remaining, result_sink=payload)
            started.add(True)
            predecessor = None
            latest.clear()
            latest.update(payload)
            return code, payload

        code = supervise(config_path, drain_once, delay_seconds=delay_seconds)
        _emit(latest or {'status': 'coalesced', 'processed': 0, 'items': [], 'capability_gaps': []})
        return code
    except Exception as exc:
        # Scheduling reads the bound database before the first pass, so a
        # missing or unreadable one failed here and was reported as an opaque
        # watchdog_error, hiding the worker's own diagnosis. When no pass ever
        # started, run one bounded pass directly: the worker names the real
        # cause. The delay is dropped because the intended wake already passed.
        if not started:
            return _run_once(config_path, python_executable, cleanup_config=False,
                             after_pid=after_pid, delay_seconds=0.0)
        _emit({'status': 'degraded', 'processed': 0, 'items': [],
               'capability_gaps': [f'watchdog_error:{type(exc).__name__}']})
        return 1
    finally:
        if cleanup_config:
            try:
                config_path.unlink(missing_ok=True)
            except OSError:
                pass


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
