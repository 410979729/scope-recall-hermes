"""A client's prompt recall server, a process of its own beside the client's (``local_endpoint.ensure_resident``).

WorkBuddy's agent (2.147.0) puts itself and every process it starts in a Windows job that ends them all with it, and
this server cannot leave that job: under WorkBuddy it lives as long as the conversation's agent process that started
it, or less (measured 2026-10-04; docs/install.md, section 12).

WorkBuddy starts the entry's MCP server, and with it the recall server its prompt hooks ask, with each conversation's
agent process and stops it with that process.  A prompt that started one met a server still opening its vector store
and its embedding connection, and was recalled by words alone: a cold server answered with its vector search 12.7 s
after its start (measured 2026-10-03), past the prompt hook's 6 s.  This server is started by the entry's hook or MCP
server when none runs, names itself resident (hooks ask it first), and ends ``resident_recall_minutes`` after the last
prompt's recall or the last mark of a live client process (``local_endpoint.keep_resident``), once the minutes are 0,
once its package on disk is replaced, or once a recall has been stuck for minutes.  One runs for each entry and
client: a second of the same version gives way to the first, and a prompt hook that finds one of another version stops
it, where it can prove and end it, and starts its own (``local_endpoint.ensure_resident``).  It writes nothing to the
store.

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
    _upgrading,
    configured_minutes,
    endpoints,
    resident_alive,
    resident_lock,
    resident_minutes,
    resident_record,
    serve,
    stop_residents,
)

#: How often the server looks whether it should end: idle long enough, its minutes now 0, its package replaced, or a
#: recall stuck too long.
IDLE_CHECK_SECONDS = 30.0
#: Checks in a row whose files could not be read before the server ends: one read caught mid-save, or held for a
#: moment by a scanner, ended a warm server (review 2 of 3.6.0rc1), and files gone for good end it a check later.
UNSURE_CHECKS = 2
#: How long a recall may run past its time before the server ends.  Such a server tells every hook it is busy, and the
#: marks of live client processes kept it up, cold for every prompt, for as long as the client ran (review 2 of
#: 3.6.0rc1); ended, it is started anew by the next look.
STUCK_END_SECONDS = 300.0
#: How long a starting server waits for the entry's lock.  A hook that looks whether one runs holds it for a moment
#: (``local_endpoint.resident_running``); held a quarter of a second, a busy machine's look made a start give way, and
#: the start stamp then kept the entry without a server for a minute (review 2 of 3.6.0rc1).  One of another version
#: this server or a hook stopped holds it until the system has ended that process.
LOCK_WAIT_SECONDS = 3.0
STOPPED_WAIT_SECONDS = 5.0
#: A mark of a live client process further ahead of the clock than this is a clock set back, not a mark; one just made
#: can read a little ahead.
FUTURE_MARK_SECONDS = 60.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scope Recall resident prompt recall server")
    parser.add_argument("--home", type=Path, required=True, help="Absolute home of a client attached to a shared store")
    parser.add_argument("--host", choices=("codex", "claude-code", "workbuddy", "dsh"), required=True)
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
    if idle <= 0 or _upgrading():
        return 0  # none kept, or the package being replaced: a later look starts one from the new files
    # One of another version runs the code it was started with, and hooks ask it nothing (review of 3.6.0rc1).  The
    # prompt hook stops one that holds the lock before it starts this one; one that took the lock meanwhile ends here.
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
    unsure = 0
    try:
        while not stopped.wait(min(IDLE_CHECK_SECONDS, idle)):
            package = _package_state()
            minutes = configured_minutes(home, host, missing=None) if configured else None
            if minutes is not None:
                # A change of the entry's minutes is taken here: set to 0 to free the server's memory, it kept serving,
                # and every prompt put its end off (review of 3.6.0rc1).
                idle = minutes * 60.0
            unsure = unsure + 1 if package == "unknown" or (configured and minutes is None) else 0
            if (idle <= 0 or package == "replaced" or unsure >= UNSURE_CHECKS
                    or endpoint.stuck_for() >= STUCK_END_SECONDS
                    or _idle_seconds(endpoint.last_used, alive) >= idle):
                break
    finally:
        # The record goes last: ``stop`` can wait for a stuck recall, and ``resident stop`` finds the server by it.
        endpoint.stop()
        _forget(record)
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


def _package_state() -> str:
    """``same``; ``replaced`` when the package on disk is no longer the one this server runs (an upgrade replaced it, or
    an uninstall took it away); ``unknown`` when its version could not be read just now.  A server of the old version
    kept the entry's lock against every one of the new version until its idle end, while hooks asked it nothing
    (review of 3.6.0rc1)."""
    from ... import _version

    folder = Path(_version.__file__).resolve().parent
    try:
        (folder / "_version.py").stat()
    except FileNotFoundError:
        return "replaced"
    except OSError:
        return "unknown"
    version = version_on_disk(folder)
    if version is None:
        return "unknown"  # held, or caught being written
    return "same" if version == _version.__version__ else "replaced"


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
