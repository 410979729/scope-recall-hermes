from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .gating import clean_text, is_trivial


@dataclass(frozen=True)
class CaptureFilterResult:
    allowed: bool
    reason: str = ""


DEFAULT_CAPTURE_SKIP_PATTERNS: tuple[str, ...] = (
    r"^\[Recent Telegram chat history",
    r"^\[CONTEXT COMPACTION",
    r"Earlier turns were compacted into the summary below",
    r"^Review the conversation above and update the skill library",
    r"call the memory tool .*output only the raw json",
    r"reply with ok and nothing else",
    r"^\s*you are an ai assistant",
    r"<available_skills>[\s\S]*?</available_skills>",
)

SECRET_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|credential(?:[_-]?[a-z0-9_]+)?|private[_-]?key)\s*[:=]\s*[^\s]+",
    re.IGNORECASE,
)


def _configured_patterns(config: dict[str, Any] | None) -> tuple[str, ...]:
    if not config:
        return DEFAULT_CAPTURE_SKIP_PATTERNS
    raw = config.get("capture_skip_patterns")
    if not raw:
        return DEFAULT_CAPTURE_SKIP_PATTERNS
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw if str(item).strip())
    return DEFAULT_CAPTURE_SKIP_PATTERNS


def should_capture_text(text: Any, config: dict[str, Any] | None = None) -> CaptureFilterResult:
    cleaned = clean_text(text)
    if not cleaned:
        return CaptureFilterResult(False, "empty")
    if is_trivial(cleaned):
        return CaptureFilterResult(False, "trivial")

    max_chars = int((config or {}).get("capture_hard_max_chars") or 4000)
    if max_chars > 0 and len(cleaned) > max_chars:
        return CaptureFilterResult(False, "too-long")

    if SECRET_RE.search(cleaned):
        return CaptureFilterResult(False, "secret-like-content")

    for pattern in _configured_patterns(config):
        if re.search(pattern, cleaned, flags=re.IGNORECASE | re.MULTILINE):
            return CaptureFilterResult(False, f"skip-pattern:{pattern}")

    return CaptureFilterResult(True, "")
