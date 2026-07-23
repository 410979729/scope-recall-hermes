"""End-to-end preservation tests for the structured fact-action envelope."""

from __future__ import annotations

import json
import sqlite3

from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionProposal,
)
from scope_recall.fact_evolution import execute_pipeline_proposal
from scope_recall.fact_repository import get_claim, insert_claim
from scope_recall.evolution_policy import evaluate_evolution_policy
from scope_recall.journal_candidates import candidate_metadata as journal_candidate_metadata
from scope_recall.journal_extractors import _journal_from_digest_candidate
from scope_recall.models import RuntimeScope
from scope_recall.nightly_digest import (
    DigestCandidate,
    MessageRecord,
    ScopeProfile,
    SessionBundle,
    _parse_llm_candidates_with_status,
    apply_candidates,
    build_prompt,
    candidate_metadata as nightly_candidate_metadata,
)
from scope_recall.sql_store import ensure_schema


def _bundle() -> SessionBundle:
    return SessionBundle(
        id="evolution-pipeline",
        source="test",
        user_id="primary-user",
        messages=[
            MessageRecord(
                id=7,
                session_id="evolution-pipeline",
                role="user",
                content="Asha lives in Bangalore and corrected the previous Mumbai location.",
                timestamp=1.0,
            )
        ],
    )


def _structured_item(*, action: str = "supersede", target_ids: list[str] | None = None) -> dict:
    return {
        "action": action,
        "content": (
            "Asha now lives in Bangalore; this direct correction replaces the previous "
            "Mumbai location as the current durable fact."
        ),
        "claim": {
            "subject": "Asha",
            "predicate": "lives in",
            "value": "Bangalore",
            "scope_id": "model-invented-scope",
            "cardinality": "single",
        },
        "target_ids": target_ids if target_ids is not None else ["memory-old", "memory-old"],
        "evidence_message_ids": [7, 999],
        "target": "user",
        "memory_type": "factual",
        "importance": 0.9,
        "confidence": 0.95,
        "entities": ["Asha"],
        "tags": ["location"],
        "reason": "direct_user_correction",
        "existing_hint": "Asha lives in Mumbai.",
    }


def test_prompt_uses_closed_action_vocabulary_and_structured_claim_schema():
    prompt = build_prompt(
        _bundle(),
        "user: Asha lives in Bangalore.",
        ["[id=memory-old target=user] Asha lives in Mumbai."],
    )

    for action in ("NOOP", "ADD", "ENRICH", "SUPERSEDE", "RETRACT", "REVIEW"):
        assert action in prompt
    assert "claim" in prompt
    assert "subject" in prompt
    assert "predicate" in prompt
    assert "value" in prompt
    assert "target_ids" in prompt
    assert "evidence_message_ids" in prompt
    assert "action=update" not in prompt
    assert "hard delete" in prompt.lower()


def test_parser_preserves_action_claim_targets_and_trusted_evidence():
    candidates, status = _parse_llm_candidates_with_status(
        json.dumps([_structured_item()]),
        bundle=_bundle(),
        scope_id="trusted-scope",
        allowed_target_ids={"memory-old"},
    )

    assert status == "parsed"
    assert len(candidates) == 1
    proposal = candidates[0].evolution
    assert proposal is not None
    assert proposal.action is EvolutionAction.SUPERSEDE
    assert proposal.claim is not None
    assert proposal.claim.scope_id == "trusted-scope"
    assert proposal.target_ids == ("memory-old",)
    assert [item.source_id for item in proposal.evidence_refs] == ["7"]
    assert [item.source_type for item in proposal.evidence_refs] == ["user_message"]
    assert proposal.evidence_refs[0].quote == (
        "Asha lives in Bangalore and corrected the previous Mumbai location."
    )
    assert proposal.existing_hint == "Asha lives in Mumbai."
    assert proposal.parser_reasons == ()
    decision = evaluate_evolution_policy(
        proposal,
        allowed_target_ids={"memory-old"},
    )
    assert decision.allowed is True
    assert decision.effective_action is EvolutionAction.SUPERSEDE
    assert decision.reason_codes == ("direct_correction_evidence",)


def test_parser_routes_target_scope_and_applies_scope_specific_target_allowlist():
    durable_candidates, durable_status = _parse_llm_candidates_with_status(
        json.dumps([_structured_item()]),
        bundle=_bundle(),
        scope_id="scope-local",
        shared_scope_id="scope-shared",
        allowed_target_ids={"memory-old", "memory-local"},
        allowed_target_ids_by_scope={
            "scope-local": {"memory-local"},
            "scope-shared": {"memory-old"},
        },
    )
    assert durable_status == "parsed"
    durable = durable_candidates[0].evolution
    assert durable is not None and durable.claim is not None
    assert durable.claim.scope_id == "scope-shared"
    assert durable.target_ids == ("memory-old",)

    wrong_scope_item = _structured_item(target_ids=["memory-local"])
    wrong_scope_candidates, _ = _parse_llm_candidates_with_status(
        json.dumps([wrong_scope_item]),
        bundle=_bundle(),
        scope_id="scope-local",
        shared_scope_id="scope-shared",
        allowed_target_ids={"memory-old", "memory-local"},
        allowed_target_ids_by_scope={
            "scope-local": {"memory-local"},
            "scope-shared": {"memory-old"},
        },
    )
    wrong_scope = wrong_scope_candidates[0].evolution
    assert wrong_scope is not None
    assert wrong_scope.action is EvolutionAction.REVIEW
    assert "target_id_not_allowed" in wrong_scope.parser_reasons

    general_item = _structured_item(action="add", target_ids=[])
    general_item["target"] = "general"
    general_candidates, general_status = _parse_llm_candidates_with_status(
        json.dumps([general_item]),
        bundle=_bundle(),
        scope_id="scope-local",
        shared_scope_id="scope-shared",
        allowed_target_ids_by_scope={
            "scope-local": set(),
            "scope-shared": set(),
        },
    )
    assert general_status == "parsed"
    general = general_candidates[0].evolution
    assert general is not None and general.claim is not None
    assert general.claim.scope_id == "scope-local"


def test_assistant_only_evidence_remains_inference_and_requires_review():
    bundle = SessionBundle(
        id="assistant-only",
        source="test",
        user_id="primary-user",
        messages=[
            MessageRecord(
                id=8,
                session_id="assistant-only",
                role="assistant",
                content="Asha now lives in Bangalore.",
                timestamp=1.0,
            )
        ],
    )
    item = _structured_item(action="add", target_ids=[])
    item["evidence_message_ids"] = [8]

    candidates, status = _parse_llm_candidates_with_status(
        json.dumps([item]),
        bundle=bundle,
        scope_id="trusted-scope",
        allowed_target_ids=set(),
    )

    assert status == "parsed"
    proposal = candidates[0].evolution
    assert proposal is not None
    assert [evidence.source_type for evidence in proposal.evidence_refs] == [
        "model_inference"
    ]
    decision = evaluate_evolution_policy(proposal, allowed_target_ids=set())
    assert decision.allowed is False
    assert decision.effective_action is EvolutionAction.REVIEW
    assert "inferred_only_evidence" in decision.reason_codes
    assert "insufficient_evidence" in decision.reason_codes


def test_missing_empty_or_forged_message_evidence_cannot_gain_direct_authority():
    for evidence_ids, message_content in (([999], "Direct factual statement."), ([7], ""), (None, "Direct factual statement.")):
        bundle = SessionBundle(
            id="invalid-evidence",
            source="test",
            user_id="primary-user",
            messages=[
                MessageRecord(
                    id=7,
                    session_id="invalid-evidence",
                    role="user",
                    content=message_content,
                    timestamp=1.0,
                )
            ],
        )
        item = _structured_item(action="add", target_ids=[])
        if evidence_ids is None:
            item.pop("evidence_message_ids", None)
        else:
            item["evidence_message_ids"] = evidence_ids
        candidates, status = _parse_llm_candidates_with_status(
            json.dumps([item]),
            bundle=bundle,
            scope_id="trusted-scope",
            allowed_target_ids=set(),
        )
        assert status == "filtered"
        assert candidates == []


def test_parser_fails_closed_when_model_targets_memory_outside_allowed_set():
    candidates, status = _parse_llm_candidates_with_status(
        json.dumps([_structured_item(target_ids=["memory-outside"])]),
        bundle=_bundle(),
        scope_id="trusted-scope",
        allowed_target_ids={"memory-old"},
    )

    assert status == "parsed"
    proposal = candidates[0].evolution
    assert proposal is not None
    assert proposal.action is EvolutionAction.REVIEW
    assert proposal.target_ids == ()
    assert "target_id_not_allowed" in proposal.parser_reasons


def test_nightly_to_journal_conversion_and_metadata_keep_same_proposal():
    candidates, status = _parse_llm_candidates_with_status(
        json.dumps([_structured_item()]),
        bundle=_bundle(),
        scope_id="trusted-scope",
        allowed_target_ids={"memory-old"},
    )
    assert status == "parsed"
    nightly_candidate = candidates[0]

    journal_candidate = _journal_from_digest_candidate(nightly_candidate)
    nightly_metadata = nightly_candidate_metadata(nightly_candidate, "run-1")
    journal_metadata = journal_candidate_metadata(journal_candidate, "run-1")

    assert journal_candidate.evolution == nightly_candidate.evolution
    assert nightly_metadata["fact_evolution"]["action"] == "supersede"
    assert journal_metadata["fact_evolution"]["action"] == "supersede"
    assert journal_metadata["fact_evolution"]["target_ids"] == ["memory-old"]


def test_real_json_user_add_passes_pollution_policy_and_executor_chain():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    scope = RuntimeScope(
        platform="test",
        user_id="primary-user",
        chat_id="chat",
        thread_id="",
        agent_identity="primary-agent",
        agent_workspace="tests",
    )
    profile = ScopeProfile(
        scope=scope,
        scope_id="trusted-scope",
        shared_scope_id="trusted-shared-scope",
        accessible_scope_ids=["trusted-scope", "trusted-shared-scope"],
        writable_scope_ids=["trusted-scope", "trusted-shared-scope"],
    )
    item = _structured_item(action="add", target_ids=[])
    candidates, status = _parse_llm_candidates_with_status(
        json.dumps([item]),
        bundle=_bundle(),
        scope_id="trusted-scope",
        allowed_target_ids=set(),
    )
    assert status == "parsed"

    result = apply_candidates(
        conn,
        None,
        profile,
        run_id="real-json-add",
        candidates=candidates,
        dry_run=False,
        runtime_config={
            "fact_evolution": {"enabled": True, "nightly_mode": "auto_apply"}
        },
    )

    assert result["counts"] == {"inserted": 1, "deleted": 0}
    assert result["pollution_counts"] == {}
    assert result["actions"][0]["action"] == "evolve"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 1
    evidence = conn.execute(
        "SELECT source_type, source_ref, excerpt FROM fact_claim_evidence"
    ).fetchall()
    assert any(
        row["source_type"] == "user_message"
        and row["source_ref"] == "7"
        and "lives in Bangalore" in row["excerpt"]
        for row in evidence
    )


def test_fact_apply_previews_structured_review_instead_of_text_merging():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    scope = RuntimeScope(
        platform="test",
        user_id="primary-user",
        chat_id="chat",
        thread_id="",
        agent_identity="primary-agent",
        agent_workspace="tests",
    )
    profile = ScopeProfile(
        scope=scope,
        scope_id="trusted-scope",
        shared_scope_id="trusted-shared-scope",
        accessible_scope_ids=["trusted-scope", "trusted-shared-scope"],
        writable_scope_ids=["trusted-scope", "trusted-shared-scope"],
    )
    candidates, status = _parse_llm_candidates_with_status(
        json.dumps([_structured_item(action="review")]),
        bundle=_bundle(),
        scope_id="trusted-scope",
        allowed_target_ids={"memory-old"},
    )
    assert status == "parsed"
    candidate: DigestCandidate = candidates[0]

    result = apply_candidates(
        conn,
        None,
        profile,
        run_id="run-1",
        candidates=[candidate],
        dry_run=True,
        runtime_config={"fact_evolution": {"enabled": True}},
    )

    assert result["counts"] == {"previewed": 1, "deleted": 0}
    assert len(result["actions"]) == 1
    assert result["actions"][0]["action"] == "preview"
    assert result["actions"][0]["evolution_action"] == "review"
    assert result["actions"][0]["status"] == "preview"
    assert result["actions"][0]["action_id"].startswith("fact_action_")
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_ambiguous_relation_evidence_is_review_and_zero_write_end_to_end():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=ClaimDraft.from_parts(
            subject="Asha",
            predicate="works at",
            value="OldCo",
            scope_id="trusted-scope",
        ),
        evidence_refs=(
            EvidenceReference(
                "user_message",
                "message-ambiguous-work",
                "Asha works on OldCo.",
                "Asha",
            ),
        ),
        confidence=0.99,
        reason="ambiguous preposition must not authorize a durable claim",
    )

    result = execute_pipeline_proposal(
        conn,
        proposal=proposal,
        lane="nightly",
        run_id="ambiguous-relation-zero-write",
        source_key="ambiguous-relation-zero-write",
        trusted_scope_id="trusted-scope",
        writable_scope_ids=("trusted-scope",),
        actor="scope-recall:test",
        source="nightly",
        target="user",
        content="Asha works at OldCo.",
        metadata={"memory_type": "factual"},
        runtime_config={
            "fact_evolution": {
                "enabled": True,
                "nightly_mode": "auto_apply",
            }
        },
        dry_run=False,
    )

    assert result.status == "review"
    assert result.applied is False
    assert "authoritative_evidence_not_claim_supporting" in result.receipt[
        "reason_codes"
    ]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_action_receipts").fetchone()[0] == 0


def test_maintenance_auto_apply_in_legacy_config_still_fails_closed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        claim=ClaimDraft.from_parts(
            subject="Asha",
            predicate="lives in",
            value="Pune",
            scope_id="scope-a",
        ),
        evidence_refs=(
            EvidenceReference(
                "user_message",
                "caller-controlled",
                "Asha lives in Pune.",
                "Asha",
            ),
        ),
        confidence=0.99,
        reason="legacy maintenance auto mode must not authorize caller evidence",
    )

    result = execute_pipeline_proposal(
        conn,
        proposal=proposal,
        lane="maintenance",
        run_id="maintenance-auto-blocked",
        source_key="maintenance-auto-blocked",
        trusted_scope_id="scope-a",
        writable_scope_ids=("scope-a",),
        actor="operator",
        source="maintenance-tool",
        target="user",
        content="Asha lives in Pune.",
        metadata={"memory_type": "factual"},
        runtime_config={
            "fact_evolution": {
                "enabled": True,
                "maintenance_mode": "auto_apply",
            }
        },
        dry_run=False,
    )

    assert result.status == "review"
    assert result.applied is False
    assert "runtime_evidence_unverified" in result.receipt["reason_codes"]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0


def test_runtime_binds_retract_target_claim_and_rejects_unrelated_evidence():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    updated_at = "2026-07-01T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (
            'memory-oldco', 'trusted-scope', 'test', 'user',
            'Asha works at OldCo', 'Asha works at OldCo', ?, ?,
            '{"lifecycle":"active","memory_type":"fact"}'
        )
        """,
        (updated_at, updated_at),
    )
    conn.execute(
        "INSERT INTO memories_fts(memory_id, content, summary) "
        "VALUES ('memory-oldco', 'Asha works at OldCo', 'Asha works at OldCo')"
    )
    insert_claim(
        conn,
        claim_id="claim-oldco",
        memory_id="memory-oldco",
        scope_id="trusted-scope",
        subject="Asha",
        predicate="works at",
        value="OldCo",
        valid_from="2025-01-01T00:00:00+00:00",
        recorded_at=updated_at,
        confidence=0.95,
        source_type="user_message",
        source_ref="message-oldco",
    )
    conn.commit()
    counts_before = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("memories", "fact_claims", "fact_action_receipts")
    }

    unrelated = EvolutionProposal(
        action=EvolutionAction.RETRACT,
        raw_action="retract",
        claim=ClaimDraft.from_parts(
            subject="Mallory",
            predicate="likes",
            value="Verbose reports",
            scope_id="model-scope",
        ),
        target_ids=("memory-oldco",),
        evidence_refs=(
            EvidenceReference(
                "user_message",
                "message-unrelated",
                "Please remember that I prefer concise weekly reports.",
            ),
        ),
        confidence=0.99,
        reason="unrelated caller correction",
    )
    denied = execute_pipeline_proposal(
        conn,
        proposal=unrelated,
        lane="maintenance",
        run_id="retract-unrelated",
        source_key="retract-unrelated",
        trusted_scope_id="trusted-scope",
        writable_scope_ids=["trusted-scope"],
        actor="scope-recall:test",
        source="maintenance",
        target="user",
        content="Asha works at OldCo",
        metadata={"memory_type": "fact"},
        runtime_config={
            "fact_evolution": {
                "enabled": True,
                "maintenance_mode": "reviewed_apply",
            }
        },
        dry_run=False,
    )

    assert denied.applied is False
    assert denied.status == "review"
    assert "authoritative_evidence_not_retraction_supporting" in denied.receipt[
        "reason_codes"
    ]
    assert {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in counts_before
    } == counts_before
    assert get_claim(conn, "claim-oldco", scope_ids=["trusted-scope"]).status == "current"  # type: ignore[union-attr]

    explicit = EvolutionProposal(
        action=EvolutionAction.RETRACT,
        raw_action="retract",
        claim=None,
        target_ids=("memory-oldco",),
        evidence_refs=(
            EvidenceReference(
                "user_message",
                "message-explicit-retract",
                "Asha no longer works at OldCo.",
            ),
        ),
        confidence=0.99,
        reason="explicit correction",
    )
    applied = execute_pipeline_proposal(
        conn,
        proposal=explicit,
        lane="maintenance",
        run_id="retract-explicit",
        source_key="retract-explicit",
        trusted_scope_id="trusted-scope",
        writable_scope_ids=["trusted-scope"],
        actor="scope-recall:test",
        source="maintenance",
        target="user",
        content="Asha works at OldCo",
        metadata={"memory_type": "fact"},
        runtime_config={
            "fact_evolution": {
                "enabled": True,
                "maintenance_mode": "reviewed_apply",
            }
        },
        dry_run=False,
    )

    assert applied.applied is True
    assert get_claim(conn, "claim-oldco", scope_ids=["trusted-scope"]).status == "retracted"  # type: ignore[union-attr]
    conn.close()
