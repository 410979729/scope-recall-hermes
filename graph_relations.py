from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .graph import clamp_float, load_metadata

ALLOWED_RELATION_TYPES = {"contradicts", "supports", "supersedes"}
HIDDEN_RELATION_PEER_LIFECYCLES = {"superseded", "obsolete", "rejected", "archived"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _relation_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _scope_set(accessible_scope_ids: Iterable[str] | None) -> set[str]:
    return {str(scope_id) for scope_id in (accessible_scope_ids or []) if str(scope_id)}


def _is_hidden_lifecycle(metadata: Any) -> bool:
    lifecycle = str(load_metadata(metadata).get("lifecycle") or "").strip().lower()
    return lifecycle in HIDDEN_RELATION_PEER_LIFECYCLES


def _memory_exists(conn: sqlite3.Connection, memory_id: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is not None
    except sqlite3.Error:
        return False


def upsert_relation(
    conn: sqlite3.Connection,
    *,
    source_memory_id: str,
    target_memory_id: str,
    relation_type: str,
    confidence: float = 0.5,
    note: str = "",
    created_at: str | None = None,
) -> bool:
    """Insert one deterministic memory relation if it is safe and new.

    Returns True only when a row is inserted. Invalid/self/unsupported or
    missing-endpoint relations are skipped instead of raising so dry-run/backfill
    callers can aggregate counters without partially mutating state.
    """

    source_id = _clean_id(source_memory_id)
    target_id = _clean_id(target_memory_id)
    normalized_type = _relation_type(relation_type)
    if not source_id or not target_id or source_id == target_id or normalized_type not in ALLOWED_RELATION_TYPES:
        return False
    if not _memory_exists(conn, source_id) or not _memory_exists(conn, target_id):
        return False
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            target_id,
            normalized_type,
            clamp_float(confidence, default=0.5, minimum=0.0, maximum=1.0),
            str(note or ""),
            created_at or _now_iso(),
        ),
    )
    return int(cursor.rowcount or 0) > 0


def backfill_supersedes_from_metadata(
    conn: sqlite3.Connection,
    *,
    apply: bool = False,
    accessible_scope_ids: Iterable[str] | None = None,
    same_scope_only: bool = True,
    limit: int | None = None,
    max_planned: int = 50,
) -> dict[str, Any]:
    """Backfill `supersedes` edges from trusted `metadata.superseded_by` ids.

    The direction is replacement/current memory -> old memory. The old memory may
    be archived/hidden, but the replacement target must be an existing visible
    row. By default the old and replacement rows must be in the same scope to
    avoid creating broad cross-scope graph edges from historical metadata.
    """

    scopes = _scope_set(accessible_scope_ids)
    rows = conn.execute(
        """
        SELECT id, scope_id, metadata
        FROM memories
        WHERE metadata LIKE '%superseded_by%'
        ORDER BY updated_at DESC
        """
    ).fetchall()
    if limit is not None and int(limit) >= 0:
        rows = rows[: int(limit)]

    result: dict[str, Any] = {
        "dry_run": not bool(apply),
        "scanned": len(rows),
        "candidate_supersedes": 0,
        "inserted_supersedes": 0,
        "existing_supersedes": 0,
        "skipped_missing_target": 0,
        "skipped_self_relation": 0,
        "skipped_inaccessible_scope": 0,
        "skipped_cross_scope": 0,
        "skipped_hidden_target": 0,
        "planned": [],
        "planned_truncated": 0,
    }

    def _append_planned(item: dict[str, Any]) -> None:
        planned = result["planned"]
        if isinstance(planned, list) and len(planned) < max(0, int(max_planned)):
            planned.append(item)
        else:
            result["planned_truncated"] += 1

    for row in rows:
        old_id = _clean_id(row["id"])
        old_scope = _clean_id(row["scope_id"])
        metadata = load_metadata(row["metadata"])
        new_id = _clean_id(metadata.get("superseded_by"))
        if not new_id:
            continue
        result["candidate_supersedes"] += 1
        if old_id == new_id:
            result["skipped_self_relation"] += 1
            continue
        new_row = conn.execute("SELECT id, scope_id, metadata FROM memories WHERE id = ?", (new_id,)).fetchone()
        if new_row is None:
            result["skipped_missing_target"] += 1
            continue
        new_scope = _clean_id(new_row["scope_id"])
        if scopes and (old_scope not in scopes or new_scope not in scopes):
            result["skipped_inaccessible_scope"] += 1
            continue
        if same_scope_only and old_scope != new_scope:
            result["skipped_cross_scope"] += 1
            continue
        if _is_hidden_lifecycle(new_row["metadata"]):
            result["skipped_hidden_target"] += 1
            continue
        existing = conn.execute(
            """
            SELECT 1 FROM memory_relations
            WHERE source_memory_id = ? AND target_memory_id = ? AND relation_type = 'supersedes'
            """,
            (new_id, old_id),
        ).fetchone()
        planned_item = {"source_memory_id": new_id, "target_memory_id": old_id, "relation_type": "supersedes"}
        if existing is not None:
            result["existing_supersedes"] += 1
            _append_planned({**planned_item, "status": "existing"})
            continue
        _append_planned({**planned_item, "status": "insert" if apply else "dry_run"})
        if apply and upsert_relation(
            conn,
            source_memory_id=new_id,
            target_memory_id=old_id,
            relation_type="supersedes",
            confidence=0.95,
            note="backfill: metadata.superseded_by",
        ):
            result["inserted_supersedes"] += 1
    return result


def relation_type_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT relation_type, COUNT(*) AS n
        FROM memory_relations
        GROUP BY relation_type
        ORDER BY n DESC, relation_type ASC
        """
    ).fetchall()
    return {str(row["relation_type"]): int(row["n"] or 0) for row in rows}


def graph_relation_stats(conn: sqlite3.Connection, *, accessible_scope_ids: Iterable[str] | None = None) -> dict[str, Any]:
    scopes = _scope_set(accessible_scope_ids)
    total_relations = int(conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0] or 0)
    total_entities = int(conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] or 0)
    memories_with_entities = int(conn.execute("SELECT COUNT(DISTINCT memory_id) FROM memory_entities").fetchone()[0] or 0)
    orphan_relations = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM memory_relations r
            LEFT JOIN memories s ON s.id = r.source_memory_id
            LEFT JOIN memories t ON t.id = r.target_memory_id
            WHERE s.id IS NULL OR t.id IS NULL
            """
        ).fetchone()[0]
        or 0
    )
    relation_rows = conn.execute(
        """
        SELECT r.source_memory_id, r.target_memory_id, r.relation_type,
               s.scope_id AS source_scope_id, t.scope_id AS target_scope_id,
               s.metadata AS source_metadata, t.metadata AS target_metadata
        FROM memory_relations r
        LEFT JOIN memories s ON s.id = r.source_memory_id
        LEFT JOIN memories t ON t.id = r.target_memory_id
        """
    ).fetchall()
    lifecycle_hidden_peer_relations = 0
    scoped_relations = 0
    for row in relation_rows:
        source_hidden = row["source_metadata"] is not None and _is_hidden_lifecycle(row["source_metadata"])
        target_hidden = row["target_metadata"] is not None and _is_hidden_lifecycle(row["target_metadata"])
        if source_hidden or target_hidden:
            lifecycle_hidden_peer_relations += 1
        if scopes and row["source_scope_id"] in scopes and row["target_scope_id"] in scopes:
            scoped_relations += 1
    return {
        "memory_entities": total_entities,
        "memories_with_entities": memories_with_entities,
        "memory_relations": total_relations,
        "scoped_memory_relations": scoped_relations if scopes else total_relations,
        "relation_types": relation_type_counts(conn),
        "orphan_relations": orphan_relations,
        "lifecycle_hidden_peer_relations": lifecycle_hidden_peer_relations,
    }


def as_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
