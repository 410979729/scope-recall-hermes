"""Regressions bound to the 2026-07-16 independent audit report."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Any

import pytest

import scope_recall.temporal_query as temporal_query
from scope_recall.doctor_temporal import temporal_evolution_report
from scope_recall.evolution_policy import evaluate_evolution_policy
from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionPlan,
    EvolutionProposal,
)
from scope_recall.fact_executor import FactExecutionContext, execute_fact_plan
from scope_recall.fact_repository import insert_claim
from scope_recall.maintenance_ops import connect_memory_db
from scope_recall.recall import RecallService
from scope_recall.sql_store import ensure_schema
from scope_recall.temporal_query import query_current_fact_views
from scope_recall.truth_connection import connect_truth_database
from scope_recall.vector_generation import CURRENT_GENERATION_KEY, ensure_vector_generation_schema


def _insert_memory(conn: sqlite3.Connection, memory_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, 'scope-a', 'audit', 'memory', ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            f"memory {memory_id}",
            f"memory {memory_id}",
            now,
            now,
            '{"lifecycle":"promoted","memory_type":"factual"}',
        ),
    )


def test_truth_connection_enables_fk_before_schema_and_cascades_claims(tmp_path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    conn = connect_truth_database(db_path, mode="rwc")
    try:
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        ensure_schema(conn)
        _insert_memory(conn, "memory-one")
        insert_claim(
            conn,
            claim_id="claim-one",
            memory_id="memory-one",
            scope_id="scope-a",
            subject="Alice",
            predicate="owns",
            value="the red car",
            valid_from="2026-01-01T00:00:00+00:00",
            recorded_at="2026-01-01T00:00:00+00:00",
            source_type="user_message",
            source_ref="message-one",
            confidence=1.0,
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM fact_claims_fts").fetchone()[0] == 1
        conn.execute("DELETE FROM memories WHERE id='memory-one'")
        assert conn.execute("SELECT 1 FROM fact_claims WHERE claim_id='claim-one'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM fact_claims_fts").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_maintenance_apply_and_read_connections_enable_foreign_keys(tmp_path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    seed = connect_truth_database(db_path, mode="rwc")
    seed.close()

    for apply in (False, True):
        conn = connect_memory_db(db_path, apply=apply)
        try:
            assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        finally:
            conn.close()


def test_temporal_doctor_fails_on_orphan_claim_without_silent_repair(tmp_path) -> None:
    home = tmp_path / "home"
    db_path = home / "scope-recall" / "memory.sqlite3"
    db_path.parent.mkdir(parents=True)
    legacy = sqlite3.connect(db_path)
    legacy.row_factory = sqlite3.Row
    try:
        ensure_schema(legacy)
        _insert_memory(legacy, "memory-one")
        insert_claim(
            legacy,
            claim_id="claim-one",
            memory_id="memory-one",
            scope_id="scope-a",
            subject="Alice",
            predicate="owns",
            value="the red car",
            valid_from="2026-01-01T00:00:00+00:00",
            recorded_at="2026-01-01T00:00:00+00:00",
            source_type="user_message",
            source_ref="message-one",
            confidence=1.0,
        )
        legacy.commit()
        assert int(legacy.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
        legacy.execute("DELETE FROM memories WHERE id='memory-one'")
        legacy.commit()
        assert legacy.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        legacy.close()

    payload, check, recommendations = temporal_evolution_report(home, {})

    assert payload["status"] == "needs_repair"
    assert payload["foreign_key_integrity"]["enabled"] is True
    assert payload["foreign_key_integrity"]["violation_count"] == 1
    assert check["ok"] is False
    assert any("foreign-key violations" in failure for failure in check["failures"])
    assert any("do not silently delete" in item for item in recommendations)

    verify = sqlite3.connect(db_path)
    try:
        assert verify.execute("SELECT 1 FROM fact_claims WHERE claim_id='claim-one'").fetchone() is not None
    finally:
        verify.close()


def test_ensure_schema_commit_false_preserves_callers_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        _insert_memory(conn, "pending")
        assert conn.in_transaction is True

        ensure_schema(conn, commit=False)

        assert conn.in_transaction is True
        conn.rollback()
        assert conn.execute("SELECT 1 FROM memories WHERE id='pending'").fetchone() is None
    finally:
        conn.close()


_AUDIT_HISTORICAL_FACT_CASES = (
    ("Alice", "owns", "the red car", "Alice owned the red car."),
    ("Alice", "needs", "wheelchair access", "Alice needed wheelchair access."),
    ("Alice", "supports", "Project X", "Alice supported Project X."),
    ("Alice", "belongs to", "Team A", "Alice belonged to Team A."),
    ("我", "住在", "北京", "我住在北京过。"),
    ("我", "住在", "北京", "我住在北京的时候，经常坐地铁。"),
)

_AUDIT_CURRENT_FACT_CASES = (
    ("Alice", "owns", "the red car", "Alice owns the red car."),
    ("Alice", "needs", "wheelchair access", "Alice needs wheelchair access."),
    ("Alice", "supports", "Project X", "Alice supports Project X."),
    ("Alice", "belongs to", "Team A", "Alice belongs to Team A."),
    ("我", "住在", "北京", "我现在住在北京。"),
    ("我", "住在", "北京", "我目前住在北京。"),
)


def _audit_add_plan(subject: str, predicate: str, value: str, quote: str) -> EvolutionPlan:
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=ClaimDraft.from_parts(
            subject=subject,
            predicate=predicate,
            value=value,
            scope_id="scope-a",
        ),
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id="independent-audit-temporal",
                quote=quote,
                speaker_subject=subject,
            ),
        ),
        confidence=0.99,
        reason="independent audit temporal regression",
        source="audit",
    )
    return EvolutionPlan(
        proposal=proposal,
        action_id="independent-audit-temporal",
        idempotency_key="independent-audit-temporal",
        policy_mode="auto_apply",
    )


def _audit_fact_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "memories",
            "memories_fts",
            "fact_claims",
            "fact_claim_evidence",
            "fact_freshness",
            "memory_relations",
            "governance_audit_events",
            "vector_outbox",
            "fact_action_receipts",
        )
    }


@pytest.mark.parametrize(
    ("subject", "predicate", "value", "quote"),
    _AUDIT_HISTORICAL_FACT_CASES,
)
def test_unknown_historical_predicates_review_and_executor_writes_nothing(
    subject: str,
    predicate: str,
    value: str,
    quote: str,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    ensure_vector_generation_schema(conn)
    conn.execute(
        "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, 'gen-audit', ?)",
        (CURRENT_GENERATION_KEY, "2026-07-16T00:00:00+00:00"),
    )
    conn.commit()
    plan = _audit_add_plan(subject, predicate, value, quote)
    policy = evaluate_evolution_policy(plan.proposal)
    before = _audit_fact_counts(conn)

    result = execute_fact_plan(
        conn,
        plan,
        policy,
        FactExecutionContext(
            scope_id="scope-a",
            writable_scope_ids=("scope-a",),
            actor="scope-recall:audit",
            timestamp="2026-07-16T00:00:00+00:00",
            source="fact_evolution",
            target="memory",
            session_id="audit-session",
            platform="test",
            user_id="audit-user",
            new_memory_id="audit-memory",
            new_claim_id="audit-claim",
            metadata={"memory_type": "fact"},
        ),
    )

    assert policy.allowed is False
    assert policy.effective_action is EvolutionAction.REVIEW
    assert result.applied is False
    assert result.status == "review"
    assert _audit_fact_counts(conn) == before
    conn.close()


@pytest.mark.parametrize(
    ("subject", "predicate", "value", "quote"),
    _AUDIT_CURRENT_FACT_CASES,
)
def test_explicit_english_and_chinese_current_states_remain_authoritative(
    subject: str,
    predicate: str,
    value: str,
    quote: str,
) -> None:
    plan = _audit_add_plan(subject, predicate, value, quote)
    policy = evaluate_evolution_policy(plan.proposal)

    assert policy.allowed is True
    assert policy.effective_action is EvolutionAction.ADD


def _recall_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    conn.commit()
    return conn


def _seed_current_fact(
    conn: sqlite3.Connection,
    *,
    index: int,
    subject: str,
    predicate: str,
    value: str,
    content: str,
    updated_at: str,
) -> str:
    memory_id = f"memory-{index:06d}"
    claim_id = f"claim-{index:06d}"
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, 'scope-a', 'audit', 'memory', ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            content,
            content,
            updated_at,
            updated_at,
            '{"lifecycle":"promoted","memory_type":"factual"}',
        ),
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
        (memory_id, content, content),
    )
    insert_claim(
        conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id="scope-a",
        subject=subject,
        predicate=predicate,
        value=value,
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at=updated_at,
        source_type="user_message",
        source_ref=f"message-{index}",
        confidence=0.9,
    )
    return memory_id


class _TemporalRecallProvider:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = RLock()
        self._accessible_scope_ids = ["scope-a"]
        self._config = {
            "temporal_queries": {
                "enabled": True,
                "current_limit": 50,
                "timezone": "UTC",
            }
        }

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn


def test_temporal_recall_preserves_relevance_against_current_distractors() -> None:
    conn = _recall_conn()
    correct_id = _seed_current_fact(
        conn,
        index=90_000,
        subject="Alice",
        predicate="phone number",
        value="+1 555 1234",
        content="Alice's phone number is +1 555 1234.",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    for index in range(20):
        _seed_current_fact(
            conn,
            index=index,
            subject="Alice",
            predicate=f"device {index} status",
            value="green",
            content=f"Alice device {index} status is green.",
            updated_at="2026-07-16T00:00:00+00:00",
        )
    conn.commit()

    service = RecallService(_TemporalRecallProvider(conn))
    result = service._temporal_current_candidates(
        "What is Alice's phone number?",
        limit=50,
        candidate_memory_ids=[],
    )

    assert result is not None
    candidates, _ = result
    assert candidates[0].id == correct_id
    assert candidates[0].score > max(item.score for item in candidates[1:])
    assert candidates[0].metadata["lexical_score"] == 1.0
    assert candidates[0].metadata["temporal_score_explain"]["matched_tokens"] == [
        "alice",
        "phone",
        "number",
    ]
    conn.close()


def test_indexed_current_recall_finds_answer_beyond_legacy_1000_window() -> None:
    conn = _recall_conn()
    for index in range(1_105):
        _seed_current_fact(
            conn,
            index=index,
            subject="Alice",
            predicate=f"device {index:04d} status",
            value="green",
            content=f"Alice device {index:04d} status is green.",
            updated_at="2026-07-16T00:00:00+00:00",
        )
    correct_id = _seed_current_fact(
        conn,
        index=99_999,
        subject="Alice",
        predicate="phone number",
        value="+1 555 9876",
        content="Alice's phone number is +1 555 9876.",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    conn.commit()
    diagnostics: dict[str, Any] = {}

    views = query_current_fact_views(
        conn,
        scope_ids=["scope-a"],
        query="What is Alice's phone number?",
        limit=10,
        diagnostics=diagnostics,
    )

    assert views[0].memory_id == correct_id
    assert diagnostics["strategy"] == "fts5_bm25"
    assert diagnostics["complete"] is False
    assert diagnostics["truncated"] is True
    conn.close()


def test_chinese_current_question_uses_shared_tokenizer_and_finds_fact() -> None:
    conn = _recall_conn()
    correct_id = _seed_current_fact(
        conn,
        index=88_888,
        subject="张三",
        predicate="住在",
        value="北京",
        content="张三住在北京。",
        updated_at="2026-07-16T00:00:00+00:00",
    )
    conn.commit()
    diagnostics: dict[str, Any] = {}

    views = query_current_fact_views(
        conn,
        scope_ids=["scope-a"],
        query="张三现在住在哪里？",
        limit=10,
        diagnostics=diagnostics,
    )

    assert views, diagnostics
    assert views[0].memory_id == correct_id
    assert "张三" in diagnostics["semantic_tokens"]
    assert "住在" in diagnostics["semantic_tokens"]
    assert views[0].score > 0.0
    conn.close()


def test_indexed_candidate_overflow_is_explicit_not_silent(monkeypatch) -> None:
    conn = _recall_conn()
    for index in range(12):
        _seed_current_fact(
            conn,
            index=index,
            subject="Alice",
            predicate=f"device {index} status",
            value="green",
            content=f"Alice device {index} status is green.",
            updated_at="2026-07-16T00:00:00+00:00",
        )
    conn.commit()
    monkeypatch.setattr(temporal_query, "MAX_CURRENT_FACT_CANDIDATES", 10)
    diagnostics: dict[str, Any] = {}

    query_current_fact_views(
        conn,
        scope_ids=["scope-a"],
        query="Alice device status",
        limit=10,
        diagnostics=diagnostics,
    )

    assert diagnostics["candidate_count"] == 10
    assert diagnostics["truncated"] is True
    assert diagnostics["complete"] is False
    conn.close()
