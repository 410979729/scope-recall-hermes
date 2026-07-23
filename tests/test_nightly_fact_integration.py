"""Nightly digest integration with the shared fact planner/executor."""

from __future__ import annotations

import sqlite3

from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionProposal,
)
from scope_recall.fact_evolution import execute_pipeline_proposal
from scope_recall.fact_repository import insert_claim
from scope_recall.models import RuntimeScope
from scope_recall.nightly_digest import DigestCandidate, ScopeProfile, apply_candidates
from scope_recall.sql_store import ensure_schema, store_row


AT = "2026-04-01T00:00:00+00:00"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    conn.commit()
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
        scope_id="scope-a",
        shared_scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        writable_scope_ids=["scope-a"],
    )


def _split_scope() -> ScopeProfile:
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


def _proposal(
    action: EvolutionAction,
    *,
    target_ids: tuple[str, ...] = (),
    value: str = "Bangalore",
    scope_id: str = "scope-a",
) -> EvolutionProposal:
    claim = None
    if action in {EvolutionAction.ADD, EvolutionAction.ENRICH, EvolutionAction.SUPERSEDE}:
        claim = ClaimDraft.from_parts(
            subject="Asha",
            predicate="lives in",
            value=value,
            scope_id=scope_id,
            valid_from="2026-03-01T00:00:00+00:00",
        )
    return EvolutionProposal(
        action=action,
        raw_action=action.value,
        claim=claim,
        target_ids=target_ids,
        evidence_refs=(
            EvidenceReference(
                source_type="user_message",
                source_id="message-42",
                quote="I live in Bangalore; please correct the old city.",
                speaker_subject="Asha",
            ),
        ),
        confidence=0.96,
        reason="direct user correction",
        source="nightly_digest",
    )


def _fact_candidate(
    action: EvolutionAction = EvolutionAction.ADD,
    *,
    target_ids: tuple[str, ...] = (),
    target: str = "user",
    scope_id: str = "scope-a",
) -> DigestCandidate:
    return DigestCandidate(
        content=(
            "Asha currently lives in Bangalore; this is a stable user fact that "
            "should be remembered for future conversations."
        ),
        target=target,
        memory_type="factual",
        importance=0.9,
        confidence=0.96,
        reason="direct user correction",
        session_id="session-1",
        message_ids=[42],
        evolution=_proposal(
            action,
            target_ids=target_ids,
            scope_id=scope_id,
        ),
    )


def _enabled(mode: str = "auto_apply") -> dict[str, object]:
    return {
        "fact_evolution": {
            "enabled": True,
            "nightly_mode": mode,
        }
    }


def test_nightly_fact_evolution_gate_off_keeps_legacy_storage_behavior():
    conn = _conn()

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-gate-off",
        candidates=[_fact_candidate()],
        dry_run=False,
        runtime_config={},
    )

    assert result["counts"]["inserted"] == 1
    assert result["counts"].get("deleted", 0) == 0
    assert result["actions"][0]["action"] == "insert"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    metadata = str(conn.execute("SELECT metadata FROM memories").fetchone()[0])
    assert '"fact_evolution"' not in metadata
    assert "user_message" not in metadata
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0


def test_nightly_gate_off_general_routes_local_and_ignores_shared_match():
    conn = _conn()
    candidate = _fact_candidate(target="general", scope_id="scope-local")
    store_row(
        conn,
        memory_id="shared-general-existing",
        scope_id="scope-shared",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="seed",
        source="nightly-digest",
        target="general",
        content=candidate.content,
        metadata="{}",
    )
    conn.commit()

    result = apply_candidates(
        conn,
        None,
        _split_scope(),
        run_id="run-gate-off-general",
        candidates=[candidate],
        dry_run=False,
        runtime_config={},
    )

    assert result["counts"]["inserted"] == 1
    rows = conn.execute(
        "SELECT scope_id, target FROM memories ORDER BY scope_id"
    ).fetchall()
    assert [(row["scope_id"], row["target"]) for row in rows] == [
        ("scope-local", "general"),
        ("scope-shared", "general"),
    ]


def test_nightly_explicit_preview_mode_does_not_write():
    conn = _conn()

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-preview",
        candidates=[_fact_candidate()],
        dry_run=False,
        runtime_config=_enabled("preview"),
    )

    assert result["counts"]["previewed"] == 1
    assert result["counts"].get("deleted", 0) == 0
    assert result["actions"][0]["action"] == "preview"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0


def test_nightly_fact_add_uses_executor_and_persists_temporal_receipt():
    conn = _conn()

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-add",
        candidates=[_fact_candidate()],
        dry_run=False,
        runtime_config=_enabled(),
    )

    assert result["counts"]["inserted"] == 1
    assert result["counts"].get("deleted", 0) == 0
    assert result["actions"][0]["action"] == "evolve"
    assert result["actions"][0]["evolution_action"] == "add"
    assert result["actions"][0]["status"] == "applied"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claim_evidence").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone()[0] == 1


def test_nightly_durable_fact_target_routes_to_shared_scope():
    conn = _conn()

    result = apply_candidates(
        conn,
        None,
        _split_scope(),
        run_id="run-shared-add",
        candidates=[_fact_candidate(scope_id="scope-local")],
        dry_run=False,
        runtime_config=_enabled(),
    )

    assert result["counts"]["inserted"] == 1
    row = conn.execute("SELECT scope_id, target FROM memories").fetchone()
    assert tuple(row) == ("scope-shared", "user")
    assert conn.execute("SELECT scope_id FROM fact_claims").fetchone()[0] == "scope-shared"


def test_nightly_general_fact_target_remains_local_scope():
    conn = _conn()

    result = apply_candidates(
        conn,
        None,
        _split_scope(),
        run_id="run-local-add",
        candidates=[_fact_candidate(target="general", scope_id="scope-local")],
        dry_run=False,
        runtime_config=_enabled(),
    )

    assert result["counts"]["inserted"] == 1
    row = conn.execute("SELECT scope_id, target FROM memories").fetchone()
    assert tuple(row) == ("scope-local", "general")


def test_reviewed_maintenance_supersede_can_target_shared_fact():
    conn = _conn()
    store_row(
        conn,
        memory_id="memory-shared-old",
        scope_id="scope-shared",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="old",
        source="manual",
        target="user",
        content="Asha lives in Mumbai.",
        timestamp=AT,
    )
    insert_claim(
        conn,
        claim_id="claim-shared-old",
        memory_id="memory-shared-old",
        scope_id="scope-shared",
        subject="Asha",
        predicate="lives in",
        value="Mumbai",
        cardinality="single",
        assertion_kind="direct",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at=AT,
        confidence=0.9,
        source_type="user_message",
        source_ref="message-old",
    )
    conn.commit()

    result = execute_pipeline_proposal(
        conn,
        proposal=_proposal(
            EvolutionAction.SUPERSEDE,
            target_ids=("memory-shared-old",),
            scope_id="scope-local",
        ),
        lane="maintenance",
        run_id="run-shared-supersede",
        source_key="shared-supersede-source",
        trusted_scope_id="scope-shared",
        writable_scope_ids=["scope-local", "scope-shared"],
        actor="test",
        source="maintenance-tool",
        target="user",
        content="Asha now lives in Bangalore.",
        metadata={"memory_type": "factual"},
        runtime_config={
            "fact_evolution": {
                "enabled": True,
                "maintenance_mode": "reviewed_apply",
            }
        },
        dry_run=False,
    )

    assert result.applied is True
    assert result.action is EvolutionAction.SUPERSEDE
    assert conn.execute(
        "SELECT json_extract(metadata, '$.lifecycle') "
        "FROM memories WHERE id = 'memory-shared-old'"
    ).fetchone()[0] == "superseded"
    scopes = {
        row[0] for row in conn.execute("SELECT DISTINCT scope_id FROM fact_claims")
    }
    assert scopes == {"scope-shared"}


def test_nightly_durable_action_rejects_local_scope_target_id():
    conn = _conn()
    store_row(
        conn,
        memory_id="memory-local",
        scope_id="scope-local",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="local",
        source="manual",
        target="user",
        content="Asha lives in Mumbai.",
        timestamp=AT,
    )
    conn.commit()

    result = apply_candidates(
        conn,
        None,
        _split_scope(),
        run_id="run-reject-local-target",
        candidates=[
            _fact_candidate(
                EvolutionAction.SUPERSEDE,
                target_ids=("memory-local",),
                scope_id="scope-local",
            )
        ],
        dry_run=False,
        runtime_config=_enabled("reviewed_apply"),
    )

    assert result["counts"]["previewed"] == 1
    assert result["actions"][0]["evolution_action"] == "review"
    assert "target_not_allowed" in result["actions"][0]["reason_codes"]
    assert conn.execute(
        "SELECT content FROM memories WHERE id = 'memory-local'"
    ).fetchone()[0] == "Asha lives in Mumbai."


def test_nightly_high_risk_supersede_never_falls_back_to_legacy_text_merge():
    conn = _conn()
    store_row(
        conn,
        memory_id="memory-old",
        scope_id="scope-a",
        platform="telegram",
        user_id="joy",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="old",
        source="manual",
        target="user",
        content="Asha lives in Mumbai.",
        timestamp=AT,
    )
    insert_claim(
        conn,
        claim_id="claim-old",
        memory_id="memory-old",
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        value="Mumbai",
        cardinality="single",
        assertion_kind="direct",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at=AT,
        confidence=0.9,
        source_type="user_message",
        source_ref="message-old",
    )
    conn.commit()

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-supersede-auto",
        candidates=[_fact_candidate(EvolutionAction.SUPERSEDE, target_ids=("memory-old",))],
        dry_run=False,
        runtime_config=_enabled("auto_apply"),
    )

    assert result["counts"]["review"] == 1
    assert result["counts"].get("deleted", 0) == 0
    assert result["actions"][0]["action"] == "review"
    row = conn.execute("SELECT content, metadata FROM memories WHERE id = 'memory-old'").fetchone()
    assert row["content"] == "Asha lives in Mumbai."
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims WHERE status = 'current'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0


def test_nightly_fact_retry_replays_same_receipt_without_duplicate_truth_rows():
    conn = _conn()
    candidate = _fact_candidate()

    first = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-replay",
        candidates=[candidate],
        dry_run=False,
        runtime_config=_enabled(),
    )
    second = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-replay",
        candidates=[candidate],
        dry_run=False,
        runtime_config=_enabled(),
    )

    assert first["counts"]["inserted"] == 1
    assert first["counts"].get("deleted", 0) == 0
    assert second["counts"]["replayed"] == 1
    assert second["counts"].get("deleted", 0) == 0
    assert second["actions"][0]["status"] == "replayed"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 1


def test_nightly_non_fact_workflow_keeps_legacy_storage_path():
    conn = _conn()
    candidate = DigestCandidate(
        content=(
            "Reusable deployment workflow: validate the backup, run the release gate, "
            "then verify rollback evidence before cleanup."
        ),
        target="ops",
        memory_type="workflow",
        importance=0.85,
        confidence=0.9,
        verification=["release gate passed"],
        reason="reusable verified procedure",
    )

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-workflow",
        candidates=[candidate],
        dry_run=False,
        runtime_config=_enabled(),
    )

    assert result["counts"]["inserted"] == 1
    assert result["actions"][0]["action"] == "insert"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0


def test_nightly_dry_run_overrides_enabled_apply_mode():
    conn = _conn()

    result = apply_candidates(
        conn,
        None,
        _scope(),
        run_id="run-dry",
        candidates=[_fact_candidate()],
        dry_run=True,
        runtime_config=_enabled(),
    )

    assert result["counts"]["previewed"] == 1
    assert result["counts"].get("deleted", 0) == 0
    assert result["actions"][0]["action"] == "preview"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
