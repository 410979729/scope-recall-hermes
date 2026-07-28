"""Strict, fail-closed tests for the structured fact-action contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from scope_recall.fact_actions import (
    ClaimDraft,
    EvolutionAction,
    EvolutionPlan,
    EvolutionProposal,
    EvolutionResult,
    EvidenceReference,
    parse_evolution_proposal,
)


def _valid_payload(action: str = "supersede") -> dict:
    return {
        "action": action,
        "claim": {
            "subject": "Asha",
            "predicate": "lives in",
            "value": "Bangalore",
            "scope_id": "shared-user-scope",
            "cardinality": "single",
        },
        "target_ids": ["memory-old", "memory-old", "memory-peer"],
        "evidence": [
            {
                "source_type": "message",
                "source_id": "message-42",
                "quote": "I moved to Bangalore; Mumbai is no longer current.",
            },
            {
                "source_type": "message",
                "source_id": "message-42",
                "quote": "duplicate evidence must collapse",
            },
        ],
        "confidence": 1.7,
        "reason": "direct_user_correction",
        "existing_hint": "Asha lives in Mumbai.",
        "source": "nightly-digest",
    }


def test_action_enum_is_exact_and_closed():
    assert [item.value for item in EvolutionAction] == [
        "noop",
        "add",
        "enrich",
        "supersede",
        "retract",
        "review",
    ]


def test_valid_supersede_proposal_is_normalized_deduplicated_and_clamped():
    proposal = parse_evolution_proposal(_valid_payload())

    assert proposal.action is EvolutionAction.SUPERSEDE
    assert proposal.raw_action == "supersede"
    assert proposal.claim is not None
    assert proposal.claim.subject == "asha"
    assert proposal.claim.predicate == "lives in"
    assert proposal.claim.value == "bangalore"
    assert proposal.claim.scope_id == "shared-user-scope"
    assert proposal.claim.cardinality == "single"
    assert proposal.target_ids == ("memory-old", "memory-peer")
    assert len(proposal.evidence_refs) == 1
    assert proposal.confidence == 1.0
    assert proposal.parser_reasons == ()
    assert proposal.requires_review is False
    assert proposal.as_dict()["claim"]["fact_key"] == proposal.claim.fact_key


def test_speaker_subject_uses_only_the_runtime_binding_channel() -> None:
    payload = _valid_payload()
    payload["evidence"][0]["speaker_subject"] = "UnrelatedPerson"

    unbound = parse_evolution_proposal(payload)
    bound = parse_evolution_proposal(
        payload,
        trusted_speaker_subjects={"message-42": "Asha"},
    )

    assert unbound.evidence_refs[0].speaker_subject == ""
    assert bound.evidence_refs[0].speaker_subject == "Asha"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"claim": _valid_payload()["claim"]}, "missing_action"),
        ({**_valid_payload(), "action": "launch"}, "unknown_action"),
        ({**_valid_payload(), "action": "update"}, "ambiguous_legacy_update"),
    ],
)
def test_missing_unknown_and_legacy_update_actions_fail_closed_to_review(payload, reason):
    proposal = parse_evolution_proposal(payload)

    assert proposal.action is EvolutionAction.REVIEW
    assert reason in proposal.parser_reasons
    assert proposal.requires_review is True


def test_delete_only_normalizes_to_retract_with_complete_identity_and_target():
    proposal = parse_evolution_proposal(_valid_payload("delete"))

    assert proposal.action is EvolutionAction.RETRACT
    assert "normalized_delete_to_retract" in proposal.parser_reasons
    assert proposal.as_dict()["action"] != "delete"


def test_delete_without_target_fails_closed_to_review():
    payload = _valid_payload("delete")
    payload["target_ids"] = []

    proposal = parse_evolution_proposal(payload)

    assert proposal.action is EvolutionAction.REVIEW
    assert "retract_requires_target" in proposal.parser_reasons


def test_skip_normalizes_to_noop_without_requiring_a_claim():
    proposal = parse_evolution_proposal({"action": "skip", "reason": "covered"})

    assert proposal.action is EvolutionAction.NOOP
    assert proposal.claim is None
    assert "normalized_skip_to_noop" in proposal.parser_reasons
    assert proposal.requires_review is False


def test_secret_like_claim_fails_closed_without_echoing_secret():
    secret = "api_key=" + "s" + "k-" + ("A" * 24)
    payload = _valid_payload("add")
    payload["target_ids"] = []
    payload["claim"]["value"] = secret

    proposal = parse_evolution_proposal(payload)
    serialized = json.dumps(proposal.as_dict(), ensure_ascii=False)

    assert proposal.action is EvolutionAction.REVIEW
    assert proposal.claim is None
    assert "secret_like_claim" in proposal.parser_reasons
    assert secret not in serialized
    assert "A" * 24 not in serialized


def test_overlong_claim_and_target_overflow_fail_closed():
    payload = _valid_payload()
    payload["claim"]["subject"] = "s" * 201
    payload["target_ids"] = [f"memory-{index}" for index in range(40)]

    proposal = parse_evolution_proposal(payload)

    assert proposal.action is EvolutionAction.REVIEW
    assert proposal.claim is None
    assert len(proposal.target_ids) == 32
    assert "invalid_claim" in proposal.parser_reasons
    assert "target_ids_truncated" in proposal.parser_reasons


def test_fact_value_runtime_boundary_accepts_2000_and_rejects_2001():
    accepted_payload = _valid_payload("add")
    accepted_payload["target_ids"] = []
    accepted_payload["claim"]["value"] = "v" * 2000
    rejected_payload = _valid_payload("add")
    rejected_payload["target_ids"] = []
    rejected_payload["claim"]["value"] = "v" * 2001

    accepted = parse_evolution_proposal(accepted_payload)
    rejected = parse_evolution_proposal(rejected_payload)

    assert accepted.action is EvolutionAction.ADD
    assert accepted.claim is not None
    assert len(accepted.claim.value) == 2000
    assert rejected.action is EvolutionAction.REVIEW
    assert rejected.claim is None
    assert "invalid_claim" in rejected.parser_reasons


def test_evidence_overflow_is_bounded_and_fails_closed():
    payload = _valid_payload("add")
    payload["target_ids"] = []
    payload["evidence"] = [
        {
            "source_type": "message",
            "source_id": f"message-{index}",
            "quote": "bounded evidence",
        }
        for index in range(40)
    ]

    proposal = parse_evolution_proposal(payload)

    assert proposal.action is EvolutionAction.REVIEW
    assert len(proposal.evidence_refs) == 32
    assert "evidence_truncated" in proposal.parser_reasons


def test_bad_json_and_non_object_payloads_return_review_instead_of_raising():
    malformed = parse_evolution_proposal("{not-json")
    non_object = parse_evolution_proposal(["supersede"])

    assert malformed.action is EvolutionAction.REVIEW
    assert malformed.parser_reasons == ("invalid_json",)
    assert non_object.action is EvolutionAction.REVIEW
    assert non_object.parser_reasons == ("proposal_not_object",)


def test_multiple_cardinality_alias_normalizes_to_multi():
    payload = _valid_payload("add")
    payload["target_ids"] = []
    payload["claim"]["cardinality"] = "multiple"

    proposal = parse_evolution_proposal(payload)

    assert proposal.action is EvolutionAction.ADD
    assert proposal.claim is not None
    assert proposal.claim.cardinality == "multi"


def test_action_contract_dataclasses_are_frozen_and_serialize_stably():
    claim = ClaimDraft.from_parts(
        subject="Asha",
        predicate="lives in",
        value="Bangalore",
        scope_id="shared-user-scope",
    )
    evidence = EvidenceReference("message", "message-42", "Direct correction")
    proposal = EvolutionProposal(
        action=EvolutionAction.ADD,
        raw_action="add",
        claim=claim,
        target_ids=(),
        evidence_refs=(evidence,),
        confidence=0.8,
        reason="new_fact",
    )
    plan = EvolutionPlan(
        proposal=proposal,
        action_id="action-1",
        idempotency_key="idem-1",
        policy_mode="preview",
    )
    result = EvolutionResult(
        action_id="action-1",
        action=EvolutionAction.ADD,
        status="preview",
        applied=False,
        receipt={"fact_key": claim.fact_key},
    )

    assert plan.as_dict()["proposal"]["action"] == "add"
    assert result.as_dict()["applied"] is False
    with pytest.raises(FrozenInstanceError):
        proposal.confidence = 0.1  # type: ignore[misc]
