"""Launch one bounded worker process without creating a second queue."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys

from .validation import absolute_path, strict_bool, strict_float, strict_int


def validate_wake_arguments(after_pid: int | None, delay_seconds: float) -> None:
    """The follow-up pid and delay a host may attach to a wake; shared with the watchdog."""
    if after_pid is not None:
        strict_int("worker_after_pid", after_pid, minimum=1, maximum=0xFFFFFFFF)
    strict_float("worker_delay_seconds", delay_seconds, minimum=0, maximum=3600)


def detached_creationflags() -> int:
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if os.name == "nt":
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return flags


def taskkill_tree(process: subprocess.Popen) -> None:
    """Windows: kill the whole Popen-owned tree at once; terminating the parent
    first can orphan native children."""
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


def reap_process(process: subprocess.Popen) -> None:
    """Wait briefly, then force the process (and its POSIX group) down."""
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


@dataclass
class WorkerProcess:
    process: subprocess.Popen[str]
    config_path: Path

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float = 120.0) -> int:
        strict_float("worker_wait_timeout", timeout, minimum=1e-9, maximum=120.0)
        try:
            return self.process.wait(timeout=float(timeout))
        except subprocess.TimeoutExpired:
            self._stop_owned()
            raise

    def communicate(self, timeout: float = 120.0) -> tuple[str, str]:
        strict_float("worker_communicate_timeout", timeout, minimum=1e-9, maximum=120.0)
        try:
            stdout, stderr = self.process.communicate(timeout=float(timeout))
        except subprocess.TimeoutExpired:
            self._stop_owned()
            # Reap pipes after the owned process tree has been stopped.  A
            # short bounded wait keeps a stuck child from becoming a leaked
            # worker while preserving the original timeout signal.
            try:
                self.process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=5.0)
            raise
        return stdout or "", stderr or ""

    def terminate(self) -> None:
        self._stop_owned()

    def _stop_owned(self) -> None:
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            taskkill_tree(self.process)
        else:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                self.process.terminate()
        reap_process(self.process)


def launch_worker(
    config_path: str | Path,
    *,
    python_executable: str | Path | None = None,
    cleanup_config: bool = False,
    after_pid: int | None = None,
    delay_seconds: float = 0.0,
    detach_output: bool = False,
    environment: dict[str, str] | None = None,
) -> WorkerProcess:
    strict_bool("worker_detach_output", detach_output)
    validate_wake_arguments(after_pid, delay_seconds)
    path = absolute_path("config", os.fspath(config_path))
    if not path.is_file():
        raise ValueError("config_not_found")
    path = path.resolve()
    executable = Path(python_executable) if python_executable is not None else Path(sys.executable)
    if not executable.is_absolute() or not executable.exists():
        raise ValueError("python_executable")
    command = [
        str(executable),
        "-m",
        "scope_recall.runtime.worker_watchdog",
        "--config",
        str(path),
        "--python",
        str(executable),
    ]
    if cleanup_config:
        command.append("--cleanup-config")
    if after_pid is not None:
        command.extend(("--after-pid", str(after_pid)))
    if delay_seconds:
        command.extend(("--delay-seconds", str(delay_seconds)))
    # The caller owns PYTHONPATH and other trusted environment configuration;
    # copying it avoids mutating the parent while preserving installed/checked
    # package resolution for the child.
    child_env = os.environ.copy()
    if environment:
        child_env.update(environment)
    package_root = Path(__file__).resolve().parents[1]
    inherited_pythonpath = child_env.get("PYTHONPATH")
    child_env["PYTHONPATH"] = str(package_root) + (
        os.pathsep + inherited_pythonpath if inherited_pythonpath else ""
    )
    process = subprocess.Popen(
        command,
        cwd=str(package_root),
        env=child_env,
        stdout=subprocess.DEVNULL if detach_output else subprocess.PIPE,
        stderr=subprocess.DEVNULL if detach_output else subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=detached_creationflags(),
        start_new_session=(os.name != "nt"),
    )
    return WorkerProcess(process=process, config_path=path)


__all__ = ["WorkerProcess", "launch_worker", "validate_wake_arguments"]
