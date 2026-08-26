"""Read-only memory query and diagnostic payloads.

These functions must not open truth transactions or mutate SQLite.
They still accept the live Provider object so existing monkeypatches keep working.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .capture_filters import sanitize_structured_value
from .adjudication_schedule import adjudication_schedule_status, schedule_target_id
from .writer_lease import sanitized_truth_writer_owner
from .freshness import attach_freshness_metadata, fact_freshness_report, memory_freshness_map
from .gating import compact_text
from .graph import clamp_float, compact_context_lines, lifecycle_visible_sql, load_metadata, normalize_entity
from .graph_relations import graph_relation_stats  # noqa: F401
from .governance import classify_memory
from .lifecycle_policy import PROFILE_HIDDEN_LIFECYCLES  # noqa: F401
from ._internal.memory.scope import accessible_scope_params, payload_entities, scope_placeholders
from .models import recall_scope_mode
from .operator_ledger import operator_ledger_report
from ._internal.recall.pipeline import humanize_filter_trace, humanize_recall_components
from .capture_control import capture_queue_report
from .relation_containment import relation_containment_report
from .relation_frequency_maintenance import relation_frequency_index_report
from .relation_rebuild_queue import relation_rebuild_queue_report
from .sql_store import curated_recall_item_id, iter_curated_entries  # noqa: F401
from .storage_views import _curated_memory_allowed


def _as_str_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}
    return {}


def _public_migration_info(payload: Any) -> dict[str, bool]:
    """Expose migration outcomes without publishing local filesystem paths."""

    info = _as_str_dict(payload)
    return {
        key: bool(info[key])
        for key in ("migrated", "config_copied")
        if key in info
    }


def _require_port_method(provider: Any, name: str) -> Any:
    getter = getattr(provider, name, None)
    if not callable(getter):
        raise TypeError(f"MemoryQueryPort.{name} is required")
    return getter


def _query_conn(provider: Any) -> Any:
    return _require_port_method(provider, "query_connection")()


def _query_lock(provider: Any) -> Any:
    return _require_port_method(provider, "query_lock")()


def _query_scope_view(provider: Any) -> dict[str, Any]:
    return _as_str_dict(_require_port_method(provider, "query_scope_view")())


def _vector_status_view(provider: Any) -> dict[str, Any]:
    return _as_str_dict(_require_port_method(provider, "vector_status_view")())


def _retrieval_status_view(provider: Any) -> dict[str, Any]:
    return _as_str_dict(_require_port_method(provider, "retrieval_status_view")())


def _runtime_status_view(provider: Any) -> dict[str, Any]:
    return _as_str_dict(_require_port_method(provider, "runtime_status_view")())


def _recall_service(provider: Any) -> Any:
    return _require_port_method(provider, "recall_service_view")()

def export_memories(
    provider: Any, *, fmt: str = "jsonl", scope_only: bool = True
) -> dict[str, Any]:
    conn = _query_conn(provider)
    if scope_only:
        where = f"WHERE scope_id IN ({scope_placeholders(provider)})"
        params: tuple[Any, ...] = tuple(accessible_scope_params(provider))
    else:
        where = ""
        params = ()
    with _query_lock(provider):
        rows = conn.execute(
            f"""
            SELECT id, scope_id, source, target, content, summary, created_at, updated_at, metadata
            FROM memories
            {where}
            ORDER BY updated_at DESC, id DESC
            """,
            params,
        ).fetchall()
    records = [dict(row) for row in rows]
    if fmt.lower() == "json":
        data: Any = records
    else:
        fmt = "jsonl"
        data = "\n".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records
        )
    return {
        "format": fmt.lower(),
        "scope_only": scope_only,
        "count": len(records),
        "data": data,
    }

def hygiene_report(provider: Any, *, limit: int = 200) -> dict[str, Any]:
    from .hygiene import build_hygiene_report

    with _query_lock(provider):
        return build_hygiene_report(
            _query_conn(provider),
            vector_store=getattr(provider, "_vector_store", None),
            limit=limit,
        )

def _row_payload(row: Any) -> dict[str, Any]:
    metadata = load_metadata(row["metadata"] if "metadata" in row.keys() else "{}")
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"]),
        "source": str(row["source"]),
        "target": str(row["target"]),
        "content": str(row["content"]),
        "summary": str(row["summary"]),
        "updated_at": str(row["updated_at"]),
        "memory_type": str(metadata.get("memory_type") or ""),
        "confidence": clamp_float(metadata.get("confidence"), default=0.5),
        "trust": clamp_float(metadata.get("trust"), default=0.5),
        "importance": clamp_float(metadata.get("importance"), default=0.5),
        "entities": payload_entities(metadata),
        "tags": metadata.get("tags") if isinstance(metadata.get("tags"), list) else [],
    }


def _profile_targets(targets: list[str] | None, *, include_general: bool) -> list[str]:
    allowed = ["user", "memory", "project", "ops", "general"]
    if targets:
        output = [target for target in targets if target in allowed]
    else:
        output = ["user", "memory", "project", "ops"]
    if include_general and "general" not in output:
        output.append("general")
    if not include_general and targets is None and "general" in output:
        output.remove("general")
    deduped: list[str] = []
    for target in output:
        if target not in deduped:
            deduped.append(target)
    return deduped


def _profile_row_payload(row: Any) -> dict[str, Any]:
    metadata = load_metadata(row["metadata"] if "metadata" in row.keys() else "{}")
    return {
        "id": str(row["id"]),
        "target": str(row["target"]),
        "source": str(row["source"]),
        "summary": str(row["summary"]),
        "content": compact_text(str(row["content"]), 360),
        "updated_at": str(row["updated_at"]),
        "scope_mode": str(
            metadata.get("scope_mode")
            or recall_scope_mode(str(row["target"]), str(row["source"]))
        ),
        "memory_type": str(
            metadata.get("memory_type") or metadata.get("category") or ""
        ),
        "trust": clamp_float(metadata.get("trust"), default=0.5),
        "importance": clamp_float(metadata.get("importance"), default=0.5),
        "confidence": clamp_float(metadata.get("confidence"), default=0.5),
        "entities": payload_entities(metadata),
    }


def _profile_curated_items(
    provider: Any, *, targets: list[str], limit: int
) -> list[dict[str, Any]]:
    if not _curated_memory_allowed(provider):
        return []
    from . import memory_ops as _ops

    items: list[dict[str, Any]] = []
    for target, content, updated_at in _ops.iter_curated_entries(_runtime_status_view(provider).get("hermes_home")):
        if target not in targets:
            continue
        metadata = classify_memory(content, target, "builtin-curated")
        items.append(
            {
                "id": curated_recall_item_id(target, content),
                "target": target,
                "source": "builtin-curated",
                "summary": compact_text(content, 220),
                "content": compact_text(content, 360),
                "updated_at": updated_at,
                "scope_mode": "curated-live",
                "memory_type": str(
                    metadata.get("memory_type") or metadata.get("category") or ""
                ),
                "trust": clamp_float(metadata.get("trust"), default=0.5),
                "importance": clamp_float(metadata.get("importance"), default=0.5),
                "confidence": clamp_float(metadata.get("confidence"), default=0.5),
                "entities": payload_entities(metadata),
            }
        )
    return items[: max(1, limit)]


def _profile_relevant_ids(
    provider: Any, *, query: str, entity: str, limit: int
) -> set[str]:
    relevant: set[str] = set()
    if query:
        for item in _recall_service(provider).search_memories(
            query, limit=max(10, min(50, limit * 4))
        ):
            relevant.add(str(item.id))
    normalized_entity = normalize_entity(entity)
    if normalized_entity:
        with _query_lock(provider):
            rows = (
                _query_conn(provider)
                .execute(
                    f"""
                SELECT m.id
                FROM memory_entities e
                JOIN memories m ON m.id = e.memory_id
                WHERE e.entity = ?
                  AND m.scope_id IN ({scope_placeholders(provider)})
                  AND {lifecycle_visible_sql("m")}
                LIMIT ?
                """,
                    [
                        normalized_entity,
                        *accessible_scope_params(provider),
                        max(10, min(100, limit * 8)),
                    ],
                )
                .fetchall()
            )
        relevant.update(str(row["id"]) for row in rows)
    return relevant


def _profile_lifecycle_sql(
    alias: str = "m", *, include_candidates: bool = False
) -> str:
    lifecycle_expr = (
        f"LOWER(COALESCE(CASE WHEN json_valid({alias}.metadata) "
        f"THEN json_extract({alias}.metadata, '$.lifecycle') ELSE 'promoted' END, 'promoted'))"
    )
    if include_candidates:
        return f"{lifecycle_expr} NOT IN ('archived', 'superseded', 'obsolete', 'rejected')"
    return f"{lifecycle_expr} = 'promoted'"


def _profile_rows_for_target(
    provider: Any,
    *,
    target: str,
    limit: int,
    relevant_ids: set[str],
    filter_to_relevance: bool,
    include_candidates: bool,
) -> list[dict[str, Any]]:
    fetch_limit = max(1, int(limit or 1)) * 3
    params: list[Any] = [target, *accessible_scope_params(provider)]
    relevance_clause = ""
    if filter_to_relevance:
        if not relevant_ids:
            return []
        relevance_clause = f" AND m.id IN ({','.join('?' for _ in relevant_ids)})"
        params.extend(sorted(relevant_ids))
    params.append(fetch_limit)
    with _query_lock(provider):
        rows = (
            _query_conn(provider)
            .execute(
                f"""
            SELECT m.*
            FROM memories m
            WHERE m.target = ?
              AND m.scope_id IN ({scope_placeholders(provider)})
              AND {_profile_lifecycle_sql("m", include_candidates=include_candidates)}{relevance_clause}
            ORDER BY
                CASE m.source
                    WHEN 'tool-store' THEN 0
                    WHEN 'journal-digest' THEN 1
                    WHEN 'nightly-digest' THEN 2
                    ELSE 3
                END,
                m.updated_at DESC,
                m.id DESC
            LIMIT ?
            """,
                params,
            )
            .fetchall()
        )
        freshness_by_id = memory_freshness_map(
            _query_conn(provider), [str(row["id"]) for row in rows]
        )
    payloads = [_profile_row_payload(row) for row in rows]
    retrieval_cfg = _retrieval_status_view(provider).get("config") or {}
    for payload in payloads:
        attach_freshness_metadata(
            payload,
            freshness_by_id.get(str(payload.get("id") or "")),
            config=retrieval_cfg,
        )
    payloads.sort(key=lambda item: 1 if item.get("needs_live_check") else 0)
    return payloads[: max(1, int(limit or 1))]


def profile_payload(
    provider: Any,
    *,
    query: str = "",
    entity: str = "",
    targets: list[str] | None = None,
    include_general: bool = False,
    include_candidates: bool = False,
    include_curated: bool = True,
    limit: int = 5,
    max_chars: int = 1200,
) -> dict[str, Any]:
    """Build the compact profile/context payload for user, memory, project, ops, and optional general rows.

    The payload should preserve target boundaries and lifecycle filtering while fitting into prompt budget."""
    limit = max(1, min(20, int(limit or 5)))
    max_chars = max(120, min(4000, int(max_chars or 1200)))
    selected_targets = _profile_targets(targets, include_general=include_general)
    relevant_ids = _profile_relevant_ids(
        provider, query=query, entity=entity, limit=limit
    )
    relevance_requested = bool(query.strip() or entity.strip())

    sections: dict[str, dict[str, Any]] = {
        target: {"count": 0, "items": []}
        for target in ["user", "memory", "project", "ops", "general"]
    }
    all_items: list[dict[str, Any]] = []
    for target in selected_targets:
        filter_to_relevance = relevance_requested and target in {"project", "ops"}
        items = _profile_rows_for_target(
            provider,
            target=target,
            limit=limit,
            relevant_ids=relevant_ids,
            filter_to_relevance=filter_to_relevance,
            include_candidates=bool(include_candidates or target == "general"),
        )
        sections[target] = {"count": len(items), "items": items}
        all_items.extend(items)

    curated_items: list[dict[str, Any]] = []
    if include_curated:
        curated_items = _profile_curated_items(
            provider, targets=selected_targets, limit=limit
        )
        for item in curated_items:
            target = str(item.get("target") or "memory")
            section_items = sections.setdefault(target, {"count": 0, "items": []})[
                "items"
            ]
            section_items.append(item)
            sections[target]["count"] = len(section_items)
        all_items = [*curated_items, *all_items]

    context = compact_context_lines(all_items, max_chars=max_chars)
    rendered_count = len([line for line in context.splitlines() if line.strip()])
    return {
        "provider": provider.name,
        "surface": "profile",
        "query": query,
        "entity": normalize_entity(entity),
        "targets": selected_targets,
        "include_general": bool(
            include_general or (targets is not None and "general" in selected_targets)
        ),
        "include_candidates": bool(include_candidates),
        "context": context,
        "sections": sections,
        "curated": {"count": len(curated_items), "items": curated_items},
        "scope": {
            "scope_id": _query_scope_view(provider).get("scope_id"),
            "shared_scope_id": _query_scope_view(provider).get("shared_scope_id"),
            "accessible_scope_count": len(list(_query_scope_view(provider).get("accessible_scope_ids") or [])),
        },
        "budget": {
            "limit_per_section": limit,
            "max_chars": max_chars,
            "rendered_chars": len(context),
            "rendered_items": rendered_count,
            "candidate_items": len(all_items),
            "truncated": rendered_count < len(all_items),
        },
        "notes": [
            "SQLite memories are read from the current accessible scope set only.",
            "SQLite profile rows default to lifecycle=promoted; pass include_candidates=true to include non-hidden candidate rows.",
            "Hermes curated USER.md/MEMORY.md entries are live-read when policy allows; they are not copied into SQLite.",
            "Raw journal rows are not exposed by this profile surface.",
        ],
    }


def context_payload(
    provider: Any, *, query: str, limit: int = 5, max_chars: int = 900
) -> dict[str, Any]:
    results = _recall_service(provider).search_memories(
        query, limit=max(1, min(20, limit))
    )
    records: list[dict[str, Any]] = []
    entity_counts: dict[str, int] = {}
    for item in results:
        metadata = load_metadata(item.metadata or {})
        entities = payload_entities(metadata)
        for entity in entities:
            entity_counts[entity] = entity_counts.get(entity, 0) + 1
        records.append(
            {
                "id": item.id,
                "target": item.target,
                "source": item.source,
                "content": item.content,
                "summary": item.summary,
                "score": round(item.score, 4),
                "updated_at": item.updated_at,
                "memory_type": str(metadata.get("memory_type") or ""),
                "entities": entities,
                "needs_live_check": bool(metadata.get("needs_live_check")),
                "fact_freshness_status": str(
                    metadata.get("fact_freshness_status") or "untracked"
                ),
                "fact_freshness_penalty": metadata.get("fact_freshness_penalty", 0.0),
            }
        )
    top_entities = [
        {"entity": entity, "count": count}
        for entity, count in sorted(
            entity_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )[:10]
    ]
    return {
        "query": query,
        "count": len(records),
        "context": compact_context_lines(
            records, max_chars=max(120, min(4000, max_chars))
        ),
        "entities": top_entities,
        "results": records,
    }


def probe_entity(provider: Any, *, entity: str, limit: int = 10) -> dict[str, Any]:
    normalized = normalize_entity(entity)
    if not normalized:
        return {"entity": "", "count": 0, "results": []}
    conn = _query_conn(provider)
    with _query_lock(provider):
        rows = conn.execute(
            f"""
            SELECT m.*
            FROM memory_entities e
            JOIN memories m ON m.id = e.memory_id
            WHERE e.entity = ?
              AND m.scope_id IN ({scope_placeholders(provider)})
              AND {lifecycle_visible_sql("m")}
            ORDER BY
                CASE m.target
                    WHEN 'user' THEN 0
                    WHEN 'project' THEN 1
                    WHEN 'ops' THEN 2
                    WHEN 'memory' THEN 3
                    ELSE 4
                END,
                m.updated_at DESC
            LIMIT ?
            """,
            [normalized, *accessible_scope_params(provider), max(1, min(50, limit))],
        ).fetchall()
    return {
        "entity": normalized,
        "count": len(rows),
        "results": [_row_payload(row) for row in rows],
    }


def related_entities(provider: Any, *, entity: str, limit: int = 12) -> dict[str, Any]:
    normalized = normalize_entity(entity)
    if not normalized:
        return {"entity": "", "count": 0, "related": []}
    conn = _query_conn(provider)
    with _query_lock(provider):
        rows = conn.execute(
            f"""
            WITH matched AS (
                SELECT e.memory_id
                FROM memory_entities e
                JOIN memories m ON m.id = e.memory_id
                WHERE e.entity = ?
                  AND m.scope_id IN ({scope_placeholders(provider)})
                  AND {lifecycle_visible_sql("m")}
            )
            SELECT e.entity, COUNT(*) AS count
            FROM memory_entities e
            JOIN matched ON matched.memory_id = e.memory_id
            WHERE e.entity != ?
            GROUP BY e.entity
            ORDER BY count DESC, e.entity ASC
            LIMIT ?
            """,
            [
                normalized,
                *accessible_scope_params(provider),
                normalized,
                max(50, min(200, max(1, int(limit)) * 8)),
            ],
        ).fetchall()
    related_counts: dict[str, int] = {}
    for row in rows:
        related_entity = normalize_entity(row["entity"])
        if not related_entity or related_entity == normalized:
            continue
        related_counts[related_entity] = related_counts.get(related_entity, 0) + int(
            row["count"]
        )
    related = [
        {"entity": entity, "count": count}
        for entity, count in sorted(
            related_counts.items(), key=lambda item: (-item[1], item[0])
        )[: max(1, min(50, limit))]
    ]
    return {"entity": normalized, "count": len(related), "related": related}

def inspect_memory(provider: Any, *, memory_id: str) -> dict[str, Any]:
    conn = _query_conn(provider)
    with _query_lock(provider):
        row = conn.execute(
            f"SELECT * FROM memories WHERE id = ? AND scope_id IN ({scope_placeholders(provider)})",
            [memory_id, *accessible_scope_params(provider)],
        ).fetchone()
        if row is None:
            return {
                "found": False,
                "id": memory_id,
                "memory": None,
                "feedback": {"count": 0, "items": []},
                "relations": {"count": 0, "items": []},
            }
        feedback_rows = conn.execute(
            "SELECT rating, note, created_at FROM memory_feedback WHERE memory_id = ? ORDER BY created_at DESC",
            (memory_id,),
        ).fetchall()
        scope_params = accessible_scope_params(provider)
        relation_rows = conn.execute(
            f"""
            SELECT r.source_memory_id, r.target_memory_id, r.relation_type, r.confidence, r.note, r.created_at
            FROM memory_relations AS r
            JOIN memories AS peer
              ON peer.id = CASE
                WHEN r.source_memory_id = ? THEN r.target_memory_id
                ELSE r.source_memory_id
              END
            WHERE (r.source_memory_id = ? OR r.target_memory_id = ?)
              AND peer.scope_id IN ({",".join("?" for _ in scope_params) or "NULL"})
              AND {lifecycle_visible_sql("peer")}
            ORDER BY r.created_at DESC
            """,
            [memory_id, memory_id, memory_id, *scope_params],
        ).fetchall()
    metadata = load_metadata(row["metadata"])
    memory = {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"]),
        "source": str(row["source"]),
        "target": str(row["target"]),
        "content": str(row["content"]),
        "summary": str(row["summary"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "metadata": metadata,
    }
    feedback = [dict(item) for item in feedback_rows]
    relations = [dict(item) for item in relation_rows]
    payload = {
        "found": True,
        "id": memory_id,
        "memory": memory,
        "feedback": {"count": len(feedback), "items": feedback},
        "relations": {"count": len(relations), "items": relations},
    }
    safe_payload, _ = sanitize_structured_value(payload)
    return safe_payload if isinstance(safe_payload, dict) else {
        "found": False,
        "id": memory_id,
        "memory": None,
        "feedback": {"count": 0, "items": []},
        "relations": {"count": 0, "items": []},
    }


def explain_query(provider: Any, *, query: str, limit: int = 5) -> dict[str, Any]:
    """Return retrieval explanations for a query without changing recall state.

    Explanations expose filters, scores, relation evidence, and ranking reasons so benchmark failures are debuggable."""
    results = _recall_service(provider).search_memories(
        query, limit=max(1, min(20, limit))
    )
    payload_results: list[dict[str, Any]] = []

    def _component_float(
        metadata: dict[str, Any], key: str, default: float = 0.0
    ) -> float:
        try:
            return float(metadata.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    component_keys = (
        "lexical_score",
        "bm25_score",
        "vector_score",
        "rrf_score",
        "pre_quality_score",
        "quality_weight_applied",
        "metadata_weight",
        "entity_overlap_bonus",
        "entity_distance_score",
        "entity_distance_weight",
        "entity_distance_bonus",
        "relation_evidence_count",
        "relation_rerank_bonus",
        "pre_decay_score",
        "base_score",
        "temporal_decay_multiplier",
        "temporal_decay_weight",
        "temporal_policy_weight",
        "recency_bonus",
        "final_score",
        "general_weight",
        "trust",
        "importance",
        "confidence",
        "min_score",
        "vector_only_min_score",
    )

    def _payload_for_item(item: Any, rank: int) -> dict[str, Any]:
        metadata = dict(item.metadata or {})
        components: dict[str, Any] = {
            key: _component_float(
                metadata,
                key,
                1.0 if key in {"temporal_decay_multiplier", "general_weight"} else 0.0,
            )
            for key in component_keys
        }
        components["final_score"] = float(
            item.score or components.get("final_score") or 0.0
        )
        components["relation_evidence_types"] = (
            metadata.get("relation_evidence_types")
            if isinstance(metadata.get("relation_evidence_types"), list)
            else []
        )
        components["relation_evidence_ids"] = (
            metadata.get("relation_evidence_ids")
            if isinstance(metadata.get("relation_evidence_ids"), list)
            else []
        )
        components["relation_rerank_enabled"] = bool(
            metadata.get("relation_rerank_enabled") or False
        )
        components["temporal_policy_class"] = str(
            metadata.get("temporal_policy_class") or ""
        )
        components["rejected_reason"] = str(metadata.get("rejected_reason") or "")
        payload = {
            "rank": rank,
            "id": item.id,
            "target": item.target,
            "source": item.source,
            "summary": item.summary,
            "score": round(item.score, 4),
            "updated_at": item.updated_at,
            "components": components,
        }
        payload.update(
            humanize_recall_components(
                components, rejected=bool(metadata.get("rejected_reason"))
            )
        )
        return payload

    for rank, item in enumerate(results, start=1):
        payload_results.append(_payload_for_item(item, rank))
    rejected_candidates = list(
        getattr(_recall_service(provider), "last_rejected_candidates", []) or []
    )
    rejected_payload: list[dict[str, Any]] = []
    for rank, item in enumerate(rejected_candidates[: max(1, min(20, limit))], start=1):
        rejected_item = _payload_for_item(item, rank)
        rejected_item.update(
            humanize_recall_components(
                rejected_item.get("components", {}), rejected=True
            )
        )
        rejected_payload.append(rejected_item)
    funnel_trace = dict(
        getattr(_recall_service(provider), "last_funnel_trace", {}) or {}
    )
    return {
        "query": query,
        "count": len(payload_results),
        "results": payload_results,
        "rejected_count": len(rejected_candidates),
        "rejected_candidates": rejected_payload,
        "funnel_trace": funnel_trace,
        "filter_explanations": humanize_filter_trace(funnel_trace),
    }


def _benchmark_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        candidates = []
    output: list[str] = []
    for item in candidates:
        clean = str(item or "").strip()
        if clean and clean not in output:
            output.append(clean)
    return output


def _benchmark_cases(
    queries: list[str] | None, cases: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    if cases:
        normalized: list[dict[str, Any]] = []
        for case in cases:
            if not isinstance(case, dict):
                continue
            query = str(case.get("query") or "").strip()
            if not query:
                continue
            normalized.append(dict(case, query=query))
        return normalized
    return [
        {"query": str(query).strip()} for query in (queries or []) if str(query).strip()
    ]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    lower = int(index)
    upper = min(len(ordered) - 1, lower + 1)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = index - lower
    return round(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction, 3)


def _merge_filter_counts(total: dict[str, int], trace: dict[str, Any]) -> None:
    filters = trace.get("filters") if isinstance(trace, dict) else {}
    if not isinstance(filters, dict):
        return
    for key, value in filters.items():
        try:
            total[str(key)] = int(total.get(str(key), 0)) + int(value or 0)
        except (TypeError, ValueError):
            continue


def benchmark_queries(
    provider: Any,
    *,
    queries: list[str] | None = None,
    cases: list[dict[str, Any]] | None = None,
    limit: int = 5,
    auto_explain_on_fail: bool = False,
    include_trace: bool = False,
    prompt_budget_chars: int = 0,
) -> dict[str, Any]:
    """Run recall benchmark queries against the current provider state.

    The benchmark path returns structured pass/fail evidence and optional explanations so release gates can catch retrieval regressions without mutating memory."""
    normalized_cases = _benchmark_cases(queries, cases)
    rows: list[dict[str, Any]] = []
    aggregate_failures: list[str] = []
    bounded_limit = max(1, min(20, limit))
    latencies: list[float] = []
    filter_counts: dict[str, int] = {}
    expected_total = 0
    expected_found = 0
    cases_with_expected = 0
    cases_with_expected_hit = 0
    forbidden_violations = 0
    prompt_budget_checked = 0
    prompt_budget_hits = 0
    for case in normalized_cases:
        query = str(case.get("query") or "").strip()
        started = time.perf_counter()
        results = _recall_service(provider).search_memories(query, limit=bounded_limit)
        latency_ms = (time.perf_counter() - started) * 1000.0
        latencies.append(latency_ms)
        trace = dict(getattr(_recall_service(provider), "last_funnel_trace", {}) or {})
        _merge_filter_counts(filter_counts, trace)
        ids = [str(item.id) for item in results]
        ranks = {memory_id: index for index, memory_id in enumerate(ids, start=1)}
        results_by_id = {str(item.id): item for item in results}
        failures: list[str] = []
        expected_ids = _benchmark_id_list(case.get("expected_ids"))
        forbidden_ids = _benchmark_id_list(case.get("forbidden_ids"))
        raw_expected_metadata = case.get("expected_metadata")
        expected_metadata: dict[str, Any] = (
            raw_expected_metadata if isinstance(raw_expected_metadata, dict) else {}
        )
        min_rank_raw = case.get("min_rank")
        try:
            min_rank = int(min_rank_raw) if min_rank_raw is not None else 0
        except (TypeError, ValueError):
            min_rank = 0
        min_top_score_raw = case.get("min_top_score")
        try:
            min_top_score = (
                float(min_top_score_raw) if min_top_score_raw is not None else 0.0
            )
        except (TypeError, ValueError):
            min_top_score = 0.0
        raw_top_score = float(results[0].score) if results else 0.0
        top_score = round(raw_top_score, 4)
        if expected_ids:
            cases_with_expected += 1
        case_expected_hit = False
        for expected_id in expected_ids:
            expected_total += 1
            rank = ranks.get(expected_id)
            if rank is None:
                failures.append(f"expected_id_missing:{expected_id}")
            else:
                expected_found += 1
                case_expected_hit = True
                if min_rank > 0 and rank > min_rank:
                    failures.append(
                        f"expected_id_rank_too_low:{expected_id}:rank={rank}:min_rank={min_rank}"
                    )
        if case_expected_hit:
            cases_with_expected_hit += 1
        for forbidden_id in forbidden_ids:
            if forbidden_id in ranks:
                forbidden_violations += 1
                failures.append(
                    f"forbidden_id_present:{forbidden_id}:rank={ranks[forbidden_id]}"
                )
        for memory_id, expected_values in expected_metadata.items():
            memory_id = str(memory_id)
            if not isinstance(expected_values, dict):
                continue
            item = results_by_id.get(memory_id)
            if item is None:
                failures.append(f"expected_metadata_id_missing:{memory_id}")
                continue
            metadata = dict(item.metadata or {})
            for key, expected_value in expected_values.items():
                actual_value = metadata.get(str(key))
                if actual_value != expected_value:
                    failures.append(
                        f"metadata_mismatch:{memory_id}:{key}:actual={actual_value!r}:expected={expected_value!r}"
                    )
        if min_top_score_raw is not None and top_score < min_top_score:
            failures.append(
                f"top_score_below_min:{top_score}:min_top_score={min_top_score}"
            )
        row: dict[str, Any] = {
            "query": query,
            "count": len(results),
            "latency_ms": round(latency_ms, 3),
            "top_score": top_score,
            "raw_top_score": raw_top_score,
            "ids": ids,
            "passed": not failures,
            "failures": failures,
        }
        if prompt_budget_chars > 0:
            prompt_budget_checked += 1
            returned_chars = int(
                (
                    (trace.get("final") or {})
                    if isinstance(trace.get("final"), dict)
                    else {}
                ).get("returned_chars")
                or 0
            )
            row["prompt_budget_chars"] = prompt_budget_chars
            row["returned_chars"] = returned_chars
            row["prompt_budget_hit"] = returned_chars <= prompt_budget_chars
            if row["prompt_budget_hit"]:
                prompt_budget_hits += 1
        if include_trace:
            row["funnel_trace"] = trace
        if failures and auto_explain_on_fail:
            row["explain"] = explain_query(provider, query=query, limit=bounded_limit)
        rows.append(row)
        aggregate_failures.extend(f"{query}: {failure}" for failure in failures)
    metrics: dict[str, Any] = {
        "latency_ms_p50": _percentile(latencies, 50),
        "latency_ms_p95": _percentile(latencies, 95),
        "known_answer_recall": round(expected_found / expected_total, 4)
        if expected_total
        else None,
        "top_k_accuracy": round(cases_with_expected_hit / cases_with_expected, 4)
        if cases_with_expected
        else None,
        "expected_total": expected_total,
        "expected_found": expected_found,
        "forbidden_violations": forbidden_violations,
        "filter_counts": filter_counts,
    }
    if prompt_budget_checked:
        metrics["prompt_budget_hit_rate"] = round(
            prompt_budget_hits / prompt_budget_checked, 4
        )
        metrics["prompt_budget_checked"] = prompt_budget_checked
    return {
        "query_count": len(normalized_cases),
        "limit": bounded_limit,
        "passed": not aggregate_failures,
        "failures": aggregate_failures,
        "metrics": metrics,
        "results": rows,
    }


def _write_transaction_stats() -> dict[str, Any]:
    from .transaction_guard import transaction_duration_stats

    return transaction_duration_stats()


def stats_payload(provider: Any) -> dict[str, Any]:
    """Build the provider stats payload consumed by tools, dashboards, and tests.

    Stats should expose runtime debt clearly while keeping examples sanitized
    and avoiding hidden mutations. Vector status is a single aggregate snapshot;
    this payload must not list companion records or run a second full audit.
    """
    from . import memory_ops as _ops

    conn = _query_conn(provider)
    scope_view = _query_scope_view(provider)
    vector_view = _vector_status_view(provider)
    with _query_lock(provider):
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        scoped = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE scope_id IN ({scope_placeholders(provider)})",
            accessible_scope_params(provider),
        ).fetchone()[0]
        local = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE scope_id = ?", (scope_view.get("scope_id"),)
        ).fetchone()[0]
        shared = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE scope_id = ?",
            (scope_view.get("shared_scope_id"),),
        ).fetchone()[0]
        shared_pool_scope_id = str(scope_view.get("shared_pool_scope_id") or "")
        shared_pool = (
            conn.execute(
                "SELECT COUNT(*) FROM memories WHERE scope_id = ?",
                (shared_pool_scope_id,),
            ).fetchone()[0]
            if shared_pool_scope_id
            else 0
        )
        entities = conn.execute(
            f"""
            SELECT COUNT(DISTINCT e.entity)
            FROM memory_entities e
            JOIN memories m ON m.id = e.memory_id
            WHERE m.scope_id IN ({scope_placeholders(provider)})
            """,
            accessible_scope_params(provider),
        ).fetchone()[0]
        feedback_rows = conn.execute(
            """
            SELECT COUNT(*)
            FROM memory_feedback f
            JOIN memories m ON m.id = f.memory_id
            WHERE m.scope_id IN ({})
            """.format(scope_placeholders(provider)),
            accessible_scope_params(provider),
        ).fetchone()[0]
        graph_stats = _ops.graph_relation_stats(
            conn, accessible_scope_ids=accessible_scope_params(provider)
        )
        relation_containment = relation_containment_report(
            conn,
            scope_ids=accessible_scope_params(provider),
        )
        relation_frequency = relation_frequency_index_report(conn)
        relation_rebuild = relation_rebuild_queue_report(conn)
        capture_queue = capture_queue_report(provider)
        operator_ledger = operator_ledger_report(conn)
        freshness = fact_freshness_report(conn)
        adjudication_report = dict(
            _runtime_status_view(provider).get("last_adjudication_report") or {}
        )
        writable_scope_ids = tuple(
            str(scope_id)
            for scope_id in (scope_view.get("writable_scope_ids") or [])
            if str(scope_id)
        )
        if writable_scope_ids:
            try:
                adjudication_target = schedule_target_id(writable_scope_ids)
                adjudication_report["schedule"] = adjudication_schedule_status(
                    conn,
                    target_id=adjudication_target,
                )
                adjudication_report["l4_schedule"] = adjudication_schedule_status(
                    conn,
                    target_id=f"{adjudication_target}:l4",
                )
            except sqlite3.OperationalError:
                # Old/read-only stores may predate the governance ledger. Stats
                # remains read-only and reports that durable schedule state is
                # not available instead of creating schema as a side effect.
                adjudication_report["schedule"] = {"status": "unavailable"}
                adjudication_report["l4_schedule"] = {"status": "unavailable"}
    runtime_view = _runtime_status_view(provider)
    retrieval_view = _retrieval_status_view(provider)
    vector_table = str(vector_view.get("table") or "")
    vector_embedder: dict[str, Any] = _as_str_dict(vector_view.get("embedder"))
    if not vector_embedder:
        vector_embedder = _as_str_dict(vector_view.get("embedder"))
    failed_writes = int(runtime_view.get("writer_failed_writes") or 0)
    reported_failures = int(runtime_view.get("writer_reported_failures") or 0)
    return {
        "provider": runtime_view.get("name") or getattr(provider, "name", ""),
        "scope_id": scope_view.get("scope_id") or "",
        "shared_scope_id": scope_view.get("shared_scope_id") or "",
        "accessible_scope_ids": list(scope_view.get("accessible_scope_ids") or []),
        "truth_writer": {
            "role": str(runtime_view.get("truth_writer_role") or "unknown"),
            "owner": sanitized_truth_writer_owner(
                runtime_view.get("truth_writer_owner")
            ),
        },
        "write_transactions": _write_transaction_stats(),
        "auto_adjudication": adjudication_report,
        "total_memories": total,
        "scope_memories": scoped,
        "local_scope_memories": local,
        "shared_scope_memories": shared,
        "shared_pool_scope_memories": shared_pool,
        "shared_pool": {
            "enabled": bool(runtime_view.get("shared_pool_enabled")),
            "write_enabled": bool(runtime_view.get("shared_pool_write_enabled")),
            "pool_id": str(runtime_view.get("shared_pool_id") or ""),
            "scope_id": shared_pool_scope_id,
            "memories": shared_pool,
        },
        "scope_entities": entities,
        "scope_feedback_rows": feedback_rows,
        "graph": graph_stats,
        "relation_containment": relation_containment,
        "relation_frequency_index": relation_frequency,
        "relation_rebuild_queue": relation_rebuild,
        "capture_queue": capture_queue,
        "operator_ledger": operator_ledger,
        "curated_memories": len(_ops.iter_curated_entries(runtime_view.get("hermes_home"))),
        "migration": _public_migration_info(runtime_view.get("migration_info")),
        "background_writer": {
            "thread_alive": bool(runtime_view.get("writer_thread_alive")),
            "failed_writes": failed_writes,
            "unreported_failures": max(0, failed_writes - reported_failures),
            "last_error_type": str(runtime_view.get("writer_last_error_type") or ""),
        },
        "freshness": {
            **freshness,
            "startup_backfill": dict(runtime_view.get("freshness_backfill") or {}),
        },
        "journal_digest": {
            "thread_alive": bool(runtime_view.get("journal_digest_thread_alive")),
            "last_started": float(runtime_view.get("journal_digest_last_started") or 0.0),
            "last_finished": float(runtime_view.get("journal_digest_last_finished") or 0.0),
            "last_status": str(runtime_view.get("journal_digest_last_status") or "never_run"),
            "last_error": str(runtime_view.get("journal_digest_last_error") or ""),
            "consecutive_failures": int(runtime_view.get("journal_digest_consecutive_failures") or 0),
        },
        "vector": {
            "schema_version": str(vector_view.get("schema_version") or "vector_status.v1"),
            "enabled": bool(vector_view.get("enabled")),
            "ready": bool(vector_view.get("ready")),
            "state": str(vector_view.get("state") or ""),
            "status": str(vector_view.get("status") or ""),
            "reason_code": str(vector_view.get("reason_code") or ""),
            "auto_recoverable": bool(vector_view.get("auto_recoverable")),
            "repair_required": bool(vector_view.get("repair_required")),
            "usable_for_query": bool(vector_view.get("usable_for_query")),
            "message": str(vector_view.get("message") or ""),
            "debt_counts": {
                str(key): int(value or 0)
                for key, value in dict(vector_view.get("debt_counts") or {}).items()
            },
            "backend": str(vector_view.get("backend") or ""),
            "table": vector_table,
            "row_count": int(vector_view.get("row_count") or 0),
            "unique_id_count": int(vector_view.get("unique_id_count") or 0),
            "duplicate_row_count": int(vector_view.get("duplicate_row_count") or 0),
            "sync_mode": str(vector_view.get("sync_mode") or "incremental"),
            "embedder": vector_embedder,
            "fallback_embedder": dict(vector_view.get("fallback_embedder") or {}),
        },
        "retrieval": {
            "mode": str(retrieval_view.get("mode") or "lexical"),
            "lexical_weight": float(retrieval_view.get("lexical_weight") or 1.0),
            "vector_weight": float(retrieval_view.get("vector_weight") or 0.0),
        },
    }
