"""`scope-recall replay-sessions`: preview, then replay, the host sessions an entry missed while its hooks were off."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scope_recall.contracts import ContractError


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    """Without --yes, list what would be replayed and its preview id; with --yes and that id, replay it."""
    from ..adapters.clients.replay_sessions import replay

    parser = argparse.ArgumentParser(prog="scope-recall replay-sessions")
    parser.add_argument("--host", choices=("claude-code", "codex", "workbuddy"), required=True)
    parser.add_argument("--instance-root", required=True)
    parser.add_argument(
        "--records",
        required=True,
        help="host session records directory declared by the operator as belonging to this entry",
    )
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--preview-id")
    args = parser.parse_args(argv)
    if args.yes != bool(args.preview_id):
        _emit({"status": "error", "error": "--yes and --preview-id must be used together"})
        return 2
    try:
        result = replay(
            args.host, Path(args.instance_root), Path(args.records), args.since, args.until, preview_id=args.preview_id
        )
    except (ContractError, OSError, RuntimeError, ValueError, KeyError) as exc:
        error = str(exc)
        message = "Records changed; run the preview again." if error == "records_changed_repreview" else error
        _emit({"status": "error", "error": error, "message": message})
        return 2
    _emit(result)
    return 0
