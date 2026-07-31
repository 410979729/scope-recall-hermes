"""Composable ranking and filtering stages for Scope Recall retrieval.

The pipeline keeps candidate merging, lifecycle filters, scoring, and prompt-budget trimming inspectable for benchmarks."""

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
            int((item.metadata or {}).get("current_state_rank") or 0),
            item.score,
            bool((item.metadata or {}).get("intent_matched")),
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


def humanize_filter_trace(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert raw funnel filter counters into operator-readable reasons."""
    filters = trace.get("filters") if isinstance(trace, dict) else {}
    filters = filters if isinstance(filters, dict) else {}
    labels = {
        "lifecycle_removed": "Rows hidden by lifecycle policy, such as archived, superseded, rejected, candidate, or in-progress memories.",
        "general_policy_removed": "General scratch rows removed by the configured general-memory policy.",
        "entity_scope_mismatch": "Rows removed because entity scoping did not match the query context.",
        "vector_only_below_min_score": "Vector-only rows rejected because their semantic score was below the stricter vector-only threshold.",
        "below_min_score": "Rows rejected because final score was below the retrieval minimum.",
    }
    explanations: list[dict[str, Any]] = []
    for key, label in labels.items():
        try:
            count = int(filters.get(key) or 0)
        except (TypeError, ValueError):
            count = 0
        explanations.append({"filter": key, "count": count, "meaning": label})
    return explanations


def humanize_recall_components(components: dict[str, Any], *, rejected: bool = False) -> dict[str, Any]:
    """Build a stable human-readable explanation from numeric retrieval components."""
    def _float(key: str, default: float = 0.0) -> float:
        try:
            return float(components.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    lexical_score = _float("lexical_score")
    vector_score = _float("vector_score")
    rrf_score = _float("rrf_score")
    recency_bonus = _float("recency_bonus")
    relation_bonus = _float("relation_rerank_bonus")
    entity_bonus = _float("entity_overlap_bonus") + _float("entity_distance_bonus")
    temporal_multiplier = _float("temporal_decay_multiplier", 1.0)
    final_score = _float("final_score")
    min_score = _float("min_score")
    rejected_reason = str(components.get("rejected_reason") or "")
    why: list[str] = []
    if lexical_score > 0:
        why.append(f"lexical match contributed {lexical_score:.3f}")
    if vector_score > 0:
        why.append(f"vector similarity contributed {vector_score:.3f}")
    if rrf_score > 0:
        why.append(f"RRF fusion contributed {rrf_score:.3f}")
    if recency_bonus > 0:
        why.append(f"freshness/recency added {recency_bonus:.3f}")
    if relation_bonus != 0:
        why.append(f"relation rerank adjusted score by {relation_bonus:.3f}")
    if entity_bonus > 0:
        why.append(f"entity evidence added {entity_bonus:.3f}")
    if temporal_multiplier != 1.0:
        why.append(f"temporal policy multiplied score by {temporal_multiplier:.3f}")
    if not why:
        why.append("no strong individual scoring signal was recorded")
    if rejected:
        why.append(rejected_reason or f"final score {final_score:.3f} did not pass threshold {min_score:.3f}")
    else:
        why.append(f"final score is {final_score:.3f}")
    return {
        "score_breakdown": {
            "lexical_score": lexical_score,
            "vector_score": vector_score,
            "rrf_contribution": rrf_score,
            "freshness_bonus": recency_bonus,
            "relation_adjustment": relation_bonus,
            "entity_bonus": entity_bonus,
            "temporal_multiplier": temporal_multiplier,
            "final_score": final_score,
            "min_score": min_score,
        },
        "why_excluded" if rejected else "why_included": why,
    }
