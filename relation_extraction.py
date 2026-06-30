"""Deterministic relation extraction and synchronization for memory graph edges.

Relation writes are companion evidence; contradiction checks and supersession links must not change the source memory text."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable

try:
    from .graph import lifecycle_visible_sql, metadata_entities
    from .scoring import semantic_similarity
    from .sql_store import now_iso
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from graph import lifecycle_visible_sql, metadata_entities
    from scoring import semantic_similarity
    from sql_store import now_iso

_SUPERSEDES_RE = re.compile(r"\b(?:supersedes?|replaces?|replaced)\b|取代|替代")
_OLD_RE = re.compile(r"\b(?:old|legacy|deprecated|previous|v\d+)\b|旧|旧版|过时")
_TYPED_RELATION_TRIGGERS = {
    "depends_on": (r"depends\s+on", r"requires", r"needs", r"依赖", r"需要"),
    "owned_by": (r"owned\s+by", r"owner\s+is", r"maintained\s+by", r"belongs\s+to", r"归属", r"负责人"),
    "affects": (r"affects", r"impacts", r"changes", r"blocks", r"影响", r"阻塞"),
}


def _clean_scope_ids(scope_ids: Iterable[str] | None) -> list[str]:
    return sorted({str(scope_id) for scope_id in (scope_ids or []) if str(scope_id)})


def _load_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _memory_rows(conn: sqlite3.Connection, *, scope_ids: Iterable[str] | None = None, memory_ids: Iterable[str] | None = None) -> list[sqlite3.Row]:
    where = [lifecycle_visible_sql("m")]
    params: list[Any] = []
    scopes = _clean_scope_ids(scope_ids)
    if scopes:
        where.append(f"m.scope_id IN ({','.join('?' for _ in scopes)})")
        params.extend(scopes)
    ids = sorted({str(memory_id) for memory_id in (memory_ids or []) if str(memory_id)})
    if ids:
        where.append(f"m.id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    rows = conn.execute(
        f"""
        SELECT m.id, m.scope_id, m.target, m.content, m.summary, m.created_at, m.updated_at, m.metadata
        FROM memories m
        WHERE {' AND '.join(where)}
        ORDER BY m.updated_at DESC, m.id DESC
        """,
        params,
    ).fetchall()
    return rows


def _row_payload(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _load_metadata(row["metadata"])
    content = str(row["content"] or "")
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"]),
        "target": str(row["target"]),
        "content": content,
        "summary": str(row["summary"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "metadata": metadata,
        "entities": set(metadata_entities(metadata, content, str(row["target"] or ""))),
    }


def _existing_relation_types(conn: sqlite3.Connection, memory_ids: Iterable[str]) -> set[tuple[str, str, str]]:
    ids = sorted({str(memory_id) for memory_id in memory_ids if str(memory_id)})
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"""
            SELECT source_memory_id, target_memory_id, relation_type
            FROM memory_relations
            WHERE source_memory_id IN ({placeholders})
               OR target_memory_id IN ({placeholders})
            """,
            [*ids, *ids],
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {(str(row["source_memory_id"]), str(row["target_memory_id"]), str(row["relation_type"]).strip().lower()) for row in rows}


def _pair_has_relation(existing: set[tuple[str, str, str]], left_id: str, right_id: str, relation_type: str) -> bool:
    relation = str(relation_type).strip().lower()
    return (left_id, right_id, relation) in existing or (right_id, left_id, relation) in existing


def _pair_has_contradiction(existing: set[tuple[str, str, str]], left_id: str, right_id: str) -> bool:
    return _pair_has_relation(existing, left_id, right_id, "contradicts")


def _pair_key(left_id: str, right_id: str) -> tuple[str, str]:
    left = str(left_id)
    right = str(right_id)
    return (left, right) if left <= right else (right, left)


def _delete_generated_relation_edges_for_pairs(conn: sqlite3.Connection, pairs: Iterable[tuple[str, str]]) -> int:
    pair_keys = sorted({_pair_key(left_id, right_id) for left_id, right_id in pairs if str(left_id) and str(right_id)})
    if not pair_keys:
        return 0
    before = conn.total_changes
    for left_id, right_id in pair_keys:
        conn.execute(
            """
            DELETE FROM memory_relations
            WHERE (
                    (source_memory_id = ? AND target_memory_id = ?)
                 OR (source_memory_id = ? AND target_memory_id = ?)
            )
              AND LOWER(COALESCE(note, '')) LIKE 'relation-extraction:%'
            """,
            (left_id, right_id, right_id, left_id),
        )
    return conn.total_changes - before


def _same_topic(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, float, str]:
    if left["scope_id"] != right["scope_id"] or left["target"] != right["target"]:
        return False, 0.0, ""
    entity_overlap = set(left["entities"]) & set(right["entities"])
    similarity = semantic_similarity(str(left["content"]), str(right["content"]))
    if entity_overlap and similarity >= 0.24:
        return True, max(0.55, min(0.95, 0.45 + similarity)), f"shared_entities={','.join(sorted(entity_overlap)[:4])}; similarity={similarity:.3f}"
    if similarity >= 0.68:
        return True, min(0.9, similarity), f"similarity={similarity:.3f}"
    return False, 0.0, ""


def _supersedes(newer: dict[str, Any], older: dict[str, Any]) -> tuple[bool, float, str]:
    if newer["id"] == older["id"]:
        return False, 0.0, ""
    if newer["updated_at"] < older["updated_at"]:
        return False, 0.0, ""
    text = str(newer["content"] or "").lower()
    older_text = str(older["content"] or "").lower()
    shared_entities = set(newer["entities"]) & set(older["entities"])
    similarity = semantic_similarity(text, older_text)
    explicit_new = bool(_SUPERSEDES_RE.search(text))
    old_marker = bool(_OLD_RE.search(older_text))
    if shared_entities and explicit_new and (old_marker or similarity >= 0.24):
        confidence = max(0.65, min(0.98, 0.55 + similarity))
        return True, confidence, f"explicit_supersedes; shared_entities={','.join(sorted(shared_entities)[:4])}; similarity={similarity:.3f}"
    return False, 0.0, ""


def _entity_pattern(entity: str) -> str:
    escaped = re.escape(entity)
    if re.fullmatch(r"[a-z0-9][a-z0-9 .:/#-]*", entity):
        return rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return escaped


def _trigger_mentions_entity(text: str, entity: str, triggers: tuple[str, ...]) -> bool:
    if len(entity) < 3:
        return False
    entity_re = _entity_pattern(entity)
    for trigger in triggers:
        if re.search(rf"(?:{trigger}).{{0,120}}{entity_re}", text, flags=re.I | re.S):
            return True
    return False


def _typed_relation(source: dict[str, Any], target: dict[str, Any], relation_type: str) -> tuple[bool, float, str]:
    if source["id"] == target["id"] or source["scope_id"] != target["scope_id"]:
        return False, 0.0, ""
    triggers = _TYPED_RELATION_TRIGGERS.get(relation_type)
    if not triggers:
        return False, 0.0, ""
    text = str(source["content"] or "").lower()
    matched_entities = [
        str(entity)
        for entity in sorted(set(target["entities"]), key=lambda value: (-len(str(value)), str(value)))
        if _trigger_mentions_entity(text, str(entity), triggers)
    ]
    if not matched_entities:
        return False, 0.0, ""
    confidence = 0.78 if relation_type in {"depends_on", "owned_by"} else 0.72
    return True, confidence, f"triggered_{relation_type}; matched_entities={','.join(matched_entities[:4])}"


def _candidate(
    *,
    source_id: str,
    target_id: str,
    relation_type: str,
    confidence: float,
    note: str,
) -> dict[str, Any]:
    return {
        "source_memory_id": source_id,
        "target_memory_id": target_id,
        "relation_type": relation_type,
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "note": note,
    }


def extract_relation_candidates(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    memory_ids: Iterable[str] | None = None,
    max_pairs: int = 5000,
) -> list[dict[str, Any]]:
    candidates, _, _ = _relation_candidate_scan(conn, scope_ids=scope_ids, memory_ids=memory_ids, max_pairs=max_pairs)
    return candidates


def _relation_candidate_scan(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    memory_ids: Iterable[str] | None = None,
    focus_memory_ids: Iterable[str] | None = None,
    max_pairs: int = 5000,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]], bool]:
    """Scan memory text for deterministic relation candidates.

    The scanner favors conservative, explainable edges because graph evidence influences recall ranking and conflict review."""
    rows = [_row_payload(row) for row in _memory_rows(conn, scope_ids=scope_ids, memory_ids=memory_ids)]
    output: dict[tuple[str, str, str], dict[str, Any]] = {}
    existing_relations = _existing_relation_types(conn, [str(row["id"]) for row in rows])
    focus_ids = {str(memory_id) for memory_id in (focus_memory_ids or []) if str(memory_id)}
    pair_budget = max(1, int(max_pairs or 5000))
    compared = 0
    compared_pairs: set[tuple[str, str]] = set()
    budget_exceeded = False
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if focus_ids and left["id"] not in focus_ids and right["id"] not in focus_ids:
                continue
            compared += 1
            if compared > pair_budget:
                budget_exceeded = True
                break
            if left["id"] == right["id"]:
                continue
            compared_pairs.add(_pair_key(str(left["id"]), str(right["id"])))
            if _pair_has_contradiction(existing_relations, str(left["id"]), str(right["id"])):
                continue
            same, confidence, note = _same_topic(left, right)
            if same:
                # same_topic is symmetric; store both directed edges so graph
                # evidence works from either result without a second query.
                for source, target in ((left, right), (right, left)):
                    key = (source["id"], target["id"], "same_topic")
                    output[key] = _candidate(
                        source_id=source["id"], target_id=target["id"], relation_type="same_topic", confidence=confidence, note=note
                    )
            for newer, older in ((left, right), (right, left)):
                supersedes, super_confidence, super_note = _supersedes(newer, older)
                if supersedes:
                    key = (newer["id"], older["id"], "supersedes")
                    output[key] = _candidate(
                        source_id=newer["id"],
                        target_id=older["id"],
                        relation_type="supersedes",
                        confidence=super_confidence,
                        note=super_note,
                    )
            for source, target in ((left, right), (right, left)):
                for relation_type in sorted(_TYPED_RELATION_TRIGGERS):
                    matched, typed_confidence, typed_note = _typed_relation(source, target, relation_type)
                    if not matched:
                        continue
                    key = (source["id"], target["id"], relation_type)
                    output[key] = _candidate(
                        source_id=source["id"],
                        target_id=target["id"],
                        relation_type=relation_type,
                        confidence=typed_confidence,
                        note=typed_note,
                    )
        if budget_exceeded:
            break
    return (
        sorted(output.values(), key=lambda item: (item["relation_type"], item["source_memory_id"], item["target_memory_id"])),
        compared_pairs,
        budget_exceeded,
    )


def rebuild_extracted_relations(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    memory_ids: Iterable[str] | None = None,
    dry_run: bool = True,
    batch_id: str = "manual",
    max_pairs: int = 5000,
    focus_memory_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Rebuild extracted relation companion rows from current SQLite memories.

    The rebuild path is deterministic so graph hygiene can recover from stale companion state without changing truth rows."""
    candidates, compared_pairs, budget_exceeded = _relation_candidate_scan(
        conn,
        scope_ids=scope_ids,
        memory_ids=memory_ids,
        focus_memory_ids=focus_memory_ids,
        max_pairs=max_pairs,
    )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "candidate_count": len(candidates),
            "inserted": 0,
            "deleted": 0,
            "compared_pair_count": len(compared_pairs),
            "budget_exceeded": budget_exceeded,
            "candidates": candidates[:50],
        }
    now = now_iso()
    note_prefix = f"relation-extraction:{batch_id}"
    deleted = _delete_generated_relation_edges_for_pairs(conn, compared_pairs)
    before_insert = conn.total_changes
    for item in candidates:
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item["source_memory_id"],
                item["target_memory_id"],
                item["relation_type"],
                item["confidence"],
                f"{note_prefix}; {item['note']}",
                now,
            ),
        )
    inserted = conn.total_changes - before_insert
    conn.commit()
    return {
        "ok": True,
        "dry_run": False,
        "candidate_count": len(candidates),
        "inserted": inserted,
        "deleted": deleted,
        "compared_pair_count": len(compared_pairs),
        "budget_exceeded": budget_exceeded,
        "candidates": candidates[:50],
    }


def sync_extracted_relations_for_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    scope_ids: Iterable[str] | None = None,
    batch_id: str = "store",
    max_pairs: int = 1000,
) -> dict[str, Any]:
    memory_id = str(memory_id or "")
    if not memory_id:
        return {"ok": False, "dry_run": False, "candidate_count": 0, "inserted": 0, "error": "missing memory_id"}
    scopes = _clean_scope_ids(scope_ids)
    new_row = conn.execute("SELECT scope_id FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if new_row is not None and not scopes:
        scopes = [str(new_row["scope_id"])]
    return rebuild_extracted_relations(
        conn,
        scope_ids=scopes,
        dry_run=False,
        batch_id=batch_id,
        max_pairs=max_pairs,
        focus_memory_ids=[memory_id],
    )
