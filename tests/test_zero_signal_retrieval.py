"""Production-orchestrator regressions for negative retrieval behavior."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from scope_recall.memory_queries import context_payload
from scope_recall.models import RecallItem
from scope_recall.prompting import render_current_turn_recall
from scope_recall.recall import RecallService


def _vector_item(memory_id: str, score: float) -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=f"Unrelated neighbor {memory_id}: workstation timezone America/New_York.",
        summary="Workstation timezone preference.",
        source="tool-store",
        target="memory",
        score=score,
        updated_at="2026-08-30T00:00:00+00:00",
        metadata={
            "lexical_score": 0.0,
            "vector_score": score,
            "scope_id": "shared-scope",
        },
    )


def _separated_vector_field() -> list[RecallItem]:
    return [
        _vector_item("vector-head", 0.99),
        _vector_item("vector-two", 0.80),
        _vector_item("vector-three", 0.70),
        _vector_item("vector-four", 0.60),
        _vector_item("vector-background", 0.50),
    ]


class _Provider:
    def __init__(self, vector_items: list[RecallItem]) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._retrieval_config = {
            "mode": "hybrid",
            "min_score": 0.18,
            "candidate_pool": 5,
            "vector_only_min_score": 0.70,
            "vector_only_min_margin": 0.035,
            "zero_signal_gate_enabled": True,
        }
        self._vector_config = {}
        self._config = {
            "auto_recall": True,
            "auto_recall_min_length": 1,
            "auto_recall_min_repeated": 0,
            "query_char_limit": 1000,
        }
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._scope = SimpleNamespace(agent_context="primary")
        self._last_recall_turns: dict[str, int] = {}
        self._current_turn = 1
        self._vector_items = vector_items
        self._recall_service = RecallService(self)

    @staticmethod
    def _search_db_memories(query: str, *, limit: int):
        del query, limit
        return []

    def _search_vector_memories(self, query: str, *, limit: int):
        del query
        return self._vector_items[:limit]

    def _search_vector_memories_with_vector(self, query_vector, *, limit: int):
        del query_vector
        return self._vector_items[:limit]

    @staticmethod
    def _search_curated_memories(query: str):
        del query
        return []

    @staticmethod
    def _dedup_key(content: str) -> str:
        return content.casefold()

    @staticmethod
    def _config_value(key: str, default):
        del key
        return default

    @staticmethod
    def _normalize_query(query: str, limit: int) -> str:
        return str(query)[:limit]

    @staticmethod
    def recall_limit() -> int:
        return 5

    def recall_service_view(self) -> RecallService:
        return self._recall_service

    @staticmethod
    def _mark_recalled(memory_ids: list[str]) -> None:
        del memory_ids


@pytest.mark.parametrize(
    "query",
    [
        "550e8400-e29b-41d4-a716-446655440000",
        "c799ccd3",
        "U2NvcGVSZWNhbGxSYW5kb21QYXlsb2Fk",
        "qzxvbnmkjhgf",
        "qzxvbnmkjhgfdspoiuytrewq",
        "峰猫泪珠墙锁qzxvbn",
        "㐀㐁㐂㐃㐄㐅㐆㐇",
        "!!! 🚀 ???",
        "XAS-OPS-404",
        "the",
    ],
)
def test_negative_zero_signal_is_empty_on_search_context_and_prefetch(query: str) -> None:
    # A real background field makes this a classifier/admission test rather
    # than allowing missing margin evidence to mask an opaque-query leak.
    provider = _Provider(_separated_vector_field())
    service = provider.recall_service_view()
    before_changes = provider._conn.total_changes

    assert service.search_memories(query, limit=5) == []
    assert provider._conn.total_changes == before_changes
    assert context_payload(provider, query=query, limit=5)["results"] == []
    assert provider._conn.total_changes == before_changes
    assert render_current_turn_recall(provider, query) == ""
    assert provider._conn.total_changes == before_changes
    assert service.last_funnel_trace["zero_signal_query"] is True
    assert service.last_funnel_trace["filters"]["no_admissible_evidence"] == 1
    provider._conn.close()


def test_single_semantic_vector_neighbor_has_no_margin_evidence() -> None:
    provider = _Provider([_vector_item("only-neighbor", 0.99)])
    service = provider.recall_service_view()

    assert service.search_memories("durable architecture retrieval", limit=5) == []
    assert (
        service.last_funnel_trace["filters"]["vector_background_unavailable"]
        == 1
    )
    provider._conn.close()


def test_flat_vector_neighbor_field_does_not_force_a_winner() -> None:
    provider = _Provider(
        [
            _vector_item("flat-one", 0.91),
            _vector_item("flat-two", 0.89),
        ]
    )
    service = provider.recall_service_view()

    assert service.search_memories("durable memory architecture", limit=5) == []
    trace = service.last_funnel_trace
    assert trace["filters"]["vector_only_below_min_margin"] == 2
    assert trace["vector_only_admission_count"] == 0


def test_vector_background_calibration_is_independent_of_output_limit() -> None:
    returned_by_limit: dict[int, list[str]] = {}
    for limit in (1, 5, 20):
        provider = _Provider(_separated_vector_field())
        returned_by_limit[limit] = [
            item.id
            for item in provider.recall_service_view().search_memories(
                "durable architecture retrieval",
                limit=limit,
            )
        ]
        assert provider.recall_service_view().last_funnel_trace["vector_top_k"] >= 5
        provider._conn.close()

    assert all("vector-head" in ids for ids in returned_by_limit.values())


def test_exact_identifier_is_admitted_before_same_content_deduplication() -> None:
    exact = RecallItem(
        id="PR-57",
        content="Duplicate release-control content.",
        summary="PR-57 exact release-control record.",
        source="tool-store",
        target="memory",
        score=0.95,
        updated_at="2026-08-29T00:00:00+00:00",
        metadata={"lexical_score": 0.95, "scope_id": "shared-scope"},
    )
    newer_unrelated_id = RecallItem(
        id="newer-row",
        content=exact.content,
        summary="Newer unrelated duplicate summary.",
        source="tool-store",
        target="memory",
        score=0.96,
        updated_at="2026-08-30T00:00:00+00:00",
        metadata={"lexical_score": 0.96, "scope_id": "shared-scope"},
    )
    provider = _Provider([])
    provider._search_db_memories = lambda _query, *, limit: [  # type: ignore[method-assign]
        newer_unrelated_id,
        exact,
    ][:limit]

    service = provider.recall_service_view()
    results = service.search_memories("PR-57", limit=5)

    assert [item.id for item in results] == ["PR-57"], service.last_funnel_trace
    provider._conn.close()


def test_multi_query_fusion_is_fail_closed_if_admission_marker_is_lost() -> None:
    provider = _Provider([])
    service = provider.recall_service_view()
    admitted = _vector_item("admitted", 0.9)
    admitted.metadata["candidate_admission"] = {"admitted": True}
    missing_marker = _vector_item("missing-marker", 0.95)

    def fake_search(query, **_kwargs):
        service.last_funnel_trace = {"query_signal_state": "positive"}
        return [admitted] if query == "main" else [missing_marker]

    service._search_memories_internal = fake_search  # type: ignore[method-assign]

    results = service.search_evidence_set(
        "main",
        query_variants=["variant"],
        limit=5,
    )

    assert [item.id for item in results] == ["admitted"]
    assert service.last_evidence_set_trace["candidate_admission_egress_rejected"] == 1
