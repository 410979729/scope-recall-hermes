"""Dry-run-first review actions for memory candidates.

Candidate review updates metadata only after an explicit apply request. Dry-runs
return the exact before/after plan without mutating SQLite truth.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .sql_store import ensure_schema, now_iso, record_governance_audit_event


def _json_loads(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_row(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, scope_id, source, target, content, summary, created_at, updated_at, metadata
        FROM memories
        WHERE id = ?
        """,
        (memory_id,),
    ).fetchone()


def _snapshot(row: sqlite3.Row, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = _json_loads(row["metadata"]) if metadata is None else dict(metadata)
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"] or ""),
        "source": str(row["source"] or ""),
        "target": str(row["target"] or ""),
        "summary": str(row["summary"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "metadata": meta,
        "lifecycle": str(meta.get("lifecycle") or "active"),
        "candidate_status": str(meta.get("candidate_status") or ""),
    }


def _candidate_like(row: sqlite3.Row, metadata: dict[str, Any]) -> bool:
    lifecycle = str(metadata.get("lifecycle") or "").lower()
    source = str(row["source"] or "").lower()
    return lifecycle == "candidate" or bool(metadata.get("event_digest")) or source in {"event-digest", "memory-candidate"}


def _reviewed_metadata(row: sqlite3.Row, *, action: str, superseded_by: str = "", actor: str) -> dict[str, Any]:
    metadata = _json_loads(row["metadata"])
    timestamp = now_iso()
    metadata["candidate_reviewed_at"] = timestamp
    metadata["candidate_reviewed_by"] = actor
    metadata["candidate_review_action"] = action
    if action == "promote":
        metadata["lifecycle"] = "promoted"
        metadata["candidate_status"] = "promoted"
    elif action == "archive":
        metadata["lifecycle"] = "archived"
        metadata["candidate_status"] = "archived"
        metadata["archive_reason"] = "candidate_review_archive"
    elif action == "supersede":
        metadata["lifecycle"] = "superseded"
        metadata["candidate_status"] = "superseded"
        metadata["superseded_by"] = superseded_by
    else:  # pragma: no cover - guarded by caller
        raise ValueError(f"unsupported candidate review action: {action}")
    return metadata


def review_candidate(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    action: str,
    superseded_by: str = "",
    dry_run: bool = True,
    actor: str = "scope-recall:candidate-review",
) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in {"promote", "archive", "supersede"}:
        return {"ok": False, "dry_run": dry_run, "error": f"unsupported action: {action}"}
    if action == "supersede" and not str(superseded_by or "").strip():
        return {"ok": False, "dry_run": dry_run, "error": "--superseded-by is required for supersede"}
    row = _load_row(conn, memory_id)
    if row is None:
        return {"ok": False, "dry_run": dry_run, "error": f"candidate not found: {memory_id}"}
    if action == "supersede" and _load_row(conn, superseded_by) is None:
        return {"ok": False, "dry_run": dry_run, "error": f"superseded-by memory not found: {superseded_by}"}
    before_metadata = _json_loads(row["metadata"])
    if not _candidate_like(row, before_metadata):
        return {"ok": False, "dry_run": dry_run, "error": f"memory is not a candidate-like row: {memory_id}"}
    after_metadata = _reviewed_metadata(row, action=action, superseded_by=superseded_by, actor=actor)
    before = _snapshot(row, before_metadata)
    after = _snapshot(row, after_metadata)
    result = {
        "ok": True,
        "dry_run": bool(dry_run),
        "action": action,
        "id": memory_id,
        "superseded_by": superseded_by,
        "before": before,
        "after": after,
        "applied": False,
    }
    if dry_run:
        return result

    ensure_schema(conn)
    updated_at = now_iso()
    conn.execute("UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ?", (_json_dumps(after_metadata), updated_at, memory_id))
    if action in {"archive", "supersede"}:
        conn.execute("DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM memory_relations WHERE source_memory_id = ? OR target_memory_id = ?", (memory_id, memory_id))
    record_governance_audit_event(
        conn,
        event_id=f"candidate-review-{uuid.uuid4().hex}",
        event_type="memory_candidate_review",
        action=action,
        scope_id=str(row["scope_id"] or ""),
        target_id=memory_id,
        before=before,
        after=after,
        reason="operator candidate review",
        actor=actor,
        dry_run=False,
    )
    conn.commit()
    result["applied"] = True
    result["updated_at"] = updated_at
    return result
