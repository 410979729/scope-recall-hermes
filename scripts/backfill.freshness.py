#!/usr/bin/env python3
"""Inventory or backfill legacy Scope Recall freshness metadata.

Dry-run is the default and scans the full bounded legacy cohort. Apply requires
maintenance confirmation, a verified online backup, bounded idempotent batches,
and an operator-ledger receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_freshness_backfill_runtime"
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

from scope_recall_freshness_backfill_runtime.capture_filters import sanitize_report_text  # noqa: E402
from scope_recall_freshness_backfill_runtime.freshness import (  # noqa: E402
    backfill_untracked_memory_freshness,
    fact_freshness_report,
    freshness_backfill_inventory,
)
from scope_recall_freshness_backfill_runtime.operator_backup import (  # noqa: E402
    create_verified_sqlite_backup,
)
from scope_recall_freshness_backfill_runtime.operator_ledger import (  # noqa: E402
    mirror_operator_receipt,
    record_committed_operator_operation,
)
from scope_recall_freshness_backfill_runtime.truth_connection import (  # noqa: E402
    connect_truth_database,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory or backfill Scope Recall legacy freshness rows"
    )
    parser.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME", "~/.hermes"),
        help="Hermes profile home containing scope-recall/memory.sqlite3",
    )
    parser.add_argument("--batch-size", type=int, default=500, help="Rows per committed batch (max 5000)")
    parser.add_argument("--max-batches", type=int, default=100, help="Maximum apply batches (max 10000)")
    parser.add_argument("--max-scan", type=int, default=1_000_000, help="Maximum rows inspected by inventory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inventory only (default)")
    mode.add_argument("--apply", action="store_true", help="Backup and apply bounded batches")
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="Confirm normal writers are stopped or controlled",
    )
    parser.add_argument("--operation-id", default="", help="Unique operator ledger id for apply")
    parser.add_argument("--reason", default="", help="Specific reason/evidence for this backfill")
    parser.add_argument("--json", action="store_true", help="Emit JSON (default output format)")
    return parser.parse_args(argv)


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _request_fingerprint(*, batch_size: int, max_batches: int, reason: str) -> str:
    payload = json.dumps(
        {
            "batch_size": batch_size,
            "max_batches": max_batches,
            "reason": reason,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _existing_operation(conn, operation_id: str, request_fingerprint: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT operation_kind, request_fingerprint, result_json
        FROM operator_operations
        WHERE operation_id = ?
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    if str(row["operation_kind"]) != "freshness.backfill":
        raise ValueError("operation id already belongs to a different operation kind")
    if str(row["request_fingerprint"]) != request_fingerprint:
        raise ValueError("operation id already exists with different backfill inputs")
    payload = json.loads(str(row["result_json"] or "{}"))
    if not isinstance(payload, dict):
        raise ValueError("stored freshness backfill result is invalid")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    home = Path(args.hermes_home).expanduser().resolve()
    db_path = home / "scope-recall" / "memory.sqlite3"
    if not db_path.is_file():
        _print({"status": "error", "error": "memory.sqlite3 not found", "path": str(db_path)})
        return 2
    batch_size = max(1, min(int(args.batch_size), 5000))
    max_batches = max(1, min(int(args.max_batches), 10_000))
    max_scan = max(batch_size, min(int(args.max_scan), 5_000_000))
    apply = bool(args.apply)
    if apply and not args.maintenance_confirmed:
        _print({"status": "error", "error": "--apply requires --maintenance-confirmed"})
        return 2
    operation_id = str(args.operation_id or "").strip()
    reason = str(args.reason or "").strip()
    if apply and not operation_id:
        _print({"status": "error", "error": "--apply requires --operation-id"})
        return 2
    if apply and len(reason) < 8:
        _print({"status": "error", "error": "--apply requires a specific --reason"})
        return 2

    backup: dict[str, Any] = {}
    progress: dict[str, int] = {"inserted": 0, "batches": 0}
    conn = connect_truth_database(db_path, mode="rw" if apply else "ro")
    try:
        before = freshness_backfill_inventory(
            conn,
            page_size=batch_size,
            max_rows=max_scan,
        )
        if not apply:
            plan = backfill_untracked_memory_freshness(
                conn,
                apply=False,
                limit=batch_size,
            )
            _print(
                {
                    "status": "dry_run",
                    "inventory": before,
                    "next_batch": plan,
                    "coverage": fact_freshness_report(conn),
                }
            )
            return 0
        if bool(before.get("truncated")):
            raise RuntimeError(
                "freshness inventory exceeded --max-scan; increase the explicit bound before apply"
            )

        request_fingerprint = _request_fingerprint(
            batch_size=batch_size,
            max_batches=max_batches,
            reason=reason,
        )
        existing = _existing_operation(conn, operation_id, request_fingerprint)
        if existing is not None:
            receipt = mirror_operator_receipt(
                conn,
                db_path=db_path,
                operation_id=operation_id,
            )
            _print(
                {
                    **existing,
                    "status": "idempotent_replay",
                    "operation_id": operation_id,
                    "receipt": receipt,
                }
            )
            return 0
        if int(before.get("eligible") or 0) <= 0:
            _print({"status": "ready", "inventory": before, "coverage": fact_freshness_report(conn)})
            return 0

        backup = create_verified_sqlite_backup(
            conn,
            db_path,
            label="freshness-backfill",
        )
        inserted = 0
        batches = 0
        batch_reports: list[dict[str, Any]] = []
        for _ in range(max_batches):
            report = backfill_untracked_memory_freshness(
                conn,
                apply=True,
                limit=batch_size,
            )
            batch_reports.append(report)
            batches += 1
            batch_inserted = int(report.get("inserted") or 0)
            inserted += batch_inserted
            progress = {"inserted": inserted, "batches": batches}
            if batch_inserted == 0 or not bool(report.get("truncated")):
                break

        after = freshness_backfill_inventory(
            conn,
            page_size=batch_size,
            max_rows=max_scan,
        )
        result = {
            "status": "complete" if int(after.get("eligible") or 0) == 0 else "partial",
            "operation_id": operation_id,
            "inserted": inserted,
            "batches": batches,
            "before": before,
            "after": after,
            "coverage": fact_freshness_report(conn),
            "backup": backup,
            "batch_reports": batch_reports[-5:],
        }
        conn.execute("BEGIN IMMEDIATE")
        try:
            operation = record_committed_operator_operation(
                conn,
                operation_id=operation_id,
                operation_kind="freshness.backfill",
                target_ref=str(db_path),
                request_fingerprint=request_fingerprint,
                before=before,
                result=result,
                backup_path=str(backup["path"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        receipt = mirror_operator_receipt(
            conn,
            db_path=db_path,
            operation_id=operation_id,
        )
        _print({**result, "operator_operation": operation, "receipt": receipt})
        return 0 if result["status"] == "complete" else 1
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        _print(
            {
                "status": "error",
                "error": sanitize_report_text(str(exc))[:500],
                "backup": backup,
                "progress": progress,
            }
        )
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
