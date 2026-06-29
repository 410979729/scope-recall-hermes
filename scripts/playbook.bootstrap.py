#!/usr/bin/env python3
"""Bootstrap curated core Experience playbooks.

Default is dry-run. Applying writes candidate playbooks and immediately promotes
only the curated seed set; use an explicit --scope-id so operators know where the
rows will live.
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

from scope_recall.experience_bootstrap import bootstrap_core_playbooks  # noqa: E402
from scope_recall.maintenance_ops import connect_memory_db, effective_apply, memory_db_path  # noqa: E402
from scope_recall.sql_store import ensure_schema  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap curated Scope Recall Experience playbooks")
    parser.add_argument("--db", default="", help="SQLite memory DB path; defaults to <hermes-home>/scope-recall/memory.sqlite3")
    parser.add_argument("--hermes-home", default="", help="Hermes home/profile path")
    parser.add_argument("--scope-id", required=True, help="Owner scope id for created playbooks")
    parser.add_argument("--shared-scope-id", default="", help="Optional shared pool scope id")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview rows without mutation (default)")
    mode.add_argument("--apply", action="store_true", help="Create/promote curated seed playbooks")
    return parser.parse_args()


def db_path(args: argparse.Namespace) -> Path:
    return memory_db_path(Path(args.hermes_home or "~/.hermes"), db_path=args.db or None)


def emit(payload: dict[str, Any], *, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("# scope-recall experience bootstrap")
    print(f"- ok: {payload.get('ok')}")
    if "error" in payload:
        print(f"- error: {payload['error']}")
        return
    print(f"- db: `{payload.get('db_path', '')}`")
    result = payload.get("result", {}) if isinstance(payload.get("result"), dict) else {}
    print(f"- dry_run: {result.get('dry_run')}")
    print(f"- created: {result.get('created', 0)}")
    print(f"- promoted: {result.get('promoted', 0)}")
    print(f"- skipped_existing: {result.get('skipped_existing', 0)}")


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
        if should_apply:
            ensure_schema(conn)
        result = bootstrap_core_playbooks(
            conn,
            scope_id=args.scope_id,
            shared_scope_id=args.shared_scope_id,
            accessible_scope_ids=[args.scope_id, args.shared_scope_id],
            dry_run=not should_apply,
        )
        payload["result"] = result
        payload["ok"] = True
        emit(payload, fmt=args.format)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
