"""Bounded, scoped, read-only reflection evidence collection."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from threading import RLock

import pytest

from scope_recall.fact_repository import close_claim_interval, insert_claim
from scope_recall.models import RecallItem
from scope_recall.reflection import (
    ReflectionBudget,
    ReflectionEvidenceError,
    build_reflection_evidence_pack,
    merge_reflection_evidence_packs,
)
from scope_recall.sql_store import ensure_schema


class ReflectionProvider:
    def __init__(self) -> None:
        self._config = {"temporal_queries": {"enabled": True, "timezone": "UTC"}}
        self._retrieval_config = {
            "mode": "lexical",
            "include_general": "same-scope",
            "entity_scope_filter_enabled": False,
            "min_score": 0.0,
        }
        self._vector_config: dict[str, object] = {}
        self._scope_id = "scope-a"
        self._shared_scope_id = "scope-a"
        self._accessible_scope_ids = ["scope-a"]
        self._lock = RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(self._conn)
        self.items: list[RecallItem] = []
        self.queries: list[str] = []

    def _search_db_memories(self, query: str, *, limit: int) -> list[RecallItem]:
        self.queries.append(query)
        return self.items[:limit]

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


def _insert_memory(
    provider: ReflectionProvider,
    *,
    memory_id: str,
    scope_id: str,
    content: str,
    lifecycle: str = "promoted",
    expose_to_recall: bool = True,
) -> None:
    metadata = {
        "lifecycle": lifecycle,
        "memory_type": "factual",
        "scope_id": scope_id,
        "lexical_score": 0.95,
        "vector_score": 0.0,
        "importance": 0.9,
    }
    provider._conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, ?, 'fact-executor', 'user', ?, ?,
                  '2024-01-01T00:00:00+00:00',
                  '2026-07-14T00:00:00+00:00', ?)
        """,
        (memory_id, scope_id, content, content, json.dumps(metadata, sort_keys=True)),
    )
    if expose_to_recall:
        provider.items.append(
            RecallItem(
                id=memory_id,
                content=content,
                summary=content,
                source="fact-executor",
                target="user",
                score=0.95,
                updated_at="2026-07-14T00:00:00+00:00",
                metadata=metadata,
            )
        )


def _insert_location_claim(
    provider: ReflectionProvider,
    *,
    claim_id: str,
    memory_id: str,
    scope_id: str,
    value: str,
    valid_from: str,
) -> None:
    insert_claim(
        provider._conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id=scope_id,
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


def _seed_location_history(provider: ReflectionProvider) -> None:
    _insert_memory(
        provider,
        memory_id="memory-old",
        scope_id="scope-a",
        content="Joy lived in Mumbai before 2025.",
        expose_to_recall=False,
    )
    _insert_memory(
        provider,
        memory_id="memory-new",
        scope_id="scope-a",
        content="Joy currently lives in Tokyo.",
    )
    _insert_location_claim(
        provider,
        claim_id="claim-old",
        memory_id="memory-old",
        scope_id="scope-a",
        value="Mumbai",
        valid_from="2020-01-01T00:00:00+00:00",
    )
    close_claim_interval(
        provider._conn,
        claim_id="claim-old",
        valid_to="2025-01-01T00:00:00+00:00",
        retired_at="2026-07-14T00:00:01+00:00",
        status="superseded",
        superseded_by_claim_id="claim-new",
    )
    _insert_location_claim(
        provider,
        claim_id="claim-new",
        memory_id="memory-new",
        scope_id="scope-a",
        value="Tokyo",
        valid_from="2025-01-01T00:00:00+00:00",
    )
    provider._conn.commit()


def test_reflection_pack_is_read_only_and_filters_out_of_scope_rows() -> None:
    provider = ReflectionProvider()
    _insert_memory(
        provider,
        memory_id="memory-in",
        scope_id="scope-a",
        content="Project Aurora uses PostgreSQL.",
    )
    _insert_memory(
        provider,
        memory_id="memory-out",
        scope_id="scope-b",
        content="Project Aurora secret tenant data.",
    )
    provider._conn.commit()
    before = provider._conn.total_changes

    pack = build_reflection_evidence_pack(
        provider,
        query="What database does Project Aurora use now?",
        budget=ReflectionBudget(max_evidence=8, max_chars=4_000),
    )

    assert provider._conn.total_changes == before
    assert pack.intent == "current"
    assert {item.memory_id for item in pack.evidence} == {"memory-in"}
    assert all(item.scope_id == "scope-a" for item in pack.evidence)
    assert pack.trace["scope_filtered"] >= 1
    assert pack.trace["write_delta"] == 0


def test_reflection_history_intent_includes_superseded_claim_but_current_does_not() -> None:
    provider = ReflectionProvider()
    _seed_location_history(provider)

    current = build_reflection_evidence_pack(
        provider,
        query="Where does Joy live now?",
        budget=ReflectionBudget(max_evidence=8, max_chars=4_000),
    )
    history = build_reflection_evidence_pack(
        provider,
        query="Where did Joy live before, and how did it change?",
        budget=ReflectionBudget(max_evidence=8, max_chars=4_000),
    )

    current_claims = {item.claim_id for item in current.evidence if item.claim_id}
    history_claims = {item.claim_id for item in history.evidence if item.claim_id}
    assert current.intent == "current"
    assert history.intent == "history"
    assert current_claims == {"claim-new"}
    assert history_claims == {"claim-old", "claim-new"}
    assert all(item.kind == "fact_history" for item in history.evidence)


def test_reflection_pack_runs_at_most_one_followup_and_deduplicates() -> None:
    provider = ReflectionProvider()
    _insert_memory(
        provider,
        memory_id="memory-one",
        scope_id="scope-a",
        content="Aurora uses PostgreSQL with row-level security.",
    )
    provider._conn.commit()

    pack = build_reflection_evidence_pack(
        provider,
        query="Aurora database",
        followup_query="Aurora row-level security",
        budget=ReflectionBudget(max_evidence=4, max_chars=1_000),
    )

    assert provider.queries == ["Aurora database", "Aurora row-level security"]
    assert [item.memory_id for item in pack.evidence] == ["memory-one"]
    assert pack.trace["retrieval_count"] == 2
    assert pack.trace["deduplicated"] >= 1


def test_reflection_pack_merge_adds_one_retrieval_and_rejects_write_traces() -> None:
    provider = ReflectionProvider()
    _insert_memory(
        provider,
        memory_id="memory-one",
        scope_id="scope-a",
        content="Aurora uses PostgreSQL with row-level security.",
    )
    provider._conn.commit()
    budget = ReflectionBudget(max_evidence=4, max_chars=1_000)

    initial = build_reflection_evidence_pack(
        provider,
        query="Aurora database",
        budget=budget,
    )
    supplemental = build_reflection_evidence_pack(
        provider,
        query="Aurora row-level security",
        budget=budget,
        query_intent=initial.intent,
    )
    merged = merge_reflection_evidence_packs(
        initial,
        supplemental,
        budget=budget,
    )

    assert provider.queries == ["Aurora database", "Aurora row-level security"]
    assert [item.memory_id for item in merged.evidence] == ["memory-one"]
    assert merged.trace["retrieval_count"] == 2
    assert merged.trace["followup_used"] is True
    assert merged.char_count <= budget.max_chars

    dirty = replace(supplemental, trace={**supplemental.trace, "write_delta": 1})
    with pytest.raises(ReflectionEvidenceError, match="writes"):
        merge_reflection_evidence_packs(initial, dirty, budget=budget)


@pytest.mark.parametrize(
    "budget",
    [
        ReflectionBudget(max_evidence=2, max_chars=180, max_item_chars=80),
        ReflectionBudget(max_evidence=1, max_chars=4_000, max_item_chars=500),
    ],
)
def test_reflection_pack_enforces_entry_and_character_budgets(budget: ReflectionBudget) -> None:
    provider = ReflectionProvider()
    for index in range(4):
        _insert_memory(
            provider,
            memory_id=f"memory-{index}",
            scope_id="scope-a",
            content=(f"Evidence {index}: " + "x" * 300),
        )
    provider._conn.commit()

    pack = build_reflection_evidence_pack(
        provider,
        query="Evidence",
        budget=budget,
    )

    assert len(pack.evidence) <= budget.max_evidence
    assert pack.char_count <= budget.max_chars
    assert all(len(item.content) <= budget.max_item_chars for item in pack.evidence)
    assert pack.trace["truncated"] is True


def test_reflection_budget_and_query_validation_fail_closed() -> None:
    provider = ReflectionProvider()
    with pytest.raises(ReflectionEvidenceError, match="max_evidence"):
        build_reflection_evidence_pack(
            provider,
            query="safe query",
            budget=ReflectionBudget(max_evidence=True),  # type: ignore[arg-type]
        )
    with pytest.raises(ReflectionEvidenceError, match="query"):
        build_reflection_evidence_pack(provider, query="x" * 2_001)
    with pytest.raises(ReflectionEvidenceError, match="followup_query"):
        build_reflection_evidence_pack(
            provider,
            query="safe query",
            followup_query=["one", "two"],  # type: ignore[arg-type]
        )
    with pytest.raises(ReflectionEvidenceError, match="query_intent"):
        build_reflection_evidence_pack(
            provider,
            query="safe query",
            query_intent="future",
        )
