"""Owned scratch roots, credential-free evidence and bounded child processes."""

from __future__ import annotations

import ctypes
import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
from uuid import uuid4

MARKER = ".TEST-real-host-owner.json"


def write_json(path, value):
    """Write UTF-8 evidence, creating only parents inside the caller's owned root."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def new_root(scratch):
    """Allocate a fresh marked directory; never adopt an existing directory."""
    scratch = scratch.absolute()
    scratch.mkdir(parents=True, exist_ok=True)
    root = scratch / ("TEST-real-hosts-" + uuid4().hex)
    root.mkdir()
    write_json(root / MARKER, dict(root=str(root.resolve()), token=uuid4().hex))
    return root


def clean_root(root, scratch, token):
    """Enumerate and remove a marked run's files without recursive deletion APIs.

    The token must come from the allocating caller. Links and reparse points
    refuse cleanup, including on Windows where a junction is not a symlink.
    """
    root, scratch = root.absolute(), scratch.resolve()
    if root.parent.resolve() != scratch or not root.name.startswith("TEST-real-hosts-"):
        raise ValueError("cleanup root is not a direct owned scratch child")
    paths = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in (directory, *directory.iterdir()):
            attrs = getattr(path.lstat(), "st_file_attributes", 0)
            if path.is_symlink() or attrs & 0x400:
                raise ValueError("cleanup refuses links and reparse points")
            if path != directory:
                paths.append(path)
                if path.is_dir():
                    pending.append(path)
    receipt = json.loads((root / MARKER).read_text(encoding="utf-8"))
    if receipt != dict(root=str(root.resolve()), token=token):
        raise ValueError("cleanup ownership mismatch")
    print(json.dumps(dict(cleanup_root=str(root), files=[str(p) for p in paths]), ensure_ascii=False), flush=True)
    for path in paths:
        if path.is_file():
            path.unlink()
    for path in sorted((p for p in paths if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        path.rmdir()
    root.rmdir()


def environment(root, *, credential_names=()):
    """Pass OS essentials and explicit credential variables, redirecting every home/cache."""
    names = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "NUMBER_OF_PROCESSORS"}
    env = {k: v for k, v in os.environ.items() if k.upper() in names or k in credential_names}
    for name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "TEMP",
        "TMP",
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
    ):
        target = root / name.lower()
        target.mkdir(parents=True, exist_ok=True)
        env[name] = str(target)
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    return env


#: What a host CLI needs from the operator's own account when it runs with their sign-in: the profile homes it
#: reads its login from, and the proxy settings it reaches its provider through.
HOST_ACCOUNT_NAMES = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def host_account_environment(isolated):
    """The isolated environment with the operator's own profile homes and proxy, for host CLI children only.

    Used when the operator chooses to run the real hosts with their existing sign-in instead of a fresh login in
    the isolated homes. Setup, installation and workers keep the isolated homes, so nothing is installed into the
    operator's hosts; the host sessions themselves are written to the operator's history and must be removed after
    the run.
    """
    env = dict(isolated)
    for name in HOST_ACCOUNT_NAMES:
        env.pop(name, None)
    for name, value in os.environ.items():
        if name.upper() in HOST_ACCOUNT_NAMES or name.upper() in PROXY_NAMES:
            env[name] = value
    return env


def shell_command(argv):
    """Quote a trusted hook invocation for the platform shell used by its host."""
    return subprocess.list2cmdline(list(map(str, argv))) if os.name == "nt" else shlex.join(list(map(str, argv)))


class Children:
    """Contain all descendants of each launched child, including detached workers.

    Windows children start suspended and enter a kill-on-close Job before any
    code executes. POSIX uses a private process group; product workers that
    create sessions are tracked by their explicit watchdog handles instead.
    """

    def __init__(self, env, cwd):
        self.env, self.cwd = env, cwd
        self.jobs, self.processes = {}, []

    def start(self, argv, *, stdin=None, env=None):
        """Start an owned child with captured output; no shell or global config mutation."""
        options = dict(
            cwd=self.cwd,
            env=self.env if env is None else env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if os.name == "nt":
            options["creationflags"] = 0x4 | 0x08000000
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(list(map(str, argv)), **options)
        self.processes.append(process)
        if os.name == "nt":
            self.jobs[process.pid] = windows_job(process)
        return process

    def stop(self, process):
        """Stop a specific owned tree; unrelated processes cannot be passed here."""
        if process not in self.processes:
            raise ValueError("process is not owned by this run")
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            job = self.jobs.pop(process.pid, None)
            if job:
                kernel.CloseHandle(job)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)

    def run(self, argv, evidence, *, timeout=180, input_text=None, env=None):
        """Retain command, exit, elapsed time and redacted output; never retry implicitly."""
        started = time.perf_counter()
        process = self.start(argv, env=env)
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.close()
            stdout, stderr = process.communicate(timeout=10)
            stderr += "\nTEST owned command timeout"
        row = dict(
            argv=list(map(str, argv)),
            exit_code=process.returncode,
            seconds=time.perf_counter() - started,
            stdout=self.redact(stdout),
            stderr=self.redact(stderr),
        )
        write_json(evidence, row)
        return row

    def redact(self, text):
        """Remove explicitly inherited credential values before evidence reaches disk."""
        for name, value in self.env.items():
            if any(part in name.upper() for part in ("TOKEN", "KEY", "SECRET", "PASSWORD")) and value:
                text = text.replace(value, "[REDACTED]")
        return text

    def close(self):
        """Stop only owned descendants and wait for every direct child to exit."""
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            for job in self.jobs.values():
                kernel.CloseHandle(job)
            self.jobs.clear()
        else:
            for process in self.processes:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def windows_job(process):
    """Assign a suspended child to a non-breakaway job, then resume its threads."""
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x2000
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        process.kill()
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(job, int(process._handle)):
        kernel.CloseHandle(job)
        process.kill()
        raise ctypes.WinError(ctypes.get_last_error())
    native = ctypes.WinDLL("ntdll")
    native.NtResumeProcess.argtypes = [wintypes.HANDLE]
    if native.NtResumeProcess(int(process._handle)):
        kernel.CloseHandle(job)
        raise OSError("cannot resume owned child")
    return job
