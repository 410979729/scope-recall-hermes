"""Deterministic evidence gates for structured factual evolution.

A model may propose an action, but this module alone decides whether the
proposal is executable. It is pure: no model calls, database access, or writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection

from .capture_filters import contains_secret_like_text
from .fact_actions import EvolutionAction, EvolutionProposal
from .fact_evidence import (
    CORROBORATING_EVIDENCE_SOURCE_TYPES,
    DIRECT_EVIDENCE_SOURCE_TYPES,
    INFERRED_EVIDENCE_SOURCE_TYPES,
    TRUSTED_EVIDENCE_SOURCE_TYPES,
    evidence_supports_claim,
    evidence_supports_retraction,
)


_DIRECT_SOURCE_TYPES = DIRECT_EVIDENCE_SOURCE_TYPES
_CORROBORATING_SOURCE_TYPES = CORROBORATING_EVIDENCE_SOURCE_TYPES
_INFERRED_SOURCE_TYPES = INFERRED_EVIDENCE_SOURCE_TYPES
_TRUSTED_SOURCE_TYPES = TRUSTED_EVIDENCE_SOURCE_TYPES
_ACTION_RISK = {
    EvolutionAction.NOOP: "none",
    EvolutionAction.ADD: "low",
    EvolutionAction.ENRICH: "low",
    EvolutionAction.SUPERSEDE: "high",
    EvolutionAction.RETRACT: "high",
    EvolutionAction.REVIEW: "review",
}
_EXECUTION_SEMANTICS = {
    EvolutionAction.NOOP: "no_op",
    EvolutionAction.ADD: "insert_claim",
    EvolutionAction.ENRICH: "link_evidence",
    EvolutionAction.SUPERSEDE: "close_then_insert",
    EvolutionAction.RETRACT: "close_interval",
    EvolutionAction.REVIEW: "human_review",
}
_REQUIRES_CLAIM = frozenset(
    {EvolutionAction.ADD, EvolutionAction.ENRICH, EvolutionAction.SUPERSEDE}
)
_REQUIRES_TARGET = frozenset(
    {EvolutionAction.ENRICH, EvolutionAction.SUPERSEDE, EvolutionAction.RETRACT}
)
_CONFIDENCE_FLOOR = {
    EvolutionAction.ADD: 0.65,
    EvolutionAction.ENRICH: 0.65,
    EvolutionAction.SUPERSEDE: 0.80,
    EvolutionAction.RETRACT: 0.80,
}


@dataclass(frozen=True, slots=True)
class EvolutionPolicyDecision:
    """Stable, serialization-safe result of the deterministic policy matrix."""

    requested_action: EvolutionAction
    effective_action: EvolutionAction
    allowed: bool
    risk_tier: str
    execution_semantics: str
    reason_codes: tuple[str, ...]
    evidence_count: int
    independent_source_count: int
    direct_source_count: int
    corroborating_source_count: int
    confidence_floor: float

    @property
    def requires_human_review(self) -> bool:
        return self.effective_action is EvolutionAction.REVIEW

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_action": self.requested_action.value,
            "effective_action": self.effective_action.value,
            "allowed": self.allowed,
            "risk_tier": self.risk_tier,
            "execution_semantics": self.execution_semantics,
            "reason_codes": list(self.reason_codes),
            "evidence_count": self.evidence_count,
            "independent_source_count": self.independent_source_count,
            "direct_source_count": self.direct_source_count,
            "corroborating_source_count": self.corroborating_source_count,
            "confidence_floor": self.confidence_floor,
            "requires_human_review": self.requires_human_review,
        }


def _dedupe_reasons(reasons: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))[:16]


def _decision(
    proposal: EvolutionProposal,
    *,
    allowed: bool,
    reasons: list[str],
    independent_sources: set[str],
    direct_sources: set[str],
    corroborating_sources: set[str],
) -> EvolutionPolicyDecision:
    effective_action = proposal.action if allowed else EvolutionAction.REVIEW
    semantics_action = proposal.action if allowed else EvolutionAction.REVIEW
    return EvolutionPolicyDecision(
        requested_action=proposal.action,
        effective_action=effective_action,
        allowed=allowed,
        risk_tier=_ACTION_RISK[proposal.action],
        execution_semantics=_EXECUTION_SEMANTICS[semantics_action],
        reason_codes=_dedupe_reasons(reasons),
        evidence_count=len(proposal.evidence_refs),
        independent_source_count=len(independent_sources),
        direct_source_count=len(direct_sources),
        corroborating_source_count=len(corroborating_sources),
        confidence_floor=_CONFIDENCE_FLOOR.get(proposal.action, 0.0),
    )


def evaluate_evolution_policy(
    proposal: EvolutionProposal,
    *,
    allowed_target_ids: Collection[str] | None = None,
    runtime_evidence_authorized: bool = True,
) -> EvolutionPolicyDecision:
    """Apply a closed evidence matrix; any ambiguity becomes REVIEW."""

    if proposal.action is EvolutionAction.REVIEW or proposal.requires_review:
        return _decision(
            proposal,
            allowed=False,
            reasons=["proposal_requires_review"],
            independent_sources=set(),
            direct_sources=set(),
            corroborating_sources=set(),
        )
    if proposal.action is EvolutionAction.NOOP:
        return _decision(
            proposal,
            allowed=True,
            reasons=["noop_safe"],
            independent_sources=set(),
            direct_sources=set(),
            corroborating_sources=set(),
        )

    reasons: list[str] = []
    if proposal.action is EvolutionAction.RETRACT and proposal.claim is None:
        reasons.append("retract_target_claim_required")
    elif proposal.action in _REQUIRES_CLAIM and proposal.claim is None:
        reasons.append("claim_required")
    if proposal.action in _REQUIRES_TARGET and not proposal.target_ids:
        reasons.append("target_required")
    if allowed_target_ids is not None:
        allowed_targets = {str(item) for item in allowed_target_ids if str(item)}
        if any(target not in allowed_targets for target in proposal.target_ids):
            reasons.append("target_not_allowed")
    if not runtime_evidence_authorized and proposal.evidence_refs:
        reasons.append("runtime_evidence_unverified")

    independent_sources: set[str] = set()
    direct_sources: set[str] = set()
    corroborating_sources: set[str] = set()
    inferred_sources: set[str] = set()
    for evidence in proposal.evidence_refs:
        source_type = evidence.source_type.strip().lower()
        source_id = evidence.source_id.strip()
        quote = evidence.quote.strip()
        if not runtime_evidence_authorized:
            continue
        if source_type not in _TRUSTED_SOURCE_TYPES:
            reasons.append("untrusted_evidence_source")
            continue
        if not source_id:
            reasons.append("missing_evidence_source_id")
            continue
        if not quote:
            reasons.append("missing_evidence_quote")
            continue
        if contains_secret_like_text(quote):
            reasons.append("secret_like_evidence")
            continue
        if source_type in (_DIRECT_SOURCE_TYPES | _CORROBORATING_SOURCE_TYPES):
            if proposal.claim is None:
                if proposal.action is EvolutionAction.RETRACT:
                    continue
            elif proposal.action is EvolutionAction.RETRACT:
                if not evidence_supports_retraction(evidence, proposal.claim):
                    reasons.append("authoritative_evidence_not_retraction_supporting")
                    continue
            elif not evidence_supports_claim(evidence, proposal.claim):
                reasons.append("authoritative_evidence_not_claim_supporting")
                continue
        independent_sources.add(source_id)
        if source_type in _DIRECT_SOURCE_TYPES:
            direct_sources.add(source_id)
        elif source_type in _CORROBORATING_SOURCE_TYPES:
            corroborating_sources.add(source_id)
        else:
            inferred_sources.add(source_id)

    floor = _CONFIDENCE_FLOOR.get(proposal.action, 1.0)
    if proposal.confidence < floor:
        reasons.append("confidence_below_threshold")

    direct_grounded = bool(direct_sources)
    independently_corroborated = len(corroborating_sources) >= 2
    if inferred_sources and not direct_sources and not corroborating_sources:
        reasons.append("inferred_only_evidence")

    structural_or_safety_failure = any(
        reason
        in {
            "claim_required",
            "retract_target_claim_required",
            "target_required",
            "target_not_allowed",
            "runtime_evidence_unverified",
            "untrusted_evidence_source",
            "missing_evidence_source_id",
            "missing_evidence_quote",
            "secret_like_evidence",
            "authoritative_evidence_not_claim_supporting",
            "authoritative_evidence_not_retraction_supporting",
            "confidence_below_threshold",
        }
        for reason in reasons
    )
    if not direct_grounded and not independently_corroborated:
        reasons.append("insufficient_evidence")

    allowed = (
        not structural_or_safety_failure
        and (direct_grounded or independently_corroborated)
    )
    if allowed:
        reasons = [
            (
                "direct_correction_evidence"
                if proposal.action in {EvolutionAction.SUPERSEDE, EvolutionAction.RETRACT}
                else "direct_evidence"
            )
            if direct_grounded
            else "corroborated_independent_evidence"
        ]
    return _decision(
        proposal,
        allowed=allowed,
        reasons=reasons,
        independent_sources=independent_sources,
        direct_sources=direct_sources,
        corroborating_sources=corroborating_sources,
    )


__all__ = ["EvolutionPolicyDecision", "evaluate_evolution_policy"]
