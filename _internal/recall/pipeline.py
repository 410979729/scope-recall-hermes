"""Composable ranking and filtering stages for Scope Recall retrieval.

The pipeline keeps candidate merging, lifecycle filters, scoring, and prompt-budget trimming inspectable for benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ...gating import query_intent_terms, query_requests_current_state
from ...lifecycle_policy import ordinary_recall_lifecycle_visible
from ...models import RecallItem
from ...capture_filters import redact_secret_like_text, sanitize_structured_value


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


def normalize_recall_mode(recall_mode: str | None) -> str:
    normalized = str(recall_mode or "advisory").strip().lower()
    if normalized not in {"advisory", "strict"}:
        raise ValueError("recall_mode must be advisory or strict")
    return normalized


def normalize_query(query: str) -> dict[str, Any]:
    """Pure query-normalization stage. Ranking is not decided here."""

    text = str(query or "")
    return {
        "query": text,
        "intent_terms": query_intent_terms(text),
        "current_state_requested": query_requests_current_state(text),
    }


def filter_recall_lifecycle(items: list[RecallItem]) -> list[RecallItem]:
    """Hide archived/candidate/scratch rows except same-scope general scratch."""

    return [
        item
        for item in items
        if ordinary_recall_lifecycle_visible(
            lifecycle=str((item.metadata or {}).get("lifecycle") or ""),
            target=item.target,
        )
    ]


def apply_general_policy(
    items: list[RecallItem],
    *,
    include_general: str,
    general_weight: float,
    general_min_importance: Any,
    current_scope_id: str,
) -> list[RecallItem]:
    """Apply include_general / weight / min-importance without touching durable rows."""

    mode = str(include_general or "same-scope").strip().lower()
    if mode not in {"same-scope", "never", "always"}:
        mode = "same-scope"
    weight = max(0.0, min(1.0, float(general_weight)))
    output: list[RecallItem] = []
    for item in items:
        if item.target != "general":
            output.append(item)
            continue
        if mode == "never":
            continue
        scope_id = str((item.metadata or {}).get("scope_id") or "")
        if mode == "same-scope" and scope_id and scope_id != str(current_scope_id):
            continue
        if mode != "always" and general_min_importance is not None:
            try:
                min_importance = float(general_min_importance)
            except (TypeError, ValueError):
                min_importance = -1.0
            raw_importance = (item.metadata or {}).get("importance")
            try:
                importance = float(raw_importance) if raw_importance not in (None, "") else 0.0
            except (TypeError, ValueError):
                importance = 0.0
            if min_importance >= 0.0 and importance < min_importance:
                continue
        if weight < 1.0:
            meta = dict(item.metadata or {})
            for key in ("lexical_score", "vector_score", "bm25_score", "rrf_score"):
                meta[key] = float(meta.get(key) or 0.0) * weight
            meta["general_weight"] = weight
            item.metadata = meta
        output.append(item)
    return output


def trim_recall_budget(ranked: list[RecallItem], *, limit: int) -> list[RecallItem]:
    """Keep the first N ranked items. Sanitization stays at the egress wrapper."""

    return list(ranked[: max(0, int(limit))])


def safe_recall_item(item: RecallItem) -> RecallItem:
    """Redact legacy sensitive payloads at the model/tool egress boundary."""

    safe_metadata, _ = sanitize_structured_value(item.metadata or {})
    return RecallItem(
        id=item.id,
        content=redact_secret_like_text(item.content),
        summary=redact_secret_like_text(item.summary),
        source=redact_secret_like_text(item.source),
        target=redact_secret_like_text(item.target),
        score=item.score,
        updated_at=item.updated_at,
        metadata=safe_metadata if isinstance(safe_metadata, dict) else {},
    )


def sanitize_recall_window(
    ranked: list[RecallItem],
    *,
    limit: int,
) -> list[RecallItem]:
    """Sanitize only items that can cross the recall egress boundary."""

    return [safe_recall_item(item) for item in trim_recall_budget(ranked, limit=limit)]


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
            "freshness_strict_excluded": 0,
            "relation_contradiction_suppressed": 0,
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
