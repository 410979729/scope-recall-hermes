"""Preview and apply-mode safety matrix for fact evolution execution."""

from __future__ import annotations

import sqlite3

import pytest

from scope_recall.evolution_policy import evaluate_evolution_policy
from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionPlan,
    EvolutionProposal,
)
from scope_recall.fact_executor import (
    FactExecutionConflictError,
    FactExecutionContext,
    execute_fact_plan,
)
from scope_recall.sql_store import ensure_schema
from scope_recall.vector_generation import ensure_vector_generation_schema


AT = "2026-04-10T12:00:00+00:00"
TARGET_ID = "reviewed-memory"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    ensure_vector_generation_schema(conn)
    conn.commit()
    return conn


def _proposal(
    action: EvolutionAction,
    *,
    evidence: bool = True,
) -> EvolutionProposal:
    claim = None
    quote = "I live in Bangalore; please correct the old city."
    if action in {EvolutionAction.ADD, EvolutionAction.ENRICH, EvolutionAction.SUPERSEDE}:
        claim = ClaimDraft.from_parts(
            subject="Asha",
            predicate="lives in",
            value="Bangalore",
            scope_id="scope-a",
            valid_from="2026-04-01T00:00:00+00:00",
        )
    elif action is EvolutionAction.RETRACT:
        claim = ClaimDraft.from_parts(
            subject="Asha",
            predicate="lives in",
            value="Mumbai",
            scope_id="scope-a",
            valid_from="2026-01-01T00:00:00+00:00",
        )
        quote = "I no longer live in Mumbai; retract the old city."
    targets = (
        (TARGET_ID,)
        if action in {EvolutionAction.ENRICH, EvolutionAction.SUPERSEDE, EvolutionAction.RETRACT}
        else ()
    )
    refs = (
        (
            EvidenceReference(
                source_type="user_message",
                source_id="message-7",
                quote=quote,
                speaker_subject="Asha",
            ),
        )
        if evidence
        else ()
    )
    return EvolutionProposal(
        action=action,
        raw_action=action.value,
        claim=claim,
        target_ids=targets,
        evidence_refs=refs,
        confidence=0.9,
        reason="direct correction",
        source="nightly_digest",
    )


def _plan(
    action: EvolutionAction,
    *,
    mode: str = "preview",
    evidence: bool = True,
) -> EvolutionPlan:
    return EvolutionPlan(
        proposal=_proposal(action, evidence=evidence),
        action_id=f"action-{action.value}-{mode}",
        idempotency_key=f"idem-{action.value}-{mode}",
        policy_mode=mode,
        expected_versions={TARGET_ID: "reviewed-version"}
        if action in {EvolutionAction.SUPERSEDE, EvolutionAction.RETRACT}
        else {},
    )


def _context() -> FactExecutionContext:
    return FactExecutionContext(
        scope_id="scope-a",
        writable_scope_ids=("scope-a",),
        actor="scope-recall:test",
        timestamp=AT,
        source="fact_evolution",
        target="memory",
        session_id="session-1",
        new_memory_id="memory-new",
        new_claim_id="claim-new",
        metadata={"memory_type": "fact"},
    )


def _policy(plan: EvolutionPlan):
    return evaluate_evolution_policy(
        plan.proposal,
        allowed_target_ids=set(plan.proposal.target_ids),
    )


def _surface_counts(conn: sqlite3.Connection) -> dict[str, int]:
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
    "action",
    [
        EvolutionAction.ADD,
        EvolutionAction.ENRICH,
        EvolutionAction.SUPERSEDE,
        EvolutionAction.RETRACT,
    ],
)
def test_preview_matrix_is_zero_write_and_bounded(action: EvolutionAction):
    conn = _conn()
    plan = _plan(action)
    before = _surface_counts(conn)
    changes_before = conn.total_changes

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.status == "preview"
    assert result.applied is False
    assert result.receipt["would_write"] is True
    assert _surface_counts(conn) == before
    assert conn.total_changes == changes_before
    assert conn.in_transaction is False
    rendered = str(result.receipt)
    assert "Bangalore" not in rendered
    assert "correct the old city" not in rendered


def test_policy_rejection_in_apply_mode_is_zero_write():
    conn = _conn()
    plan = _plan(EvolutionAction.ADD, mode="reviewed_apply", evidence=False)
    policy = _policy(plan)
    before = _surface_counts(conn)
    changes_before = conn.total_changes

    result = execute_fact_plan(conn, plan, policy, _context())

    assert policy.allowed is False
    assert result.status == "review"
    assert result.action is EvolutionAction.REVIEW
    assert result.applied is False
    assert _surface_counts(conn) == before
    assert conn.total_changes == changes_before


@pytest.mark.parametrize(
    ("quote", "speaker_subject"),
    [
        ("I don't live in Bangalore.", "Asha"),
        ("I live in Bangalore.", "AnotherPerson"),
    ],
)
def test_adversarial_direct_evidence_is_review_only_and_zero_write(
    quote: str,
    speaker_subject: str,
) -> None:
    conn = _conn()
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=ClaimDraft.from_parts(
            subject="Asha",
            predicate="lives in",
            value="Bangalore",
            scope_id="scope-a",
        ),
        evidence_refs=(
            EvidenceReference(
                "user_message",
                "message-adversarial",
                quote,
                speaker_subject=speaker_subject,
            ),
        ),
        confidence=0.99,
    )
    plan = EvolutionPlan(
        proposal=proposal,
        action_id="action-adversarial",
        idempotency_key="idem-adversarial",
        policy_mode="auto_apply",
    )
    policy = _policy(plan)
    before = _surface_counts(conn)
    changes_before = conn.total_changes

    result = execute_fact_plan(conn, plan, policy, _context())

    assert policy.allowed is False
    assert "authoritative_evidence_not_claim_supporting" in policy.reason_codes
    assert result.status == "review"
    assert result.applied is False
    assert _surface_counts(conn) == before
    assert conn.total_changes == changes_before


def test_high_risk_auto_apply_is_forced_back_to_review_without_reads_or_writes():
    conn = _conn()
    plan = _plan(EvolutionAction.SUPERSEDE, mode="auto_apply")
    before = _surface_counts(conn)

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.status == "review"
    assert result.applied is False
    assert _surface_counts(conn) == before


def test_unknown_apply_mode_is_blocked_without_receipt():
    conn = _conn()
    plan = _plan(EvolutionAction.ADD, mode="unrecognized")
    before = _surface_counts(conn)

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.status == "blocked"
    assert result.applied is False
    assert _surface_counts(conn) == before


def test_noop_apply_creates_no_audit_or_idempotency_receipt():
    conn = _conn()
    plan = _plan(EvolutionAction.NOOP, mode="reviewed_apply", evidence=False)
    before = _surface_counts(conn)
    changes_before = conn.total_changes

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.status == "noop"
    assert result.action is EvolutionAction.NOOP
    assert result.applied is False
    assert _surface_counts(conn) == before
    assert conn.total_changes == changes_before


def test_policy_from_another_action_cannot_authorize_plan():
    conn = _conn()
    add_plan = _plan(EvolutionAction.ADD, mode="reviewed_apply")
    noop_policy = _policy(_plan(EvolutionAction.NOOP, mode="reviewed_apply", evidence=False))
    before = _surface_counts(conn)

    with pytest.raises(FactExecutionConflictError, match="policy does not bind"):
        execute_fact_plan(conn, add_plan, noop_policy, _context())

    assert _surface_counts(conn) == before


def test_preview_runs_on_query_only_connection(tmp_path):
    db_path = tmp_path / "preview.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    ensure_schema(writer)
    ensure_vector_generation_schema(writer)
    writer.commit()
    writer.close()

    reader = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    reader.execute("PRAGMA query_only=ON")
    plan = _plan(EvolutionAction.ADD)

    result = execute_fact_plan(reader, plan, _policy(plan), _context())

    assert result.status == "preview"
    assert result.applied is False
    assert reader.in_transaction is False
    reader.close()


def test_add_without_active_vector_generation_commits_truth_and_receipt_only():
    conn = _conn()
    plan = _plan(EvolutionAction.ADD, mode="reviewed_apply")

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.status == "applied"
    assert result.applied is True
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 0
