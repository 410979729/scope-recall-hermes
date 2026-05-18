from __future__ import annotations

import sqlite3
from typing import Any

from .gating import build_fts_query, compact_text, like_terms, query_tokens
from .models import RecallItem
from .scoring import lexical_score
from .sql_store import curated_recall_item_id, iter_curated_entries
from .vector_runtime import mark_vector_needs_repair


def _scope_placeholders(provider: Any) -> str:
    return ",".join("?" for _ in provider._accessible_scope_ids)


def _accessible_scope_params(provider: Any) -> list[str]:
    return [str(scope_id) for scope_id in provider._accessible_scope_ids]


def search_db_memories(provider: Any, query: str, *, limit: int) -> list[RecallItem]:
    conn = provider._require_conn()
    tokens = query_tokens(query)
    fts_query = build_fts_query(tokens)
    rows: list[sqlite3.Row] = []
    candidate_pool = max(limit * 2, limit)
    recent_scan_limit = max(
        candidate_pool,
        int((provider._retrieval_config or {}).get("candidate_pool") or candidate_pool) * 4,
        48,
    )
    with provider._lock:
        if fts_query:
            rows.extend(
                conn.execute(
                    """
                    SELECT m.*
                    FROM memories_fts
                    JOIN memories m ON m.id = memories_fts.memory_id
                    WHERE memories_fts MATCH ? AND m.scope_id IN ({})
                    ORDER BY m.updated_at DESC
                    LIMIT ?
                    """.format(_scope_placeholders(provider)),
                    [fts_query, *_accessible_scope_params(provider), candidate_pool],
                ).fetchall()
            )
        like_query_terms = like_terms(query, tokens)
        if like_query_terms:
            clause = " OR ".join(["content LIKE ?", "summary LIKE ?"] * len(like_query_terms))
            params: list[Any] = []
            for term in like_query_terms:
                needle = f"%{term}%"
                params.extend([needle, needle])
            params.extend([*_accessible_scope_params(provider), candidate_pool])
            rows.extend(
                conn.execute(
                    f"""
                    SELECT *
                    FROM memories
                    WHERE ({clause}) AND scope_id IN ({_scope_placeholders(provider)})
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            )
        if len(rows) < candidate_pool:
            rows.extend(
                conn.execute(
                    """
                    SELECT *
                    FROM memories
                    WHERE scope_id IN ({})
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """.format(_scope_placeholders(provider)),
                    [*_accessible_scope_params(provider), recent_scan_limit],
                ).fetchall()
            )

    dedup_rows: dict[str, sqlite3.Row] = {row["id"]: row for row in rows}
    min_score = float((provider._retrieval_config or {}).get("min_score") or provider._config_value("min_score", 0.18))
    results: list[RecallItem] = []
    for row in dedup_rows.values():
        score = lexical_score(
            query=query,
            content=row["content"],
            summary=row["summary"],
            source=row["source"],
            target=row["target"],
        )
        if score < min_score * 0.5:
            continue
        results.append(
            RecallItem(
                id=row["id"],
                content=row["content"],
                summary=row["summary"],
                source=row["source"],
                target=row["target"],
                score=score,
                updated_at=row["updated_at"],
                metadata={"lexical_score": score, "vector_score": 0.0, "scope_id": row["scope_id"]},
            )
        )
    return results


def search_vector_memories(provider: Any, query: str, *, limit: int) -> list[RecallItem]:
    if not provider._vector_ready or not provider._vector_store or not provider._embedder:
        return []
    try:
        query_vector = provider._embedder.embed(query)
        top_k = max(limit, int((provider._vector_config or {}).get("top_k") or limit))
        rows = []
        for scope_id in provider._accessible_scope_ids:
            rows.extend(provider._vector_store.search(query_vector, scope_id=scope_id, limit=top_k))
    except Exception as exc:
        mark_vector_needs_repair(provider, exc)
        return []
    threshold = float((provider._retrieval_config or {}).get("vector_min_score") or 0.12)
    results: list[RecallItem] = []
    for row in rows:
        distance = float(row.get("_distance") or 0.0)
        vector_score = max(0.0, 1.0 - distance)
        if vector_score < threshold:
            continue
        results.append(
            RecallItem(
                id=row["id"],
                content=row["content"],
                summary=row["summary"],
                source=row["source"],
                target=row["target"],
                score=vector_score,
                updated_at=row["updated_at"],
                metadata={"lexical_score": 0.0, "vector_score": vector_score, "scope_id": row.get("scope_id")},
            )
        )
    return results


def _curated_memory_allowed(provider: Any) -> bool:
    raw_cfg = (provider._config or {}).get("curated_memory", {})
    if raw_cfg is False:
        return False
    cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
    mode = str(cfg.get("mode") or "single-user").strip().lower()
    if mode in {"disabled", "off", "false", "none"}:
        return False

    user_id = str(getattr(provider, "_scope", None).user_id or "")
    allowed = [str(item).strip() for item in (cfg.get("allowed_user_ids") or []) if str(item).strip()]
    if allowed:
        return bool(user_id and user_id in allowed)
    if mode in {"explicit-users", "allow-list", "allowlist"}:
        return False
    if mode in {"profile-global", "global", "all-users"}:
        return True
    # Safe default: global curated files may be injected only when Hermes is not
    # running with an explicit gateway user id. Provider-owned SQLite rows remain
    # the scoped durable store for multi-user contexts.
    return not bool(user_id)


def search_curated_memories(provider: Any, query: str) -> list[RecallItem]:
    if not _curated_memory_allowed(provider):
        return []
    min_score = float((provider._retrieval_config or {}).get("min_score") or provider._config_value("min_score", 0.18))
    results: list[RecallItem] = []
    for target, content, updated_at in iter_curated_entries(provider._hermes_home):
        summary = compact_text(content, 220)
        score = lexical_score(
            query=query,
            content=content,
            summary=summary,
            source="builtin-curated",
            target=target,
        )
        if score < min_score:
            continue
        results.append(
            RecallItem(
                id=curated_recall_item_id(target, content),
                content=content,
                summary=summary,
                source="builtin-curated",
                target=target,
                score=score,
                updated_at=updated_at,
                metadata={"lexical_score": score, "vector_score": 0.0},
            )
        )
    return results
