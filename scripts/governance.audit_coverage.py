#!/usr/bin/env python3
"""Report and backfill governance audit coverage for archived memories."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scope_recall.governance_cleanup import backfill_legacy_archive_audit, governance_audit_coverage_report  # noqa: E402
from scope_recall.maintenance_ops import connect_memory_db, effective_apply, memory_db_path  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report archived-memory governance audit coverage")
    parser.add_argument("--db", default="", help="SQLite memory DB path; defaults to <hermes-home>/scope-recall/memory.sqlite3")
    parser.add_argument("--hermes-home", default="", help="Hermes home/profile path")
    parser.add_argument("--scope-id", action="append", default=[], help="Restrict report/backfill to one scope id; repeatable")
    parser.add_argument("--limit", type=int, default=500, help="Maximum legacy backfill rows per apply")
    parser.add_argument("--sample-limit", type=int, default=8, help="Maximum sample rows in report")
    parser.add_argument("--actor", default="governance.audit_coverage.py", help="Actor recorded in governance audit events")
    parser.add_argument("--batch-id", default="", help="Optional batch id for legacy backfill apply")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview legacy archive audit backfill candidates (default)")
    mode.add_argument("--apply", action="store_true", help="Write legacy_archive_backfill governance audit rows")
    return parser.parse_args()


def db_path(args: argparse.Namespace) -> Path:
    return memory_db_path(Path(args.hermes_home or "~/.hermes"), db_path=args.db or None)


def emit(payload: dict[str, Any], *, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("# scope-recall governance audit coverage")
    print(f"- ok: {payload.get('ok')}")
    if "error" in payload:
        print(f"- error: {payload['error']}")
        return
    print(f"- db: `{payload.get('db_path', '')}`")
    before = payload.get("before", {}) if isinstance(payload.get("before"), dict) else {}
    print(f"- status: {before.get('status')}")
    print(f"- archived_total: {before.get('archived_total', 0)}")
    print(f"- archived_without_audit: {before.get('archived_without_audit', 0)}")
    print(f"- coverage_percent: {before.get('coverage_percent', 0)}")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    if result:
        print(f"- dry_run: {result.get('dry_run')}")
        print(f"- batch_id: `{result.get('batch_id', '')}`")
        print(f"- candidate_count: {result.get('candidate_count', 0)}")
        print(f"- backfilled: {result.get('backfilled', 0)}")
    after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
    if after:
        print(f"- after_status: {after.get('status')}")
        print(f"- after_archived_without_audit: {after.get('archived_without_audit', 0)}")


def main() -> int:
    args = parse_args()
    path = db_path(args)
    payload: dict[str, Any] = {"ok": False, "db_path": str(path)}
    if not path.exists():
        payload["error"] = "database not found"
        emit(payload, fmt=args.format)
        return 2
    should_apply = effective_apply(apply=args.apply, dry_run=args.dry_run)
    conn = connect_memory_db(path, apply=should_apply, timeout=30)
    try:
        payload["before"] = governance_audit_coverage_report(conn, scope_ids=args.scope_id, sample_limit=args.sample_limit)
        payload["result"] = backfill_legacy_archive_audit(
            conn,
            scope_ids=args.scope_id,
            dry_run=not should_apply,
            limit=args.limit,
            batch_id=args.batch_id or None,
            actor=args.actor,
        )
        payload["after"] = governance_audit_coverage_report(conn, scope_ids=args.scope_id, sample_limit=args.sample_limit)
        payload["ok"] = True
        emit(payload, fmt=args.format)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
