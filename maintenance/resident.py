"""See or stop an entry's resident prompt recall server (``adapters/codex/resident_entry``).

    scope-recall resident status --home <entry home> --host workbuddy
    scope-recall resident stop   --home <entry home> --host workbuddy

A resident server runs from the entry's package: stop it before a ``package-upgrade`` of that package, as the client
itself.  It writes nothing, so stopping it loses nothing; the next prompt starts one again when the client keeps one.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scope-recall resident")
    parser.add_argument("action", choices=("status", "stop"))
    parser.add_argument("--home", required=True, help="absolute home of a client attached to a shared store")
    parser.add_argument("--host", required=True, choices=("codex", "claude-code", "workbuddy"))
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser()
    if not home.is_absolute():
        print(json.dumps({"status": "error", "code": "home_not_absolute"}))
        return 2
    from ..adapters.codex.local_endpoint import _residents, resident_minutes, stop_residents

    if args.action == "stop":
        stopped = stop_residents(home, args.host)
        print(json.dumps({"status": "ok", "action": "stop", "stopped": stopped}, indent=2))
        return 0
    running = [{"pid": int(info["pid"]), "version": info.get("version")}
               for _path, info in _residents(home, args.host, any_version=True)]
    print(json.dumps({"status": "ok", "action": "status", "resident_recall_minutes": resident_minutes(home, args.host),
                      "running": running}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
