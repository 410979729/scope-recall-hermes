"""Public trivial-prompt gate: only blank or slash-command input is skipped."""
from __future__ import annotations

from typing import Optional


def is_trivial_prompt(text: Optional[str]) -> bool:
    stripped = (text or "").strip()
    return not stripped or stripped.startswith("/")
