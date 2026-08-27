"""Forgetting and governance reporting for duplicate, stale, or low-value memories.

Default actions are soft-archive and dry-run oriented so operators can review rollback material before destructive cleanup."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Any, Protocol, Sequence

from .capture_filters import contains_secret_like_text, sanitize_report_text, should_capture_text
from .gating import compact_text
from .lifecycle_registry import FORGETTING_ARCHIVE, HARD_DELETE_FORGETTING
from .lifecycle_service import hard_delete_memories, transition_memory_lifecycle
from .maintenance_ops import json_dumps_stable, make_batch_id, now_utc_iso
from .response_schemas import FORGETTING_REPORT_SCHEMA_VERSION, FORGETTING_RUN_SCHEMA_VERSION
from .sql_store import ensure_schema


class VectorDeleteStore(Protocol):
    def delete_by_ids(self, ids: list[str]) -> None: ...

VERY_SHORT_CHARS = 12

_DEFAULT_FORGETTING_POLICY: dict[str, bool] = {
    "enabled": True,
    "soft_archive_default": True,
    "archive_very_short": True,
    "archive_assistant_scratch": True,
    "archive_duplicates": True,
    "hard_delete_sensitive": False,
}


def _forgetting_policy(config: dict[str, Any] | None) -> dict[str, bool]:
    raw = config if isinstance(config, dict) else {}
    policy: dict[str, bool] = {}
    for key, default in _DEFAULT_FORGETTING_POLICY.items():
        value = raw.get(key, default)
        if isinstance(value, str):
            policy[key] = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            policy[key] = bool(value)
    return policy


def _now_iso() -> str:
    return now_utc_iso()


def _json_loads(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(str(raw))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _json_dumps(value: Any) -> str:
    return json_dumps_stable(value)


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


def _scoped_rows(conn: sqlite3.Connection, accessible_scope_ids: Sequence[str] | None) -> list[sqlite3.Row]:
    if accessible_scope_ids is None:
        return conn.execute(
            """
            SELECT id, scope_id, source, target, content, summary, created_at, updated_at, dedup_key, metadata
            FROM memories
            ORDER BY updated_at DESC, id ASC
            """
        ).fetchall()
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


def build_forgetting_report(
    conn: sqlite3.Connection,
    *,
    accessible_scope_ids: Sequence[str] | None,
    limit: int = 200,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建只读遗忘报告。

    默认只提出“软归档”候选；物理删除只用于明确敏感内容或运行噪声。
    """

    policy = _forgetting_policy(config)
    if not policy["enabled"]:
        empty = _limited([], limit)
        return {
            "schema_version": FORGETTING_REPORT_SCHEMA_VERSION,
            "enabled": False,
            "policy": policy,
            "total_rows": 0,
            "soft_archive_candidates": empty,
            "hard_delete_candidates": empty,
            "review_debt": empty,
            "duplicate_groups": empty,
        }
    rows = _scoped_rows(conn, accessible_scope_ids)
    soft_by_id: dict[str, dict[str, Any]] = {}
    hard_by_id: dict[str, dict[str, Any]] = {}
    review_debt_by_id: dict[str, dict[str, Any]] = {}
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
        if (
            policy["archive_assistant_scratch"]
            and target == "general"
            and source == "turn-assistant"
        ):
            soft_by_id.setdefault(str(row["id"]), _preview(row, reason="assistant-prose-scratch"))
        if _journal_template_transcript_noise(row):
            soft_by_id.setdefault(str(row["id"]), _preview(row, reason="journal-template-transcript-noise"))
        if policy["archive_very_short"] and len(content.strip()) <= VERY_SHORT_CHARS:
            soft_by_id.setdefault(str(row["id"]), _preview(row, reason="very-short-low-value"))
        metadata = _json_loads(row["metadata"])
        row_lifecycle = str(metadata.get("lifecycle") or "").strip().lower()
        expires_at = str(metadata.get("expires_at") or "").strip().lower()
        candidate_status = str(metadata.get("candidate_status") or "").strip().lower()
        if row_lifecycle == "candidate":
            if candidate_status in {"rejected_low_value", "candidate_expired"}:
                soft_by_id.setdefault(str(row["id"]), _preview(row, reason=f"candidate-{candidate_status.replace('_', '-')}"))
            else:
                review_debt_by_id.setdefault(str(row["id"]), _preview(row, reason="candidate-review-debt"))
        elif expires_at == "stale-review":
            review_debt_by_id.setdefault(str(row["id"]), _preview(row, reason="stale-review-needs-freshness-validation"))

    duplicate_groups: list[dict[str, Any]] = []
    if not policy["archive_duplicates"]:
        duplicate_map.clear()
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
    review_debt = list(review_debt_by_id.values())
    return {
        "schema_version": FORGETTING_REPORT_SCHEMA_VERSION,
        "enabled": True,
        "policy": policy,
        "total_rows": len(rows),
        "soft_archive_candidates": _limited(soft, limit),
        "hard_delete_candidates": _limited(hard, limit),
        "review_debt": _limited(review_debt, limit),
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
    row = conn.execute(
        "SELECT id, updated_at, metadata FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return False
    metadata = _json_loads(row["metadata"])
    if str(metadata.get("lifecycle") or "") == "archived":
        return False
    at = _now_iso()
    updates: dict[str, Any] = {
        **metadata,
        "forget_reason": reason,
        "archived_at": at,
        "archived_by": actor,
    }
    if batch_id:
        updates["rollback_batch_id"] = batch_id
    if superseded_by:
        updates["superseded_by"] = superseded_by
    transition_memory_lifecycle(
        conn,
        memory_id=memory_id,
        lifecycle="archived",
        metadata_updates=updates,
        expected_updated_at=str(row["updated_at"] or ""),
        actor=actor,
        reason=reason,
        operation_id=FORGETTING_ARCHIVE,
        batch_id=batch_id,
        timestamp=at,
    )
    return True


def run_forgetting(
    conn: sqlite3.Connection,
    *,
    accessible_scope_ids: Sequence[str],
    dry_run: bool = True,
    hard_delete: bool = False,
    soft_archive: bool | None = None,
    config: dict[str, Any] | None = None,
    limit: int = 200,
    vector_store: VectorDeleteStore | None = None,
    allow_sql_delete_without_vector: bool = False,
    batch_id: str | None = None,
    actor: str = "scope-recall-forgetting",
) -> dict[str, Any]:
    """Execute a forgetting plan in dry-run or apply mode.

    Default behavior is soft archive with rollback evidence; hard delete paths are intentionally explicit because durable memory cleanup must be auditable and reversible where possible."""
    policy = _forgetting_policy(config)
    if not policy["enabled"]:
        return {
            "schema_version": FORGETTING_RUN_SCHEMA_VERSION,
            "enabled": False,
            "dry_run": bool(dry_run),
            "archived": 0,
            "deleted": 0,
            "archive_ids": [],
            "delete_ids": [],
        }
    if not dry_run:
        ensure_schema(conn)
    report = build_forgetting_report(
        conn,
        accessible_scope_ids=accessible_scope_ids,
        limit=limit,
        config=policy,
    )
    batch = batch_id or make_batch_id("forgetting")
    soft_archive_enabled = (
        policy["soft_archive_default"]
        if soft_archive is None
        else bool(soft_archive)
    )
    hard_delete_requested = bool(hard_delete)
    hard_delete_allowed = policy["hard_delete_sensitive"]
    soft_items = (
        report["soft_archive_candidates"]["items"]
        if soft_archive_enabled
        else []
    )
    hard_items = (
        report["hard_delete_candidates"]["items"]
        if hard_delete_requested and hard_delete_allowed
        else []
    )
    review_debt_items = report.get("review_debt", {}).get("items", [])
    result = {
        "schema_version": FORGETTING_RUN_SCHEMA_VERSION,
        "enabled": True,
        "dry_run": bool(dry_run),
        "batch_id": batch,
        "soft_archive_enabled": soft_archive_enabled,
        "hard_delete_requested": hard_delete_requested,
        "hard_delete_allowed": hard_delete_allowed,
        "archived": len(soft_items),
        "deleted": len(hard_items),
        "review_debt": len(review_debt_items),
        "archive_ids": [item["id"] for item in soft_items],
        "delete_ids": [item["id"] for item in hard_items],
    }
    if hard_delete_requested and not hard_delete_allowed:
        result["policy_error"] = (
            "hard delete refused: forgetting.hard_delete_sensitive must be true "
            "and hard_delete must be explicitly requested"
        )
    if dry_run:
        return result
    archived = 0
    archived_ids: list[str] = []
    now = _now_iso()
    for item in soft_items:
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
    if archived:
        conn.commit()
    # Lifecycle transition already committed one causal vector-delete intent.
    # This maintenance path may inspect store availability, but never mutates
    # the physical companion directly.
    archived_vector_deleted = 0
    vector_error = ""
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
    deleted = 0
    if deleted_ids:
        try:
            hard_result = hard_delete_memories(
                conn,
                memory_ids=deleted_ids,
                scope_ids=accessible_scope_ids,
                vector_delete=None,
                require_vector_delete=not allow_sql_delete_without_vector,
                actor=actor,
                reason="secret-like-content",
                operation_id=HARD_DELETE_FORGETTING,
                batch_id=batch,
                timestamp=now,
            )
            deleted = int(hard_result["deleted"])
            deleted_ids = [str(memory_id) for memory_id in hard_result["ids"]]
            hard_vector_error = str(hard_result.get("vector_error") or "")
            if hard_vector_error:
                vector_error = hard_vector_error
            vector_deleted = 0
            result["vector_status"] = str(hard_result.get("vector_status") or "")
            result["vector_pending"] = bool(hard_result.get("vector_pending"))
        except Exception as exc:
            vector_error = sanitize_report_text(str(exc))
            deleted_ids = []
    result["archived"] = archived
    result["archived_vector_deleted"] = archived_vector_deleted
    result["deleted"] = deleted
    result["delete_ids"] = deleted_ids
    result["vector_deleted"] = vector_deleted
    if vector_error:
        result["vector_error"] = vector_error
    return result
