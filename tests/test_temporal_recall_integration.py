"""Current temporal facts integrated into ordinary recall without stale leakage."""

from __future__ import annotations

import json
import sqlite3
from threading import RLock

import scope_recall.recall as recall_module
from scope_recall.fact_repository import close_claim_interval, insert_claim
from scope_recall.models import RecallItem
from scope_recall.recall import RecallService
from scope_recall.sql_store import ensure_schema


class TemporalRecallProvider:
    def __init__(self, *, enabled: bool, db_items: list[RecallItem]) -> None:
        self._config = {
            "temporal_queries": {
                "enabled": enabled,
                "timezone": "UTC",
                "current_limit": 50,
            }
        }
        self._retrieval_config = {
            "mode": "lexical",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": False,
            "min_score": 0.0,
        }
        self._scope_id = "scope-a"
        self._shared_scope_id = "scope-a"
        self._accessible_scope_ids = ["scope-a"]
        self._db_items = list(db_items)
        self._lock = RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(self._conn)

    def _search_db_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        return self._db_items[:limit]

    def _search_vector_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        return []

    def _search_curated_memories(self, query: str) -> list[RecallItem]:
        return []

    def _dedup_key(self, content: str) -> str:
        return str(content).strip().casefold()

    def _config_value(self, key: str, default):
        return self._config.get(key, default)

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()


def _memory(
    provider: TemporalRecallProvider,
    memory_id: str,
    *,
    content: str,
    valid_item: bool = True,
) -> RecallItem:
    metadata = {
        "lifecycle": "promoted",
        "memory_type": "factual",
        "scope_id": "scope-a",
        "lexical_score": 0.95,
        "vector_score": 0.0,
        "importance": 0.9,
    }
    provider._conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, 'scope-a', 'fact-executor', 'user', ?, ?,
                  '2026-01-01T00:00:00+00:00',
                  '2026-07-14T00:00:00+00:00', ?)
        """,
        (memory_id, content, content, json.dumps(metadata, sort_keys=True)),
    )
    item = RecallItem(
        id=memory_id,
        content=content,
        summary=content,
        source="fact-executor",
        target="user",
        score=0.95,
        updated_at="2026-07-14T00:00:00+00:00",
        metadata=metadata,
    )
    if valid_item:
        provider._db_items.append(item)
    return item


def _claim(
    provider: TemporalRecallProvider,
    *,
    claim_id: str,
    memory_id: str,
    value: str,
    valid_from: str = "2020-01-01T00:00:00+00:00",
) -> None:
    insert_claim(
        provider._conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id="scope-a",
        subject="Joy",
        predicate="lives in",
        value=value,
        cardinality="single",
        assertion_kind="direct",
        valid_from=valid_from,
        recorded_at="2026-07-14T00:00:00+00:00",
        confidence=0.98,
        source_type="user_message",
        source_ref=f"message:{claim_id}",
    )


def _seed_supersede(provider: TemporalRecallProvider) -> tuple[RecallItem, RecallItem]:
    old = _memory(
        provider,
        "memory-old",
        content="Joy currently lives in Mumbai.",
        valid_item=False,
    )
    new = _memory(
        provider,
        "memory-new",
        content="Joy currently lives in Tokyo.",
        valid_item=False,
    )
    _claim(provider, claim_id="claim-old", memory_id=old.id, value="Mumbai")
    close_claim_interval(
        provider._conn,
        claim_id="claim-old",
        valid_to="2025-01-01T00:00:00+00:00",
        retired_at="2026-07-14T00:00:01+00:00",
        status="superseded",
        superseded_by_claim_id="claim-new",
    )
    _claim(
        provider,
        claim_id="claim-new",
        memory_id=new.id,
        value="Tokyo",
        valid_from="2025-01-01T00:00:00+00:00",
    )
    provider._conn.commit()
    return old, new


def test_temporal_gate_off_preserves_legacy_recall_result():
    provider = TemporalRecallProvider(enabled=False, db_items=[])
    try:
        old, _new = _seed_supersede(provider)
        provider._db_items = [old]

        results = RecallService(provider).search_memories(
            "Where does Joy live now?",
            limit=5,
        )

        assert [item.id for item in results] == ["memory-old"]
        assert not (results[0].metadata or {}).get("temporal_fact_current")
    finally:
        provider.close()


def test_temporal_gate_injects_current_and_suppresses_superseded_memory():
    provider = TemporalRecallProvider(enabled=True, db_items=[])
    try:
        old, _new = _seed_supersede(provider)
        # Simulate a lagging lexical/vector index that only returns the old row.
        provider._db_items = [old]
        before = provider._conn.total_changes

        service = RecallService(provider)
        results = service.search_memories("Where does Joy live now?", limit=5)

        assert provider._conn.total_changes == before
        assert [item.id for item in results] == ["memory-new"]
        metadata = results[0].metadata or {}
        assert metadata["temporal_fact_current"] is True
        assert metadata["temporal_claim_id"] == "claim-new"
        assert metadata["temporal_value"] == "Tokyo"
        assert metadata["temporal_status"] == "current"
        assert metadata["temporal_semantic_at"].endswith("+00:00")
        assert service.last_funnel_trace["filters"]["temporal_stale_removed"] == 1
        assert service.last_funnel_trace["stages"]["temporal_current"]["count"] == 1
    finally:
        provider.close()


def test_temporal_precedence_and_current_views_share_provider_lock(monkeypatch):
    provider = TemporalRecallProvider(enabled=True, db_items=[])
    try:
        old, _new = _seed_supersede(provider)
        provider._db_items = [old]
        observed: list[str] = []
        real_precedence = recall_module.query_temporal_memory_precedence
        real_current = recall_module.query_current_fact_views

        def checked_precedence(*args, **kwargs):
            assert provider._lock._is_owned()  # type: ignore[attr-defined]
            observed.append("precedence")
            return real_precedence(*args, **kwargs)

        def checked_current(*args, **kwargs):
            assert provider._lock._is_owned()  # type: ignore[attr-defined]
            observed.append("current")
            return real_current(*args, **kwargs)

        monkeypatch.setattr(
            recall_module,
            "query_temporal_memory_precedence",
            checked_precedence,
        )
        monkeypatch.setattr(recall_module, "query_current_fact_views", checked_current)

        results = RecallService(provider).search_memories(
            "Where does Joy live now?",
            limit=5,
        )

        assert [item.id for item in results] == ["memory-new"]
        assert observed == ["precedence", "current"]
    finally:
        provider.close()


def test_temporal_gate_suppresses_current_status_claim_before_valid_from():
    provider = TemporalRecallProvider(enabled=True, db_items=[])
    try:
        future = _memory(
            provider,
            "memory-future",
            content="Joy currently lives in Luna City.",
            valid_item=False,
        )
        _claim(
            provider,
            claim_id="claim-future",
            memory_id=future.id,
            value="Luna City",
            valid_from="2999-01-01T00:00:00+00:00",
        )
        provider._conn.commit()
        provider._db_items = [future]

        results = RecallService(provider).search_memories(
            "Where does Joy live now?",
            limit=5,
        )

        assert results == []
    finally:
        provider.close()
