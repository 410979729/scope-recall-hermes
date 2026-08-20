"""Pure nightly-digest admission. Separate from journal admission on purpose."""

from __future__ import annotations

from typing import Any

from ...capture_filters import should_capture_text
from ...digest_quality import score_digest_candidate

NIGHTLY_TARGETS = {"user", "memory", "project", "ops", "general"}


def candidate_is_allowed(candidate: Any) -> bool:
    if candidate.target not in NIGHTLY_TARGETS:
        return False
    if len(candidate.content) < 40:
        return False
    if not should_capture_text(candidate.content).allowed:
        return False
    quality = score_digest_candidate(candidate)
    if quality.recommended_action == "reject":
        return False
    return True
