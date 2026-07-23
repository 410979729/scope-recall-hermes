"""Graph companion hygiene checks and repair helpers.

Repairs remove orphan companion rows only after comparing against SQLite truth, never the other way around."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .graph import lifecycle_visible_sql
    from .maintenance_lease import install_activation_lease_authorizer
    from .relation_frequency_maintenance import (
        drain_relation_frequency_work,
        relation_frequency_debt_exists,
        relation_frequency_index_report,
    )
    from .relation_rebuild_queue import (
        drain_relation_rebuild_queue,
        ensure_relation_rebuild_schema,
        relation_rebuild_queue_report,
        seed_scope_relation_rebuilds,
    )
    from .truth_connection import connect_truth_database
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from graph import lifecycle_visible_sql
    from maintenance_lease import install_activation_lease_authorizer
    from relation_frequency_maintenance import (
        drain_relation_frequency_work,
        relation_frequency_debt_exists,
        relation_frequency_index_report,
    )
    from relation_rebuild_queue import (
        drain_relation_rebuild_queue,
        ensure_relation_rebuild_schema,
        relation_rebuild_queue_report,
        seed_scope_relation_rebuilds,
    )
    from truth_connection import connect_truth_database

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
    conn = connect_truth_database(db_path, mode=mode)
    if apply:
        install_activation_lease_authorizer(conn, db_path)
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


def _backup_relation_rebuild_sqlite(
    conn: sqlite3.Connection, db_path: Path
) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / (
        f"memory.sqlite3.pre-relation-rebuild.{stamp}.{uuid.uuid4().hex[:8]}.sqlite3"
    )
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
        check = str(target.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        target.close()
    if check.lower() != "ok":
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"relation rebuild backup quick_check failed: {check}")
    return destination


def repair_relation_rebuild_debt(
    hermes_home: Path,
    *,
    seed: bool = False,
    drain: bool = False,
    scope_ids: list[str] | None = None,
    max_events: int = 100,
    pair_limit: int = 250,
) -> dict[str, Any]:
    """Inspect or apply bounded relation-debt seeding/draining with a backup."""

    db_path = memory_db_path(hermes_home)
    if not db_path.exists():
        return {
            "ok": False,
            "status": "missing",
            "path": str(db_path),
            "error": "SQLite truth DB not found",
        }
    apply = bool(seed or drain)
    conn = connect_truth_database(db_path, mode="rw" if apply else "ro")
    backup_path: Path | None = None
    seed_result: dict[str, int] = {"eligible": 0, "queued": 0}
    drain_result: dict[str, int] = {
        "claimed": 0,
        "chunks_completed": 0,
        "events_completed": 0,
        "superseded": 0,
        "failed": 0,
        "dead_lettered": 0,
    }
    frequency_result: dict[str, int] = {
        "processed_changes": 0,
        "backfilled_memories": 0,
        "reclassification_enqueued": 0,
    }
    try:
        before = relation_rebuild_queue_report(conn)
        frequency_before = relation_frequency_index_report(conn)
        if apply:
            install_activation_lease_authorizer(conn, db_path)
            backup_path = _backup_relation_rebuild_sqlite(conn, db_path)
            ensure_relation_rebuild_schema(conn)
        if seed:
            seed_result = seed_scope_relation_rebuilds(
                conn,
                scope_ids=scope_ids,
                reason="operator seeded graph-hygiene relation rebuild",
                commit=True,
            )
        if drain:
            maintenance_budget = max(
                1,
                min(int(max_events) * max(1, int(pair_limit)), 10000),
            )
            frequency_result = drain_relation_frequency_work(
                conn,
                change_limit=maintenance_budget,
                backfill_limit=maintenance_budget,
                reclassification_limit=maintenance_budget,
            )
            if not relation_frequency_debt_exists(conn):
                drain_result = drain_relation_rebuild_queue(
                    conn,
                    max_events=max(0, min(int(max_events), 10000)),
                    pair_limit=max(1, min(int(pair_limit), 5000)),
                )
            else:
                drain_result["frequency_deferred"] = 1
        after = relation_rebuild_queue_report(conn)
        frequency_after = relation_frequency_index_report(conn)
    finally:
        conn.close()
    frequency_remaining = (
        int(frequency_after.get("dirty_memories") or 0)
        + int(frequency_after.get("backfill_pending_scopes") or 0)
        + int(frequency_after.get("reclassification_pending_scopes") or 0)
    )
    ok = (
        int(drain_result.get("failed") or 0) == 0
        and int(after.get("dead_letter") or 0) == 0
        and frequency_remaining == 0
    )
    return {
        "ok": ok,
        "status": (
            "ready"
            if int(after.get("unresolved") or 0) == 0 and frequency_remaining == 0
            else "debt"
        ),
        "dry_run": not apply,
        "path": str(db_path),
        "backup_path": str(backup_path) if backup_path else "",
        "before": before,
        "frequency_before": frequency_before,
        "seed": seed_result,
        "frequency_drain": frequency_result,
        "drain": drain_result,
        "after": after,
        "frequency_after": frequency_after,
    }
