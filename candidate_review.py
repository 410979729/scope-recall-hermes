"""Dry-run-first review actions for memory candidates.

Candidate review updates metadata only after an explicit apply request. Dry-runs
return the exact before/after plan without mutating SQLite truth.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .graph import lifecycle_is_hidden
from .lifecycle_registry import (
    CANDIDATE_REVIEW_ARCHIVE,
    CANDIDATE_REVIEW_PROMOTE,
    CANDIDATE_REVIEW_SUPERSEDE,
)
from .lifecycle_service import LifecycleConflictError, transition_memory_lifecycle
from .sql_store import ensure_schema, now_iso


def _json_loads(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _public_admission_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    token = value.strip().casefold()
    if not token or len(token) > 64:
        return ""
    if not all(character.isalnum() or character in "._:-" for character in token):
        return ""
    return token


def _public_admission_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return False


def _public_automatic_admission(value: Any) -> dict[str, Any]:
    """Project only the stable, non-private admission contract."""

    if not isinstance(value, Mapping):
        return {}
    admission: dict[str, Any] = {
        "source": _public_admission_token(value.get("source")),
        "route": _public_admission_token(value.get("route")),
        "reviewed": _public_admission_bool(value.get("reviewed")),
    }
    if "time_sensitive" in value:
        admission["time_sensitive"] = _public_admission_bool(
            value.get("time_sensitive")
        )
    reviewed_at = value.get("reviewed_at")
    if isinstance(reviewed_at, str):
        timestamp = reviewed_at.strip()
        if 10 <= len(timestamp) <= 64:
            try:
                normalized_timestamp = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).isoformat()
            except ValueError:
                pass
            else:
                admission["reviewed_at"] = normalized_timestamp
    return admission


def candidate_identity_fields(
    *, source: str, metadata: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return explicit origin/lifecycle/review fields for public payloads."""

    meta = dict(metadata or {})
    raw_admission = meta.get("automatic_admission")
    admission = _public_automatic_admission(raw_admission)
    lifecycle = _public_admission_token(meta.get("lifecycle")) or "active"
    normalized_source = str(source or "").strip()
    origin_kind = _public_admission_token(meta.get("origin_kind"))
    if not origin_kind:
        origin_kind = str(admission.get("source") or "").strip()
    if not origin_kind and (
        bool(meta.get("event_digest"))
        or normalized_source.lower() == "event-digest"
    ):
        origin_kind = "event_digest"
    if not origin_kind:
        origin_kind = (
            _public_admission_token(normalized_source.replace("-", "_"))
            or "unknown"
        )
    review_status = _public_admission_token(
        meta.get("review_status") or meta.get("candidate_status") or ""
    )
    if not review_status and (
        meta.get("admission_reviewed_at") or meta.get("candidate_reviewed_at")
    ):
        review_status = "reviewed"
    if not review_status and lifecycle == "candidate":
        review_status = "pending"
    return {
        "origin_kind": origin_kind,
        "source": normalized_source,
        "lifecycle": lifecycle,
        "automatic_admission": admission,
        "review_status": review_status,
    }


def _public_snapshot_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project a lifecycle snapshot onto the explicit review API contract."""

    raw = dict(payload or {})
    metadata_value = raw.get("metadata")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    source = str(raw.get("source") or "")
    identity = candidate_identity_fields(source=source, metadata=metadata)
    candidate_status = _public_admission_token(metadata.get("candidate_status"))
    public_metadata: dict[str, Any] = {
        "origin_kind": identity["origin_kind"],
        "lifecycle": identity["lifecycle"],
        "candidate_status": candidate_status,
        "review_status": identity["review_status"],
        "automatic_admission": identity["automatic_admission"],
    }
    for key in ("event_digest", "correction_possible"):
        if key in metadata:
            public_metadata[key] = _public_admission_bool(metadata.get(key))
    return {
        key: raw.get(key, "")
        for key in ("id", "scope_id", "source", "target", "summary", "updated_at")
    } | {
        "metadata": public_metadata,
        "lifecycle": identity["lifecycle"],
        "candidate_status": candidate_status,
        "origin_kind": identity["origin_kind"],
        "automatic_admission": identity["automatic_admission"],
        "review_status": identity["review_status"],
    }


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
    payload = {
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
    return _public_snapshot_payload(payload)


def _candidate_like(row: sqlite3.Row, metadata: dict[str, Any]) -> bool:
    lifecycle = str(metadata.get("lifecycle") or "").lower()
    source = str(row["source"] or "").lower()
    return lifecycle == "candidate" or bool(metadata.get("event_digest")) or source in {"event-digest", "memory-candidate"}


def transition_candidate_metadata(
    metadata: dict[str, Any],
    *,
    action: str,
    actor: str,
    reason: str,
    timestamp: str = "",
    batch_id: str = "",
    superseded_by: str = "",
) -> dict[str, Any]:
    """Return one internally consistent candidate lifecycle transition."""

    action = str(action or "").strip().lower()
    if action not in {"promote", "archive", "supersede"}:
        raise ValueError(f"unsupported candidate review action: {action}")
    at = timestamp or now_iso()
    updated = dict(metadata)
    updated["candidate_reviewed_at"] = at
    updated["candidate_reviewed_by"] = actor
    updated["candidate_review_action"] = action
    automatic_admission = updated.get("automatic_admission")
    if isinstance(automatic_admission, Mapping):
        reviewed_admission = dict(automatic_admission)
        reviewed_admission["reviewed"] = True
        reviewed_admission["reviewed_at"] = at
        updated["automatic_admission"] = reviewed_admission
        updated["admission_reviewed_at"] = at
    if batch_id:
        updated["candidate_promotion_batch_id"] = batch_id
    if action == "promote":
        updated["lifecycle"] = "promoted"
        updated["candidate_status"] = "promoted"
        updated["review_status"] = "promoted"
        updated["promoted_at"] = at
        updated["promoted_by"] = actor
        updated["promotion_reason"] = reason
    elif action == "archive":
        updated["lifecycle"] = "archived"
        updated["candidate_status"] = "archived"
        updated["review_status"] = "archived"
        updated["archived_at"] = at
        updated["archived_by"] = actor
        updated["archive_reason"] = reason
    else:
        updated["lifecycle"] = "superseded"
        updated["candidate_status"] = "superseded"
        updated["review_status"] = "superseded"
        updated["superseded_by"] = superseded_by
        updated["superseded_at"] = at
        updated["superseded_by_actor"] = actor
        updated["supersede_reason"] = reason
    return updated


def cleanup_hidden_memory_companions(conn: sqlite3.Connection, memory_id: str) -> None:
    """Delete SQLite graph companions for a hidden durable-memory row."""

    conn.execute("DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,))
    conn.execute(
        "DELETE FROM memory_relations WHERE source_memory_id = ? OR target_memory_id = ?",
        (memory_id, memory_id),
    )


def _reviewed_metadata(row: sqlite3.Row, *, action: str, superseded_by: str = "", actor: str) -> dict[str, Any]:
    metadata = _json_loads(row["metadata"])
    return transition_candidate_metadata(
        metadata,
        action=action,
        actor=actor,
        reason="candidate_review_archive" if action == "archive" else "operator candidate review",
        superseded_by=superseded_by,
    )


def review_candidate(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    action: str,
    superseded_by: str = "",
    dry_run: bool = True,
    actor: str = "scope-recall:candidate-review",
    expected_updated_at: str = "",
    expected_lifecycle: str = "",
) -> dict[str, Any]:
    """Review one candidate using the shared lifecycle CAS transition."""

    action = str(action or "").strip().lower()
    if action not in {"promote", "archive", "supersede"}:
        return {"ok": False, "dry_run": dry_run, "status": "invalid", "error": f"unsupported action: {action}"}
    if action == "supersede" and not str(superseded_by or "").strip():
        return {"ok": False, "dry_run": dry_run, "status": "invalid", "error": "--superseded-by is required for supersede"}
    row = _load_row(conn, memory_id)
    if row is None:
        return {"ok": False, "dry_run": dry_run, "status": "not_found", "error": f"candidate not found: {memory_id}"}
    if action == "supersede":
        replacement = _load_row(conn, superseded_by)
        if replacement is None:
            return {"ok": False, "dry_run": dry_run, "status": "invalid_target", "error": f"superseded-by memory not found: {superseded_by}"}
        if lifecycle_is_hidden(_json_loads(replacement["metadata"])):
            return {"ok": False, "dry_run": dry_run, "status": "invalid_target", "error": f"superseded-by memory is hidden: {superseded_by}"}
    before_metadata = _json_loads(row["metadata"])
    if not _candidate_like(row, before_metadata):
        return {"ok": False, "dry_run": dry_run, "status": "invalid_state", "error": f"memory is not a candidate-like row: {memory_id}"}
    current_updated_at = str(row["updated_at"] or "")
    current_lifecycle = str(before_metadata.get("lifecycle") or "active").strip().lower()
    after_metadata = _reviewed_metadata(row, action=action, superseded_by=superseded_by, actor=actor)
    before = _snapshot(row, before_metadata)
    after = _snapshot(row, after_metadata)
    result = {
        "ok": True,
        "status": "planned" if dry_run else "pending",
        "dry_run": bool(dry_run),
        "action": action,
        "id": memory_id,
        "superseded_by": superseded_by,
        "before": before,
        "after": after,
        "applied": False,
        "expected_updated_at": current_updated_at,
        "expected_lifecycle": current_lifecycle,
        "version_token": {"updated_at": current_updated_at, "lifecycle": current_lifecycle},
    }
    if dry_run:
        return result

    ensure_schema(conn)
    try:
        transition = transition_memory_lifecycle(
            conn,
            memory_id=memory_id,
            lifecycle=str(after_metadata["lifecycle"]),
            metadata_updates=after_metadata,
            expected_updated_at=str(expected_updated_at or current_updated_at),
            expected_lifecycle=str(expected_lifecycle or current_lifecycle),
            actor=actor,
            reason="candidate_review_archive" if action == "archive" else "operator candidate review",
            operation_id={
                "archive": CANDIDATE_REVIEW_ARCHIVE,
                "promote": CANDIDATE_REVIEW_PROMOTE,
                "supersede": CANDIDATE_REVIEW_SUPERSEDE,
            }[action],
            batch_id=str(after_metadata.get("candidate_promotion_batch_id") or ""),
        )
        conn.commit()
    except LifecycleConflictError as exc:
        conn.rollback()
        return {
            "ok": False,
            "status": "conflict",
            "dry_run": False,
            "action": action,
            "id": memory_id,
            "applied": False,
            "conflict": exc.as_dict(),
            "error": str(exc),
        }
    except Exception:
        conn.rollback()
        raise
    result["status"] = "applied"
    result["applied"] = True
    result["updated_at"] = transition["updated_at"]
    result["after"] = _public_snapshot_payload(transition["after"])
    result["outbox_enqueued"] = transition["outbox_enqueued"]
    return result
