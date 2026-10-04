"""See or stop an entry's resident prompt recall server (``adapters/codex/resident_entry``).

    scope-recall resident status --home <entry home> --host workbuddy
    scope-recall resident stop   --home <entry home> --host workbuddy

A resident server runs from the entry's package: stop it before a ``package-upgrade`` of that package, after the client
itself (a live MCP server of the client starts one again at its next look, every ``RESIDENT_KEEP_SECONDS``, once the
last start is a minute old).  It writes nothing, so stopping it loses nothing; the next prompt starts one again when
the client keeps one.  ``running`` says whether one holds the entry's lock; ``servers`` lists those whose process is
known.  One whose identity cannot be proven, where a process's start time cannot be read (macOS), is listed
``verified: false`` and never stopped: it ends itself within ``IDLE_CHECK_SECONDS`` once its package is replaced or its
minutes are 0 (``adapters/codex/resident_entry``).  A ``stop`` after which one still holds the lock says
``still_running`` and exits 1, so that an upgrade script does not go on.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

#: How long ``stop`` waits for a stopped server to let go of the entry's lock.
STOP_WAIT_SECONDS = 5.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scope-recall resident")
    parser.add_argument("action", choices=("status", "stop"))
    parser.add_argument("--home", required=True, help="absolute home of a client attached to a shared store")
    parser.add_argument("--host", required=True, choices=("codex", "claude-code", "workbuddy", "dsh"))
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser()
    if not home.is_absolute():
        print(json.dumps({"status": "error", "code": "home_not_absolute"}))
        return 2
    from ..adapters.codex.local_endpoint import _residents, resident_minutes, resident_running, stop_residents

    stopped = stop_residents(home, args.host) if args.action == "stop" else []
    # A stopped process lets go of the entry's lock as it ends: what is said after, and an upgrade after that, waits
    # for it a moment.
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while stopped and resident_running(home, args.host) and time.monotonic() < deadline:
        time.sleep(0.1)
    servers = [{"pid": int(info["pid"]), "version": info.get("version"), "verified": proven}
               for _paths, info, proven in _residents(home, args.host, any_version=True)]
    running = resident_running(home, args.host)
    said = {"status": "ok", "action": args.action, "resident_recall_minutes": resident_minutes(home, args.host),
            "running": running, "servers": servers}
    if args.action == "stop":
        said["stopped"] = stopped
        if running:
            # A server with no record and no name (in its first moments), or one that is not proven, was not stopped
            # (review 2 of 3.6.0rc1).
            said["status"] = "still_running"
    print(json.dumps(said, indent=2))
    return 1 if said["status"] == "still_running" else 0


if __name__ == "__main__":
    raise SystemExit(main())
