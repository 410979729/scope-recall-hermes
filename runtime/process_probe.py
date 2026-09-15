"""Read-only liveness probe for an operating-system process.

Exists so that a diagnostic can ask "is the process that wrote this record
still running?" without ever signalling, waiting on, or otherwise touching
that process.  ``runtime/running_code.py`` is the only caller today.

Two things this module is deliberately careful about:

* **It never signals.**  ``os.kill(pid, 0)`` is the usual POSIX liveness idiom,
  but on Windows CPython routes any signal other than CTRL_C/CTRL_BREAK to
  ``TerminateProcess`` — so the "harmless" probe would kill the gateway.  The
  Windows path therefore opens a query-only handle and reads the exit code.
* **It reports a start token, not just a live/dead bit.**  Process ids are
  reused.  A record that pins the start token of the process that wrote it
  cannot be mistaken for a different, younger process that happens to have
  inherited the same id.

Not responsible for: starting, stopping, or waiting on processes
(``runtime/worker_watchdog.py`` owns that), nor for what the process is doing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Windows ``GetExitCodeProcess`` reports this while the process is running.
_STILL_ACTIVE = 259
#: ``PROCESS_QUERY_LIMITED_INFORMATION``; enough for exit code and start time,
#: and grantable across integrity levels where the full query right is not.
_QUERY_LIMITED = 0x1000
#: ``OpenProcess`` sets this when no process holds the id.
_ERROR_INVALID_PARAMETER = 87


@dataclass(frozen=True)
class ProcessState:
    """What a single probe observed.  ``start_token`` is opaque by design."""

    pid: int
    running: bool
    #: Stable for the life of one process; ``None`` when this platform cannot
    #: supply one, in which case callers fall back to pid-only identity and
    #: must say so rather than implying they checked more than they did.
    start_token: str | None = None


def probe_process(pid: int) -> ProcessState:
    """Report whether ``pid`` is live, without signalling it."""
    if type(pid) is not int or type(pid) is bool or not 1 <= pid <= 0xFFFFFFFF:
        raise ValueError("pid")
    if os.name == "nt":
        return _probe_windows(pid)
    return _probe_posix(pid)


def _probe_windows(pid: int) -> ProcessState:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    handle = kernel.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        # Access denied means a process does hold the id, we simply may not
        # look at it.  Reporting that as "dead" would silently retire a real
        # record, so it counts as running with an unknown start token.
        return ProcessState(pid=pid, running=ctypes.get_last_error() != _ERROR_INVALID_PARAMETER)
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return ProcessState(pid=pid, running=True)
        if code.value != _STILL_ACTIVE:
            return ProcessState(pid=pid, running=False)
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        token = None
        if kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in (created, exited, kernel_time, user_time))):
            token = str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        return ProcessState(pid=pid, running=True, start_token=token)
    finally:
        kernel.CloseHandle(handle)


def _probe_posix(pid: int) -> ProcessState:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessState(pid=pid, running=False)
    except PermissionError:
        # Live, owned by somebody else.  Same reasoning as ACCESS_DENIED above.
        return ProcessState(pid=pid, running=True)
    return ProcessState(pid=pid, running=True, start_token=_posix_start_token(pid))


def _posix_start_token(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat`` — start time in clock ticks."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        # The comm field may itself contain spaces and parentheses, so the
        # fields are counted from the last ')' rather than from the left.
        return raw.rsplit(")", 1)[1].split()[19]
    except (IndexError, ValueError):
        return None


def is_same_process(pid: int, start_token: str | None) -> bool:
    """True when ``pid`` is live and is the same process that produced the token.

    A record written without a token (platform could not supply one) degrades
    to a pid-only check rather than being treated as dead.
    """
    state = probe_process(pid)
    if not state.running:
        return False
    if start_token is None or state.start_token is None:
        return True
    return state.start_token == start_token


__all__ = ["ProcessState", "probe_process", "is_same_process"]
