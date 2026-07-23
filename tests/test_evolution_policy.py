"""Deterministic evidence gates for factual evolution actions."""

from __future__ import annotations

import pytest

from scope_recall.evolution_policy import evaluate_evolution_policy
from scope_recall.fact_actions import (
    ClaimDraft,
    EvidenceReference,
    EvolutionAction,
    EvolutionProposal,
)


def _claim() -> ClaimDraft:
    return ClaimDraft.from_parts(
        subject="Asha",
        predicate="lives in",
        value="Bangalore",
        scope_id="scope-a",
    )


def _proposal(
    action: EvolutionAction,
    *,
    evidence: tuple[EvidenceReference, ...] = (),
    confidence: float = 0.9,
    targets: tuple[str, ...] = (),
    claim: ClaimDraft | None = None,
) -> EvolutionProposal:
    if claim is None and action in {
        EvolutionAction.ADD,
        EvolutionAction.ENRICH,
        EvolutionAction.SUPERSEDE,
    }:
        claim = _claim()
    return EvolutionProposal(
        action=action,
        raw_action=action.value,
        claim=claim,
        target_ids=targets,
        evidence_refs=evidence,
        confidence=confidence,
        reason="test-policy",
    )


def _direct(source_id: str = "message-1") -> EvidenceReference:
    return EvidenceReference(
        source_type="user_message",
        source_id=source_id,
        quote="I live in Bangalore; please correct the old city.",
        speaker_subject="Asha",
    )


def _retract_direct(source_id: str = "message-retract") -> EvidenceReference:
    return EvidenceReference(
        source_type="user_message",
        source_id=source_id,
        quote="I no longer live in Bangalore; retract the old city.",
        speaker_subject="Asha",
    )


def _document(source_id: str) -> EvidenceReference:
    return EvidenceReference(
        source_type="document",
        source_id=source_id,
        quote="Asha's verified profile states that Asha lives in Bangalore.",
    )


def test_noop_is_safe_without_evidence():
    decision = evaluate_evolution_policy(_proposal(EvolutionAction.NOOP))

    assert decision.allowed is True
    assert decision.effective_action is EvolutionAction.NOOP
    assert decision.risk_tier == "none"
    assert decision.reason_codes == ("noop_safe",)


def test_add_requires_grounding_and_accepts_one_direct_user_quote():
    denied = evaluate_evolution_policy(_proposal(EvolutionAction.ADD))
    allowed = evaluate_evolution_policy(
        _proposal(EvolutionAction.ADD, evidence=(_direct(),), confidence=0.75)
    )

    assert denied.allowed is False
    assert denied.effective_action is EvolutionAction.REVIEW
    assert "insufficient_evidence" in denied.reason_codes
    assert allowed.allowed is True
    assert allowed.effective_action is EvolutionAction.ADD
    assert allowed.direct_source_count == 1


def test_negated_direct_quote_does_not_support_positive_claim():
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ADD,
            evidence=(
                EvidenceReference(
                    source_type="user_message",
                    source_id="message-negated",
                    quote="I do not live in Bangalore.",
                ),
            ),
            confidence=0.99,
        )
    )

    assert decision.allowed is False
    assert "authoritative_evidence_not_claim_supporting" in decision.reason_codes


@pytest.mark.parametrize(
    ("claim", "quote"),
    [
        (_claim(), "I don't live in Bangalore."),
        (_claim(), "I haven't moved to Bangalore."),
        (
            ClaimDraft.from_parts(
                subject="Asha",
                predicate="住在",
                value="北京",
                scope_id="scope-a",
            ),
            "我不住在北京。",
        ),
        (
            ClaimDraft.from_parts(
                subject="Asha",
                predicate="住在",
                value="北京",
                scope_id="scope-a",
            ),
            "我没住在北京。",
        ),
    ],
)
def test_direct_quote_polarity_fail_closed_for_contractions_and_cjk_negation(
    claim: ClaimDraft,
    quote: str,
) -> None:
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ADD,
            claim=claim,
            evidence=(EvidenceReference("user_message", "message-negated", quote),),
            confidence=0.99,
        )
    )

    assert decision.allowed is False
    assert "authoritative_evidence_not_claim_supporting" in decision.reason_codes


def test_first_person_quote_does_not_authorize_an_unbound_claim_subject() -> None:
    unrelated_claim = ClaimDraft.from_parts(
        subject="UnrelatedPerson",
        predicate="lives in",
        value="Bangalore",
        scope_id="scope-a",
    )
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ADD,
            claim=unrelated_claim,
            evidence=(
                EvidenceReference(
                    "user_message",
                    "message-actual-speaker",
                    "I live in Bangalore.",
                ),
            ),
            confidence=0.99,
        )
    )

    assert decision.allowed is False
    assert "authoritative_evidence_not_claim_supporting" in decision.reason_codes


@pytest.mark.parametrize(
    ("claim", "quote"),
    [
        (
            ClaimDraft.from_parts(
                subject="Bob",
                predicate="manages",
                value="Alice",
                scope_id="scope-a",
            ),
            "Alice manages Bob.",
        ),
        (
            ClaimDraft.from_parts(
                subject="Al",
                predicate="lives in",
                value="Paris",
                scope_id="scope-a",
            ),
            "Alice lives in Paris.",
        ),
        (
            ClaimDraft.from_parts(
                subject="Ann",
                predicate="works with",
                value="Joy",
                scope_id="scope-a",
            ),
            "Joanne works with Joy.",
        ),
    ],
)
def test_claim_argument_order_and_entity_boundaries_fail_closed(
    claim: ClaimDraft,
    quote: str,
) -> None:
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ADD,
            claim=claim,
            evidence=(EvidenceReference("user_message", "message-swapped", quote),),
            confidence=0.99,
        )
    )

    assert decision.allowed is False
    assert "authoritative_evidence_not_claim_supporting" in decision.reason_codes


@pytest.mark.parametrize(
    ("claim", "quote"),
    [
        (
            ClaimDraft.from_parts(
                subject="Asha",
                predicate="works at",
                value="OldCo",
                scope_id="scope-a",
            ),
            "Asha works on OldCo.",
        ),
        (
            ClaimDraft.from_parts(
                subject="Alice",
                predicate="prefers",
                value="tea",
                scope_id="scope-a",
            ),
            "Alice wants tea removed.",
        ),
        (
            ClaimDraft.from_parts(
                subject="Alice",
                predicate="lives in",
                value="Paris",
                scope_id="scope-a",
            ),
            "Alice moved Paris to the archive.",
        ),
        (
            ClaimDraft.from_parts(
                subject="Asha",
                predicate="works at",
                value="OldCo",
                scope_id="scope-a",
            ),
            "Asha left OldCo's mailing list.",
        ),
        (
            ClaimDraft.from_parts(
                subject="Asha",
                predicate="works at",
                value="OldCo",
                scope_id="scope-a",
            ),
            "Asha quit OldCo's newsletter.",
        ),
        (
            ClaimDraft.from_parts(
                subject="玉",
                predicate="居住在",
                value="上海",
                scope_id="scope-a",
            ),
            "玉衡居住在上海。",
        ),
        (
            ClaimDraft.from_parts(
                subject="张三",
                predicate="居住在",
                value="海",
                scope_id="scope-a",
            ),
            "张三居住在上海。",
        ),
    ],
)
def test_ambiguous_predicate_frames_and_cjk_substrings_fail_closed(
    claim: ClaimDraft,
    quote: str,
) -> None:
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ADD,
            claim=claim,
            evidence=(
                EvidenceReference("user_message", "message-frame-drift", quote),
            ),
            confidence=0.99,
        )
    )

    assert decision.allowed is False
    assert decision.effective_action is EvolutionAction.REVIEW
    assert "authoritative_evidence_not_claim_supporting" in decision.reason_codes


def test_unrelated_direct_quote_cannot_launder_inferred_claim_support():
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ADD,
            evidence=(
                EvidenceReference(
                    source_type="user_message",
                    source_id="message-preference",
                    quote="Please remember that I prefer concise weekly reports.",
                ),
                EvidenceReference(
                    source_type="model_inference",
                    source_id="assistant-guess",
                    quote=(
                        "Asha may live in Bangalore, but this was not stated by the user."
                    ),
                ),
            ),
            confidence=0.99,
        )
    )

    assert decision.allowed is False
    assert decision.effective_action is EvolutionAction.REVIEW
    assert "authoritative_evidence_not_claim_supporting" in decision.reason_codes
    assert decision.direct_source_count == 0


def test_each_corroborating_quote_must_support_the_claim_itself():
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.SUPERSEDE,
            evidence=(
                _document("doc-supporting"),
                EvidenceReference(
                    source_type="tool_result",
                    source_id="tool-unrelated",
                    quote="The verified profile was refreshed successfully.",
                ),
            ),
            targets=("memory-old",),
            confidence=0.95,
        ),
        allowed_target_ids={"memory-old"},
    )

    assert decision.allowed is False
    assert decision.effective_action is EvolutionAction.REVIEW
    assert "authoritative_evidence_not_claim_supporting" in decision.reason_codes
    assert decision.corroborating_source_count == 1


def test_supersede_accepts_direct_correction_with_exact_target():
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.SUPERSEDE,
            evidence=(_direct(),),
            targets=("memory-old",),
            confidence=0.85,
        ),
        allowed_target_ids={"memory-old"},
    )

    assert decision.allowed is True
    assert decision.effective_action is EvolutionAction.SUPERSEDE
    assert decision.risk_tier == "high"
    assert decision.reason_codes == ("direct_correction_evidence",)


def test_supersede_can_use_two_independent_corroborating_sources():
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.SUPERSEDE,
            evidence=(_document("doc-a"), _document("doc-b")),
            targets=("memory-old",),
            confidence=0.9,
        ),
        allowed_target_ids={"memory-old"},
    )

    assert decision.allowed is True
    assert decision.independent_source_count == 2
    assert decision.reason_codes == ("corroborated_independent_evidence",)


def test_duplicate_source_ids_do_not_count_as_independent_corroboration():
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.SUPERSEDE,
            evidence=(
                _document("same-source"),
                EvidenceReference(
                    "tool_result",
                    "same-source",
                    "The same source was wrapped by a second adapter.",
                ),
            ),
            targets=("memory-old",),
            confidence=0.95,
        ),
        allowed_target_ids={"memory-old"},
    )

    assert decision.allowed is False
    assert decision.independent_source_count == 1
    assert decision.effective_action is EvolutionAction.REVIEW


def test_inferred_only_or_unknown_sources_fail_closed():
    inferred = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.SUPERSEDE,
            evidence=(
                EvidenceReference(
                    "model_inference",
                    "inference-1",
                    "The model inferred a different city.",
                ),
            ),
            targets=("memory-old",),
            confidence=0.99,
        ),
        allowed_target_ids={"memory-old"},
    )
    unknown = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ADD,
            evidence=(EvidenceReference("unknown_feed", "feed-1", "Claimed city"),),
            confidence=0.99,
        )
    )

    assert inferred.allowed is False
    assert "inferred_only_evidence" in inferred.reason_codes
    assert unknown.allowed is False
    assert "untrusted_evidence_source" in unknown.reason_codes


def test_target_scope_is_rechecked_at_policy_boundary():
    missing = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.RETRACT,
            evidence=(_direct(),),
            targets=(),
            claim=None,
        )
    )
    out_of_scope = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ENRICH,
            evidence=(_direct(),),
            targets=("memory-other",),
        ),
        allowed_target_ids={"memory-current"},
    )

    assert missing.allowed is False
    assert "target_required" in missing.reason_codes
    assert out_of_scope.allowed is False
    assert "target_not_allowed" in out_of_scope.reason_codes


def test_retract_uses_same_high_risk_evidence_gate_and_never_hard_deletes():
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.RETRACT,
            evidence=(_retract_direct(),),
            targets=("memory-old",),
            claim=_claim(),
            confidence=0.85,
        ),
        allowed_target_ids={"memory-old"},
    )

    assert decision.allowed is True
    assert decision.effective_action is EvolutionAction.RETRACT
    assert decision.execution_semantics == "close_interval"


def test_retract_without_target_claim_binding_rejects_unrelated_direct_quote():
    decision = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.RETRACT,
            evidence=(
                EvidenceReference(
                    "user_message",
                    "message-unrelated-retract",
                    "Please remember that I prefer concise weekly reports.",
                ),
            ),
            targets=("memory-old",),
            claim=None,
            confidence=0.99,
        ),
        allowed_target_ids={"memory-old"},
    )

    assert decision.allowed is False
    assert decision.effective_action is EvolutionAction.REVIEW
    assert "retract_target_claim_required" in decision.reason_codes


def test_low_confidence_and_secret_like_evidence_fail_closed_with_bounded_reasons():
    low = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ADD,
            evidence=(_direct(),),
            confidence=0.2,
        )
    )
    secret = "api_key=" + "s" + "k-" + ("A" * 24)
    unsafe = evaluate_evolution_policy(
        _proposal(
            EvolutionAction.ADD,
            evidence=(EvidenceReference("user_message", "message-1", secret),),
            confidence=0.99,
        )
    )

    assert low.allowed is False
    assert "confidence_below_threshold" in low.reason_codes
    assert unsafe.allowed is False
    assert "secret_like_evidence" in unsafe.reason_codes
    assert secret not in str(unsafe.as_dict())


def test_existing_review_proposal_cannot_be_upgraded_by_policy():
    proposal = EvolutionProposal(
        action=EvolutionAction.REVIEW,
        raw_action="update",
        claim=None,
        parser_reasons=("ambiguous_legacy_update",),
    )

    decision = evaluate_evolution_policy(proposal)

    assert decision.allowed is False
    assert decision.effective_action is EvolutionAction.REVIEW
    assert decision.reason_codes == ("proposal_requires_review",)
