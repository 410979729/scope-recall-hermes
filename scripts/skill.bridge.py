#!/usr/bin/env python3
"""Operator CLI for Experience-to-Skill bridge review artifacts."""

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
    from scope_recall.skill_bridge import generate_skill_candidates
except Exception:  # pragma: no cover - source checkout execution fallback
    from skill_bridge import generate_skill_candidates  # type: ignore


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate review-only skill candidates from Experience playbooks")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("skill-candidates", help="Generate skill_candidate.v1 payloads as dry-run review artifacts")
    cmd.add_argument("--hermes-home", help="Hermes home/profile path")
    cmd.add_argument("--db", help="Explicit memory.sqlite3 path; overrides --hermes-home")
    cmd.add_argument("--scope-id", action="append", default=[], help="Restrict to a scope id; repeatable. Defaults to all playbook scopes.")
    cmd.add_argument("--limit", type=int, default=20)
    cmd.add_argument("--min-success-count", type=int, default=2)
    cmd.add_argument("--min-confidence", type=float, default=0.75)
    cmd.add_argument("--dry-run", action="store_true", default=True, help="Generate candidates without writing formal skills (default)")
    cmd.add_argument("--json", action="store_true", help="Emit JSON output (default; accepted for operator consistency)")
    return parser.parse_args(argv)


def _db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db).expanduser().resolve()
    if args.hermes_home:
        return Path(args.hermes_home).expanduser().resolve() / "scope-recall" / "memory.sqlite3"
    return Path.home() / ".hermes" / "scope-recall" / "memory.sqlite3"


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
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            scopes = _accessible_scope_ids(conn, list(args.scope_id or []))
            payload = generate_skill_candidates(
                conn,
                accessible_scope_ids=scopes,
                limit=max(1, min(100, int(args.limit or 20))),
                min_success_count=max(1, int(args.min_success_count or 2)),
                min_confidence=max(0.0, min(1.0, float(args.min_confidence if args.min_confidence is not None else 0.75))),
                dry_run=True,
            )
            payload["action"] = "skill-candidates"
            payload["db_path"] = str(db_path)
            payload["scope_ids"] = scopes
            return payload
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": "skill_bridge_failed", "detail": str(exc), "path": str(db_path)}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
