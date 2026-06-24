from __future__ import annotations

from scope_recall.models import RecallItem
from scope_recall.recall import RecallService


class DummyProvider:
    def __init__(self, retrieval_config, items):
        self._retrieval_config = dict(retrieval_config)
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._items = list(items)

    def _search_db_memories(self, query, *, limit):
        return self._items[:limit]

    def _search_vector_memories(self, query, *, limit):
        return []

    def _search_curated_memories(self, query):
        return []

    def _dedup_key(self, content):
        return str(content).lower()

    def _config_value(self, key, default):
        return default


def _item(memory_id: str, *, memory_type: str, target: str = "project") -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=f"Project Atlas deploy command memory {memory_id}.",
        summary=f"Project Atlas deploy command memory {memory_id}.",
        source="tool-store",
        target=target,
        score=0.8,
        updated_at="2020-01-01T00:00:00+00:00",
        metadata={
            "lexical_score": 0.8,
            "vector_score": 0.0,
            "bm25_score": 0.0,
            "memory_type": memory_type,
            "scope_id": "shared-scope",
        },
    )


def test_temporal_policy_decays_durable_facts_less_than_episodic_rows():
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "include_general": "same-scope",
            "temporal_decay_enabled": True,
            "temporal_decay_weight": 1.0,
            "temporal_decay_half_life_days": 7,
            "temporal_decay_floor": 0.1,
            "temporal_policy_enabled": True,
        },
        [
            _item("durable-project-fact", memory_type="project"),
            _item("episodic-chat-note", memory_type="episodic"),
        ],
    )

    results = RecallService(provider).search_memories("Project Atlas deploy command", limit=2)

    by_id = {item.id: item for item in results}
    assert by_id["durable-project-fact"].score > by_id["episodic-chat-note"].score
    assert by_id["durable-project-fact"].metadata["temporal_policy_class"] == "durable_fact"
    assert by_id["durable-project-fact"].metadata["temporal_policy_weight"] < by_id["episodic-chat-note"].metadata["temporal_policy_weight"]


def test_temporal_policy_can_be_disabled_for_legacy_decay_behavior():
    provider = DummyProvider(
        {
            "mode": "lexical",
            "min_score": 0.01,
            "include_general": "same-scope",
            "temporal_decay_enabled": True,
            "temporal_decay_weight": 1.0,
            "temporal_decay_half_life_days": 7,
            "temporal_decay_floor": 0.1,
            "temporal_policy_enabled": False,
        },
        [
            _item("durable-project-fact", memory_type="project"),
            _item("episodic-chat-note", memory_type="episodic"),
        ],
    )

    results = RecallService(provider).search_memories("Project Atlas deploy command", limit=2)
    by_id = {item.id: item for item in results}

    assert by_id["durable-project-fact"].metadata["temporal_policy_class"] == "disabled"
    assert by_id["episodic-chat-note"].metadata["temporal_policy_class"] == "disabled"
    assert by_id["durable-project-fact"].score == by_id["episodic-chat-note"].score
