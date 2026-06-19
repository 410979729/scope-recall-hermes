from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Sequence

from .capture_filters import contains_secret_like_text, sanitize_report_text, should_capture_text
from .gating import compact_text
from .sql_store import ensure_schema

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


def build_forgetting_report(conn: sqlite3.Connection, *, accessible_scope_ids: Sequence[str], limit: int = 200) -> dict[str, Any]:
    """构建只读遗忘报告。

    默认只提出“软归档”候选；物理删除只用于明确敏感内容或运行噪声。
    """

    ensure_schema(conn)
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


def _archive_memory(conn: sqlite3.Connection, *, memory_id: str, reason: str, superseded_by: str = "") -> bool:
    row = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return False
    metadata = _json_loads(row["metadata"])
    if str(metadata.get("lifecycle") or "") == "archived":
        return False
    metadata["lifecycle"] = "archived"
    metadata["forget_reason"] = reason
    metadata["archived_at"] = _now_iso()
    if superseded_by:
        metadata["superseded_by"] = superseded_by
    conn.execute("UPDATE memories SET metadata = ?, updated_at = ? WHERE id = ?", (_json_dumps(metadata), _now_iso(), memory_id))
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
) -> dict[str, Any]:
    report = build_forgetting_report(conn, accessible_scope_ids=accessible_scope_ids, limit=limit)
    soft_items = report["soft_archive_candidates"]["items"]
    hard_items = report["hard_delete_candidates"]["items"] if hard_delete else []
    result = {
        "dry_run": bool(dry_run),
        "archived": len(soft_items),
        "deleted": len(hard_items),
        "archive_ids": [item["id"] for item in soft_items],
        "delete_ids": [item["id"] for item in hard_items],
    }
    if dry_run:
        return result
    archived = 0
    deleted = 0
    for item in soft_items:
        if _archive_memory(
            conn,
            memory_id=str(item["id"]),
            reason=str(item.get("reason") or "forgetting-run"),
            superseded_by=str(item.get("superseded_by") or ""),
        ):
            archived += 1
    for item in hard_items:
        if _delete_memory(conn, str(item["id"])):
            deleted += 1
    conn.commit()
    result["archived"] = archived
    result["deleted"] = deleted
    return result
