"""Atomic cross-surface execution and replay contracts."""

from __future__ import annotations

import json
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
    FactExecutionError,
    execute_fact_plan,
)
from scope_recall.fact_repository import (
    claims_as_of,
    current_claims,
    get_claim,
    insert_claim,
)
from scope_recall.sql_store import ensure_schema
from scope_recall.vector_generation import (
    CURRENT_GENERATION_KEY,
    ensure_vector_generation_schema,
)


AT = "2026-04-10T12:00:00+00:00"


def _conn(*, vector: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    if vector:
        ensure_vector_generation_schema(conn)
        conn.execute(
            "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, 'gen-test', ?)",
            (CURRENT_GENERATION_KEY, AT),
        )
    conn.commit()
    return conn


def _proposal(
    action: EvolutionAction,
    *,
    value: str = "Bangalore",
    valid_from: str = "2026-04-01T00:00:00+00:00",
    valid_to: str = "",
    targets: tuple[str, ...] = (),
) -> EvolutionProposal:
    claim = None
    quote = f"I live in {value}; please correct the old city."
    if action in {EvolutionAction.ADD, EvolutionAction.ENRICH, EvolutionAction.SUPERSEDE}:
        claim = ClaimDraft.from_parts(
            subject="Asha",
            predicate="lives in",
            value=value,
            scope_id="scope-a",
            valid_from=valid_from,
            valid_to=valid_to,
        )
    elif action is EvolutionAction.RETRACT:
        claim = ClaimDraft.from_parts(
            subject="Asha",
            predicate="lives in",
            value="Mumbai",
            scope_id="scope-a",
            valid_from="2025-01-01T00:00:00+00:00",
        )
        quote = "I no longer live in Mumbai; retract the old city."
    return EvolutionProposal(
        action=action,
        raw_action=action.value,
        claim=claim,
        target_ids=targets,
        evidence_refs=(
            EvidenceReference(
                "user_message",
                "message-7",
                quote,
                speaker_subject="Asha",
            ),
        ),
        confidence=0.9,
        reason="direct user correction",
        source="nightly_digest",
    )


def _plan(
    action: EvolutionAction,
    *,
    value: str = "Bangalore",
    valid_from: str = "2026-04-01T00:00:00+00:00",
    valid_to: str = "",
    targets: tuple[str, ...] = (),
    expected: dict[str, str] | None = None,
    action_id: str = "action-1",
    idempotency_key: str = "idem-1",
    mode: str = "reviewed_apply",
) -> EvolutionPlan:
    return EvolutionPlan(
        proposal=_proposal(
            action,
            value=value,
            valid_from=valid_from,
            valid_to=valid_to,
            targets=targets,
        ),
        action_id=action_id,
        idempotency_key=idempotency_key,
        policy_mode=mode,
        expected_versions=expected or {},
    )


def _context(
    *,
    new_memory_id: str = "memory-new",
    new_claim_id: str = "claim-new",
    effective_at: str = "",
) -> FactExecutionContext:
    return FactExecutionContext(
        scope_id="scope-a",
        writable_scope_ids=("scope-a",),
        actor="scope-recall:test",
        timestamp=AT,
        source="fact_evolution",
        target="memory",
        session_id="session-1",
        platform="test",
        user_id="user-1",
        new_memory_id=new_memory_id,
        new_claim_id=new_claim_id,
        effective_at=effective_at,
        metadata={"memory_type": "fact"},
    )


def _policy(plan: EvolutionPlan):
    return evaluate_evolution_policy(
        plan.proposal,
        allowed_target_ids=set(plan.proposal.target_ids),
    )


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
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
    "quote",
    [
        "Alice worked at OldCo last summer.",
        "Alice worked at OldCo.",
        "Alice worked at OldCo from June to August.",
        "Alice works at OldCo from June to August.",
        "Alice works at OldCo between June and August.",
        "Alice works at OldCo until August.",
        "Alice has worked at OldCo.",
        "Alice did work at OldCo.",
        "Alice was working at OldCo over the summer.",
        "Alice started working at OldCo.",
        "Alice stopped working at OldCo.",
        "Alice began working at OldCo.",
        "Alice finished working at OldCo.",
        "Alice tried working at OldCo.",
        "Alice considered working at OldCo.",
        "Alice returned to working at OldCo.",
        "Alice is resuming working at OldCo.",
        "Alice commenced working at OldCo.",
        "Alice ceased working at OldCo.",
        "Alice attempted working at OldCo.",
        "Alice contemplated working at OldCo.",
        "Alice worked at OldCo during June.",
        "Alice works at OldCo from June through August.",
        "Alice works at OldCo from Jun to Aug.",
        "Alice works at OldCo from Jun. through Aug.",
        "Alice works at OldCo from Jan. to Mar.",
        "Alice works at OldCo from Jun.-Aug.",
        "Alice works at OldCo from Sep.–Nov.",
        "Alice works at OldCo from June-August.",
        "Alice works at OldCo between Jun and Aug.",
        "Alice works at OldCo Jun-Aug.",
        "Alice works at OldCo for a six-month contract.",
        "Alice works at OldCo for six months.",
        "Alice works at OldCo for a 6-month contract.",
        "Alice works at OldCo for 6 months.",
        "Alice works at OldCo for five years.",
        "Alice works at OldCo on a contract through August.",
        "Alice works at OldCo under a fixed-term contract.",
        "Alice works at OldCo June through August.",
        "Alice works at OldCo for now.",
        "Alice works at OldCo for the time being.",
        "Alice works at OldCo for another six months.",
        "Alice works at OldCo for another year.",
        "Alice works at OldCo for up-to six months.",
        "Alice works at OldCo for upto six months.",
        "Alice works at OldCo for no-more-than six months.",
        "Alice works at OldCo for max six months.",
        "Alice works at OldCo for min six months.",
        "Alice works at OldCo for a max of six months.",
        "Alice works at OldCo for maximum six months.",
        "Alice works at OldCo for minimum six months.",
        "Alice works at OldCo for up-to-six months.",
        "Alice works at OldCo for upto-six months.",
        "Alice works at OldCo for no-more-than-six months.",
        "Alice works at OldCo for no more than six months.",
        "Alice works at OldCo for up to six months.",
        "Alice works at OldCo for a half-year contract.",
        "Alice works at OldCo on probation.",
        "Alice works at OldCo for only six months.",
        "Alice works at OldCo for just six months.",
        "Alice works at OldCo for six more months.",
        "Alice works at OldCo for the coming six months.",
        "Alice works at OldCo for under six months.",
        "Alice works at OldCo for over six months.",
        "Alice works at OldCo for fewer than six months.",
        "Alice works at OldCo for a maximum of six months.",
        "Alice works at OldCo for a minimum of six months.",
        "Alice works at OldCo for six to twelve months.",
        "Alice works at OldCo for 6-12 months.",
        "Alice works at OldCo for six–twelve months.",
        "Alice works at OldCo for the rest of the year.",
        "Alice works at OldCo for the remainder of the year.",
        "Alice works at OldCo between June 1 and August 31.",
        "Alice works at OldCo from 6/1 to 8/31.",
        "Alice works at OldCo from 2026-06-01 to 2026-08-31.",
        "Alice works at OldCo from 2026/06/01 to 2026/08/31.",
        "Alice works at OldCo from 2026.06.01 through 2026.08.31.",
        "Alice works at OldCo 2026-06-01–2026-08-31.",
        "Alice works at OldCo between 6/1 and 8/31.",
        "Alice works at OldCo 6/1-8/31.",
        "Alice works at OldCo from 06-01 through 08-31.",
        "Alice works at OldCo，为期六个月。",
        "Alice worked at OldCo throughout the winter.",
        "Alice is working at OldCo this spring.",
    ],
)
def test_last_summer_employment_requires_review_and_executor_writes_nothing(
    quote: str,
):
    conn = _conn()
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=ClaimDraft.from_parts(
            subject="Alice",
            predicate="works at",
            value="OldCo",
            scope_id="scope-a",
        ),
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id="past-employment",
                quote=quote,
                speaker_subject="Alice",
            ),
        ),
        confidence=0.99,
        reason="past employment must not occupy current slot",
    )
    plan = EvolutionPlan(
        proposal=proposal,
        action_id="past-employment-action",
        idempotency_key="past-employment-idempotency",
        policy_mode="auto_apply",
    )
    policy = _policy(plan)
    before = _counts(conn)

    result = execute_fact_plan(conn, plan, policy, _context())

    assert policy.allowed is False
    assert policy.effective_action is EvolutionAction.REVIEW
    assert result.applied is False
    assert result.status == "review"
    assert _counts(conn) == before
    conn.close()


def _seed_old(conn: sqlite3.Connection) -> str:
    updated_at = "2026-03-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary, created_at,
            updated_at, metadata
        ) VALUES (
            'memory-old', 'scope-a', 'test', 'memory', 'Asha lives in Mumbai',
            'Asha lives in Mumbai', ?, ?, '{"lifecycle":"active","memory_type":"fact"}'
        )
        """,
        (updated_at, updated_at),
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) VALUES ('memory-old', 'Asha lives in Mumbai', 'Asha lives in Mumbai')"
    )
    insert_claim(
        conn,
        claim_id="claim-old",
        memory_id="memory-old",
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        value="Mumbai",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at="2026-03-01T00:00:00+00:00",
        confidence=0.9,
        source_type="user_message",
        source_ref="message-old",
    )
    conn.commit()
    return updated_at


def test_add_applies_all_mandatory_surfaces_in_one_committed_transaction():
    conn = _conn()
    plan = _plan(EvolutionAction.ADD)

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.applied is True
    assert result.status == "applied"
    assert result.receipt["memory_ids"] == ["memory-new"]
    assert result.receipt["claim_ids"] == ["claim-new"]
    assert _counts(conn) == {
        "memories": 1,
        "memories_fts": 1,
        "fact_claims": 1,
        "fact_claim_evidence": 1,
        "fact_freshness": 1,
        "memory_relations": 0,
        "governance_audit_events": 1,
        "vector_outbox": 1,
        "fact_action_receipts": 1,
    }
    outbox_payload = conn.execute("SELECT payload FROM vector_outbox").fetchone()[0]
    assert "Bangalore" not in str(outbox_payload)


def test_same_idempotency_key_replays_without_duplicate_side_effects():
    conn = _conn()
    plan = _plan(EvolutionAction.ADD)
    first = execute_fact_plan(conn, plan, _policy(plan), _context())
    before = _counts(conn)

    replay = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert first.applied is True
    assert replay.applied is True
    assert replay.status == "replayed"
    assert replay.receipt["replayed"] is True
    assert _counts(conn) == before


def test_idempotency_key_collision_with_changed_request_fails_closed():
    conn = _conn()
    first = _plan(EvolutionAction.ADD)
    execute_fact_plan(conn, first, _policy(first), _context())
    changed = _plan(EvolutionAction.ADD, value="Delhi")
    before = _counts(conn)

    with pytest.raises(FactExecutionConflictError, match="idempotency"):
        execute_fact_plan(conn, changed, _policy(changed), _context())

    assert _counts(conn) == before


def test_supersede_closes_old_claim_hides_old_memory_and_links_successor():
    conn = _conn()
    expected = _seed_old(conn)
    plan = _plan(
        EvolutionAction.SUPERSEDE,
        targets=("memory-old",),
        expected={"memory-old": expected},
    )

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.applied is True
    old_claim = get_claim(conn, "claim-old", scope_ids=["scope-a"])
    assert old_claim is not None
    assert old_claim.status == "superseded"
    assert old_claim.valid_to == "2026-04-01T00:00:00+00:00"
    assert old_claim.superseded_by_claim_id == "claim-new"
    current = current_claims(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
    )
    assert [claim.claim_id for claim in current] == ["claim-new"]
    old_row = conn.execute("SELECT metadata FROM memories WHERE id='memory-old'").fetchone()
    assert json.loads(old_row[0])["lifecycle"] == "superseded"
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='memory-old'").fetchone()[0] == 1
    relation = conn.execute(
        "SELECT source_memory_id, target_memory_id, relation_type FROM memory_relations"
    ).fetchone()
    assert tuple(relation) == ("memory-new", "memory-old", "supersedes")
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 2
    events = result.receipt["vector_outbox_events"]
    assert [(item["memory_id"], item["operation"]) for item in events] == [
        ("memory-old", "delete"),
        ("memory-new", "upsert"),
    ]
    outbox_keys = {
        str(row[0]) for row in conn.execute("SELECT event_key FROM vector_outbox")
    }
    assert set(result.receipt["vector_outbox_keys"]) == outbox_keys


def test_future_supersede_fails_closed_without_current_truth_gap():
    conn = _conn()
    expected = _seed_old(conn)
    plan = _plan(
        EvolutionAction.SUPERSEDE,
        valid_from="2099-01-01T00:00:00+00:00",
        targets=("memory-old",),
        expected={"memory-old": expected},
        action_id="future-supersede",
        idempotency_key="future-supersede",
    )
    before = _counts(conn)

    with pytest.raises(FactExecutionConflictError, match="future-effective"):
        execute_fact_plan(conn, plan, _policy(plan), _context())

    assert _counts(conn) == before
    current = current_claims(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at=AT,
    )
    assert [claim.value for claim in current] == ["Mumbai"]


def test_finite_historical_add_fails_closed_without_claim_slot_pollution():
    conn = _conn()
    plan = _plan(
        EvolutionAction.ADD,
        valid_from="2020-01-01T00:00:00+00:00",
        valid_to="2021-01-01T00:00:00+00:00",
        action_id="finite-history-add",
        idempotency_key="finite-history-add",
    )
    before = _counts(conn)

    with pytest.raises(FactExecutionConflictError, match="finite historical ADD"):
        execute_fact_plan(conn, plan, _policy(plan), _context())

    assert _counts(conn) == before


def test_future_add_fails_closed_without_occupying_the_current_slot():
    conn = _conn()
    plan = _plan(
        EvolutionAction.ADD,
        valid_from="2099-01-01T00:00:00+00:00",
        action_id="future-add",
        idempotency_key="future-add",
    )
    before = _counts(conn)

    with pytest.raises(FactExecutionConflictError, match="future-effective add"):
        execute_fact_plan(conn, plan, _policy(plan), _context())

    assert _counts(conn) == before


def test_stale_expected_version_rolls_back_without_partial_successor():
    conn = _conn()
    _seed_old(conn)
    plan = _plan(
        EvolutionAction.SUPERSEDE,
        targets=("memory-old",),
        expected={"memory-old": "stale-version"},
    )
    before = _counts(conn)

    with pytest.raises(FactExecutionConflictError, match="changed after review"):
        execute_fact_plan(conn, plan, _policy(plan), _context())

    assert _counts(conn) == before
    assert get_claim(conn, "claim-old", scope_ids=["scope-a"]).status == "current"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "failure_stage",
    ["after_memory_insert", "after_claim_insert", "after_companions", "before_receipt"],
)
def test_injected_failure_rolls_back_every_surface(failure_stage: str):
    conn = _conn()
    plan = _plan(EvolutionAction.ADD)
    before = _counts(conn)

    def inject(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(f"injected at {stage}")

    with pytest.raises(FactExecutionError, match="injected"):
        execute_fact_plan(
            conn,
            plan,
            _policy(plan),
            _context(),
            fault_injector=inject,
        )

    assert _counts(conn) == before


def test_caller_owned_transaction_remains_open_and_can_roll_back_everything():
    conn = _conn()
    plan = _plan(EvolutionAction.ADD)
    conn.execute("BEGIN")

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.applied is True
    assert result.status == "applied_pending_outer_commit"
    assert conn.in_transaction is True
    assert _counts(conn)["fact_action_receipts"] == 1
    conn.rollback()
    assert _counts(conn)["memories"] == 0
    assert _counts(conn)["fact_action_receipts"] == 0


def test_retract_closes_claim_and_preserves_historical_memory_row():
    conn = _conn()
    expected = _seed_old(conn)
    plan = _plan(
        EvolutionAction.RETRACT,
        targets=("memory-old",),
        expected={"memory-old": expected},
    )

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.applied is True
    assert get_claim(conn, "claim-old", scope_ids=["scope-a"]).status == "retracted"  # type: ignore[union-attr]
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='memory-old'").fetchone()[0] == 1
    metadata = json.loads(
        conn.execute("SELECT metadata FROM memories WHERE id='memory-old'").fetchone()[0]
    )
    assert metadata["lifecycle"] == "obsolete"
    assert [
        (item["memory_id"], item["operation"])
        for item in result.receipt["vector_outbox_events"]
    ] == [("memory-old", "delete")]
    outbox_key = str(
        conn.execute(
            "SELECT event_key FROM vector_outbox WHERE memory_id='memory-old'"
        ).fetchone()[0]
    )
    assert result.receipt["vector_outbox_keys"] == [outbox_key]


def test_retract_defaults_valid_time_boundary_to_execution_timestamp():
    conn = _conn()
    expected = _seed_old(conn)
    plan = _plan(
        EvolutionAction.RETRACT,
        targets=("memory-old",),
        expected={"memory-old": expected},
        action_id="retract-effective-now",
        idempotency_key="retract-effective-now",
    )

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.applied is True
    claim = get_claim(conn, "claim-old", scope_ids=["scope-a"])
    assert claim is not None
    assert claim.valid_to == AT
    future = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2027-01-01T00:00:00+00:00",
    )
    assert future == []


def test_retract_explicit_past_boundary_preserves_recorded_as_of_semantics():
    conn = _conn()
    expected = _seed_old(conn)
    plan = _plan(
        EvolutionAction.RETRACT,
        targets=("memory-old",),
        expected={"memory-old": expected},
        action_id="retract-effective-past",
        idempotency_key="retract-effective-past",
    )
    effective_at = "2026-04-01T00:00:00+00:00"

    result = execute_fact_plan(
        conn,
        plan,
        _policy(plan),
        _context(effective_at=effective_at),
    )

    assert result.applied is True
    assert result.receipt["effective_at"] == effective_at
    claim = get_claim(conn, "claim-old", scope_ids=["scope-a"])
    assert claim is not None and claim.valid_to == effective_at
    known_before_correction = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2026-04-05T00:00:00+00:00",
        known_at="2026-04-05T12:00:00+00:00",
    )
    known_after_correction = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2026-04-05T00:00:00+00:00",
        known_at="2026-04-11T00:00:00+00:00",
    )
    assert [item.value for item in known_before_correction] == ["Mumbai"]
    assert known_after_correction == []


def test_future_retract_fails_closed_without_hiding_current_truth():
    conn = _conn()
    expected = _seed_old(conn)
    plan = _plan(
        EvolutionAction.RETRACT,
        targets=("memory-old",),
        expected={"memory-old": expected},
        action_id="retract-effective-future",
        idempotency_key="retract-effective-future",
    )
    before = _counts(conn)

    with pytest.raises(FactExecutionConflictError, match="future-effective retract"):
        execute_fact_plan(
            conn,
            plan,
            _policy(plan),
            _context(effective_at="2099-01-01T00:00:00+00:00"),
        )

    assert _counts(conn) == before
    claim = get_claim(conn, "claim-old", scope_ids=["scope-a"])
    assert claim is not None and claim.status == "current" and claim.valid_to is None


def test_enrich_links_new_evidence_without_rewriting_memory_content():
    conn = _conn()
    expected = _seed_old(conn)
    before_content = conn.execute(
        "SELECT content FROM memories WHERE id='memory-old'"
    ).fetchone()[0]
    plan = _plan(
        EvolutionAction.ENRICH,
        value="Mumbai",
        targets=("memory-old",),
        expected={"memory-old": expected},
    )

    result = execute_fact_plan(conn, plan, _policy(plan), _context())

    assert result.applied is True
    assert conn.execute("SELECT COUNT(*) FROM fact_claim_evidence").fetchone()[0] == 1
    assert conn.execute("SELECT content FROM memories WHERE id='memory-old'").fetchone()[0] == before_content
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 0
