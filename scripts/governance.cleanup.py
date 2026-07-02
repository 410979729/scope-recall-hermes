#!/usr/bin/env python3
"""Soft-archive historical scope-recall template/transcript memory pollution.

This script is intentionally narrow: it targets historical digest/template noise,
soft-archives by default, writes governance audit events on apply, and supports
rollback by batch id. SQLite remains the truth layer; vectors do not need hard
mutation because archived rows are filtered from recall.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scope_recall.governance_cleanup import active_dirty_counts, apply_cleanup, rollback_cleanup_batch  # noqa: E402
from scope_recall.maintenance_ops import connect_memory_db, effective_apply, memory_db_path  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Govern historical scope-recall template/transcript noise")
    parser.add_argument("--db", default="", help="SQLite memory DB path; defaults to <hermes-home>/scope-recall/memory.sqlite3")
    parser.add_argument("--hermes-home", default="", help="Hermes home/profile path")
    parser.add_argument("--scope-id", action="append", default=[], help="Restrict cleanup to one scope id; repeatable")
    parser.add_argument("--limit", type=int, default=500, help="Maximum candidate rows per run")
    parser.add_argument("--reason", default="historical-template-noise", help="Operator-visible cleanup reason")
    parser.add_argument("--actor", default="governance.cleanup.py", help="Actor recorded in governance audit events")
    parser.add_argument("--batch-id", default="", help="Optional batch id for apply, or required target for rollback")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview candidates without mutation (default)")
    mode.add_argument("--apply", action="store_true", help="Soft-archive candidates / apply rollback and write audit events")
    parser.add_argument("--rollback-batch", action="store_true", help="Rollback a previous soft-archive batch id; combine with --apply to mutate")
    return parser.parse_args()


def db_path(args: argparse.Namespace) -> Path:
    return memory_db_path(Path(args.hermes_home or "~/.hermes"), db_path=args.db or None)


def emit(payload: dict[str, Any], *, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("# scope-recall governance cleanup")
    print(f"- ok: {payload.get('ok')}")
    if "error" in payload:
        print(f"- error: {payload['error']}")
        return
    print(f"- db: `{payload.get('db_path', '')}`")
    if "before_counts" in payload:
        print("- before_counts:")
        for key, value in payload.get("before_counts", {}).items():
            print(f"  - {key}: {value}")
    if "result" in payload:
        result = payload["result"]
        print(f"- dry_run: {result.get('dry_run')}")
        print(f"- batch_id: `{result.get('batch_id', '')}`")
        print(f"- candidate_count: {result.get('candidate_count', result.get('rollback_candidates', 0))}")
        print(f"- archived: {result.get('archived', 0)}")
        print(f"- restored: {result.get('restored', 0)}")
        if result.get("reason_counts"):
            print("- reason_counts:")
            for key, value in result["reason_counts"].items():
                print(f"  - {key}: {value}")
    if "after_counts" in payload:
        print("- after_counts:")
        for key, value in payload.get("after_counts", {}).items():
            print(f"  - {key}: {value}")


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
        payload["before_counts"] = active_dirty_counts(conn, scope_ids=args.scope_id)
        if args.rollback_batch:
            if not args.batch_id:
                payload["error"] = "--batch-id is required with --rollback-batch"
                emit(payload, fmt=args.format)
                return 2
            result = rollback_cleanup_batch(conn, batch_id=args.batch_id, dry_run=not should_apply, actor=args.actor)
        else:
            result = apply_cleanup(
                conn,
                scope_ids=args.scope_id,
                dry_run=not should_apply,
                limit=args.limit,
                reason=args.reason,
                actor=args.actor,
                batch_id=args.batch_id or None,
            )
        payload["result"] = result
        payload["after_counts"] = active_dirty_counts(conn, scope_ids=args.scope_id)
        payload["ok"] = True
        emit(payload, fmt=args.format)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
