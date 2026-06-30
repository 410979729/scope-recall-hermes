"""Tests for recall pipeline filtering, merging, ranking, and prompt-budget trimming.

They keep retrieval stages inspectable as ranking policy evolves."""

from __future__ import annotations

from scope_recall.models import RecallItem
from scope_recall.recall_pipeline import build_search_plan, final_trace_payload, initial_trace, merge_recall_candidates, rank_recall_items, recall_dedup_key


def _item(memory_id: str, content: str, *, target: str = "memory", score: float = 0.1, updated_at: str = "2026-01-01T00:00:00+00:00", metadata: dict | None = None) -> RecallItem:
    return RecallItem(id=memory_id, content=content, summary=content[:20], source="test", target=target, score=score, updated_at=updated_at, metadata=metadata or {})


def test_search_plan_and_initial_trace_preserve_existing_keys():
    plan = build_search_plan(limit=2, retrieval_config={"candidate_pool": 5, "top_k": 7}, vector_config={"top_k": 9})
    trace = initial_trace(query="project atlas", plan=plan, accessible_scope_count=3)

    assert plan.bounded_limit == 2
    assert plan.candidate_pool == 5
    assert plan.configured_top_k == 7
    assert plan.vector_top_k == 9
    assert trace["limit"] == 2
    assert trace["filters"] == {
        "lifecycle_removed": 0,
        "general_policy_removed": 0,
        "entity_scope_mismatch": 0,
        "vector_only_below_min_score": 0,
        "below_min_score": 0,
    }


def test_recall_dedup_key_separates_general_and_durable_and_curated():
    def dedup(text: str) -> str:
        return text.lower().replace(" ", "-")

    assert recall_dedup_key(_item("curated:docs", "same"), content_dedup_key=dedup) == "curated:docs"
    assert recall_dedup_key(_item("general", "Same", target="general"), content_dedup_key=dedup) == "scratch:same"
    assert recall_dedup_key(_item("memory", "Same", target="memory"), content_dedup_key=dedup) == "durable:same"


def test_merge_recall_candidates_preserves_best_scores_and_preferred_item():
    older = _item("old", "Duplicate content", score=0.1, updated_at="2026-01-01T00:00:00+00:00", metadata={"lexical_score": 0.2, "base_score": 0.2})
    newer = _item("new", "Duplicate content", score=0.3, updated_at="2026-02-01T00:00:00+00:00", metadata={"vector_score": 0.8, "base_score": 0.3})

    merged = merge_recall_candidates(
        [older, newer],
        content_dedup_key=lambda text: text.lower(),
        preferred_duplicate=lambda current, incoming: current if current.updated_at >= incoming.updated_at else incoming,
        final_score=lambda meta: max(float(meta.get("lexical_score") or 0.0), float(meta.get("vector_score") or 0.0), float(meta.get("base_score") or 0.0)),
    )

    [item] = list(merged.values())
    assert item.id == "new"
    assert item.metadata["lexical_score"] == 0.2
    assert item.metadata["vector_score"] == 0.8
    assert item.score == 0.8


def test_rank_and_final_trace_payload_match_search_ordering_contract():
    items = [
        _item("a", "A", score=0.5, updated_at="2026-01-01T00:00:00+00:00", metadata={"base_score": 0.4}),
        _item("b", "B", score=0.5, updated_at="2026-02-01T00:00:00+00:00", metadata={"base_score": 0.4}),
        _item("c", "C", score=0.6, updated_at="2026-01-01T00:00:00+00:00", metadata={"base_score": 0.1}),
    ]

    ranked = rank_recall_items(items)
    assert [item.id for item in ranked] == ["c", "b", "a"]
    assert final_trace_payload(returned=ranked[:2], ranked_rejected=ranked[2:]) == {
        "returned_count": 2,
        "returned_ids": ["c", "b"],
        "returned_chars": 2,
        "rejected_count": 1,
    }
