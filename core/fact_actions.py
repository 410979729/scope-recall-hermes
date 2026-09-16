"""Fail-closed contracts for structured fact-evolution proposals.

This module is intentionally pure: it parses, normalizes, validates, and
serializes proposals but never opens a database, calls a model, or applies an
action. Unknown or ambiguous inputs become REVIEW proposals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
from typing import Any, Collection, Mapping
import unicodedata

from .capture_filters import (
    contains_secret_like_text,
    redact_secret_like_text,
    sanitize_report_text,
)
from .fact_identity import (
    MAX_FACT_PREDICATE_CHARS,
    MAX_FACT_SUBJECT_CHARS,
    MAX_FACT_VALUE_CHARS,
    FactIdentityError,
    build_fact_identity,
)


MAX_TARGET_IDS = 32
MAX_EVIDENCE_REFS = 32
MAX_TARGET_ID_CHARS = 160
MAX_SCOPE_ID_CHARS = 240
MAX_REASON_CHARS = 500
MAX_HINT_CHARS = 1000
MAX_EVIDENCE_QUOTE_CHARS = 800
_CARDINALITY_ALIASES = {
    "single": "single",
    "multi": "multi",
    "multiple": "multi",
    "many": "multi",
}


class EvolutionAction(str, Enum):
    """Closed public action vocabulary for factual evolution."""

    NOOP = "noop"
    ADD = "add"
    ENRICH = "enrich"
    SUPERSEDE = "supersede"
    RETRACT = "retract"
    REVIEW = "review"


FactActionKind = EvolutionAction


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A bounded reference to evidence already present in an allowed source."""

    source_type: str
    source_id: str
    quote: str = ""
    speaker_subject: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "quote": self.quote,
            "speaker_subject": self.speaker_subject,
        }


@dataclass(frozen=True, slots=True)
class ClaimDraft:
    """Validated structured factual assertion proposed for one scope."""

    subject: str
    predicate: str
    value: str
    display_value: str
    scope_id: str
    fact_key: str
    value_fingerprint: str
    cardinality: str = "single"
    valid_from: str = ""
    valid_to: str = ""

    @classmethod
    def from_parts(
        cls,
        *,
        subject: Any,
        predicate: Any,
        value: Any,
        scope_id: Any,
        cardinality: Any = "single",
        valid_from: Any = "",
        valid_to: Any = "",
    ) -> "ClaimDraft":
        display_value = unicodedata.normalize(
            "NFKC",
            _bounded_text(
                value,
                max_chars=MAX_FACT_VALUE_CHARS,
                field_name="value",
            ),
        )
        identity = build_fact_identity(subject, predicate, display_value)
        normalized_scope = _bounded_text(
            scope_id,
            max_chars=MAX_SCOPE_ID_CHARS,
            field_name="scope_id",
        )
        raw_cardinality = str(cardinality or "single").strip().lower()
        normalized_cardinality = _CARDINALITY_ALIASES.get(raw_cardinality)
        if normalized_cardinality is None:
            raise FactIdentityError("cardinality must be single or multi")
        return cls(
            subject=identity.subject,
            predicate=identity.predicate,
            value=identity.value,
            display_value=display_value,
            scope_id=normalized_scope,
            fact_key=identity.fact_key,
            value_fingerprint=identity.value_fingerprint,
            cardinality=normalized_cardinality,
            valid_from=_bounded_optional_text(valid_from, max_chars=64),
            valid_to=_bounded_optional_text(valid_to, max_chars=64),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "display_value": self.display_value,
            "scope_id": self.scope_id,
            "fact_key": self.fact_key,
            "value_fingerprint": self.value_fingerprint,
            "cardinality": self.cardinality,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    """Normalized proposal produced by a parser or deterministic planner."""

    action: EvolutionAction
    raw_action: str = ""
    claim: ClaimDraft | None = None
    target_ids: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceReference, ...] = ()
    confidence: float = 0.0
    reason: str = ""
    parser_reasons: tuple[str, ...] = ()
    existing_hint: str = ""
    source: str = ""

    @property
    def requires_review(self) -> bool:
        return self.action is EvolutionAction.REVIEW

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "raw_action": self.raw_action,
            "claim": self.claim.as_dict() if self.claim is not None else None,
            "target_ids": list(self.target_ids),
            "evidence": [item.as_dict() for item in self.evidence_refs],
            "confidence": self.confidence,
            "reason": self.reason,
            "parser_reasons": list(self.parser_reasons),
            "existing_hint": self.existing_hint,
            "source": self.source,
            "requires_review": self.requires_review,
        }


@dataclass(frozen=True, slots=True)
class EvolutionPlan:
    """Execution-neutral plan envelope with replay and policy identifiers."""

    proposal: EvolutionProposal
    action_id: str
    idempotency_key: str
    policy_mode: str = "preview"
    expected_versions: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal": self.proposal.as_dict(),
            "action_id": self.action_id,
            "idempotency_key": self.idempotency_key,
            "policy_mode": self.policy_mode,
            "expected_versions": dict(self.expected_versions),
        }


@dataclass(frozen=True, slots=True)
class EvolutionResult:
    """Stable result envelope shared by preview and future apply paths."""

    action_id: str
    action: EvolutionAction
    status: str
    applied: bool
    receipt: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action": self.action.value,
            "status": self.status,
            "applied": self.applied,
            "receipt": dict(self.receipt),
            "error": self.error,
        }


def _bounded_text(value: Any, *, max_chars: int, field_name: str) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    if not cleaned:
        raise FactIdentityError(f"{field_name} is required")
    if len(cleaned) > max_chars:
        raise FactIdentityError(f"{field_name} exceeds {max_chars} characters")
    return cleaned


def _bounded_optional_text(value: Any, *, max_chars: int) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    return cleaned[:max_chars]


def _safe_report_text(value: Any, *, max_chars: int) -> str:
    return sanitize_report_text(value)[:max_chars]


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _unique_target_ids(value: Any) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    reasons: list[str] = []
    fatal = False
    if value in (None, ""):
        return (), (), False
    if not isinstance(value, list):
        return (), ("target_ids_not_list",), True
    items = value
    if len(items) > MAX_TARGET_IDS:
        reasons.append("target_ids_truncated")
        fatal = True
        items = items[:MAX_TARGET_IDS]
    output: list[str] = []
    seen: set[str] = set()
    for raw in items:
        clean = " ".join(str(raw or "").split()).strip()
        if not clean:
            continue
        if len(clean) > MAX_TARGET_ID_CHARS:
            reasons.append("invalid_target_id")
            fatal = True
            continue
        if clean in seen:
            continue
        seen.add(clean)
        output.append(clean)
    if len(output) > MAX_TARGET_IDS:
        output = output[:MAX_TARGET_IDS]
        reasons.append("target_ids_truncated")
        fatal = True
    return tuple(output), tuple(dict.fromkeys(reasons)), fatal


def _evidence_references(
    value: Any,
    *,
    trusted_speaker_subjects: Mapping[str, str] | None = None,
) -> tuple[tuple[EvidenceReference, ...], tuple[str, ...], bool]:
    if value in (None, ""):
        return (), (), False
    if not isinstance(value, list):
        return (), ("evidence_not_list",), True
    output: list[EvidenceReference] = []
    seen: set[tuple[str, str]] = set()
    reasons: list[str] = []
    fatal = False
    items = value
    if len(items) > MAX_EVIDENCE_REFS:
        reasons.append("evidence_truncated")
        fatal = True
        items = items[:MAX_EVIDENCE_REFS]
    for raw in items:
        if not isinstance(raw, Mapping):
            reasons.append("invalid_evidence_ref")
            fatal = True
            continue
        source_type = _safe_report_text(raw.get("source_type"), max_chars=64).lower()
        source_id = _safe_report_text(raw.get("source_id"), max_chars=MAX_TARGET_ID_CHARS)
        if not source_type or not source_id:
            reasons.append("invalid_evidence_ref")
            fatal = True
            continue
        key = (source_type, source_id)
        if key in seen:
            continue
        seen.add(key)
        quote = _safe_report_text(raw.get("quote"), max_chars=MAX_EVIDENCE_QUOTE_CHARS)
        # A model/caller may cite a trusted source ID, but it must never be able
        # to self-assert who "I" denotes.  First-person identity is supplied on
        # a separate runtime-only channel by the adapter that owns the source.
        speaker_subject = ""
        if trusted_speaker_subjects is not None:
            speaker_subject = _safe_report_text(
                trusted_speaker_subjects.get(source_id),
                max_chars=MAX_FACT_SUBJECT_CHARS,
            )
        output.append(
            EvidenceReference(
                source_type,
                source_id,
                quote,
                speaker_subject=speaker_subject,
            )
        )
    if len(output) > MAX_EVIDENCE_REFS:
        output = output[:MAX_EVIDENCE_REFS]
        reasons.append("evidence_truncated")
        fatal = True
    return tuple(output), tuple(dict.fromkeys(reasons)), fatal


def _action(raw_action: str) -> tuple[EvolutionAction, tuple[str, ...], bool]:
    if not raw_action:
        return EvolutionAction.REVIEW, ("missing_action",), True
    try:
        return EvolutionAction(raw_action), (), False
    except ValueError:
        pass
    if raw_action == "skip":
        return EvolutionAction.NOOP, ("normalized_skip_to_noop",), False
    if raw_action in {"insert", "create"}:
        return EvolutionAction.ADD, ("normalized_insert_to_add",), False
    if raw_action == "delete":
        return EvolutionAction.RETRACT, ("normalized_delete_to_retract",), False
    if raw_action == "update":
        return EvolutionAction.REVIEW, ("ambiguous_legacy_update",), True
    return EvolutionAction.REVIEW, ("unknown_action",), True


def _empty_review(reason: str) -> EvolutionProposal:
    return EvolutionProposal(
        action=EvolutionAction.REVIEW,
        raw_action="",
        claim=None,
        parser_reasons=(reason,),
    )


def parse_evolution_proposal(
    payload: Any,
    *,
    trusted_scope_id: str = "",
    allowed_target_ids: Collection[str] | None = None,
    trusted_speaker_subjects: Mapping[str, str] | None = None,
) -> EvolutionProposal:
    """Parse one proposal without raising; unsafe or ambiguous input becomes REVIEW."""

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return _empty_review("invalid_json")
    if not isinstance(payload, Mapping):
        return _empty_review("proposal_not_object")

    raw_action = str(payload.get("action") or "").strip().lower().replace("-", "_")[:64]
    action, action_reasons, fatal = _action(raw_action)
    reasons = list(action_reasons)

    target_value = payload.get("target_ids", payload.get("existing_target_ids"))
    target_ids, target_reasons, target_fatal = _unique_target_ids(target_value)
    reasons.extend(target_reasons)
    fatal = fatal or target_fatal
    if allowed_target_ids is not None:
        allowed = {str(item) for item in allowed_target_ids if str(item)}
        allowed_targets = tuple(item for item in target_ids if item in allowed)
        if len(allowed_targets) != len(target_ids):
            reasons.append("target_id_not_allowed")
            fatal = True
        target_ids = allowed_targets

    evidence_value = payload.get("evidence", payload.get("evidence_refs"))
    evidence_refs, evidence_reasons, evidence_fatal = _evidence_references(
        evidence_value,
        trusted_speaker_subjects=trusted_speaker_subjects,
    )
    reasons.extend(evidence_reasons)
    fatal = fatal or evidence_fatal

    reason = _safe_report_text(payload.get("reason"), max_chars=MAX_REASON_CHARS)
    existing_hint = _safe_report_text(
        payload.get("existing_hint"),
        max_chars=MAX_HINT_CHARS,
    )
    source = _safe_report_text(payload.get("source"), max_chars=120)

    raw_claim = payload.get("claim")
    if raw_claim is None and any(key in payload for key in ("subject", "predicate", "value")):
        raw_claim = {
            key: payload.get(key)
            for key in (
                "subject",
                "predicate",
                "value",
                "scope_id",
                "cardinality",
                "valid_from",
                "valid_to",
            )
        }

    claim: ClaimDraft | None = None
    if isinstance(raw_claim, Mapping):
        raw_claim_fields = {
            "subject": str(raw_claim.get("subject") or ""),
            "predicate": str(raw_claim.get("predicate") or ""),
            "value": str(raw_claim.get("value") or ""),
        }
        overlong_claim = (
            len(raw_claim_fields["subject"]) > MAX_FACT_SUBJECT_CHARS
            or len(raw_claim_fields["predicate"]) > MAX_FACT_PREDICATE_CHARS
            or len(raw_claim_fields["value"]) > MAX_FACT_VALUE_CHARS
        )
        if overlong_claim:
            reasons.append("invalid_claim")
            fatal = True
        elif contains_secret_like_text("\n".join(raw_claim_fields.values())):
            reasons.append("secret_like_claim")
            fatal = True
        else:
            try:
                claim = ClaimDraft.from_parts(
                    subject=raw_claim.get("subject"),
                    predicate=raw_claim.get("predicate"),
                    value=raw_claim.get("value"),
                    scope_id=(
                        trusted_scope_id
                        or raw_claim.get("scope_id", payload.get("scope_id"))
                    ),
                    cardinality=raw_claim.get("cardinality", "single"),
                    valid_from=raw_claim.get("valid_from", ""),
                    valid_to=raw_claim.get("valid_to", ""),
                )
            except FactIdentityError:
                reasons.append("invalid_claim")
                fatal = True
    elif raw_claim is not None:
        reasons.append("claim_not_object")
        fatal = True

    if action in {
        EvolutionAction.ADD,
        EvolutionAction.ENRICH,
        EvolutionAction.SUPERSEDE,
        EvolutionAction.RETRACT,
    } and claim is None:
        reasons.append("claim_required")
        fatal = True
    if action in {
        EvolutionAction.ENRICH,
        EvolutionAction.SUPERSEDE,
        EvolutionAction.RETRACT,
    } and not target_ids:
        reason_code = (
            "retract_requires_target"
            if action is EvolutionAction.RETRACT
            else "target_required"
        )
        reasons.append(reason_code)
        fatal = True

    if fatal and action is not EvolutionAction.NOOP:
        action = EvolutionAction.REVIEW

    # Do not let defensive parsing return plaintext secret fragments through any
    # report field even when the claim itself was rejected.
    reason = redact_secret_like_text(reason)[:MAX_REASON_CHARS]
    existing_hint = redact_secret_like_text(existing_hint)[:MAX_HINT_CHARS]
    source = redact_secret_like_text(source)[:120]

    return EvolutionProposal(
        action=action,
        raw_action=raw_action,
        claim=claim if "secret_like_claim" not in reasons else None,
        target_ids=target_ids,
        evidence_refs=evidence_refs,
        confidence=_confidence(payload.get("confidence")),
        reason=reason,
        parser_reasons=tuple(dict.fromkeys(reasons)),
        existing_hint=existing_hint,
        source=source,
    )


__all__ = [
    "ClaimDraft",
    "EvolutionAction",
    "EvolutionPlan",
    "EvolutionProposal",
    "EvolutionResult",
    "EvidenceReference",
    "FactActionKind",
    "parse_evolution_proposal",
]
