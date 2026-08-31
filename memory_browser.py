"""Read-only browser helpers for Scope Recall SQLite truth rows.

The browser is intentionally query-only: it lists and explains existing rows for
operator inspection without mutating memory, candidates, or vector companions.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .candidate_review import candidate_identity_fields
from .capture_filters import contains_secret_like_text, sanitize_mapping_key, sanitize_report_text
from .lifecycle_policy import ordinary_recall_lifecycle_visible

_CANDIDATE_SUMMARY_METADATA_KEYS = {
    "automatic_admission",
    "candidate_status",
    "confidence",
    "entities",
    "event_digest",
    "evidence_refs",
    "importance",
    "lifecycle",
    "memory_type",
    "origin_kind",
    "recommended_action",
    "review_status",
    "risk_flags",
    "tags",
}


def memory_db_path(hermes_home: Path) -> Path:
    return Path(hermes_home).expanduser().resolve() / "scope-recall" / "memory.sqlite3"


def open_readonly_memory_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Scope Recall memory DB not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _memory_columns(conn: sqlite3.Connection) -> list[str]:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    requested = ["id", "scope_id", "source", "target", "summary", "content", "created_at", "updated_at", "metadata"]
    return [column if column in existing else f"'' AS {column}" for column in requested]


def _parse_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sanitize_metadata(value: Any, *, key: str = "", derived_from_sensitive_content: bool = False) -> Any:
    """Redact secrets and private paths from metadata before operator display."""
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for item_key, item in value.items():
            safe_key, _ = sanitize_mapping_key(item_key)
            candidate = safe_key
            suffix = 2
            while candidate in output:
                candidate = f"{safe_key}#{suffix}"
                suffix += 1
            output[candidate] = _sanitize_metadata(
                item,
                key=safe_key,
                derived_from_sensitive_content=derived_from_sensitive_content,
            )
        return output
    if isinstance(value, list):
        return [_sanitize_metadata(item, key=key, derived_from_sensitive_content=derived_from_sensitive_content) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_metadata(item, key=key, derived_from_sensitive_content=derived_from_sensitive_content) for item in value]
    if isinstance(value, str):
        safe = sanitize_report_text(value)
        if derived_from_sensitive_content and key in {"entities", "tags"} and safe == value:
            return "[REDACTED_METADATA]"
        return safe
    return value


def _row_payload(row: sqlite3.Row, *, include_content: bool = False, raw: bool = False) -> dict[str, Any]:
    raw_metadata = _parse_json(row["metadata"])
    raw_content = str(row["content"] or "")
    metadata = raw_metadata if raw else _sanitize_metadata(raw_metadata, derived_from_sensitive_content=contains_secret_like_text(raw_content))
    identity = candidate_identity_fields(source=str(row["source"]), metadata=metadata)
    if not raw and "automatic_admission" in metadata:
        metadata = dict(metadata)
        metadata["automatic_admission"] = identity["automatic_admission"]
    safe_content = raw_content if raw else sanitize_report_text(raw_content)
    raw_summary = str(row["summary"] or "")
    payload: dict[str, Any] = {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"]),
        "source": str(row["source"]),
        "target": str(row["target"]),
        "summary": raw_summary if raw else sanitize_report_text(raw_summary),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "metadata": metadata,
        "lifecycle": str(metadata.get("lifecycle") or "active"),
        "memory_type": str(metadata.get("memory_type") or metadata.get("type") or ""),
        "redacted": (not raw) and (metadata != raw_metadata or safe_content != raw_content),
    }
    if include_content:
        payload["content"] = safe_content
    else:
        payload["content_chars"] = len(raw_content)
    payload.update(identity)
    return payload


def _compact_candidate_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_compact_candidate_value(item) for item in value[:6]]
    if isinstance(value, dict):
        return {str(key): _compact_candidate_value(item) for key, item in list(value.items())[:8]}
    if isinstance(value, str) and len(value) > 120:
        return value[:117].rstrip() + "..."
    return value


def _candidate_summary_payload(row: sqlite3.Row, *, raw: bool = False) -> dict[str, Any]:
    payload = _row_payload(row, raw=raw)
    metadata_value = payload.get("metadata")
    metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
    kept = {
        key: _compact_candidate_value(metadata[key])
        for key in sorted(_CANDIDATE_SUMMARY_METADATA_KEYS)
        if key in metadata
    }
    omitted = sorted(str(key) for key in metadata if key not in _CANDIDATE_SUMMARY_METADATA_KEYS)
    summary = str(payload.get("summary") or "")
    payload["summary_chars"] = len(summary)
    if len(summary) > 320:
        payload["summary"] = summary[:317].rstrip() + "..."
    payload["metadata"] = kept
    payload["metadata_chars"] = len(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    payload["metadata_omitted_keys"] = omitted[:12]
    payload["metadata_omitted_keys_count"] = len(omitted)
    return payload


def list_memories(conn: sqlite3.Connection, *, target: str = "", limit: int = 20, scope_id: str = "", raw: bool = False) -> dict[str, Any]:
    where = []
    params: list[Any] = []
    if target:
        where.append("target = ?")
        params.append(target)
    if scope_id:
        where.append("scope_id = ?")
        params.append(scope_id)
    sql = f"SELECT {', '.join(_memory_columns(conn))} FROM memories"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    rows = conn.execute(sql, params).fetchall()
    return {"ok": True, "read_only": True, "raw": raw, "count": len(rows), "memories": [_row_payload(row, raw=raw) for row in rows]}


def inspect_memory(conn: sqlite3.Connection, *, memory_id: str, raw: bool = False) -> dict[str, Any]:
    row = conn.execute(f"SELECT {', '.join(_memory_columns(conn))} FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return {"ok": False, "read_only": True, "error": f"memory not found: {memory_id}"}
    return {"ok": True, "read_only": True, "raw": raw, "memory": _row_payload(row, include_content=True, raw=raw)}


def list_candidates(
    conn: sqlite3.Connection,
    *,
    target: str = "",
    limit: int = 20,
    scope_id: str = "",
    raw: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    where = [
        """
        (
            (
                LOWER(COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.lifecycle') END, '')) = 'candidate'
                OR (
                    (
                        LOWER(source) IN ('event-digest', 'memory-candidate')
                        OR (json_valid(metadata) AND json_extract(metadata, '$.event_digest') IN (1, '1', 'true'))
                    )
                    AND LOWER(COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.lifecycle') END, '')) IN ('', 'candidate')
                )
            )
            AND LOWER(COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.lifecycle') END, ''))
                NOT IN ('promoted', 'archived', 'rejected', 'superseded', 'obsolete', 'in_progress')
            AND LOWER(COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.candidate_status') END, ''))
                NOT IN ('promoted', 'archived', 'rejected', 'superseded', 'obsolete', 'in_progress')
        )
        """
    ]
    params: list[Any] = []
    if target:
        where.append("target = ?")
        params.append(target)
    if scope_id:
        where.append("scope_id = ?")
        params.append(scope_id)
    sql = f"SELECT {', '.join(_memory_columns(conn))} FROM memories WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    rows = conn.execute(sql, params).fetchall()
    full_detail = bool(full or raw)
    candidates = [
        _row_payload(row, raw=raw) if full_detail else _candidate_summary_payload(row, raw=raw)
        for row in rows
    ]
    return {
        "ok": True,
        "read_only": True,
        "raw": raw,
        "detail": "full" if full_detail else "summary",
        "count": len(candidates),
        "candidates": candidates,
    }


def _tokens(query: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[\w\-]+", query, flags=re.UNICODE) if token.strip()]


def explain_recall(conn: sqlite3.Connection, *, query: str, limit: int = 5, scope_id: str = "") -> dict[str, Any]:
    tokens = _tokens(query)
    sql = f"SELECT {', '.join(_memory_columns(conn))} FROM memories"
    params: list[Any] = []
    if scope_id:
        sql += " WHERE scope_id = ?"
        params.append(scope_id)
    sql += " ORDER BY updated_at DESC LIMIT 200"
    rows = conn.execute(sql, params).fetchall()
    considered = 0
    lifecycle_filtered = 0
    scored: list[dict[str, Any]] = []
    for row in rows:
        item = _row_payload(row, include_content=True)
        lifecycle = str(item.get("lifecycle") or "").lower()
        target = str(item.get("target") or "").lower()
        source = str(item.get("source") or "").lower()
        if not ordinary_recall_lifecycle_visible(lifecycle=lifecycle, target=target) or source in {
            "event-digest",
            "memory-candidate",
        }:
            lifecycle_filtered += 1
            continue
        considered += 1
        haystack = f"{item.get('summary', '')}\n{item.get('content', '')}".lower()
        matched_terms = [token for token in tokens if token in haystack]
        lexical_score = (len(matched_terms) / max(1, len(tokens))) if tokens else 0.0
        if lexical_score <= 0.0:
            continue
        item.pop("content", None)
        item["explain"] = {
            "lexical_score": round(lexical_score, 4),
            "matched_terms": matched_terms,
            "vector_score": None,
            "rrf_contribution": None,
            "decision": "included_by_readonly_lexical_preview",
        }
        scored.append(item)
    scored.sort(key=lambda item: (float(item["explain"]["lexical_score"]), str(item.get("updated_at") or "")), reverse=True)
    returned = scored[: max(1, int(limit))]
    return {
        "ok": True,
        "read_only": True,
        "mode": "lexical-readonly-preview",
        "query": query,
        "count": len(returned),
        "trace": {
            "rows_scanned": len(rows),
            "considered_after_lifecycle": considered,
            "lifecycle_filtered": lifecycle_filtered,
            "scored_positive": len(scored),
        },
        "results": returned,
    }


def render_text(payload: dict[str, Any], *, key: str) -> str:
    if not payload.get("ok"):
        return f"error: {payload.get('error', 'unknown error')}"
    rows = payload.get(key) or payload.get("results") or []
    lines = [f"ok: {len(rows)} item(s), read_only={payload.get('read_only')}"]
    for item in rows:
        lines.append(f"- {item.get('id')} [{item.get('target')}] {item.get('summary')}")
    return "\n".join(lines)
