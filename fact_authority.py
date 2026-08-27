"""Strict memory-type authority routing for the existing Fact Ledger.

The legacy classification normalizer intentionally remains permissive for old
memory rows.  It must not decide whether a new structured proposal is allowed
to create or mutate Claim authority.  This module is the small, pure boundary
that makes that decision without an unknown-to-factual fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


CLAIM_BACKED_MEMORY_TYPES = frozenset(
    {"factual", "preference", "project", "resource", "constraint"}
)
MEMORY_ONLY_TYPES = frozenset(
    {
        "procedure",
        "workflow",
        "tool_trace",
        "summary",
        "mental_model",
        "pitfall",
        "decision",
        "episodic",
    }
)
MEMORY_ONLY_COMPATIBILITY_ALIASES = {
    "experience": "episodic",
    "narrative": "episodic",
    "scratch": "episodic",
}

# ``fact`` is the physical projection marker written by the current Fact
# Executor.  It is not a public/canonical memory type and can never authorize a
# new Claim.  Existing target-bound mutations derive authority from the ledger,
# not from this marker.
FACT_PROJECTION_MARKER = "fact"


class FactAuthorityLane(str, Enum):
    """Closed authority outcome for one supplied memory type."""

    CLAIM_BACKED = "claim_backed"
    MEMORY_ONLY = "memory_only"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class FactAuthorityRoute:
    """A decontented routing decision safe to include in diagnostics."""

    canonical_type: str
    lane: FactAuthorityLane
    reason_code: str

    @property
    def claim_backed(self) -> bool:
        return self.lane is FactAuthorityLane.CLAIM_BACKED

    def as_dict(self) -> dict[str, str]:
        return {
            "canonical_type": self.canonical_type,
            "lane": self.lane.value,
            "reason_code": self.reason_code,
        }


def _normalized_token(value: Any) -> str:
    return str(value or "").strip().casefold()


def route_fact_authority(value: Any) -> FactAuthorityRoute:
    """Route only the frozen canonical vocabulary into Fact authority.

    Unknown, empty, historical aliases, and the internal ``fact`` projection
    marker are review-only.  The three frozen compatibility aliases normalize
    to ``episodic`` and therefore remain memory-only.
    """

    token = _normalized_token(value)
    if token in CLAIM_BACKED_MEMORY_TYPES:
        return FactAuthorityRoute(
            canonical_type=token,
            lane=FactAuthorityLane.CLAIM_BACKED,
            reason_code="canonical_claim_backed_type",
        )
    canonical = MEMORY_ONLY_COMPATIBILITY_ALIASES.get(token, token)
    if canonical in MEMORY_ONLY_TYPES:
        reason = (
            "memory_only_compatibility_alias"
            if token in MEMORY_ONLY_COMPATIBILITY_ALIASES
            else "canonical_memory_only_type"
        )
        return FactAuthorityRoute(
            canonical_type=canonical,
            lane=FactAuthorityLane.MEMORY_ONLY,
            reason_code=reason,
        )
    if not token:
        return FactAuthorityRoute(
            canonical_type="",
            lane=FactAuthorityLane.REVIEW,
            reason_code="missing_memory_type",
        )
    if token == FACT_PROJECTION_MARKER:
        return FactAuthorityRoute(
            canonical_type="",
            lane=FactAuthorityLane.REVIEW,
            reason_code="internal_projection_not_claim_authority",
        )
    return FactAuthorityRoute(
        canonical_type="",
        lane=FactAuthorityLane.REVIEW,
        reason_code="unknown_memory_type",
    )


def memory_type_is_claim_backed(value: Any) -> bool:
    """Return true only for an explicit canonical Claim-backed type."""

    return route_fact_authority(value).claim_backed


def is_fact_projection_marker(value: Any) -> bool:
    """Return whether a stored row carries the Executor's internal marker."""

    return _normalized_token(value) == FACT_PROJECTION_MARKER


__all__ = [
    "CLAIM_BACKED_MEMORY_TYPES",
    "FACT_PROJECTION_MARKER",
    "MEMORY_ONLY_COMPATIBILITY_ALIASES",
    "MEMORY_ONLY_TYPES",
    "FactAuthorityLane",
    "FactAuthorityRoute",
    "is_fact_projection_marker",
    "memory_type_is_claim_backed",
    "route_fact_authority",
]
