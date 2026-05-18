from __future__ import annotations

from scope_recall.models import RecallItem
from scope_recall.recall import RecallService
from scope_recall.scoring import lexical_score


class DummyProvider:
    def __init__(self, retrieval_config):
        self._retrieval_config = dict(retrieval_config)
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]

    def _search_db_memories(self, query, *, limit):
        return [
            RecallItem(
                id="general-1",
                content="Deploy command is uv run app.",
                summary="Deploy command is uv run app.",
                source="turn-user",
                target="general",
                score=1.0,
                updated_at="2026-05-01T00:00:00+00:00",
                metadata={"lexical_score": 1.0, "vector_score": 0.0, "scope_id": self._scope_id},
            ),
            RecallItem(
                id="memory-1",
                content="Deploy command is uv run app.",
                summary="Deploy command is uv run app.",
                source="tool-store",
                target="memory",
                score=0.8,
                updated_at="2026-05-01T00:00:00+00:00",
                metadata={"lexical_score": 0.8, "vector_score": 0.0, "scope_id": self._shared_scope_id},
            ),
        ]

    def _search_vector_memories(self, query, *, limit):
        return []

    def _search_curated_memories(self, query):
        return []

    def _dedup_key(self, content):
        return str(content).lower()

    def _config_value(self, key, default):
        return default


def test_lexical_score_durable_target_beats_comparable_general_scratch():
    general = lexical_score(
        query="deploy command uv run app",
        content="Deploy command is uv run app.",
        summary="Deploy command is uv run app.",
        source="turn-user",
        target="general",
    )
    durable = lexical_score(
        query="deploy command uv run app",
        content="Deploy command is uv run app.",
        summary="Deploy command is uv run app.",
        source="tool-store",
        target="memory",
    )

    assert durable > general


def test_include_general_never_suppresses_general_in_automatic_recall():
    provider = DummyProvider({"mode": "lexical", "include_general": "never", "general_weight": 0.35, "min_score": 0.18})

    results = RecallService(provider).search_memories("deploy command", limit=5)

    assert [item.target for item in results] == ["memory"]


def test_include_general_same_scope_downranks_but_keeps_local_scratch():
    provider = DummyProvider({"mode": "lexical", "include_general": "same-scope", "general_weight": 0.35, "min_score": 0.18})

    results = RecallService(provider).search_memories("deploy command", limit=5)

    assert [item.target for item in results] == ["memory", "general"]
    assert results[0].score > results[1].score


def test_include_general_always_allows_general_debug_mode():
    provider = DummyProvider({"mode": "lexical", "include_general": "always", "general_weight": 1.0, "min_score": 0.18})

    results = RecallService(provider).search_memories("deploy command", limit=5)

    assert {item.target for item in results} == {"memory", "general"}
