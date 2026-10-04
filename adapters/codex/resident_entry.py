"""A client's prompt recall server kept apart from the client's own processes (``local_endpoint.ensure_resident``).

WorkBuddy starts the entry's MCP server, and with it the recall server its prompt hooks ask, with each conversation's
agent process and stops it with that process.  A prompt that started one met a server still opening its vector store
and its embedding connection, and was recalled by words alone: a cold server answered with its vector search 12.7 s
after its start (measured 2026-10-03), past the prompt hook's 6 s.  This server is started by the entry's hook or MCP
server when none runs, names itself resident (hooks ask it first), and ends ``resident_recall_minutes`` after the last
prompt's recall or the last mark of a live client process (``local_endpoint.keep_resident``), once the minutes are 0,
or once its package on disk is replaced.  One runs for each entry and client: a second of the same version gives way
to the first, and one of another version is stopped by the one starting.  It writes nothing to the store.

    python -I -B -m scope_recall.adapters.codex.resident_entry --home <entry home> --host workbuddy [--env-file <file>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from ...core.file_lock import advisory_file_lock
from ...runtime.resume_entry import host_process_credential_environment
from ...runtime.running_code import version_on_disk
from .config import load_shared_client
from .local_endpoint import (
    _forget,
    endpoints,
    resident_alive,
    resident_lock,
    resident_minutes,
    resident_record,
    serve,
    stop_residents,
)

#: How often the server looks whether it should end: idle long enough, its minutes now 0, or its package replaced.
IDLE_CHECK_SECONDS = 30.0
#: How long a starting server waits for the entry's lock: a hook that looks whether one runs holds it for a moment
#: (``local_endpoint.resident_running``).  One of another version this server stopped holds it until it is gone.
LOCK_WAIT_SECONDS = 0.25
STOPPED_WAIT_SECONDS = 5.0
#: A mark of a live client process further ahead of the clock than this is a clock set back, not a mark; one just made
#: can read a little ahead.
FUTURE_MARK_SECONDS = 60.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scope Recall resident prompt recall server")
    parser.add_argument("--home", type=Path, required=True, help="Absolute home of a client attached to a shared store")
    parser.add_argument("--host", choices=("codex", "claude-code", "workbuddy"), required=True)
    parser.add_argument("--env-file", type=Path, default=None,
                        help="Absolute file holding the credential names the runtime config declares")
    # For tests: an idle end in seconds instead of the configured minutes.
    parser.add_argument("--idle-seconds", type=float, default=None, help=argparse.SUPPRESS)
    # Start the server from this process and end at once (``local_endpoint.ensure_resident``).
    parser.add_argument("--detach", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    home = args.home.expanduser()
    if not home.is_absolute() or (args.env_file is not None and not args.env_file.is_absolute()):
        return 2
    if args.detach:
        # Started by the client's MCP server, which lives as long as the conversation, the server was its child:
        # WorkBuddy ending the conversation's process tree ended it too (measured 2026-10-03, its tree killed as
        # ``taskkill /T`` does).  Started from this process, which ends now, it has no living parent in that tree.
        from .local_endpoint import _start_apart

        command = [sys.executable, "-I", "-B", "-m", "scope_recall.adapters.codex.resident_entry",
                   "--home", str(home), "--host", args.host]
        if args.env_file is not None:
            command += ["--env-file", str(args.env_file)]
        return 0 if _start_apart(command, cwd=endpoints(home)) else 1
    configured = args.idle_seconds is None
    idle = resident_minutes(home, args.host) * 60.0 if configured else args.idle_seconds
    if idle <= 0:
        return 0
    # One of another version runs the code it was started with, and hooks ask it nothing; it held the lock against
    # every server of this version until its idle end (review of 3.6.0rc1).
    stopped = stop_residents(home, args.host, other_versions=True)
    try:
        with advisory_file_lock(resident_lock(home, args.host),
                                timeout_seconds=STOPPED_WAIT_SECONDS if stopped else LOCK_WAIT_SECONDS):
            return _serve_until_idle(home, args.host, args.env_file, idle, configured=configured)
    except TimeoutError:
        return 0  # another resident server of this entry and client runs


def _serve_until_idle(home: Path, host: str, env_file: Path | None, idle: float, *, configured: bool) -> int:
    config = load_shared_client(home, host)
    credentials = None
    if env_file is not None:
        def credentials() -> dict[str, str]:
            return host_process_credential_environment(config.runtime_config_path, env_file)
    endpoint = serve(home, host, env_file=env_file, runtime_config=config.runtime_config_path,
                     credentials=credentials, warm=True, resident=True)
    if endpoint is None:
        return 1
    record = resident_record(home, host)
    _keep_record(record, host)
    alive = resident_alive(home, host)
    stopped = threading.Event()
    try:
        while not stopped.wait(min(IDLE_CHECK_SECONDS, idle)):
            if configured:
                # A change of the entry's minutes is taken here: set to 0 to free the server's memory, it kept serving,
                # and every prompt put its end off (review of 3.6.0rc1).
                idle = resident_minutes(home, host) * 60.0
            if idle <= 0 or _package_replaced() or _idle_seconds(endpoint.last_used, alive) >= idle:
                break
    finally:
        _forget(record)
        endpoint.stop()
    return 0


def _keep_record(path: Path, host: str) -> None:
    """This server's process id, start and version beside the lock it holds (``local_endpoint.resident_record``)."""
    from ..._version import __version__
    from ...runtime.process_probe import probe_process

    record = {"host": host, "pid": os.getpid(), "start": probe_process(os.getpid()).start_token,
              "version": __version__}
    pending = path.with_name(path.name + ".tmp")
    try:
        pending.write_text(json.dumps(record), encoding="utf-8")
        os.replace(pending, path)
    except OSError:
        pass  # its name says the same, until a hook removes that


def _package_replaced() -> bool:
    """Whether the package on disk is no longer the one this server runs: an upgrade replaced it, or an uninstall took
    it away.  A server of the old version kept the entry's lock against every one of the new version until its idle
    end, while hooks asked it nothing (review of 3.6.0rc1)."""
    from ... import _version

    return version_on_disk(Path(_version.__file__).resolve().parent) != _version.__version__


def _idle_seconds(last_used: float, alive: Path) -> float:
    """Seconds since the last prompt's recall (``last_used``, by the monotonic clock) or the last mark of a live client
    process (``alive``), whichever came later.  A mark from the future, a clock set back, counts as none: it kept the
    server up until the clock passed it."""
    idle = time.monotonic() - last_used
    try:
        age = time.time() - alive.stat().st_mtime
    except OSError:
        return idle
    return min(idle, max(age, 0.0)) if age > -FUTURE_MARK_SECONDS else idle


if __name__ == "__main__":
    raise SystemExit(main())
