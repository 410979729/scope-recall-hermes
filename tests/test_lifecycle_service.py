"""Atomic lifecycle transition tests across SQLite truth and companions."""

from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.freshness import upsert_memory_freshness
from scope_recall.lifecycle_service import LifecycleConflictError, hard_delete_memories, transition_memory_lifecycle
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation


def _fixture(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    metadata = {
        "lifecycle": "promoted",
        "memory_type": "factual",
        "entities": ["Scope Recall", "Joy"],
        "freshness": {"fact_key": "scope-recall:test", "truth_type": "config", "validator_kind": "manual"},
    }
    for memory_id in ("subject", "incoming-peer", "outgoing-peer"):
        store_row(
            conn,
            memory_id=memory_id,
            scope_id="scope-a",
            platform="test",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="session",
            source="fixture",
            target="memory",
            content=f"Lifecycle fixture {memory_id} for Scope Recall and Joy.",
            metadata=metadata,
            allow_duplicate=True,
        )
    upsert_memory_freshness(conn, memory_id="subject", metadata=metadata, commit=False)
    conn.execute(
        "INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at) VALUES ('subject', 'outgoing-peer', 'supports', 0.8, '', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at) VALUES ('incoming-peer', 'subject', 'conflicts_with', 0.7, '', '2026-01-01T00:00:00+00:00')"
    )
    identity = GenerationIdentity(
        backend="lancedb",
        provider="local-hash",
        model="hash-v1",
        dimensions=16,
        metric="cosine",
        prompt_profile="default-v1",
    )
    generation = bootstrap_legacy_generation(conn, identity=identity, row_count=3)
    conn.commit()
    return conn, generation["generation_id"]


def _counts(conn, memory_id="subject"):
    return {
        "memory": conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?", (memory_id,)).fetchone()[0],
        "fts": conn.execute("SELECT COUNT(*) FROM memories_fts WHERE memory_id = ?", (memory_id,)).fetchone()[0],
        "entities": conn.execute("SELECT COUNT(*) FROM memory_entities WHERE memory_id = ?", (memory_id,)).fetchone()[0],
        "relations": conn.execute(
            "SELECT COUNT(*) FROM memory_relations WHERE source_memory_id = ? OR target_memory_id = ?",
            (memory_id, memory_id),
        ).fetchone()[0],
        "freshness": conn.execute(
            "SELECT COUNT(*) FROM fact_freshness WHERE subject_type = 'memory' AND subject_id = ?",
            (memory_id,),
        ).fetchone()[0],
        "audit": conn.execute("SELECT COUNT(*) FROM governance_audit_events WHERE target_id = ?", (memory_id,)).fetchone()[0],
        "outbox": conn.execute("SELECT COUNT(*) FROM vector_outbox WHERE memory_id = ?", (memory_id,)).fetchone()[0],
    }


def test_hidden_transition_updates_all_companions_in_one_transaction(tmp_path):
    conn, generation_id = _fixture(tmp_path)
    before = _counts(conn)
    assert before["fts"] == 1
    assert before["entities"] >= 1
    assert before["relations"] == 2
    assert before["freshness"] == 1

    conn.execute("BEGIN IMMEDIATE")
    result = transition_memory_lifecycle(
        conn,
        memory_id="subject",
        lifecycle="archived",
        metadata_updates={"archived_by": "test", "archived_reason": "atomic transition"},
        actor="test",
        reason="atomic transition",
        event_type="test_lifecycle",
        action="soft_archive",
        batch_id="batch-a",
    )
    conn.commit()

    assert result["applied"] is True
    metadata = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = 'subject'").fetchone()[0])
    assert metadata["lifecycle"] == "archived"
    after = _counts(conn)
    assert after == {"memory": 1, "fts": 0, "entities": 0, "relations": 0, "freshness": 0, "audit": 1, "outbox": 1}
    event = conn.execute("SELECT operation, generation_id, status FROM vector_outbox WHERE memory_id = 'subject'").fetchone()
    assert tuple(event) == ("delete", generation_id, "pending")
    conn.close()


def test_candidate_transition_enqueues_vector_delete(tmp_path):
    conn, generation_id = _fixture(tmp_path)

    conn.execute("BEGIN IMMEDIATE")
    result = transition_memory_lifecycle(
        conn,
        memory_id="subject",
        lifecycle="candidate",
        actor="test",
        reason="requires review",
        event_type="test_candidate_transition",
        action="mark_candidate",
    )
    conn.commit()

    assert result["vector_operation"] == "delete"
    event = conn.execute("SELECT operation, generation_id, status FROM vector_outbox WHERE memory_id = 'subject'").fetchone()
    assert tuple(event) == ("delete", generation_id, "pending")
    conn.close()


def test_identical_lifecycle_transition_is_a_true_noop(tmp_path):
    """Repeated governance must not churn timestamps, audit, or vector debt."""

    conn, _generation_id = _fixture(tmp_path)
    before_row = conn.execute(
        "SELECT updated_at, metadata FROM memories WHERE id = 'subject'"
    ).fetchone()
    before_counts = _counts(conn)
    metadata = json.loads(before_row["metadata"])

    conn.execute("BEGIN IMMEDIATE")
    result = transition_memory_lifecycle(
        conn,
        memory_id="subject",
        lifecycle="promoted",
        metadata_updates=metadata,
        actor="test",
        reason="repeat governance",
        event_type="test_repeat_governance",
        action="promote",
    )
    conn.commit()

    after_row = conn.execute(
        "SELECT updated_at, metadata FROM memories WHERE id = 'subject'"
    ).fetchone()
    assert result["applied"] is False
    assert result["status"] == "no_change"
    assert tuple(after_row) == tuple(before_row)
    assert _counts(conn) == before_counts
    conn.close()


def test_transition_sanitizes_metadata_updates_before_truth_write(tmp_path):
    conn, _generation_id = _fixture(tmp_path)
    marker = "L" * 24
    private_path = "/home/synthetic/.ssh/id_rsa"

    conn.execute("BEGIN IMMEDIATE")
    transition_memory_lifecycle(
        conn,
        memory_id="subject",
        lifecycle="archived",
        metadata_updates={"archive_reason": "api_key=sk-" + marker + " " + private_path},
        actor="test",
        reason="api_key=sk-" + marker + " " + private_path,
        event_type="test_sanitized_transition",
        action="soft_archive",
    )
    conn.commit()

    rendered = conn.execute("SELECT metadata FROM memories WHERE id='subject'").fetchone()[0]
    assert marker not in rendered
    assert private_path not in rendered
    assert "[REDACTED_" in rendered
    audit = conn.execute(
        "SELECT after_json, reason FROM governance_audit_events WHERE target_id='subject'"
    ).fetchone()
    assert marker not in str(tuple(audit))
    assert private_path not in str(tuple(audit))
    conn.close()


def test_transition_failure_rolls_back_truth_and_every_companion(tmp_path):
    conn, _generation_id = _fixture(tmp_path)
    before_counts = _counts(conn)
    before_metadata = conn.execute("SELECT metadata, updated_at FROM memories WHERE id = 'subject'").fetchone()
    conn.execute(
        """
        CREATE TRIGGER fail_lifecycle_audit
        BEFORE INSERT ON governance_audit_events
        WHEN NEW.event_type = 'test_failure'
        BEGIN
            SELECT RAISE(ABORT, 'injected audit failure');
        END
        """
    )
    conn.commit()

    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
        transition_memory_lifecycle(
            conn,
            memory_id="subject",
            lifecycle="archived",
            actor="test",
            reason="inject failure",
            event_type="test_failure",
            action="soft_archive",
        )
    conn.rollback()

    assert _counts(conn) == before_counts
    after_metadata = conn.execute("SELECT metadata, updated_at FROM memories WHERE id = 'subject'").fetchone()
    assert tuple(after_metadata) == tuple(before_metadata)
    conn.close()


def test_hard_delete_commits_audit_outbox_and_never_calls_direct_vector_callback(tmp_path):
    conn, generation_id = _fixture(tmp_path)
    vector_calls: list[list[str]] = []

    result = hard_delete_memories(
        conn,
        memory_ids=["subject"],
        scope_ids=["scope-a"],
        vector_delete=lambda ids: vector_calls.append(list(ids)),
        require_vector_delete=True,
        actor="test",
        reason="secret-like-content",
        event_type="test_hard_delete",
        batch_id="hard-delete-success",
    )

    assert result["deleted"] == 1
    assert result["ids"] == ["subject"]
    assert vector_calls == []
    assert _counts(conn) == {
        "memory": 0,
        "fts": 0,
        "entities": 0,
        "relations": 0,
        "freshness": 0,
        "audit": 1,
        "outbox": 1,
    }
    event = conn.execute(
        "SELECT generation_id, operation, status FROM vector_outbox WHERE memory_id = 'subject'"
    ).fetchone()
    assert tuple(event) == (generation_id, "delete", "pending")
    assert result["vector_status"] == "pending"
    conn.close()


def test_hard_delete_audit_failure_rolls_back_before_vector_side_effect(tmp_path):
    conn, _generation_id = _fixture(tmp_path)
    before = _counts(conn)
    vector_calls: list[list[str]] = []
    conn.execute(
        """
        CREATE TRIGGER fail_hard_delete_audit
        BEFORE INSERT ON governance_audit_events
        WHEN NEW.action = 'hard_delete'
        BEGIN
            SELECT RAISE(ABORT, 'injected hard delete audit failure');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected hard delete audit failure"):
        hard_delete_memories(
            conn,
            memory_ids=["subject"],
            scope_ids=["scope-a"],
            vector_delete=lambda ids: vector_calls.append(list(ids)),
            require_vector_delete=True,
            actor="test",
            reason="inject audit failure",
            event_type="test_hard_delete",
            batch_id="hard-delete-audit-failure",
        )

    assert vector_calls == []
    assert _counts(conn) == before
    conn.close()


def test_hard_delete_outbox_failure_rolls_back_before_vector_side_effect(tmp_path):
    conn, _generation_id = _fixture(tmp_path)
    before = _counts(conn)
    vector_calls: list[list[str]] = []
    conn.execute(
        """
        CREATE TRIGGER fail_hard_delete_outbox
        BEFORE INSERT ON vector_outbox
        WHEN NEW.operation = 'delete'
        BEGIN
            SELECT RAISE(ABORT, 'injected hard delete outbox failure');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected hard delete outbox failure"):
        hard_delete_memories(
            conn,
            memory_ids=["subject"],
            scope_ids=["scope-a"],
            vector_delete=lambda ids: vector_calls.append(list(ids)),
            require_vector_delete=True,
            actor="test",
            reason="inject outbox failure",
            event_type="test_hard_delete",
            batch_id="hard-delete-outbox-failure",
        )

    assert vector_calls == []
    assert _counts(conn) == before
    conn.close()


def test_hard_delete_truth_failure_rolls_back_before_vector_callback(tmp_path):
    conn, _generation_id = _fixture(tmp_path)
    before = _counts(conn)
    vector_calls: list[list[str]] = []
    conn.execute(
        """
        CREATE TRIGGER fail_truth_delete_before_external_io
        BEFORE DELETE ON memories
        WHEN OLD.id = 'subject'
        BEGIN
            SELECT RAISE(ABORT, 'injected truth delete failure');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected truth delete failure"):
        hard_delete_memories(
            conn,
            memory_ids=["subject"],
            scope_ids=["scope-a"],
            vector_delete=lambda ids: vector_calls.append(list(ids)),
            require_vector_delete=True,
            actor="test",
            reason="inject pre-callback SQL failure",
            event_type="test_hard_delete",
            batch_id="hard-delete-truth-failure",
        )

    assert vector_calls == []
    assert _counts(conn) == before
    conn.close()


def test_transition_cas_conflict_has_zero_side_effects(tmp_path):
    conn, _generation_id = _fixture(tmp_path)
    before = _counts(conn)
    current = conn.execute("SELECT updated_at FROM memories WHERE id = 'subject'").fetchone()[0]

    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(LifecycleConflictError) as captured:
        transition_memory_lifecycle(
            conn,
            memory_id="subject",
            lifecycle="archived",
            expected_updated_at="1970-01-01T00:00:00+00:00",
            actor="test",
            reason="stale review",
            event_type="test_conflict",
            action="soft_archive",
        )
    conn.rollback()

    assert captured.value.current_updated_at == current
    assert _counts(conn) == before
    conn.close()
