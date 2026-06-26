from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .models import RecallItem


@dataclass(frozen=True)
class RecallSearchPlan:
    bounded_limit: int
    configured_candidate_pool: int
    candidate_pool: int
    configured_top_k: int
    vector_top_k: int


def positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(1, parsed)


def build_search_plan(*, limit: int, retrieval_config: dict[str, Any], vector_config: dict[str, Any]) -> RecallSearchPlan:
    bounded_limit = max(1, int(limit or 1))
    configured_candidate_pool = positive_int(retrieval_config.get("candidate_pool"), bounded_limit)
    candidate_pool = max(bounded_limit, configured_candidate_pool)
    configured_top_k = positive_int(retrieval_config.get("top_k"), bounded_limit)
    vector_top_k = max(candidate_pool, positive_int(vector_config.get("top_k"), candidate_pool))
    return RecallSearchPlan(
        bounded_limit=bounded_limit,
        configured_candidate_pool=configured_candidate_pool,
        candidate_pool=candidate_pool,
        configured_top_k=configured_top_k,
        vector_top_k=vector_top_k,
    )


def initial_trace(*, query: str, plan: RecallSearchPlan, accessible_scope_count: int) -> dict[str, Any]:
    return {
        "query": query,
        "limit": plan.bounded_limit,
        "configured_top_k": plan.configured_top_k,
        "candidate_pool": plan.candidate_pool,
        "configured_candidate_pool": plan.configured_candidate_pool,
        "vector_top_k": plan.vector_top_k,
        "accessible_scope_count": accessible_scope_count,
        "stages": {},
        "filters": {
            "lifecycle_removed": 0,
            "general_policy_removed": 0,
            "entity_scope_mismatch": 0,
            "vector_only_below_min_score": 0,
            "below_min_score": 0,
        },
        "timings_ms": {},
    }


def recall_dedup_key(item: RecallItem, *, content_dedup_key: Callable[[str], str]) -> str:
    if item.id.startswith("curated:"):
        return item.id
    dedup_class = "scratch" if item.target == "general" else "durable"
    return f"{dedup_class}:{content_dedup_key(item.content)}"


def merge_recall_candidates(
    candidates: list[RecallItem],
    *,
    content_dedup_key: Callable[[str], str],
    preferred_duplicate: Callable[[RecallItem, RecallItem], RecallItem],
    final_score: Callable[[dict[str, Any]], float],
) -> dict[str, RecallItem]:
    merged: dict[str, RecallItem] = {}
    for item in candidates:
        item_key = recall_dedup_key(item, content_dedup_key=content_dedup_key)
        current = merged.get(item_key)
        if current is None:
            merged[item_key] = item
            continue
        incoming = dict(item.metadata or {})
        preferred = preferred_duplicate(current, item)
        other = item if preferred is current else current
        meta = dict(preferred.metadata or {})
        for meta_key, value in dict(other.metadata or {}).items():
            meta.setdefault(meta_key, value)
        current_meta = dict(current.metadata or {})
        for meta_key in ("lexical_score", "vector_score", "base_score", "recency_bonus", "rrf_score"):
            meta[meta_key] = max(
                float(meta.get(meta_key) or 0.0),
                float(incoming.get(meta_key) or 0.0),
                float(current_meta.get(meta_key) or 0.0),
            )
        preferred.metadata = meta
        preferred.score = final_score(meta)
        merged[item_key] = preferred
    return merged


def rank_recall_items(items: list[RecallItem]) -> list[RecallItem]:
    return sorted(
        items,
        key=lambda item: (
            item.score,
            float((item.metadata or {}).get("base_score") or 0.0),
            item.updated_at,
            item.id,
        ),
        reverse=True,
    )


def final_trace_payload(*, returned: list[RecallItem], ranked_rejected: list[RecallItem]) -> dict[str, Any]:
    return {
        "returned_count": len(returned),
        "returned_ids": [item.id for item in returned],
        "returned_chars": sum(len(str(item.content or "")) for item in returned),
        "rejected_count": len(ranked_rejected),
    }
