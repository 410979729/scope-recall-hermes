"""Historical SplitPlan shadow and atomic-apply contracts."""

from __future__ import annotations

from dataclasses import replace
import json
import sqlite3

import pytest

import scope_recall.fact_split_shadow as split_shadow_impl
from scope_recall.fact_actions import ClaimDraft
from scope_recall.fact_repository import insert_claim
from scope_recall.fact_split_shadow import (
    SplitCandidateClaim,
    SplitEvidenceSpan,
    SplitPlanApproval,
    SplitPlanApplyError,
    SplitPlanConflictError,
    SplitPlanError,
    apply_split_plan,
    build_split_plan,
)
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_generation import (
    CURRENT_GENERATION_KEY,
    ensure_vector_generation_schema,
)


AT = "2026-08-27T12:00:00+00:00"
SOURCE_CONTENT = "Joy lives in Paris. Joy prefers concise reports."
ENABLED = {"fact_backfill": {"shadow_enabled": True}}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    ensure_vector_generation_schema(conn)
    conn.execute(
        "INSERT INTO vector_generation_state(key, value, updated_at) VALUES (?, 'gen-test', ?)",
        (CURRENT_GENERATION_KEY, AT),
    )
    conn.commit()
    return conn


def _seed_source(
    conn: sqlite3.Connection,
    *,
    memory_id: str = "mixed-source",
    target: str = "memory",
) -> None:
    stored_id, _summary, _updated_at, stored = store_row(
        conn,
        memory_id=memory_id,
        scope_id="scope-a",
        platform="test",
        user_id="user-a",
        chat_id="chat-a",
        thread_id="thread-a",
        gateway_session_key="gateway-a",
        agent_identity="agent-a",
        agent_workspace="workspace-a",
        session_id="session-a",
        source="historical-import",
        target=target,
        content=SOURCE_CONTENT,
        metadata=json.dumps({"memory_type": "episodic"}),
        allow_duplicate=True,
        timestamp=AT,
    )
    assert stored and stored_id == memory_id


def _candidates() -> tuple[SplitCandidateClaim, ...]:
    return (
        SplitCandidateClaim(
            memory_type="factual",
            claim=ClaimDraft.from_parts(
                subject="Joy",
                predicate="lives in",
                value="Paris",
                scope_id="scope-a",
            ),
            confidence=0.98,
        ),
        SplitCandidateClaim(
            memory_type="preference",
            claim=ClaimDraft.from_parts(
                subject="Joy",
                predicate="prefers",
                value="concise reports",
                scope_id="scope-a",
            ),
            confidence=0.97,
        ),
    )


def _spans() -> tuple[SplitEvidenceSpan, ...]:
    first = "Joy lives in Paris."
    second = "Joy prefers concise reports."
    return (
        SplitEvidenceSpan(0, 0, len(first)),
        SplitEvidenceSpan(1, SOURCE_CONTENT.index(second), len(SOURCE_CONTENT)),
    )


def _build(conn: sqlite3.Connection):
    return build_split_plan(
        conn,
        source_memory_id="mixed-source",
        candidate_claims=_candidates(),
        evidence_spans=_spans(),
        projection_texts=("Joy lives in Paris.", "Joy prefers concise reports."),
        extractor_policy_version="fact-split-v1",
        runtime_config=ENABLED,
        readable_scope_ids=("scope-a",),
    )


def _approval(plan, *, approval_id: str = "approval-1") -> SplitPlanApproval:
    return SplitPlanApproval(
        plan_hash=plan.plan_hash,
        approval_id=approval_id,
        approved_by="scope-recall:test-reviewer",
        approved_at=AT,
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
            "governance_audit_events",
            "vector_outbox",
            "fact_action_receipts",
        )
    }


def _metadata(conn: sqlite3.Connection, memory_id: str = "mixed-source") -> dict:
    raw = conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()[0]
    return json.loads(str(raw))


def test_shadow_builder_is_read_only_and_default_artifact_is_decontented() -> None:
    conn = _conn()
    _seed_source(conn)
    before_counts = _counts(conn)
    before_changes = conn.total_changes

    plan = _build(conn)

    assert _counts(conn) == before_counts
    assert conn.total_changes == before_changes
    assert len(plan.candidate_claims) == 2
    assert all(span.span_sha256 for span in plan.evidence_spans)
    safe = json.dumps(plan.as_decontented_artifact(), sort_keys=True)
    assert "Paris" not in safe
    assert "concise reports" not in safe
    assert "mixed-source" not in safe
    private = json.dumps(plan.as_local_private_artifact(), sort_keys=True)
    assert "Paris" in private and "concise reports" in private
    assert _build(conn).plan_hash == plan.plan_hash


def test_shadow_builder_fails_closed_when_flag_is_off_or_source_is_general() -> None:
    conn = _conn()
    _seed_source(conn)
    before = _counts(conn)
    with pytest.raises(SplitPlanError, match="shadow_enabled"):
        build_split_plan(
            conn,
            source_memory_id="mixed-source",
            candidate_claims=_candidates(),
            evidence_spans=_spans(),
            projection_texts=("Joy lives in Paris.", "Joy prefers concise reports."),
            extractor_policy_version="fact-split-v1",
            runtime_config={},
            readable_scope_ids=("scope-a",),
        )
    assert _counts(conn) == before

    other = _conn()
    _seed_source(other, target="general")
    with pytest.raises(SplitPlanError, match="general scratch"):
        _build(other)


def test_shadow_builder_rejects_tampered_claim_identity_and_secret_span() -> None:
    conn = _conn()
    _seed_source(conn)
    candidates = list(_candidates())
    forged = replace(candidates[0].claim, fact_key=candidates[1].claim.fact_key)
    candidates[0] = replace(candidates[0], claim=forged)
    with pytest.raises(SplitPlanError, match="identity is not canonical"):
        build_split_plan(
            conn,
            source_memory_id="mixed-source",
            candidate_claims=tuple(candidates),
            evidence_spans=_spans(),
            projection_texts=("Joy lives in Paris.", "Joy prefers concise reports."),
            extractor_policy_version="fact-split-v1",
            runtime_config=ENABLED,
            readable_scope_ids=("scope-a",),
        )

    conn.execute(
        "UPDATE memories SET content = ?, summary = ? WHERE id = 'mixed-source'",
        (
            "Joy lives in api_key=legacy_token_example_12345. "
            "Joy prefers concise reports.",
            "secret",
        ),
    )
    conn.commit()
    secret_content = str(
        conn.execute("SELECT content FROM memories WHERE id = 'mixed-source'").fetchone()[0]
    )
    with pytest.raises(SplitPlanError, match="secret-like"):
        build_split_plan(
            conn,
            source_memory_id="mixed-source",
            candidate_claims=_candidates(),
            evidence_spans=(
                SplitEvidenceSpan(0, 0, secret_content.index(". Joy") + 1),
                _spans()[1],
            ),
            projection_texts=("Joy lives in Paris.", "Joy prefers concise reports."),
            extractor_policy_version="fact-split-v1",
            runtime_config=ENABLED,
            readable_scope_ids=("scope-a",),
        )


def test_apply_requires_exact_plan_bound_approval_and_untampered_plan() -> None:
    conn = _conn()
    _seed_source(conn)
    plan = _build(conn)
    before = _counts(conn)
    wrong = replace(_approval(plan), plan_hash="0" * 64)
    with pytest.raises(SplitPlanError, match="not bound"):
        apply_split_plan(
            conn,
            plan=plan,
            approval=wrong,
            runtime_config=ENABLED,
            writable_scope_ids=("scope-a",),
        )
    tampered = replace(plan, projection_texts=("tampered", *plan.projection_texts[1:]))
    with pytest.raises(SplitPlanConflictError, match="hash"):
        apply_split_plan(
            conn,
            plan=tampered,
            approval=_approval(plan),
            runtime_config=ENABLED,
            writable_scope_ids=("scope-a",),
        )
    assert _counts(conn) == before
    assert _metadata(conn)["lifecycle"] == "promoted"

    forged = replace(
        plan,
        candidate_claims=(plan.candidate_claims[0],),
        evidence_spans=(plan.evidence_spans[0],),
        projection_texts=(plan.projection_texts[0],),
    )
    forged = replace(
        forged,
        plan_hash=split_shadow_impl._sha256_json(
            split_shadow_impl._plan_material(
                source_memory_id=forged.source_memory_id,
                source_scope_id=forged.source_scope_id,
                source_target=forged.source_target,
                expected_updated_at=forged.expected_updated_at,
                expected_lifecycle=forged.expected_lifecycle,
                source_content_hash=forged.source_content_hash,
                candidate_claims=forged.candidate_claims,
                evidence_spans=forged.evidence_spans,
                projection_texts=forged.projection_texts,
                extractor_policy_version=forged.extractor_policy_version,
            )
        ),
    )
    with pytest.raises(SplitPlanError, match="between 2 and 32"):
        apply_split_plan(
            conn,
            plan=forged,
            approval=_approval(forged),
            runtime_config=ENABLED,
            writable_scope_ids=("scope-a",),
        )
    assert _counts(conn) == before


@pytest.mark.parametrize("drift", ["updated_at", "content", "lifecycle", "fact_owned"])
def test_apply_refuses_source_drift_without_partial_split(drift: str) -> None:
    conn = _conn()
    _seed_source(conn)
    plan = _build(conn)
    if drift == "updated_at":
        conn.execute("UPDATE memories SET updated_at = ? WHERE id = 'mixed-source'", ("2026-08-27T13:00:00+00:00",))
    elif drift == "content":
        conn.execute("UPDATE memories SET content = ? WHERE id = 'mixed-source'", (SOURCE_CONTENT + " changed",))
    elif drift == "lifecycle":
        metadata = _metadata(conn)
        metadata["lifecycle"] = "candidate"
        conn.execute("UPDATE memories SET metadata = ? WHERE id = 'mixed-source'", (json.dumps(metadata),))
    else:
        insert_claim(
            conn,
            claim_id="rogue-claim",
            memory_id="mixed-source",
            scope_id="scope-a",
            subject="Joy",
            predicate="knows",
            value="something",
            recorded_at=AT,
            confidence=0.9,
            source_type="test",
        )
    conn.commit()
    before = _counts(conn)
    with pytest.raises(SplitPlanConflictError):
        apply_split_plan(
            conn,
            plan=plan,
            approval=_approval(plan),
            runtime_config=ENABLED,
            writable_scope_ids=("scope-a",),
        )
    assert _counts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault_stage",
    ["after_source_cas", "after_candidate_0", "before_source_transition", "after_source_transition"],
)
def test_any_apply_fault_rolls_back_whole_batch(fault_stage: str) -> None:
    conn = _conn()
    _seed_source(conn)
    plan = _build(conn)
    before = _counts(conn)

    def fail(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError("injected split failure")

    with pytest.raises(SplitPlanApplyError, match="injected split failure"):
        apply_split_plan(
            conn,
            plan=plan,
            approval=_approval(plan),
            runtime_config=ENABLED,
            writable_scope_ids=("scope-a",),
            fault_injector=fail,
        )
    assert _counts(conn) == before
    assert _metadata(conn)["lifecycle"] == "promoted"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_success_creates_independent_pairs_then_retires_source_and_replays() -> None:
    conn = _conn()
    _seed_source(conn)
    plan = _build(conn)
    approval = _approval(plan)

    result = apply_split_plan(
        conn,
        plan=plan,
        approval=approval,
        runtime_config=ENABLED,
        writable_scope_ids=("scope-a",),
    )

    assert result.status == "applied"
    assert not result.replayed
    assert len(result.projection_pairs) == 2
    assert len({pair["memory_id"] for pair in result.projection_pairs}) == 2
    assert len({pair["claim_id"] for pair in result.projection_pairs}) == 2
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM fact_claims WHERE memory_id = 'mixed-source'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM memories_fts WHERE memory_id = 'mixed-source'").fetchone()[0] == 0
    metadata = _metadata(conn)
    assert metadata["lifecycle"] == "superseded"
    assert metadata["split_plan_hash"] == plan.plan_hash
    assert len(metadata["split_projection_pairs"]) == 2

    before = _counts(conn)
    replay = apply_split_plan(
        conn,
        plan=plan,
        approval=approval,
        runtime_config=ENABLED,
        writable_scope_ids=("scope-a",),
    )
    assert replay.status == "replayed" and replay.replayed
    assert replay.source_transition_event_id == result.source_transition_event_id
    assert _counts(conn) == before

    first_projection = result.projection_pairs[0]["memory_id"]
    conn.execute("UPDATE memories SET content = '' WHERE id = ?", (first_projection,))
    conn.commit()
    with pytest.raises(SplitPlanConflictError):
        apply_split_plan(
            conn,
            plan=plan,
            approval=approval,
            runtime_config=ENABLED,
            writable_scope_ids=("scope-a",),
        )


def test_caller_owned_transaction_remains_pending_and_can_roll_back() -> None:
    conn = _conn()
    _seed_source(conn)
    plan = _build(conn)
    before = _counts(conn)
    conn.execute("BEGIN IMMEDIATE")

    result = apply_split_plan(
        conn,
        plan=plan,
        approval=_approval(plan),
        runtime_config=ENABLED,
        writable_scope_ids=("scope-a",),
    )

    assert result.status == "applied_pending_outer_commit"
    assert conn.in_transaction
    conn.rollback()
    assert _counts(conn) == before
    assert _metadata(conn)["lifecycle"] == "promoted"
