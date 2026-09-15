"""Semantic retention profiles shared by immediate and journal LLM extraction.

Profiles control how much durable context an extractor preserves. They never
change raw journal capture or authorize transcript duplication into recall rows.
"""

from __future__ import annotations

DEFAULT_RETENTION_PROFILE = "balanced"
RETENTION_PROFILES = ("light", "balanced", "full")

_PROFILE_INSTRUCTIONS = {
    "light": (
        "Retention profile: light. Extract only minimal durable facts: explicit "
        "preferences, constraints, corrected facts, and essential reusable outcomes. "
        "Omit conversational background and rationale unless it is required to "
        "understand the fact."
    ),
    "balanced": (
        "Retention profile: balanced. Preserve durable facts together with the "
        "reasoning and reusable steps needed to apply them later, while omitting "
        "one-off progress and conversational filler."
    ),
    "full": (
        "Retention profile: full. Preserve durable decision rationale, alternatives, "
        "corrections, ordered steps, and verification context when they remain useful "
        "beyond the current session."
    ),
}

_NO_TRANSCRIPT_DUPLICATION = (
    "Never copy the full transcript into durable memory; the sanitized raw journal "
    "remains the evidence source. Produce self-contained searchable summaries and "
    "cite only source message IDs exposed in the current input."
)


def normalize_retention_profile(value: object) -> str:
    """Return a supported profile, failing safely to the existing balanced policy."""

    profile = str(value or "").strip().lower()
    return profile if profile in RETENTION_PROFILES else DEFAULT_RETENTION_PROFILE


def retention_profile_instruction(value: object) -> str:
    """Render the stable extraction instruction for one retention profile."""

    profile = normalize_retention_profile(value)
    return f"{_PROFILE_INSTRUCTIONS[profile]} {_NO_TRANSCRIPT_DUPLICATION}"
