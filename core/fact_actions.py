"""Fail-closed contracts for structured fact-evolution proposals.

This module is intentionally pure: it parses, normalizes, validates, and
serializes proposals but never opens a database, calls a model, or applies an
action. Unknown or ambiguous inputs become REVIEW proposals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import unicodedata

from .fact_identity import (
    MAX_FACT_VALUE_CHARS,
    FactIdentityError,
    build_fact_identity,
)


MAX_SCOPE_ID_CHARS = 240
_CARDINALITY_ALIASES = {
    "single": "single",
    "multi": "multi",
    "multiple": "multi",
    "many": "multi",
}


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


__all__ = ['ClaimDraft', 'EvidenceReference']
