"""Deterministic digest anti-pollution and quarantine integration tests."""

from __future__ import annotations

import json
import sqlite3

import pytest

from scope_recall.digest_pollution import assess_digest_batch
from scope_recall.fact_actions import parse_evolution_proposal
from scope_recall.journal import apply_journal_candidates
from scope_recall.journal_candidates import JournalDigestCandidate
from scope_recall.journal_store import ensure_journal_schema
from scope_recall.models import RuntimeScope
from scope_recall.nightly_digest import (
    DigestCandidate,
    ScopeProfile,
    apply_candidates,
    ensure_digest_schema,
)
from scope_recall.sql_store import ensure_schema


def _proposal(*, value: str, source_id: str, quote: str):
    return parse_evolution_proposal(
        {
            "action": "add",
            "claim": {
                "subject": "Joy",
                "predicate": "lives in",
                "value": value,
                "cardinality": "single",
            },
            "evidence": [
                {
                    "source_type": "user_message",
                    "source_id": source_id,
                    "quote": quote,
                }
            ],
            "confidence": 0.98,
            "reason": "direct statement",
        },
        trusted_scope_id="scope-shared",
    )


def _fact_candidate(*, value: str, quote: str, session_id: str = "session-fact") -> DigestCandidate:
    return DigestCandidate(
        content=f"Joy currently lives in {value}, which is the durable current location fact.",
        target="user",
        memory_type="factual",
        confidence=0.98,
        session_id=session_id,
        message_ids=[11],
        evolution=_proposal(value=value, source_id="message:11", quote=quote),
    )


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    ensure_digest_schema(conn)
    return conn


def _scope() -> ScopeProfile:
    runtime = RuntimeScope(
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    return ScopeProfile(
        scope=runtime,
        scope_id="scope-local",
        shared_scope_id="scope-shared",
        accessible_scope_ids=["scope-local", "scope-shared"],
        writable_scope_ids=["scope-local", "scope-shared"],
    )


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (
            "PLAN_STATE: current_task=T16; last_completed_task=T15; next_action=write tests.",
            "task_state_snapshot",
        ),
        (
            "The current validation snapshot says 170 tests passed with 1 warning.",
            "test_run_snapshot",
        ),
        (
            "Git status reports the worktree clean at commit 797b028601d0e48cf2c7727e06e9e3eb94e7e464.",
            "repository_audit_snapshot",
        ),
        (
            "Historical Task Snapshot from context compaction lists the previous implementation state.",
            "historical_snapshot",
        ),
    ],
)
def test_one_off_task_audit_and_test_snapshots_are_quarantined(content: str, reason: str):
    candidate = DigestCandidate(content=content, memory_type="summary")

    assessment = assess_digest_batch([candidate])[0]

    assert assessment.quarantined is True
    assert reason in assessment.reason_codes


def test_reusable_workflow_without_run_result_snapshot_is_not_quarantined():
    candidate = DigestCandidate(
        content=(
            "When changing a SQLite schema, run focused tests, static checks, and a rollback "
            "rehearsal before deployment; record failures without copying transient counters."
        ),
        target="ops",
        memory_type="workflow",
    )

    assessment = assess_digest_batch([candidate])[0]

    assert assessment.quarantined is False
    assert assessment.reason_codes == ()


def test_claim_value_must_be_anchored_in_same_batch_evidence():
    candidate = _fact_candidate(
        value="Bangalore",
        quote="Joy currently lives in Mumbai.",
    )

    assessment = assess_digest_batch(
        [candidate],
        batch_evidence={"session-fact": ["Joy currently lives in Mumbai."]},
    )[0]

    assert assessment.quarantined is True
    assert "claim_value_not_in_batch_evidence" in assessment.reason_codes


def test_conflicting_single_value_claims_from_same_batch_quarantine_both():
    candidates = [
        _fact_candidate(value="Mumbai", quote="Joy currently lives in Mumbai."),
        _fact_candidate(value="Bangalore", quote="Joy currently lives in Bangalore."),
    ]

    assessments = assess_digest_batch(
        candidates,
        batch_evidence={
            "session-fact": [
                "Joy currently lives in Mumbai.",
                "Joy currently lives in Bangalore.",
            ]
        },
    )

    assert all(item.quarantined for item in assessments)
    assert all(
        "same_batch_single_value_conflict" in item.reason_codes
        for item in assessments
    )


def test_apply_quarantines_polluted_candidate_but_keeps_stable_workflow():
    conn = _connection()
    polluted = DigestCandidate(
        content=(
            "PLAN_STATE current_task: T16; the repository worktree is clean and 170 tests passed. "
            "The next action is T17."
        ),
        target="memory",
        memory_type="summary",
        session_id="session-task",
    )
    stable = DigestCandidate(
        content=(
            "Reusable migration workflow: create an additive schema, run focused checks, rehearse "
            "rollback on a copy, and only then deploy under an explicit maintenance window."
        ),
        target="ops",
        memory_type="workflow",
        session_id="session-task",
    )

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-pollution",
        candidates=[polluted, stable],
        dry_run=False,
        runtime_config={},
        batch_evidence={"session-task": [polluted.content, stable.content]},
    )

    assert result["counts"]["quarantined"] == 1
    assert result["counts"]["inserted"] == 1
    assert [action["action"] for action in result["actions"]] == [
        "quarantine",
        "insert",
    ]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    quarantine = conn.execute(
        "SELECT run_id, session_id, reason_codes, candidate_hash "
        "FROM nightly_digest_quarantine"
    ).fetchone()
    assert quarantine["run_id"] == "run-pollution"
    assert quarantine["session_id"] == "session-task"
    assert "task_state_snapshot" in json.loads(quarantine["reason_codes"])
    assert len(quarantine["candidate_hash"]) == 64
    conn.close()


def test_invented_fact_is_quarantined_before_memory_or_claim_write():
    conn = _connection()
    candidate = _fact_candidate(
        value="Bangalore",
        quote="Joy currently lives in Mumbai.",
    )

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-invented",
        candidates=[candidate],
        dry_run=False,
        runtime_config={
            "fact_evolution": {"enabled": True, "nightly_mode": "auto_apply"}
        },
        batch_evidence={"session-fact": ["Joy currently lives in Mumbai."]},
    )

    assert result["counts"]["quarantined"] == 1
    assert result["actions"][0]["action"] == "quarantine"
    assert "claim_value_not_in_batch_evidence" in result["actions"][0][
        "reason_codes"
    ]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0
    conn.close()


def test_unrelated_direct_quote_cannot_be_laundered_by_assistant_batch_text():
    conn = _connection()
    direct_quote = "Please remember that I prefer concise weekly reports."
    inferred_quote = (
        "Joy may live in Bangalore, but this was not stated by the user."
    )
    proposal = parse_evolution_proposal(
        {
            "action": "add",
            "claim": {
                "subject": "Joy",
                "predicate": "lives in",
                "value": "Bangalore",
                "cardinality": "single",
            },
            "evidence": [
                {
                    "source_type": "user_message",
                    "source_id": "message:preference",
                    "quote": direct_quote,
                },
                {
                    "source_type": "model_inference",
                    "source_id": "assistant:guess",
                    "quote": inferred_quote,
                },
            ],
            "confidence": 0.99,
            "reason": "model guess",
        },
        trusted_scope_id="scope-shared",
    )
    candidate = DigestCandidate(
        content="Joy currently lives in Bangalore.",
        target="user",
        memory_type="factual",
        confidence=0.99,
        session_id="session-laundering",
        message_ids=[1, 2],
        evolution=proposal,
    )

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-laundering",
        candidates=[candidate],
        dry_run=False,
        runtime_config={
            "fact_evolution": {"enabled": True, "nightly_mode": "auto_apply"}
        },
        batch_evidence={
            "session-laundering": [direct_quote, inferred_quote]
        },
    )

    assert result["counts"]["quarantined"] == 1
    assert result["actions"][0]["action"] == "quarantine"
    assert "claim_not_supported_by_authoritative_evidence" in result["actions"][0][
        "reason_codes"
    ]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0
    conn.close()


def test_dry_run_reports_quarantine_without_persisting_audit_row():
    conn = _connection()
    candidate = DigestCandidate(
        content="Historical Task Snapshot: current_task is T16 and 42 tests passed.",
        memory_type="summary",
        session_id="session-dry",
    )

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-dry",
        candidates=[candidate],
        dry_run=True,
        runtime_config={},
        batch_evidence={"session-dry": [candidate.content]},
    )

    assert result["counts"]["quarantined"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM nightly_digest_quarantine"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    conn.close()


def test_journal_apply_uses_same_pollution_gate_and_rejection_receipt():
    conn = _connection()
    candidate = JournalDigestCandidate(
        content=(
            "Historical Task Snapshot: current_task is T16, the worktree is clean, "
            "and 170 tests passed before the next action."
        ),
        target="memory",
        memory_type="summary",
        entry_ids=[77],
        session_ids=["journal-session"],
    )

    result = apply_journal_candidates(
        conn,
        None,
        _scope().scope,
        run_id="journal-pollution",
        candidates=[candidate],
        dry_run=False,
        runtime_config={},
    )

    assert result["counts"]["quarantined"] == 1
    assert result["processed_entry_ids"] == [77]
    assert result["actions"][0]["action"] == "quarantine"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    rejection = conn.execute(
        "SELECT run_id, reason FROM journal_rejections WHERE journal_entry_id = 77"
    ).fetchone()
    assert rejection["run_id"] == "journal-pollution"
    assert "digest pollution" in rejection["reason"]
    conn.close()
