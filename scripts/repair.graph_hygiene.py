#!/usr/bin/env python3
"""Repair orphan rows in scope-recall SQLite graph companion tables.

SQLite `memories` remains the truth source. `memory_entities` and
`memory_relations` are rebuildable graph/lookup companions; orphan rows should
not survive deletes or migrations because they can pollute entity graph hygiene
reports and future graph reads.

Default mode is a read-only dry run. Pass `--apply` to delete orphan graph rows.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remove orphan scope-recall graph rows")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--apply", action="store_true", help="delete orphan rows; default is read-only dry-run")
    parser.add_argument("--dry-run", action="store_true", help="explicit read-only dry-run (default; accepted for operator convenience)")
    parser.add_argument("--json", action="store_true", help="emit JSON output (accepted for operator convenience)")
    return parser.parse_args()


def _lifecycle_visible_clause(alias: str = "m") -> str:
    lifecycle_expr = f"LOWER(COALESCE(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.lifecycle') ELSE '' END, ''))"
    return f"{lifecycle_expr} NOT IN ('archived','superseded','obsolete','rejected')"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = _tables(conn)
    counts = {
        "orphan_entities": 0,
        "orphan_relations": 0,
        "orphan_relation_sources": 0,
        "orphan_relation_targets": 0,
        "hidden_lifecycle_entities": 0,
        "hidden_lifecycle_relations": 0,
        "hidden_lifecycle_relation_sources": 0,
        "hidden_lifecycle_relation_targets": 0,
    }
    if {"memories", "memory_entities"} <= tables:
        counts["orphan_entities"] = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM memory_entities e
                LEFT JOIN memories m ON m.id = e.memory_id
                WHERE m.id IS NULL
                """
            ).fetchone()[0]
        )
        counts["hidden_lifecycle_entities"] = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM memory_entities e
                JOIN memories m ON m.id = e.memory_id
                WHERE NOT ({_lifecycle_visible_clause('m')})
                """
            ).fetchone()[0]
        )
    if {"memories", "memory_relations"} <= tables:
        row = conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN s.id IS NULL THEN 1 ELSE 0 END) AS orphan_sources,
                SUM(CASE WHEN t.id IS NULL THEN 1 ELSE 0 END) AS orphan_targets,
                SUM(CASE WHEN s.id IS NULL OR t.id IS NULL THEN 1 ELSE 0 END) AS orphan_relations,
                SUM(CASE WHEN s.id IS NOT NULL AND NOT ({_lifecycle_visible_clause('s')}) THEN 1 ELSE 0 END) AS hidden_sources,
                SUM(CASE WHEN t.id IS NOT NULL AND NOT ({_lifecycle_visible_clause('t')}) THEN 1 ELSE 0 END) AS hidden_targets,
                SUM(CASE WHEN (s.id IS NOT NULL AND NOT ({_lifecycle_visible_clause('s')})) OR (t.id IS NOT NULL AND NOT ({_lifecycle_visible_clause('t')})) THEN 1 ELSE 0 END) AS hidden_relations
            FROM memory_relations r
            LEFT JOIN memories s ON s.id = r.source_memory_id
            LEFT JOIN memories t ON t.id = r.target_memory_id
            """
        ).fetchone()
        counts["orphan_relation_sources"] = int((row or (0, 0, 0, 0, 0, 0))[0] or 0)
        counts["orphan_relation_targets"] = int((row or (0, 0, 0, 0, 0, 0))[1] or 0)
        counts["orphan_relations"] = int((row or (0, 0, 0, 0, 0, 0))[2] or 0)
        counts["hidden_lifecycle_relation_sources"] = int((row or (0, 0, 0, 0, 0, 0))[3] or 0)
        counts["hidden_lifecycle_relation_targets"] = int((row or (0, 0, 0, 0, 0, 0))[4] or 0)
        counts["hidden_lifecycle_relations"] = int((row or (0, 0, 0, 0, 0, 0))[5] or 0)
    return counts


def _delete_orphans(conn: sqlite3.Connection) -> dict[str, int]:
    tables = _tables(conn)
    deleted = {"memory_entities": 0, "memory_relations": 0}
    if {"memories", "memory_entities"} <= tables:
        before = conn.total_changes
        conn.execute(
            f"""
            DELETE FROM memory_entities
            WHERE memory_id NOT IN (SELECT id FROM memories)
               OR memory_id IN (SELECT m.id FROM memories m WHERE NOT ({_lifecycle_visible_clause('m')}))
            """
        )
        deleted["memory_entities"] = conn.total_changes - before
    if {"memories", "memory_relations"} <= tables:
        before = conn.total_changes
        conn.execute(
            f"""
            DELETE FROM memory_relations
            WHERE source_memory_id NOT IN (SELECT id FROM memories)
               OR target_memory_id NOT IN (SELECT id FROM memories)
               OR source_memory_id IN (SELECT s.id FROM memories s WHERE NOT ({_lifecycle_visible_clause('s')}))
               OR target_memory_id IN (SELECT t.id FROM memories t WHERE NOT ({_lifecycle_visible_clause('t')}))
            """
        )
        deleted["memory_relations"] = conn.total_changes - before
    return deleted


def repair_graph_hygiene(hermes_home: Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = hermes_home.expanduser() / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return {"ok": False, "status": "missing", "path": str(db_path), "error": "SQLite truth DB not found"}

    mode = "rw" if apply else "ro"
    conn = sqlite3.connect(f"file:{db_path}?mode={mode}", uri=True)
    try:
        before = _counts(conn)
        deleted = {"memory_entities": 0, "memory_relations": 0}
        if apply:
            deleted = _delete_orphans(conn)
            conn.commit()
        after = _counts(conn)
    finally:
        conn.close()

    remaining = sum(int(value or 0) for value in after.values())
    return {
        "ok": remaining == 0,
        "status": "ready" if remaining == 0 else "needs_repair",
        "dry_run": not apply,
        "path": str(db_path),
        "before": before,
        "deleted": deleted,
        "after": after,
    }


def main() -> int:
    args = parse_args()
    effective_apply = bool(args.apply and not args.dry_run)
    payload = repair_graph_hygiene(Path(args.hermes_home), apply=effective_apply)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") or not effective_apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
