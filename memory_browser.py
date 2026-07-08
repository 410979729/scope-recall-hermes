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

from .capture_filters import contains_secret_like_text, sanitize_report_text

_HIDDEN_RECALL_LIFECYCLES = {"archived", "candidate", "in_progress", "obsolete", "rejected", "superseded"}


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
        return {
            str(item_key): _sanitize_metadata(
                item,
                key=str(item_key),
                derived_from_sensitive_content=derived_from_sensitive_content,
            )
            for item_key, item in value.items()
        }
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


def list_candidates(conn: sqlite3.Connection, *, target: str = "", limit: int = 20, scope_id: str = "", raw: bool = False) -> dict[str, Any]:
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
    candidates = [_row_payload(row, raw=raw) for row in rows]
    return {"ok": True, "read_only": True, "raw": raw, "count": len(candidates), "candidates": candidates}


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
        source = str(item.get("source") or "").lower()
        if lifecycle in _HIDDEN_RECALL_LIFECYCLES or source in {"event-digest", "memory-candidate"}:
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
