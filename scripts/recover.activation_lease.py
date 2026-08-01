#!/usr/bin/env python3
"""Inspect or recover a stale Scope Recall activation maintenance lease.

Dry-run is the default. Apply is allowed only when the lease PID probe proves the
owner dead, creates and verifies an online SQLite backup, removes orphan guards,
and records/mirrors an operator receipt.
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
PACKAGE_NAME = "scope_recall_activation_lease_recovery_runtime"
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

from scope_recall_activation_lease_recovery_runtime.capture_filters import sanitize_report_text  # noqa: E402
from scope_recall_activation_lease_recovery_runtime.maintenance_lease import (  # noqa: E402
    activation_lease_status,
    recover_stale_activation_lease,
)
from scope_recall_activation_lease_recovery_runtime.operator_backup import (  # noqa: E402
    create_verified_sqlite_backup,
)
from scope_recall_activation_lease_recovery_runtime.operator_ledger import mirror_operator_receipt  # noqa: E402
from scope_recall_activation_lease_recovery_runtime.truth_connection import connect_truth_database  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect or recover a stale Scope Recall activation lease"
    )
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME", "~/.hermes"),
        help="Hermes profile home containing scope-recall/memory.sqlite3",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect only (default)")
    mode.add_argument("--apply", action="store_true", help="Recover only a definitely stale lease")
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="Confirm no activation is running and operator recovery is authorized",
    )
    parser.add_argument("--operation-id", default="", help="Unique operator ledger id for apply")
    parser.add_argument("--reason", default="", help="Specific evidence/reason for stale recovery")
    parser.add_argument("--json", action="store_true", help="Emit JSON (default output format)")
    return parser.parse_args(argv)


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    home = Path(args.hermes_home).expanduser().resolve()
    db_path = home / "scope-recall" / "memory.sqlite3"
    if not db_path.is_file():
        _print({"status": "error", "error": "memory.sqlite3 not found", "path": str(db_path)})
        return 2
    if not args.apply:
        _print(activation_lease_status(db_path))
        return 0
    if not args.maintenance_confirmed:
        _print({"status": "error", "error": "--apply requires --maintenance-confirmed"})
        return 2
    if not str(args.operation_id or "").strip():
        _print({"status": "error", "error": "--apply requires --operation-id"})
        return 2
    if len(str(args.reason or "").strip()) < 8:
        _print({"status": "error", "error": "--apply requires a specific --reason"})
        return 2

    backup: dict[str, Any] = {}
    try:
        status = activation_lease_status(db_path)
        if not bool(status.get("recoverable")):
            raise RuntimeError("activation maintenance lease is not stale")
        source_conn = connect_truth_database(db_path, mode="ro")
        try:
            backup = create_verified_sqlite_backup(
                source_conn,
                db_path,
                label="activation-lease-recovery",
            )
        finally:
            source_conn.close()
        backup_path = str(backup["path"])
        result = recover_stale_activation_lease(
            db_path,
            apply=True,
            operation_id=args.operation_id,
            reason=args.reason,
            backup_path=backup_path,
        )
        conn = connect_truth_database(db_path, mode="rw")
        try:
            receipt = mirror_operator_receipt(
                conn,
                db_path=db_path,
                operation_id=str(args.operation_id).strip(),
            )
        finally:
            conn.close()
        _print(
            {
                **result,
                "backup": backup,
                "backup_path": backup_path,
                "receipt": receipt,
            }
        )
        return 0
    except Exception as exc:
        _print(
            {
                "status": "error",
                "error": sanitize_report_text(str(exc))[:500],
                "backup": backup,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
