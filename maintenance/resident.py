"""Inspect or stop the worker-owned recall service shared by local entries of a library.

The existing --home and --host arguments resolve an attached entry to its library. Status includes the service lock
and stable process identity; stop never signals an unproven process. The worker may restart the service while client
activity remains recent. Setting residence to zero disables that restart.
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
    from ..adapters.clients.local_endpoint import live_residents, resident_minutes, resident_running, stop_residents

    stopped = stop_residents(home, args.host) if args.action == "stop" else []
    # A stopped process lets go of the entry's lock as it ends: what is said after, and an upgrade after that, waits
    # for it a moment.
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while stopped and resident_running(home, args.host) and time.monotonic() < deadline:
        time.sleep(0.1)
    servers = [
        {"pid": int(info["pid"]), "version": info.get("version"), "verified": proven}
        for _paths, info, proven in live_residents(home, args.host, any_version=True)
    ]
    running = resident_running(home, args.host)
    said = {
        "status": "ok",
        "action": args.action,
        "resident_recall_minutes": resident_minutes(home, args.host),
        "running": running,
        "servers": servers,
    }
    if args.action == "stop":
        said["stopped"] = stopped
        if running:
            # A server with no record and no name (in its first moments), or one that is not proven, was not stopped.
            said["status"] = "still_running"
    print(json.dumps(said, indent=2))
    return 1 if said["status"] == "still_running" else 0


if __name__ == "__main__":
    raise SystemExit(main())
