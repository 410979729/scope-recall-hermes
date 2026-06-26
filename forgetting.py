from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence

from .capture_filters import contains_secret_like_text, sanitize_report_text, should_capture_text
from .gating import compact_text
from .graph import sync_memory_entities
from .sql_store import delete_rows, ensure_schema, record_governance_audit_event


class VectorDeleteStore(Protocol):
    def delete_by_ids(self, ids: list[str]) -> None: ...

VERY_SHORT_CHARS = 12


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_loads(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(str(raw))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _limited(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    return {"count": len(items), "items": items[: max(0, int(limit))]}


def _preview(row: sqlite3.Row, *, reason: str, superseded_by: str = "") -> dict[str, Any]:
    item = {
        "id": str(row["id"]),
        "target": str(row["target"] or ""),
        "source": str(row["source"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "reason": reason,
        "preview": compact_text(sanitize_report_text(str(row["content"] or "")), 180),
    }
    if superseded_by:
        item["superseded_by"] = superseded_by
    return item


def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"] or ""),
        "source": str(row["source"] or ""),
        "target": str(row["target"] or ""),
        "summary": str(row["summary"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "metadata": _json_loads(row["metadata"]),
    }


def _scoped_rows(conn: sqlite3.Connection, accessible_scope_ids: Sequence[str]) -> list[sqlite3.Row]:
    scopes = [str(scope_id) for scope_id in accessible_scope_ids if str(scope_id)]
    if not scopes:
        return []
    placeholders = ",".join("?" for _ in scopes)
    return conn.execute(
        f"""
        SELECT id, scope_id, source, target, content, summary, created_at, updated_at, dedup_key, metadata
        FROM memories
        WHERE scope_id IN ({placeholders})
        ORDER BY updated_at DESC, id ASC
        """,
        scopes,
    ).fetchall()


def _already_archived(row: sqlite3.Row) -> bool:
    return str(_json_loads(row["metadata"]).get("lifecycle") or "") == "archived"


def _journal_template_transcript_noise(row: sqlite3.Row) -> bool:
    source = str(row["source"] or "")
    if source != "journal-digest":
        return False
    content = str(row["content"] or "")
    lowered = content.lower()
    template_prefix = lowered.startswith("operations workflow summary from journal digest:") or lowered.startswith("journal digest memory")
    role_transcript = bool(re.search(r"(?:^|[\s。；;])(?:user|assistant):", lowered))
    return template_prefix or role_transcript


def build_forgetting_report(conn: sqlite3.Connection, *, accessible_scope_ids: Sequence[str], limit: int = 200) -> dict[str, Any]:
    """构建只读遗忘报告。

    默认只提出“软归档”候选；物理删除只用于明确敏感内容或运行噪声。
    """

    rows = _scoped_rows(conn, accessible_scope_ids)
    soft_by_id: dict[str, dict[str, Any]] = {}
    hard_by_id: dict[str, dict[str, Any]] = {}
    duplicate_map: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)

    for row in rows:
        key = str(row["dedup_key"] or "")
        if key:
            duplicate_map[(str(row["scope_id"]), str(row["target"]), key)].append(row)
        if _already_archived(row):
            continue
        content = str(row["content"] or "")
        target = str(row["target"] or "")
        source = str(row["source"] or "")
        capture = should_capture_text(content)
        if contains_secret_like_text(content):
            hard_by_id.setdefault(str(row["id"]), _preview(row, reason="secret-like-content"))
            continue
        if not capture.allowed and capture.reason.startswith("skip-pattern:"):
            hard_by_id.setdefault(str(row["id"]), _preview(row, reason="runtime-wrapper-noise"))
            continue
        if target == "general" and source == "turn-assistant":
            soft_by_id.setdefault(str(row["id"]), _preview(row, reason="assistant-prose-scratch"))
        if _journal_template_transcript_noise(row):
            soft_by_id.setdefault(str(row["id"]), _preview(row, reason="journal-template-transcript-noise"))
        if len(content.strip()) <= VERY_SHORT_CHARS:
            soft_by_id.setdefault(str(row["id"]), _preview(row, reason="very-short-low-value"))
        metadata = _json_loads(row["metadata"])
        if str(metadata.get("expires_at") or "") == "stale-review" or str(metadata.get("lifecycle") or "") == "candidate":
            soft_by_id.setdefault(str(row["id"]), _preview(row, reason="stale-review-candidate"))

    duplicate_groups: list[dict[str, Any]] = []
    for (scope_id, target, key), group in duplicate_map.items():
        active = [row for row in group if not _already_archived(row)]
        if len(active) <= 1:
            continue
        # Keep the oldest stable id so repeated runs converge deterministically.
        ordered = sorted(active, key=lambda row: (str(row["created_at"]), str(row["id"])))
        keep = ordered[0]
        members = [_preview(row, reason="duplicate-memory", superseded_by=str(keep["id"])) for row in ordered]
        duplicate_groups.append(
            {
                "scope_id": scope_id,
                "target": target,
                "dedup_key": key,
                "keep_id": str(keep["id"]),
                "archive_ids": [str(row["id"]) for row in ordered[1:]],
                "members": members,
            }
        )
        for row in ordered[1:]:
            soft_by_id.setdefault(str(row["id"]), _preview(row, reason="duplicate-memory", superseded_by=str(keep["id"])))

    soft = list(soft_by_id.values())
    hard = list(hard_by_id.values())
    return {
        "total_rows": len(rows),
        "soft_archive_candidates": _limited(soft, limit),
        "hard_delete_candidates": _limited(hard, limit),
        "duplicate_groups": _limited(duplicate_groups, limit),
    }


def _archive_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    reason: str,
    superseded_by: str = "",
    batch_id: str = "",
    actor: str = "scope-recall-forgetting",
) -> bool:
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return False
    metadata = _json_loads(row["metadata"])
    if str(metadata.get("lifecycle") or "") == "archived":
        return False
    metadata["lifecycle"] = "archived"
    metadata["forget_reason"] = reason
    metadata["archived_at"] = _now_iso()
    metadata["archived_by"] = actor
    if batch_id:
        metadata["rollback_batch_id"] = batch_id
    if superseded_by:
        metadata["superseded_by"] = superseded_by
    conn.execute("UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ?", (_json_dumps(metadata), _now_iso(), memory_id))
    conn.execute("DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,))
    conn.execute("DELETE FROM memory_relations WHERE source_memory_id = ? OR target_memory_id = ?", (memory_id, memory_id))
    return True


def _delete_memory(conn: sqlite3.Connection, memory_id: str) -> bool:
    if conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is None:
        return False
    conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
    conn.execute("DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,))
    conn.execute("DELETE FROM memory_feedback WHERE memory_id = ?", (memory_id,))
    conn.execute("DELETE FROM memory_relations WHERE source_memory_id = ? OR target_memory_id = ?", (memory_id, memory_id))
    conn.execute("DELETE FROM memory_journal_sources WHERE memory_id = ?", (memory_id,))
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    return True


def run_forgetting(
    conn: sqlite3.Connection,
    *,
    accessible_scope_ids: Sequence[str],
    dry_run: bool = True,
    hard_delete: bool = False,
    limit: int = 200,
    vector_store: VectorDeleteStore | None = None,
    allow_sql_delete_without_vector: bool = False,
    batch_id: str | None = None,
    actor: str = "scope-recall-forgetting",
) -> dict[str, Any]:
    if not dry_run:
        ensure_schema(conn)
    report = build_forgetting_report(conn, accessible_scope_ids=accessible_scope_ids, limit=limit)
    batch = batch_id or f"forgetting-{uuid.uuid4().hex}"
    soft_items = report["soft_archive_candidates"]["items"]
    hard_items = report["hard_delete_candidates"]["items"] if hard_delete else []
    result = {
        "dry_run": bool(dry_run),
        "batch_id": batch,
        "archived": len(soft_items),
        "deleted": len(hard_items),
        "archive_ids": [item["id"] for item in soft_items],
        "delete_ids": [item["id"] for item in hard_items],
    }
    if dry_run:
        return result
    archived = 0
    archived_ids: list[str] = []
    archive_rollback_snapshots: dict[str, dict[str, Any]] = {}
    now = _now_iso()
    for item in soft_items:
        row = conn.execute(
            "SELECT id, scope_id, source, target, summary, updated_at, metadata FROM memories WHERE id = ?",
            (str(item["id"]),),
        ).fetchone()
        before = _snapshot(row) if row is not None else {}
        if _archive_memory(
            conn,
            memory_id=str(item["id"]),
            reason=str(item.get("reason") or "forgetting-run"),
            superseded_by=str(item.get("superseded_by") or ""),
            batch_id=batch,
            actor=actor,
        ):
            archived += 1
            archived_ids.append(str(item["id"]))
            archive_rollback_snapshots[str(item["id"])] = before
            after_row = conn.execute(
                "SELECT id, scope_id, source, target, summary, updated_at, metadata FROM memories WHERE id = ?",
                (str(item["id"]),),
            ).fetchone()
            record_governance_audit_event(
                conn,
                event_id=f"gov_{uuid.uuid4().hex}",
                event_type="forgetting",
                action="soft_archive",
                scope_id=str(item.get("scope_id") or before.get("scope_id") or ""),
                target_id=str(item["id"]),
                batch_id=batch,
                before=before,
                after=_snapshot(after_row) if after_row is not None else {},
                reason=str(item.get("reason") or "forgetting-run"),
                actor=actor,
                dry_run=False,
                created_at=now,
            )
    archived_vector_deleted = 0
    vector_error = ""
    if archived_ids and vector_store is not None:
        try:
            vector_store.delete_by_ids(archived_ids)
            archived_vector_deleted = len(archived_ids)
        except Exception as exc:
            conn.rollback()
            for memory_id, before in archive_rollback_snapshots.items():
                before_metadata = before.get("metadata") if isinstance(before.get("metadata"), dict) else {}
                current = conn.execute("SELECT content, target FROM memories WHERE id = ?", (memory_id,)).fetchone()
                conn.execute(
                    "UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ?",
                    (_json_dumps(before_metadata), str(before.get("updated_at") or now), memory_id),
                )
                if current is not None:
                    sync_memory_entities(
                        conn,
                        memory_id=memory_id,
                        content=str(current["content"] or ""),
                        target=str(current["target"] or ""),
                        metadata=dict(before_metadata or {}),
                    )
            conn.execute("DELETE FROM governance_audit_events WHERE batch_id = ? AND event_type = 'forgetting' AND action = 'soft_archive'", (batch,))
            conn.commit()
            vector_error = sanitize_report_text(str(exc))
            result["archived"] = 0
            result["deleted"] = 0
            result["archived_vector_deleted"] = 0
            result["vector_deleted"] = 0
            result["vector_error"] = vector_error
            result["archive_ids"] = []
            return result
    if archived:
        conn.commit()
    deleted_ids = [str(item["id"]) for item in hard_items if str(item.get("id") or "")]
    vector_deleted = 0
    if deleted_ids and vector_store is None and not allow_sql_delete_without_vector:
        # Hard delete is destructive while vectors are rebuildable leak surfaces.
        # Fail closed unless the operator explicitly accepts SQL-only deletion;
        # otherwise a future direct script call can leave stale vector hits after
        # SQLite truth has already been removed.
        vector_error = "hard delete refused: vector_store is required before deleting SQLite truth"
        result["archived"] = archived
        result["deleted"] = 0
        result["vector_deleted"] = 0
        result["vector_error"] = vector_error
        result["delete_ids"] = []
        return result
    if deleted_ids and vector_store is not None:
        try:
            # Delete the rebuildable companion first. If companion deletion fails,
            # keep SQLite truth intact so the row can be retried or repaired from
            # the authoritative store instead of leaving a stale vector-only leak.
            vector_store.delete_by_ids(deleted_ids)
            vector_deleted = len(deleted_ids)
        except Exception as exc:
            vector_error = sanitize_report_text(str(exc))
            result["archived"] = archived
            result["deleted"] = 0
            result["vector_deleted"] = 0
            result["vector_error"] = vector_error
            result["delete_ids"] = []
            return result
    delete_snapshots: dict[str, dict[str, Any]] = {}
    for memory_id in deleted_ids:
        row = conn.execute(
            "SELECT id, scope_id, source, target, summary, updated_at, metadata FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if row is not None:
            delete_snapshots[memory_id] = _snapshot(row)
    deleted = delete_rows(conn, deleted_ids)
    for memory_id, before in delete_snapshots.items():
        record_governance_audit_event(
            conn,
            event_id=f"gov_{uuid.uuid4().hex}",
            event_type="forgetting",
            action="hard_delete",
            scope_id=str(before.get("scope_id") or ""),
            target_id=memory_id,
            batch_id=batch,
            before=before,
            after={"id": memory_id, "deleted": True},
            reason="secret-like-content",
            actor=actor,
            dry_run=False,
            created_at=now,
        )
    if delete_snapshots:
        conn.commit()
    result["archived"] = archived
    result["archived_vector_deleted"] = archived_vector_deleted
    result["deleted"] = deleted
    result["vector_deleted"] = vector_deleted
    if vector_error:
        result["vector_error"] = vector_error
    return result
