#!/usr/bin/env python3
"""Plan or apply exact cleanup of retired Scope Recall relation work."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _ensure_source_import() -> None:
    here = Path(__file__).resolve()
    root = here.parents[1]
    parent = root.parent
    for candidate in (str(parent), str(root)):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


_ensure_source_import()

from scope_recall.capture_filters import sanitize_report_text  # noqa: E402
from scope_recall.maintenance_ops import (  # noqa: E402
    effective_apply,
    memory_db_path,
)
from scope_recall.relation_cleanup import run_legacy_relation_cleanup  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backup-first exact cleanup for retired relation work"
    )
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME", "~/.hermes"),
        help="Hermes profile home",
    )
    parser.add_argument("--db-path", default="", help="exact memory.sqlite3 path")
    parser.add_argument(
        "--scope-id",
        action="append",
        required=True,
        help="exact scope selector; repeat for multiple scopes",
    )
    parser.add_argument(
        "--queue-status",
        action="append",
        choices=("pending", "retry", "processing", "dead_letter"),
        help="exact unresolved queue status; repeat as needed",
    )
    parser.add_argument("--expected-target-revision", type=int)
    parser.add_argument(
        "--terminal-state",
        choices=("cancelled", "superseded"),
        default="cancelled",
    )
    parser.add_argument("--max-rows", type=int, default=10_000)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument("--expected-plan-sha256", default="")
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    apply = effective_apply(apply=args.apply, dry_run=args.dry_run)
    db_path = memory_db_path(
        Path(args.hermes_home),
        db_path=Path(args.db_path) if args.db_path else None,
    )
    try:
        payload = run_legacy_relation_cleanup(
            db_path,
            apply=apply,
            maintenance_confirmed=bool(args.maintenance_confirmed),
            scope_ids=list(args.scope_id or []),
            queue_statuses=args.queue_status,
            expected_target_revision=args.expected_target_revision,
            terminal_state=args.terminal_state,
            expected_plan_sha256=args.expected_plan_sha256,
            operation_id=args.operation_id,
            reason=args.reason,
            max_rows=args.max_rows,
        )
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "status": "blocked",
                "dry_run": not apply,
                "error": sanitize_report_text(f"{type(exc).__name__}: {exc}"),
            }
        )
        return 2
    _emit(payload)
    return 0 if bool(payload.get("ok")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
