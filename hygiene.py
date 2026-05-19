from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .capture_filters import should_capture_text
from .gating import compact_text
from .governance import extract_candidates
from .sql_store import fts_integrity_report

VERY_SHORT_CHARS = 12
VERY_LONG_CHARS = 2500


def _limited(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    return {"count": len(items), "items": items[: max(0, int(limit))]}


def _preview(row: Any) -> dict[str, Any]:
    content = str(row["content"] or "")
    return {
        "id": str(row["id"]),
        "target": str(row["target"]),
        "source": str(row["source"]),
        "updated_at": str(row["updated_at"]),
        "chars": len(content),
        "preview": compact_text(content, 180),
    }


def _vector_records(vector_store: Any) -> dict[str, dict[str, Any]]:
    if vector_store is None or not hasattr(vector_store, "list_records"):
        return {}
    try:
        records = vector_store.list_records()
    except Exception:
        return {}
    if isinstance(records, dict):
        return {str(key): dict(value) for key, value in records.items() if key}
    return {}


def build_hygiene_report(conn: Any, vector_store: Any = None, limit: int = 200) -> dict[str, Any]:
    """Build a read-only, JSON-friendly memory hygiene report.

    The report intentionally performs no cleanup. It points operators at likely
    noise, duplicate, promotion, and deletion candidates while keeping SQLite as
    the untouched source of truth.
    """
    rows = conn.execute(
        """
        SELECT id, scope_id, source, target, content, summary, created_at, updated_at, dedup_key, metadata
        FROM memories
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()

    totals_by_target = Counter(str(row["target"] or "") for row in rows)
    runtime_noise: list[dict[str, Any]] = []
    assistant_rows: list[dict[str, Any]] = []
    very_short: list[dict[str, Any]] = []
    very_long: list[dict[str, Any]] = []
    promotion_candidates: list[dict[str, Any]] = []
    delete_candidates: dict[str, dict[str, Any]] = {}

    duplicate_map: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for row in rows:
        target = str(row["target"] or "")
        source = str(row["source"] or "")
        content = str(row["content"] or "")
        key = str(row["dedup_key"] or "")
        duplicate_map[(str(row["scope_id"] or ""), target, key)].append(row)

        capture_result = should_capture_text(content)
        preview = _preview(row)
        if not capture_result.allowed and capture_result.reason.startswith("skip-pattern:"):
            item = dict(preview, reason=capture_result.reason)
            runtime_noise.append(item)
            delete_candidates.setdefault(str(row["id"]), dict(item, reason="runtime-wrapper-noise"))
        if target == "general" and source == "turn-assistant":
            assistant_rows.append(preview)
            delete_candidates.setdefault(str(row["id"]), dict(preview, reason="assistant-prose-scratch"))
        if len(content.strip()) <= VERY_SHORT_CHARS:
            very_short.append(preview)
        if len(content) >= VERY_LONG_CHARS:
            very_long.append(preview)
        if target == "general" and capture_result.allowed:
            extracted = extract_candidates(content)
            if extracted:
                promotion_candidates.append(
                    dict(
                        preview,
                        suggested_targets=sorted({candidate.target for candidate in extracted}),
                        suggested_count=len(extracted),
                    )
                )

    duplicate_groups: list[dict[str, Any]] = []
    for (scope_id, target, key), group_rows in duplicate_map.items():
        if not key or len(group_rows) <= 1:
            continue
        members = [_preview(row) for row in sorted(group_rows, key=lambda row: (str(row["updated_at"]), str(row["id"])), reverse=True)]
        duplicate_groups.append(
            {
                "scope_id": scope_id,
                "target": target,
                "dedup_key": key,
                "count": len(members),
                "keep_id": members[0]["id"],
                "delete_ids": [member["id"] for member in members[1:]],
                "preview": members[0]["preview"],
                "members": members[: max(0, int(limit))],
            }
        )
    duplicate_groups.sort(key=lambda group: group["count"], reverse=True)

    sqlite_targets_by_id = {str(row["id"]): str(row["target"] or "") for row in rows}
    records = _vector_records(vector_store)
    general_vector_rows = []
    for memory_id, record in records.items():
        target = str(record.get("target") or sqlite_targets_by_id.get(memory_id) or "")
        if target == "general":
            general_vector_rows.append(
                {
                    "id": memory_id,
                    "target": target,
                    "updated_at": str(record.get("updated_at") or ""),
                    "preview": compact_text(str(record.get("content") or record.get("summary") or ""), 180),
                }
            )

    likely_delete = list(delete_candidates.values())
    return {
        "total_rows": len(rows),
        "totals_by_target": dict(sorted(totals_by_target.items())),
        "fts_index": fts_integrity_report(conn),
        "runtime_wrapper_noise": _limited(runtime_noise, limit),
        "assistant_prose_rows": _limited(assistant_rows, limit),
        "duplicate_dedupe_keys": _limited(duplicate_groups, limit),
        "very_short_rows": _limited(very_short, limit),
        "very_long_rows": _limited(very_long, limit),
        "general_vector_rows": _limited(general_vector_rows, limit),
        "likely_promotion_candidates": _limited(promotion_candidates, limit),
        "likely_delete_candidates": _limited(likely_delete, limit),
    }


def build_provider_hygiene_report(provider: Any, *, limit: int = 200) -> dict[str, Any]:
    with provider._lock:
        return build_hygiene_report(provider._require_conn(), vector_store=provider._vector_store, limit=limit)
