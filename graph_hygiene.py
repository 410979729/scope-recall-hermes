"""Graph companion hygiene checks and repair helpers.

Repairs remove orphan companion rows only after comparing against SQLite truth, never the other way around."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

try:
    from .graph import lifecycle_visible_sql
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from graph import lifecycle_visible_sql

GRAPH_HYGIENE_COUNT_KEYS = (
    "orphan_entities",
    "orphan_relations",
    "orphan_relation_sources",
    "orphan_relation_targets",
    "hidden_lifecycle_entities",
    "hidden_lifecycle_relations",
    "hidden_lifecycle_relation_sources",
    "hidden_lifecycle_relation_targets",
)


def graph_hygiene_count_keys() -> tuple[str, ...]:
    return GRAPH_HYGIENE_COUNT_KEYS


def empty_graph_hygiene_counts() -> dict[str, int]:
    return {key: 0 for key in GRAPH_HYGIENE_COUNT_KEYS}


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def graph_hygiene_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = table_names(conn)
    counts = empty_graph_hygiene_counts()
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
                WHERE NOT ({lifecycle_visible_sql('m')})
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
                SUM(CASE WHEN s.id IS NOT NULL AND NOT ({lifecycle_visible_sql('s')}) THEN 1 ELSE 0 END) AS hidden_sources,
                SUM(CASE WHEN t.id IS NOT NULL AND NOT ({lifecycle_visible_sql('t')}) THEN 1 ELSE 0 END) AS hidden_targets,
                SUM(CASE WHEN (s.id IS NOT NULL AND NOT ({lifecycle_visible_sql('s')})) OR (t.id IS NOT NULL AND NOT ({lifecycle_visible_sql('t')})) THEN 1 ELSE 0 END) AS hidden_relations
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


def count_deletable_graph_hygiene_rows(conn: sqlite3.Connection) -> dict[str, int]:
    tables = table_names(conn)
    counts = {"memory_entities": 0, "memory_relations": 0}
    if {"memories", "memory_entities"} <= tables:
        counts["memory_entities"] = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM memory_entities
                WHERE memory_id NOT IN (SELECT id FROM memories)
                   OR memory_id IN (SELECT m.id FROM memories m WHERE NOT ({lifecycle_visible_sql('m')}))
                """
            ).fetchone()[0]
        )
    if {"memories", "memory_relations"} <= tables:
        counts["memory_relations"] = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM memory_relations
                WHERE source_memory_id NOT IN (SELECT id FROM memories)
                   OR target_memory_id NOT IN (SELECT id FROM memories)
                   OR source_memory_id IN (SELECT s.id FROM memories s WHERE NOT ({lifecycle_visible_sql('s')}))
                   OR target_memory_id IN (SELECT t.id FROM memories t WHERE NOT ({lifecycle_visible_sql('t')}))
                """
            ).fetchone()[0]
        )
    return counts


def delete_graph_hygiene_rows(conn: sqlite3.Connection) -> dict[str, int]:
    tables = table_names(conn)
    deleted = {"memory_entities": 0, "memory_relations": 0}
    if {"memories", "memory_entities"} <= tables:
        before = conn.total_changes
        conn.execute(
            f"""
            DELETE FROM memory_entities
            WHERE memory_id NOT IN (SELECT id FROM memories)
               OR memory_id IN (SELECT m.id FROM memories m WHERE NOT ({lifecycle_visible_sql('m')}))
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
               OR source_memory_id IN (SELECT s.id FROM memories s WHERE NOT ({lifecycle_visible_sql('s')}))
               OR target_memory_id IN (SELECT t.id FROM memories t WHERE NOT ({lifecycle_visible_sql('t')}))
            """
        )
        deleted["memory_relations"] = conn.total_changes - before
    return deleted


def remaining_graph_hygiene_rows(counts: dict[str, int]) -> int:
    return sum(int(counts.get(key) or 0) for key in GRAPH_HYGIENE_COUNT_KEYS)


def memory_db_path(hermes_home: Path) -> Path:
    return hermes_home.expanduser() / "scope-recall" / "memory.sqlite3"


def repair_graph_hygiene(hermes_home: Path, *, apply: bool = False) -> dict[str, Any]:
    db_path = memory_db_path(hermes_home)
    if not db_path.exists():
        return {"ok": False, "status": "missing", "path": str(db_path), "error": "SQLite truth DB not found"}

    mode = "rw" if apply else "ro"
    conn = sqlite3.connect(f"file:{db_path}?mode={mode}", uri=True)
    try:
        before = graph_hygiene_counts(conn)
        deleted = count_deletable_graph_hygiene_rows(conn)
        if apply:
            deleted = delete_graph_hygiene_rows(conn)
            conn.commit()
        after = graph_hygiene_counts(conn)
    finally:
        conn.close()

    remaining = remaining_graph_hygiene_rows(after)
    return {
        "ok": remaining == 0,
        "status": "ready" if remaining == 0 else "needs_repair",
        "dry_run": not apply,
        "path": str(db_path),
        "before": before,
        "deleted": deleted,
        "after": after,
    }
