"""Temporal adapter defers eligibility to the bound host capability."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from scope_recall._internal.recall.deadline import RequestDeadline
from scope_recall.recall_source_adapters import bind_source_capabilities
from scope_recall._internal.recall.sources import RecallSourceContext
from scope_recall.models import RecallItem
from scope_recall.recall import RecallService


def _item(memory_id: str, content: str, *, temporal: bool = False) -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=content,
        summary=content,
        source="temporal-fact" if temporal else "tool-store",
        target="user",
        score=0.92,
        updated_at="2026-08-30T00:00:00+00:00",
        metadata={
            "lexical_score": 0.92,
            "vector_score": 0.0,
            "scope_id": "shared-scope",
            "importance": 0.8,
            "confidence": 0.9,
            "memory_type": "factual",
            "temporal_authoritative": temporal,
            "temporal_fact_current": temporal,
        },
    )


def _source_context(query: str = "Where does the sample user live now?") -> RecallSourceContext:
    return RecallSourceContext(
        query=query,
        candidate_pool=5,
        vector_depth=5,
        query_vector=None,
        deadline=RequestDeadline.from_budget(8.0),
    )


class _ConfiglessProvider:
    """Ordinary search host with no raw ``temporal_queries`` configuration."""

    def __init__(self) -> None:
        self._config = {
            "auto_recall": True,
            "auto_recall_min_length": 1,
            "auto_recall_min_repeated": 0,
            "query_char_limit": 1000,
        }
        self._retrieval_config = {
            "mode": "hybrid",
            "min_score": 0.18,
            "candidate_pool": 5,
            "top_k": 5,
        }
        self._vector_config: dict[str, Any] = {}
        self._scope_id = "local-scope"
        self._shared_scope_id = "shared-scope"
        self._accessible_scope_ids = [self._scope_id, self._shared_scope_id]
        self._scope = SimpleNamespace(agent_context="primary")
        self._last_recall_turns: dict[str, int] = {}
        self._current_turn = 1
        self.conn_calls = 0

    def _search_db_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        del query, limit
        return []

    def _search_vector_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        del query, limit
        return []

    def _search_vector_memories_with_vector(
        self, query_vector: list[float], *, limit: int
    ) -> list[RecallItem]:
        del query_vector, limit
        return []

    def _search_curated_memories(self, query: str) -> list[RecallItem]:
        del query
        return []

    @staticmethod
    def _dedup_key(content: str) -> str:
        return content.casefold()

    @staticmethod
    def _config_value(key: str, default: Any) -> Any:
        del key
        return default

    @staticmethod
    def _normalize_query(query: str, limit: int) -> str:
        return str(query)[:limit]

    @staticmethod
    def recall_limit() -> int:
        return 5

    def _mark_recalled(self, memory_ids: list[str]) -> None:
        del memory_ids


class _DisabledSqlProvider(_ConfiglessProvider):
    def _require_conn(self) -> Any:
        self.conn_calls += 1
        raise AssertionError("disabled temporal must not open temporal SQL")


def test_overridden_temporal_capability_is_used_without_raw_temporal_queries_config() -> None:
    temporal = _item(
        "temporal-current-city",
        "The sample user lives in Tokyo.",
        temporal=True,
    )
    calls: list[str] = []

    class _OverrideService(RecallService):
        def _temporal_current_candidates(
            self,
            query: str,
            *,
            limit: int,
            candidate_memory_ids: list[str],
        ) -> tuple[list[RecallItem], frozenset[str]] | None:
            del query, limit, candidate_memory_ids
            calls.append("override")
            return ([temporal], frozenset())

    provider = _ConfiglessProvider()
    assert "temporal_queries" not in provider._config
    service = _OverrideService(provider)

    collected = bind_source_capabilities(service).temporal(_source_context(), ())
    assert calls == ["override"]
    assert collected.state == "ok"
    assert [item.id for item in collected.items] == ["temporal-current-city"]

    returned_ids = {
        item.id
        for item in service.search_memories(
            "Where does the sample user live now?",
            limit=5,
        )
    }
    assert "temporal-current-city" in returned_ids
    assert provider.conn_calls == 0


def test_standard_disabled_temporal_capability_does_not_access_temporal_sql() -> None:
    configs: tuple[dict[str, Any], ...] = (
        {},
        {"temporal_queries": {"enabled": False}},
    )
    for extra in configs:
        provider = _DisabledSqlProvider()
        provider._config.update(extra)
        service = RecallService(provider)
        collected = bind_source_capabilities(service).temporal(_source_context(), ())
        assert collected.state == "skipped"
        assert collected.reason_code == "temporal_disabled"
        assert provider.conn_calls == 0
        assert service.search_memories("Where does the sample user live now?", limit=5) == []
        assert provider.conn_calls == 0
