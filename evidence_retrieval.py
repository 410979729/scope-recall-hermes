"""Multi-query evidence-set fusion for long-context and multi-hop recall.

The normal recall path answers one query and remains unchanged. This module
combines independently ranked query variants without an LLM dependency,
preserves per-query provenance, and reserves evidence coverage across variants.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .models import RecallItem
from .schemas import DEFAULT_EVIDENCE_DIVERSITY_DEPTH, MAX_EVIDENCE_DIVERSITY_DEPTH


def merge_evidence_rankings(
    rankings: list[tuple[str, list[RecallItem]]],
    *,
    limit: int,
    rrf_k: int = 60,
    diversity_depth: int = DEFAULT_EVIDENCE_DIVERSITY_DEPTH,
) -> list[RecallItem]:
    """Fuse ranked evidence lists with RRF and deterministic query diversity.

    Duplicate query variants are ignored rather than double-counted. By default,
    the top three hits from every explicit variant receive round-robin diversity
    slots before the remaining candidates are filled by the global RRF rank.
    Callers may explicitly request up to six slots for broad multi-hop evidence
    sets; the limit-per-query ratio still bounds small result windows.
    """

    bounded_limit = max(1, int(limit or 1))
    bounded_rrf_k = max(1, int(rrf_k or 1))
    bounded_diversity_depth = max(
        1,
        min(
            MAX_EVIDENCE_DIVERSITY_DEPTH,
            int(diversity_depth or DEFAULT_EVIDENCE_DIVERSITY_DEPTH),
        ),
    )
    unique_rankings: list[tuple[str, list[RecallItem]]] = []
    seen_queries: set[str] = set()
    for raw_query, items in rankings:
        query = str(raw_query or "").strip()
        normalized = query.casefold()
        if not query or normalized in seen_queries:
            continue
        seen_queries.add(normalized)
        unique_rankings.append((query, list(items)))

    states: dict[str, dict[str, Any]] = {}
    for query, items in unique_rankings:
        seen_ids: set[str] = set()
        for rank, item in enumerate(items, 1):
            memory_id = str(item.id or "").strip()
            if not memory_id or memory_id in seen_ids:
                continue
            seen_ids.add(memory_id)
            state = states.setdefault(
                memory_id,
                {
                    "item": item,
                    "rrf": 0.0,
                    "ranks": {},
                    "max_score": float(item.score or 0.0),
                },
            )
            if float(item.score or 0.0) > float(state["max_score"]):
                state["item"] = item
                state["max_score"] = float(item.score or 0.0)
            state["rrf"] += 1.0 / (bounded_rrf_k + rank)
            state["ranks"][query] = rank

    def sort_key(memory_id: str) -> tuple[int, float, float, str]:
        state = states[memory_id]
        return (
            len(state["ranks"]),
            float(state["rrf"]),
            float(state["max_score"]),
            memory_id,
        )

    selected: list[str] = []
    selected_set: set[str] = set()

    per_query_quota = min(
        bounded_diversity_depth,
        max(1, bounded_limit // max(1, len(unique_rankings))),
    )
    ranked_ids = [
        [
            str(item.id or "").strip()
            for item in items
            if str(item.id or "").strip() in states
        ]
        for _query, items in unique_rankings
    ]
    for rank_index in range(per_query_quota):
        for ids in ranked_ids:
            if rank_index >= len(ids):
                continue
            memory_id = ids[rank_index]
            if memory_id in selected_set:
                continue
            selected.append(memory_id)
            selected_set.add(memory_id)
            if len(selected) >= bounded_limit:
                break
        if len(selected) >= bounded_limit:
            break

    remaining = sorted(
        (memory_id for memory_id in states if memory_id not in selected_set),
        key=sort_key,
        reverse=True,
    )
    selected.extend(remaining[: max(0, bounded_limit - len(selected))])

    output: list[RecallItem] = []
    for memory_id in selected:
        state = states[memory_id]
        item = state["item"]
        metadata = dict(item.metadata or {})
        metadata.update(
            {
                "evidence_rrf_score": float(state["rrf"]),
                "evidence_query_hits": len(state["ranks"]),
                "evidence_query_ranks": dict(state["ranks"]),
                "evidence_original_score_max": float(state["max_score"]),
            }
        )
        output.append(replace(item, metadata=metadata))
    return output
