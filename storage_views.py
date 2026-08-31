"""Read views over curated files, SQLite truth rows, and vector companion hits.

These views apply lifecycle and visibility filters before recall merges candidates."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from .gating import build_fts_query, compact_text, like_terms, normalized_token_set, retrieval_query_tokens
from .governance import classify_memory
from .graph import load_metadata
from .lifecycle_policy import ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES, ordinary_recall_lifecycle_visible_sql
from .lexical_generation import LEXICAL_POSTINGS_TABLE, supplemental_table_for_search
from .lexical_query import (
    cjk_query_ngrams,
    cjk_substring_score,
    trigram_fts_query,
)
from .models import RecallItem
from .scoring import bm25_to_score, lexical_score
from .sql_store import curated_recall_item_id, iter_curated_entries
from .sqlite_params import chunked_sql_parameters
from .vector_runtime import mark_vector_needs_repair

# Defensive retrieval boundary: lifecycle filtering must happen in the candidate
# SQL/vector-adapter layer, not only after merge/dedupe. Fresh archived rows can
# otherwise consume LIMIT budget or suppress active duplicates.
_RECALL_HIDDEN_LIFECYCLE_VALUES = ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES
_RECALL_HIDDEN_LIFECYCLE_SET = set(_RECALL_HIDDEN_LIFECYCLE_VALUES)


def _recall_lifecycle_visible_sql(alias: str) -> str:
    return ordinary_recall_lifecycle_visible_sql(alias)


_ACTIVE_MEMORY_SQL = _recall_lifecycle_visible_sql("memories")
_ACTIVE_MEMORY_SQL_M = _recall_lifecycle_visible_sql("m")


def _scope_placeholders(provider: Any) -> str:
    return ",".join("?" for _ in provider._accessible_scope_ids)


def _accessible_scope_params(provider: Any) -> list[str]:
    return [str(scope_id) for scope_id in provider._accessible_scope_ids]


def _partition_cjk_bigram_terms(
    conn: sqlite3.Connection,
    supplemental_table: str,
    terms: list[str],
) -> tuple[list[str], frozenset[str]]:
    """Partition query bigrams with bounded document-frequency probes.

    The postings primary key starts with ``term``, and every probe stops at
    ``df_cap + 1``.  A corpus-wide term therefore cannot fan out query planning
    merely so the caller can learn that it is common.
    """

    if not terms:
        return [], frozenset()
    document_total = int(
        conn.execute(f"SELECT COUNT(*) FROM {supplemental_table}").fetchone()[0]
    )
    df_cap = max(50, int(document_total * 0.05))
    probe_limit = df_cap + 1
    rare_terms: list[str] = []
    common_terms: set[str] = set()
    for term in terms:
        document_frequency = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM (
                    SELECT 1
                    FROM {LEXICAL_POSTINGS_TABLE}
                    WHERE term = ?
                    LIMIT ?
                )
                """,
                (term, probe_limit),
            ).fetchone()[0]
        )
        if document_frequency <= df_cap:
            rare_terms.append(term)
        else:
            common_terms.add(term)
    return rare_terms, frozenset(common_terms)


def _like_fallback_scan_limit(provider: Any, candidate_pool: int) -> int:
    """Return the hard per-scope row window inspected by LIKE fallback."""

    config = provider._retrieval_config or {}
    default = max(64, max(1, int(candidate_pool)) * 8)
    try:
        configured = int(config.get("like_fallback_scan_limit", default))
    except (TypeError, ValueError):
        configured = default
    return max(1, min(configured, 2_000))


def _bounded_like_fallback_rows(
    conn: sqlite3.Connection,
    provider: Any,
    terms: list[str],
    *,
    result_limit: int,
    scan_limit: int,
) -> list[sqlite3.Row]:
    """Apply leading-wildcard LIKE only after an indexed recent-row bound."""

    if not terms or result_limit <= 0:
        return []
    clause = " OR ".join(
        ["recent.content LIKE ?", "recent.summary LIKE ?"] * len(terms)
    )
    needles: list[str] = []
    for term in terms:
        needle = f"%{term}%"
        needles.extend([needle, needle])
    rows: list[sqlite3.Row] = []
    for scope_id in _accessible_scope_params(provider):
        rows.extend(
            conn.execute(
                f"""
                SELECT *
                FROM (
                    SELECT *
                    FROM memories INDEXED BY idx_scope_recall_scope_updated
                    WHERE scope_id = ? AND {_ACTIVE_MEMORY_SQL}
                    ORDER BY updated_at DESC
                    LIMIT ?
                ) AS recent
                WHERE ({clause})
                ORDER BY recent.updated_at DESC
                LIMIT ?
                """,
                [scope_id, scan_limit, *needles, result_limit],
            ).fetchall()
        )
    deduped = {str(row["id"]): row for row in rows}
    return sorted(
        deduped.values(),
        key=lambda row: str(row["updated_at"] or ""),
        reverse=True,
    )[:result_limit]


def _alias_like_terms(query: str, tokens: list[str]) -> list[str]:
    """Return alias-expanded LIKE terms that are not already in the raw query.

    This preserves lexical-only recall for curated aliases such as response→reply
    after removing the unsafe arbitrary-recency backfill. We include both the
    canonical alias and known surface forms because SQLite LIKE is not aware of
    our stemming/alias map (e.g. response→reply must still discover rows that
    literally contain "replies").
    """
    raw_terms = set(tokens)
    raw_query = (query or "").lower()
    # Importing here keeps this module's SQL discovery policy in sync with
    # lexical scoring without broad recent-row scans.
    from .aliases import _ALIAS_MAP, canonicalize_alias  # type: ignore[attr-defined]

    canonical_to_terms: dict[str, list[str]] = {}
    for raw in normalized_token_set(tokens):
        canonical_to_terms.setdefault(canonicalize_alias(raw), [])
    for surface, canonical in _ALIAS_MAP.items():
        if canonical in canonical_to_terms:
            canonical_to_terms.setdefault(canonical, []).append(surface)
    terms: list[str] = []
    seen: set[str] = set()
    for canonical, surfaces in canonical_to_terms.items():
        for term in [canonical, *surfaces]:
            if not term or term in raw_terms or term in raw_query or term in seen:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= 12:
                return terms
    return terms


def _row_metadata(
    row: sqlite3.Row,
    *,
    lexical_score: float = 0.0,
    vector_score: float = 0.0,
    bm25_score: float | None = None,
) -> dict[str, Any]:
    metadata = load_metadata(row["metadata"] if "metadata" in row.keys() else "{}")
    metadata.update(
        {
            "lexical_score": lexical_score,
            "vector_score": vector_score,
            "scope_id": row["scope_id"],
            "created_at": row["created_at"] if "created_at" in row.keys() else row["updated_at"],
        }
    )
    if bm25_score is not None:
        metadata["bm25_score"] = bm25_score
    return metadata


def search_db_memories(
    provider: Any,
    query: str,
    *,
    limit: int,
    generation_override: str | None = None,
    allow_unreviewed_generation: bool = False,
) -> list[RecallItem]:
    """Search SQLite truth rows for accessible recall candidates.

    Lifecycle and scope filters are applied here before ranking so downstream retrieval cannot accidentally surface archived or inaccessible state."""
    conn = provider._require_conn()
    tokens = retrieval_query_tokens(query)
    fts_query = build_fts_query(tokens)
    rows: list[sqlite3.Row] = []
    supplemental_row_ids: set[str] = set()
    exact_identifier_row_ids: set[str] = set()
    try:
        configured_pool = int((provider._retrieval_config or {}).get("candidate_pool") or 0)
    except (TypeError, ValueError):
        configured_pool = 0
    candidate_pool = max(limit * 2, limit, configured_pool)
    with provider._lock:
        # Exact memory identifiers are a separate, bounded lexical authority.
        # This lets opaque identifiers (UUID/SHA/project-style IDs) find the
        # row they literally name without granting those queries vector-only
        # admission.  Scope and ordinary lifecycle constraints remain the
        # same as every other public recall lane.
        exact_query = str(query or "").strip()
        if exact_query:
            exact_rows = conn.execute(
                """
                SELECT m.*, NULL AS bm25_score
                FROM memories m
                WHERE m.id = ? AND m.scope_id IN ({}) AND {}
                LIMIT 1
                """.format(_scope_placeholders(provider), _ACTIVE_MEMORY_SQL_M),
                [exact_query, *_accessible_scope_params(provider)],
            ).fetchall()
            rows.extend(exact_rows)
            exact_identifier_row_ids.update(str(row["id"]) for row in exact_rows)
        supplemental_table = supplemental_table_for_search(
            conn,
            generation_override,
            allow_unreviewed_override=allow_unreviewed_generation,
        )
        if fts_query:
            rows.extend(
                conn.execute(
                    """
                    SELECT m.*, bm25(memories_fts) AS bm25_score
                    FROM memories_fts
                    JOIN memories m ON m.id = memories_fts.memory_id
                    WHERE memories_fts MATCH ? AND m.scope_id IN ({}) AND {}
                    ORDER BY bm25(memories_fts) ASC, m.updated_at DESC
                    LIMIT ?
                    """.format(_scope_placeholders(provider), _ACTIVE_MEMORY_SQL_M),
                    [fts_query, *_accessible_scope_params(provider), candidate_pool],
                ).fetchall()
            )
        if supplemental_table:
            shadow_rows: list[sqlite3.Row] = []
            bigram_terms = [
                term for term in cjk_query_ngrams(query, limit=24) if len(term) == 2
            ]
            rare_terms, common_bigram_terms = _partition_cjk_bigram_terms(
                conn,
                supplemental_table,
                bigram_terms,
            )
            shadow_query = trigram_fts_query(
                query,
                tokens,
                common_cjk_bigrams=common_bigram_terms,
            )
            if shadow_query:
                # The inner rank window must stay wider than the candidate
                # pool: ties on near-identical rows (for example daily-report
                # noise) would otherwise evict newer rows before the outer
                # updated_at tie-breaker can run.
                inner_window = max(candidate_pool * 5, 100)
                shadow_rows = conn.execute(
                    f"""
                    SELECT m.*, NULL AS bm25_score
                    FROM (
                        SELECT {supplemental_table}.rowid AS docid, rank AS fts_rank
                        FROM {supplemental_table}
                        WHERE {supplemental_table} MATCH ?
                        ORDER BY rank
                        LIMIT ?
                    ) cand
                    JOIN memories m ON m.rowid = cand.docid
                    WHERE m.scope_id IN ({_scope_placeholders(provider)})
                      AND {_ACTIVE_MEMORY_SQL_M}
                    ORDER BY cand.fts_rank ASC, m.updated_at DESC
                    LIMIT ?
                    """,
                    [
                        shadow_query,
                        inner_window,
                        *_accessible_scope_params(provider),
                        candidate_pool,
                    ],
                ).fetchall()
                rows.extend(shadow_rows)
                supplemental_row_ids.update(str(row["id"]) for row in shadow_rows)
            if bigram_terms and len(shadow_rows) < candidate_pool:
                # Indexed postings replace the old correlated instr() scan. A
                # bounded df prefilter also keeps runaway terms out of the FTS
                # rank expression above, before they can create corpus fan-out.
                term_rows: list[sqlite3.Row] = []
                if rare_terms:
                    rare_placeholders = ",".join("?" for _ in rare_terms)
                    term_rows = conn.execute(
                        f"""
                        SELECT m.*, COUNT(*) AS cjk_match_count
                        FROM {LEXICAL_POSTINGS_TABLE} p
                        CROSS JOIN memories m ON m.rowid = p.docid
                        WHERE p.term IN ({rare_placeholders})
                          AND m.scope_id IN ({_scope_placeholders(provider)})
                          AND {_ACTIVE_MEMORY_SQL_M}
                        GROUP BY m.rowid
                        ORDER BY cjk_match_count DESC, m.updated_at DESC
                        LIMIT ?
                        """,
                        [
                            *rare_terms,
                            *_accessible_scope_params(provider),
                            candidate_pool,
                        ],
                    ).fetchall()
                rows.extend(term_rows)
                supplemental_row_ids.update(str(row["id"]) for row in term_rows)
        def _remaining_candidate_slots() -> int:
            return max(
                0,
                candidate_pool
                - len({str(row["id"]) for row in rows if str(row["id"])}),
            )

        like_query_terms = like_terms(query, tokens)
        like_scan_limit = _like_fallback_scan_limit(provider, candidate_pool)
        fallback_limit = _remaining_candidate_slots()
        if like_query_terms and fallback_limit > 0:
            rows.extend(
                _bounded_like_fallback_rows(
                    conn,
                    provider,
                    like_query_terms,
                    result_limit=fallback_limit,
                    scan_limit=like_scan_limit,
                )
            )
        alias_terms = _alias_like_terms(query, tokens)
        fallback_limit = _remaining_candidate_slots()
        if alias_terms and fallback_limit > 0:
            rows.extend(
                _bounded_like_fallback_rows(
                    conn,
                    provider,
                    alias_terms,
                    result_limit=fallback_limit,
                    scan_limit=like_scan_limit,
                )
            )
        # Do not backfill retrieval with arbitrary recent memories.
        # Earlier versions scanned newest rows when lexical LIKE/FTS returned too
        # few candidates, then accepted durable/tool rows on source/target bonus
        # alone. That made unrelated fresh conversations recall stale ops notes
        # (for example OpenClaw/凌晨 task context) despite zero token overlap.
        # Recency is only a reranking bonus after relevance is established.

    bm25_raw_scores: dict[str, float | None] = {}
    for row in rows:
        if "bm25_score" not in row.keys():
            continue
        try:
            bm25_raw_scores[str(row["id"])] = float(row["bm25_score"])
        except (TypeError, ValueError):
            continue
    bm25_scores = bm25_to_score(bm25_raw_scores)
    dedup_rows: dict[str, sqlite3.Row] = {row["id"]: row for row in rows}
    min_score = float((provider._retrieval_config or {}).get("min_score") or provider._config_value("min_score", 0.18))
    results: list[RecallItem] = []
    for row in dedup_rows.values():
        exact_identifier_evidence = str(row["id"]) in exact_identifier_row_ids
        score = (
            1.0
            if exact_identifier_evidence
            else lexical_score(
                query=query,
                content=row["content"],
                summary=row["summary"],
                source=row["source"],
                target=row["target"],
            )
        )
        supplemental_score = 0.0
        if str(row["id"]) in supplemental_row_ids:
            supplemental_score = cjk_substring_score(
                query,
                str(row["content"]),
                str(row["summary"]),
            )
            score = max(score, supplemental_score)
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
                metadata=_row_metadata(
                    row,
                    lexical_score=score,
                    vector_score=0.0,
                    bm25_score=bm25_scores.get(str(row["id"])),
                ),
            )
        )
        if results[-1].metadata is not None and str(row["id"]) in bm25_raw_scores:
            results[-1].metadata["bm25_raw"] = bm25_raw_scores[str(row["id"])]
        if results[-1].metadata is not None and supplemental_score > 0.0:
            results[-1].metadata["supplemental_lexical_score"] = supplemental_score
        if results[-1].metadata is not None and exact_identifier_evidence:
            results[-1].metadata["exact_identifier_evidence"] = True
    results.sort(key=lambda item: float(item.score), reverse=True)
    return results[: max(0, int(limit))]


def search_vector_memories(provider: Any, query: str, *, limit: int) -> list[RecallItem]:
    """Embed one query, then search the active vector companion.

    Query-provider failures are transient retrieval degradation, not companion
    corruption.  A short in-process cooldown prevents a failing hosted provider
    from being hammered while preserving automatic recovery without repair or
    restart.
    """

    if not provider._vector_ready or not provider._vector_store or not provider._embedder:
        return []
    now = time.monotonic()
    degraded_until = float(
        getattr(provider, "_vector_query_degraded_until_monotonic", 0.0) or 0.0
    )
    if now < degraded_until:
        return []
    try:
        query_vector = provider._embedder.embed_query(query)
    except Exception as exc:
        failures = min(
            6,
            int(getattr(provider, "_vector_query_failure_count", 0) or 0) + 1,
        )
        provider._vector_query_failure_count = failures
        provider._vector_query_degraded_until_monotonic = now + min(
            30.0,
            float(2 ** (failures - 1)),
        )
        provider._vector_query_last_error = type(exc).__name__
        return []
    provider._vector_query_failure_count = 0
    provider._vector_query_degraded_until_monotonic = 0.0
    provider._vector_query_last_error = ""
    return search_vector_memories_with_vector(provider, query_vector, limit=limit)


def search_vector_memories_with_vector(
    provider: Any,
    query_vector: list[float],
    *,
    limit: int,
) -> list[RecallItem]:
    """Search a precomputed vector while preserving SQLite truth checks."""

    if not provider._vector_ready or not provider._vector_store:
        return []
    try:
        top_k = max(limit, int((provider._vector_config or {}).get("top_k") or limit))
        rows = []
        for scope_id in provider._accessible_scope_ids:
            rows.extend(provider._vector_store.search(query_vector, scope_id=scope_id, limit=top_k))
    except Exception as exc:
        mark_vector_needs_repair(provider, exc)
        return []
    threshold = float((provider._retrieval_config or {}).get("vector_min_score") or 0.12)
    unique_ids = list(
        dict.fromkeys(
            str(row.get("id") or "")
            for row in rows
            if str(row.get("id") or "")
        )
    )
    truth_by_id: dict[str, sqlite3.Row] = {}
    if unique_ids:
        try:
            scope_params = _accessible_scope_params(provider)
            with provider._lock:
                conn = provider._require_conn()
                for id_chunk in chunked_sql_parameters(
                    conn,
                    unique_ids,
                    reserved=len(scope_params),
                ):
                    id_placeholders = ",".join("?" for _ in id_chunk)
                    truth_rows = conn.execute(
                        f"""
                        SELECT id, scope_id, source, target, content, summary,
                               created_at, updated_at, metadata
                        FROM memories
                        WHERE id IN ({id_placeholders})
                          AND scope_id IN ({_scope_placeholders(provider)})
                          AND {_ACTIVE_MEMORY_SQL}
                        """,
                        [*id_chunk, *scope_params],
                    ).fetchall()
                    truth_by_id.update(
                        {str(truth_row["id"]): truth_row for truth_row in truth_rows}
                    )
        except Exception:
            # SQLite is the authority.  If it cannot be read and checked, no
            # companion row is safe to surface.
            truth_by_id = {}
    results: list[RecallItem] = []
    companion_mismatch_count = 0
    for row in rows:
        row_id = str(row.get("id") or "")
        truth_row = truth_by_id.get(row_id)
        if truth_row is None:
            companion_mismatch_count += 1
            continue
        companion_fields = {
            "scope_id": str(row.get("scope_id") or ""),
            "source": str(row.get("source") or ""),
            "target": str(row.get("target") or ""),
            "content": str(row.get("content") or ""),
            "summary": str(row.get("summary") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }
        if any(
            companion_fields[field] != str(truth_row[field] or "")
            for field in companion_fields
        ):
            companion_mismatch_count += 1
            continue
        distance = float(row.get("_distance") or 0.0)
        vector_score = max(0.0, 1.0 - distance)
        if vector_score < threshold:
            continue
        metadata = _row_metadata(
            truth_row,
            lexical_score=0.0,
            vector_score=vector_score,
        )
        results.append(
            RecallItem(
                id=truth_row["id"],
                content=truth_row["content"],
                summary=truth_row["summary"],
                source=truth_row["source"],
                target=truth_row["target"],
                score=vector_score,
                updated_at=truth_row["updated_at"],
                metadata=metadata,
            )
        )
    if companion_mismatch_count:
        mark_vector_needs_repair(
            provider,
            "vector companion rows disagree with active accessible SQLite truth "
            f"({companion_mismatch_count} mismatch(es))",
        )
    best_by_id: dict[str, RecallItem] = {}
    for item in results:
        current = best_by_id.get(str(item.id))
        if current is None or float(item.score) > float(current.score):
            best_by_id[str(item.id)] = item
    ranked = sorted(
        best_by_id.values(),
        key=lambda item: float(item.score),
        reverse=True,
    )
    return ranked[: max(0, int(limit))]


def _curated_memory_allowed(provider: Any) -> bool:
    raw_cfg = (provider._config or {}).get("curated_memory", {})
    if raw_cfg is False:
        return False
    cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
    mode = str(cfg.get("mode") or "single-user").strip().lower()
    if mode in {"disabled", "off", "false", "none"}:
        return False

    scope = getattr(provider, "_scope", None)
    user_id = str(getattr(scope, "user_id", "") or "")
    allowed = [str(item).strip() for item in (cfg.get("allowed_user_ids") or []) if str(item).strip()]
    if allowed:
        return bool(user_id and user_id in allowed)
    if mode in {"explicit-users", "allow-list", "allowlist"}:
        return False
    if mode in {"shared"}:
        # Deprecated compatibility alias kept explicit so operator-facing schema
        # can advertise only the canonical runtime modes.
        mode = "profile-global"
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
        metadata = classify_memory(content, target, "builtin-curated")
        metadata.update({"lexical_score": score, "vector_score": 0.0})
        results.append(
            RecallItem(
                id=curated_recall_item_id(target, content),
                content=content,
                summary=summary,
                source="builtin-curated",
                target=target,
                score=score,
                updated_at=updated_at,
                metadata=metadata,
            )
        )
    return results
