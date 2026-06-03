from __future__ import annotations

from scope_recall.models import RecallItem
from scope_recall.recall import RecallService
from scope_recall.scoring import bm25_to_score


class DummyProvider:
    def __init__(self, retrieval_config, *, db_items=None, vector_items=None):
        self._retrieval_config = dict(retrieval_config)
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._db_items = list(db_items or [])
        self._vector_items = list(vector_items or [])

    def _search_db_memories(self, query, *, limit):
        return self._db_items[:limit]

    def _search_vector_memories(self, query, *, limit):
        return self._vector_items[:limit]

    def _search_curated_memories(self, query):
        return []

    def _dedup_key(self, content):
        return str(content).lower()

    def _config_value(self, key, default):
        return default


def _item(memory_id: str, *, bm25: float = 0.0, lexical: float = 0.5, updated_at: str = "2026-06-01T00:00:00+00:00", created_at: str | None = None, source: str = "tool-store") -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=f"Scope Recall roadmap retrieval fact {memory_id}.",
        summary=f"Scope Recall roadmap retrieval fact {memory_id}.",
        source=source,
        target="memory",
        score=lexical,
        updated_at=updated_at,
        metadata={
            "lexical_score": lexical,
            "vector_score": 0.0,
            "bm25_score": bm25,
            "scope_id": "shared-scope",
            "created_at": created_at or updated_at,
        },
    )


def test_bm25_to_score_treats_lower_sqlite_bm25_as_better():
    scores = bm25_to_score({"weak": -0.1, "strong": -3.0})

    assert scores["strong"] > scores["weak"]
    assert scores["strong"] == 1.0
    assert 0.0 <= scores["weak"] < 1.0


def test_hybrid_final_score_uses_normalized_bm25_not_only_candidate_pool():
    weak_bm25_first = _item("weak-bm25", bm25=0.05, lexical=0.42)
    strong_bm25_second = _item("strong-bm25", bm25=0.95, lexical=0.42)
    provider = DummyProvider(
        {
            "mode": "hybrid",
            "lexical_weight": 0.40,
            "bm25_weight": 0.25,
            "vector_weight": 0.35,
            "include_general": "same-scope",
            "min_score": 0.18,
        },
        db_items=[weak_bm25_first, strong_bm25_second],
    )

    results = RecallService(provider).search_memories("Scope Recall retrieval", limit=2)

    assert [item.id for item in results] == ["strong-bm25", "weak-bm25"]
    assert results[0].metadata["base_score"] > results[1].metadata["base_score"]


def test_temporal_decay_curve_can_downrank_stale_memories_without_query_freshness_hint():
    old = _item(
        "old-stale",
        bm25=0.0,
        lexical=0.58,
        created_at="2020-01-01T00:00:00+00:00",
        updated_at="2020-01-01T00:00:00+00:00",
    )
    fresh = _item(
        "fresh-slightly-lower",
        bm25=0.0,
        lexical=0.56,
        created_at="2026-06-01T00:00:00+00:00",
        updated_at="2026-06-01T00:00:00+00:00",
    )
    provider = DummyProvider(
        {
            "mode": "lexical",
            "include_general": "same-scope",
            "min_score": 0.18,
            "temporal_decay_enabled": True,
            "temporal_decay_weight": 0.35,
            "temporal_decay_half_life_days": 30,
            "temporal_decay_floor": 0.35,
        },
        db_items=[old, fresh],
    )

    results = RecallService(provider).search_memories("Scope Recall retrieval", limit=2)

    assert [item.id for item in results] == ["fresh-slightly-lower", "old-stale"]
    assert 0.0 < results[1].metadata["temporal_decay_multiplier"] < 1.0
