"""A client's prompt recall server kept apart from the client's own processes (``local_endpoint.ensure_resident``).

WorkBuddy starts the entry's MCP server, and with it the recall server its prompt hooks ask, with each conversation's
agent process and stops it with that process.  A prompt that started one met a server still opening its vector store
and its embedding connection, and was recalled by words alone: a cold server answered with its vector search 12.7 s
after its start (measured 2026-10-03), past the prompt hook's 6 s.  This server is started by the entry's hook or MCP
server when none runs, names itself resident (hooks ask it first), and ends after ``resident_recall_minutes`` without a
prompt's recall.  One runs for each entry and client: a second gives way to the first.  It writes nothing to the store.

    python -I -B -m scope_recall.adapters.codex.resident_entry --home <entry home> --host workbuddy [--env-file <file>]
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from ...core.file_lock import advisory_file_lock
from ...runtime.resume_entry import host_process_credential_environment
from .config import load_shared_client
from .local_endpoint import endpoints, resident_minutes, serve

#: How often the server looks whether it has been idle long enough to end.
IDLE_CHECK_SECONDS = 30.0


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
    idle = args.idle_seconds if args.idle_seconds is not None else resident_minutes(home, args.host) * 60.0
    if idle <= 0:
        return 0
    try:
        with advisory_file_lock(endpoints(home) / f"resident-{args.host}.lock", timeout_seconds=0):
            return _serve_until_idle(home, args.host, args.env_file, idle)
    except TimeoutError:
        return 0  # another resident server of this entry and client runs


def _serve_until_idle(home: Path, host: str, env_file: Path | None, idle: float) -> int:
    config = load_shared_client(home, host)
    credentials = None
    if env_file is not None:
        def credentials() -> dict[str, str]:
            return host_process_credential_environment(config.runtime_config_path, env_file)
    endpoint = serve(home, host, env_file=env_file, runtime_config=config.runtime_config_path,
                     credentials=credentials, warm=True, resident=True)
    if endpoint is None:
        return 1
    stopped = threading.Event()
    try:
        while not stopped.wait(min(IDLE_CHECK_SECONDS, idle)):
            if time.monotonic() - endpoint.last_used >= idle:
                break
    finally:
        endpoint.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
