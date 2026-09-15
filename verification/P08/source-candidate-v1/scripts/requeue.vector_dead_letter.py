#!/usr/bin/env python3
"""Inspect or audibly requeue vector outbox dead-letter events.

Dry-run is the default. Apply requires an explicit selector, maintenance
confirmation, reason, operation id, an online SQLite backup, and an operator
ledger receipt.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_vector_dead_letter_runtime"
if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load scope-recall package from {PLUGIN_ROOT}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)

from scope_recall_vector_dead_letter_runtime.capture_filters import sanitize_report_text  # noqa: E402
from scope_recall_vector_dead_letter_runtime.operator_backup import (  # noqa: E402
    create_verified_sqlite_backup,
)
from scope_recall_vector_dead_letter_runtime.operator_ledger import mirror_operator_receipt  # noqa: E402
from scope_recall_vector_dead_letter_runtime.truth_connection import connect_truth_database  # noqa: E402
from scope_recall_vector_dead_letter_runtime.vector_dead_letter import (  # noqa: E402
    dead_letter_vector_events_report,
    requeue_dead_letter_vector_events,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect or requeue Scope Recall vector dead-letter events"
    )
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME", "~/.hermes"),
        help="Hermes profile home containing scope-recall/memory.sqlite3",
    )
    parser.add_argument(
        "--event-id",
        action="append",
        type=int,
        default=[],
        help="Exact vector_outbox event id; repeat for multiple events",
    )
    parser.add_argument(
        "--generation-id",
        default="",
        help="Bound selection to one generation (required for generation-wide apply)",
    )
    parser.add_argument("--limit", type=int, default=100, help="Bounded report/requeue limit (max 500)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect only (default)")
    mode.add_argument("--apply", action="store_true", help="Create backup and requeue selected events")
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="Confirm normal writers are controlled and this operator mutation is authorized",
    )
    parser.add_argument("--operation-id", default="", help="Idempotent operator ledger id for apply")
    parser.add_argument("--reason", default="", help="Specific reason the failed dependency is now repaired")
    parser.add_argument("--json", action="store_true", help="Emit JSON (default output format)")
    return parser.parse_args(argv)


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    hermes_home = Path(args.hermes_home).expanduser().resolve()
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.is_file():
        _print({"status": "error", "error": "memory.sqlite3 not found", "path": str(db_path)})
        return 2
    if args.apply and not args.maintenance_confirmed:
        _print({"status": "error", "error": "--apply requires --maintenance-confirmed"})
        return 2
    if args.apply and not (args.event_id or str(args.generation_id).strip()):
        _print({"status": "error", "error": "--apply requires --event-id or --generation-id"})
        return 2

    backup: dict[str, Any] = {}
    conn = connect_truth_database(db_path, mode="rw" if args.apply else "ro")
    try:
        if not args.event_id and not str(args.generation_id).strip():
            payload = dead_letter_vector_events_report(conn, limit=args.limit)
            _print(payload)
            return 1 if int(payload.get("dead_letter") or 0) else 0

        if not args.apply:
            payload = requeue_dead_letter_vector_events(
                conn,
                event_ids=args.event_id,
                generation_id=args.generation_id,
                apply=False,
                limit=args.limit,
            )
            _print(payload)
            return 0

        operation_exists = conn.execute(
            "SELECT 1 FROM operator_operations WHERE operation_id = ?",
            (str(args.operation_id or "").strip(),),
        ).fetchone()
        if operation_exists is None:
            plan = requeue_dead_letter_vector_events(
                conn,
                event_ids=args.event_id,
                generation_id=args.generation_id,
                apply=False,
                limit=args.limit,
            )
            if int(plan.get("planned") or 0) <= 0:
                raise ValueError("no dead-letter vector events matched the apply request")
            backup = create_verified_sqlite_backup(
                conn,
                db_path,
                label="vector-dead-letter-requeue",
            )
            backup_path = str(backup["path"])
        else:
            backup_path = ""

        result = requeue_dead_letter_vector_events(
            conn,
            event_ids=args.event_id,
            generation_id=args.generation_id,
            apply=True,
            operation_id=args.operation_id,
            reason=args.reason,
            backup_path=backup_path,
            limit=args.limit,
        )
        receipt = mirror_operator_receipt(
            conn,
            db_path=db_path,
            operation_id=str(args.operation_id or "").strip(),
        )
        _print({**result, "status": "requeued", "backup": backup, "receipt": receipt})
        return 0
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        _print(
            {
                "status": "error",
                "error": sanitize_report_text(str(exc))[:500],
                "backup": backup,
            }
        )
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
