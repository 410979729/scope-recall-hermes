"""Pure, deterministic identity helpers for structured factual assertions.

A fact key identifies a subject/predicate slot. The value fingerprint identifies
one value asserted for that slot. This separation lets temporal evolution close
or supersede one value without losing the stable slot identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any


FACT_IDENTITY_VERSION = 1
MAX_FACT_SUBJECT_CHARS = 200
MAX_FACT_PREDICATE_CHARS = 120
MAX_FACT_VALUE_CHARS = 2000
_WHITESPACE_RE = re.compile(r"\s+")


class FactIdentityError(ValueError):
    """Raised when a structured fact cannot form a safe, stable identity."""


def normalize_fact_component(
    value: Any,
    *,
    field_name: str = "component",
    max_chars: int = MAX_FACT_VALUE_CHARS,
    allow_empty: bool = False,
) -> str:
    """Normalize one identity component with NFKC, whitespace folding, and casefold."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip().casefold()
    if not normalized and not allow_empty:
        raise FactIdentityError(f"{field_name} is required")
    if len(normalized) > max_chars:
        raise FactIdentityError(
            f"{field_name} exceeds {max_chars} characters"
        )
    return normalized


def _digest(kind: str, components: list[str]) -> str:
    material = json.dumps(
        {
            "components": components,
            "kind": kind,
            "version": FACT_IDENTITY_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def canonical_fact_key(subject: Any, predicate: Any) -> str:
    """Return the versioned key for one subject/predicate fact slot."""

    normalized_subject = normalize_fact_component(
        subject,
        field_name="subject",
        max_chars=MAX_FACT_SUBJECT_CHARS,
    )
    normalized_predicate = normalize_fact_component(
        predicate,
        field_name="predicate",
        max_chars=MAX_FACT_PREDICATE_CHARS,
    )
    return f"fact:v{FACT_IDENTITY_VERSION}:{_digest('slot', [normalized_subject, normalized_predicate])}"


def canonical_fact_fingerprint(subject: Any, predicate: Any, value: Any) -> str:
    """Return a versioned fingerprint for one normalized slot/value assertion."""

    normalized_subject = normalize_fact_component(
        subject,
        field_name="subject",
        max_chars=MAX_FACT_SUBJECT_CHARS,
    )
    normalized_predicate = normalize_fact_component(
        predicate,
        field_name="predicate",
        max_chars=MAX_FACT_PREDICATE_CHARS,
    )
    normalized_value = normalize_fact_component(
        value,
        field_name="value",
        max_chars=MAX_FACT_VALUE_CHARS,
    )
    return (
        f"assertion:v{FACT_IDENTITY_VERSION}:"
        f"{_digest('assertion', [normalized_subject, normalized_predicate, normalized_value])}"
    )


@dataclass(frozen=True, slots=True)
class FactIdentity:
    """Normalized identity of one fact slot and asserted value."""

    subject: str
    predicate: str
    value: str
    fact_key: str
    value_fingerprint: str

    @classmethod
    def from_parts(cls, subject: Any, predicate: Any, value: Any) -> "FactIdentity":
        normalized_subject = normalize_fact_component(
            subject,
            field_name="subject",
            max_chars=MAX_FACT_SUBJECT_CHARS,
        )
        normalized_predicate = normalize_fact_component(
            predicate,
            field_name="predicate",
            max_chars=MAX_FACT_PREDICATE_CHARS,
        )
        normalized_value = normalize_fact_component(
            value,
            field_name="value",
            max_chars=MAX_FACT_VALUE_CHARS,
        )
        return cls(
            subject=normalized_subject,
            predicate=normalized_predicate,
            value=normalized_value,
            fact_key=canonical_fact_key(normalized_subject, normalized_predicate),
            value_fingerprint=canonical_fact_fingerprint(
                normalized_subject,
                normalized_predicate,
                normalized_value,
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "fact_key": self.fact_key,
            "value_fingerprint": self.value_fingerprint,
        }


def build_fact_identity(subject: Any, predicate: Any, value: Any) -> FactIdentity:
    """Build a validated normalized identity for one factual assertion."""

    return FactIdentity.from_parts(subject, predicate, value)
