"""Tests for human-readable recall explanation payloads."""

from __future__ import annotations

import json

from scope_recall.memory_ops import explain_query
from scope_recall.models import RecallItem
from scope_recall.recall_pipeline import humanize_filter_trace, humanize_recall_components
from scope_recall.tooling import ScopeRecallToolService


class FakeRecallService:
    def __init__(self) -> None:
        self.last_rejected_candidates = [
            RecallItem(
                id="rejected-low",
                content="low confidence vector-only result",
                summary="low confidence vector-only result",
                source="tool-store",
                target="memory",
                score=0.12,
                updated_at="2026-01-01T00:00:00+00:00",
                metadata={"vector_score": 0.2, "final_score": 0.12, "min_score": 0.18, "rejected_reason": "below_min_score"},
            )
        ]
        self.last_funnel_trace = {
            "filters": {
                "lifecycle_removed": 2,
                "general_policy_removed": 1,
                "entity_scope_mismatch": 0,
                "vector_only_below_min_score": 1,
                "below_min_score": 1,
            }
        }

    def search_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        return [
            RecallItem(
                id="hit-1",
                content="Scope Recall explain should expose lexical vector and relation signals.",
                summary="Scope Recall explain evidence",
                source="tool-store",
                target="memory",
                score=0.82,
                updated_at="2026-01-01T00:00:00+00:00",
                metadata={
                    "lexical_score": 0.7,
                    "vector_score": 0.62,
                    "rrf_score": 0.05,
                    "recency_bonus": 0.03,
                    "relation_rerank_bonus": 0.02,
                    "entity_overlap_bonus": 0.04,
                    "entity_distance_bonus": 0.01,
                    "temporal_decay_multiplier": 0.9,
                    "final_score": 0.82,
                    "min_score": 0.18,
                },
            )
        ]


class FakeProvider:
    def __init__(self) -> None:
        self._recall_service = FakeRecallService()
        self._retrieval_config = {"top_k": 5}

    def _config_value(self, key: str, default):
        return default

    def _normalize_query(self, query: str, limit: int) -> str:
        return query[:limit].strip()

    def _explain_query(self, *, query: str, limit: int):
        return explain_query(self, query=query, limit=limit)


def test_humanize_recall_components_names_scores_and_reasons():
    payload = humanize_recall_components(
        {
            "lexical_score": 0.5,
            "vector_score": 0.4,
            "rrf_score": 0.1,
            "recency_bonus": 0.03,
            "relation_rerank_bonus": -0.02,
            "entity_overlap_bonus": 0.04,
            "final_score": 0.7,
            "min_score": 0.18,
        }
    )

    assert payload["score_breakdown"]["lexical_score"] == 0.5
    assert payload["score_breakdown"]["rrf_contribution"] == 0.1
    assert any("lexical" in item for item in payload["why_included"])
    assert any("relation" in item for item in payload["why_included"])


def test_humanize_filter_trace_explains_lifecycle_and_threshold_filters():
    payload = humanize_filter_trace({"filters": {"lifecycle_removed": 2, "below_min_score": 1}})

    by_filter = {item["filter"]: item for item in payload}
    assert by_filter["lifecycle_removed"]["count"] == 2
    assert "lifecycle policy" in by_filter["lifecycle_removed"]["meaning"]
    assert by_filter["below_min_score"]["count"] == 1


def test_explain_query_returns_humanized_results_rejections_and_filter_explanations():
    payload = explain_query(FakeProvider(), query="scope recall explain", limit=5)

    assert payload["count"] == 1
    result = payload["results"][0]
    assert result["score_breakdown"]["vector_score"] == 0.62
    assert result["score_breakdown"]["freshness_bonus"] == 0.03
    assert any("RRF" in item for item in result["why_included"])
    assert payload["filter_explanations"][0]["filter"] == "lifecycle_removed"
    rejected = payload["rejected_candidates"][0]
    assert any("below_min_score" in item or "threshold" in item for item in rejected["why_excluded"])


def test_tooling_explain_marks_payload_humanized():
    service = ScopeRecallToolService(FakeProvider())

    payload = json.loads(service.handle("scope_recall_explain", {"query": "scope recall explain", "limit": 3}))

    assert payload["humanized"] is True
    assert payload["results"][0]["score_breakdown"]["lexical_score"] == 0.7
