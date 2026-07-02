#!/usr/bin/env python3
"""Operator CLI for listing, inspecting, reviewing, and promoting Experience playbooks.

This script is a human review surface; it should not hide duplicate/superseded status behind terse output."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from scope_recall.experience_store import find_duplicate_playbooks, review_playbook, search_playbooks
    from scope_recall.sql_store import ensure_schema
except Exception:  # pragma: no cover - source-tree execution fallback
    from experience_store import find_duplicate_playbooks, review_playbook, search_playbooks  # type: ignore
    from sql_store import ensure_schema  # type: ignore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Operate Scope Recall Experience playbooks")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "dedupe", "review", "promote", "quarantine", "supersede"):
        cmd = sub.add_parser(name, help=f"playbook {name}")
        cmd.add_argument("--hermes-home", help="Hermes home/profile path")
        cmd.add_argument("--db", help="Explicit memory.sqlite3 path; overrides --hermes-home")
        cmd.add_argument("--scope-id", action="append", default=[], help="Restrict to a scope id; repeatable. Defaults to all playbook scopes.")
        cmd.add_argument("--limit", type=int, default=20)
        cmd.add_argument("--status", default="")
        cmd.add_argument("--json", action="store_true", help="Emit JSON output (default; accepted for operator consistency)")
        if name == "list":
            cmd.add_argument("--query", default="")
        if name in {"review", "promote", "quarantine", "supersede"}:
            cmd.add_argument("--id", required=True, help="Playbook id")
            cmd.add_argument("--reason", default="", help="Operator review reason")
        if name == "supersede":
            cmd.add_argument("--superseded-by", required=True, help="Canonical playbook id replacing --id")
    return parser.parse_args(argv)


def _db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db).expanduser().resolve()
    if args.hermes_home:
        return Path(args.hermes_home).expanduser().resolve() / "scope-recall" / "memory.sqlite3"
    return Path.home() / ".hermes" / "scope-recall" / "memory.sqlite3"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _accessible_scope_ids(conn: sqlite3.Connection, raw_scope_ids: list[str]) -> list[str]:
    explicit = [item for item in raw_scope_ids if str(item).strip()]
    if explicit:
        return explicit
    rows = conn.execute("SELECT DISTINCT scope_id FROM procedural_playbooks ORDER BY scope_id").fetchall()
    return [str(row["scope_id"]) for row in rows] or [""]


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    db_path = _db_path(args)
    if not db_path.exists():
        return {"ok": False, "error": "db_missing", "path": str(db_path)}
    with _connect(db_path) as conn:
        scopes = _accessible_scope_ids(conn, list(args.scope_id or []))
        limit = max(1, min(100, int(args.limit or 20)))
        if args.command == "list":
            rows = search_playbooks(conn, query=str(args.query or ""), accessible_scope_ids=scopes, limit=limit, status=str(args.status or ""))
            return {"ok": True, "action": "list", "count": len(rows), "playbooks": rows, "scope_ids": scopes}
        if args.command == "dedupe":
            groups = find_duplicate_playbooks(conn, accessible_scope_ids=scopes, status=str(args.status or ""), limit=limit)
            return {"ok": True, "action": "dedupe", "count": len(groups), "groups": groups, "scope_ids": scopes}
        if args.command in {"review", "promote", "quarantine", "supersede"}:
            action = {"review": "review", "promote": "promote", "quarantine": "quarantine", "supersede": "supersede"}[str(args.command)]
            payload = review_playbook(
                conn,
                playbook_id=str(args.id),
                accessible_scope_ids=scopes,
                action=action,
                reason=str(args.reason or ""),
                superseded_by=str(getattr(args, "superseded_by", "") or ""),
            )
            payload.setdefault("action", action)
            payload.setdefault("scope_ids", scopes)
            payload["ok"] = bool(payload.get("reviewed"))
            return payload
    return {"ok": False, "error": "unknown_command", "command": args.command}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
