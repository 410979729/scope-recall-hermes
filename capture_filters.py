from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
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
    r"Conversation continues after context compression",
    r"^\[System note:",
    r"The conversation history below is intact",
    r"Your previous turn was interrupted",
    r"finish processing those results and summarize what was accomplished",
    r"^\[Your active task list was preserved across context compression\]",
    r"^\[IMPORTANT: Background process ",
    r"^## Active Task(?:\n|\r|$)",
    r"^## Remaining Work(?:\n|\r|$)",
    r"^Review the conversation above and update the skill library",
    r"call the memory tool .*output only the raw json",
    r"reply with ok and nothing else",
    r"^\s*you are an ai assistant",
    r"<available_skills>[\s\S]*?</available_skills>",
)

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM private-key blocks must be redacted as a whole, not just the BEGIN line.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    # Common assignment forms: api_key=..., api key: ..., token is ..., private-key = ...
    re.compile(
        r"(?:api[_ \t-]?key|token|secret|password|passwd|credential(?:[_ \t-]?[a-z0-9_]+)?|private[_ \t-]?key)"
        r"(?:[ \t]*(?::|=|是)[ \t]*|[ \t]+is[ \t]+)[^\s]+",
        re.IGNORECASE,
    ),
    # Provider-specific and transport token forms that often appear without labels,
    # including partially masked values returned by upstream auth errors.
    re.compile(r"s" r"k-[A-Za-z0-9_*][A-Za-z0-9_*-]{8,}"),
    re.compile(r"g" r"h[pousr]_[A-Za-z0-9_*_]{20,}"),
    re.compile(r"bea" r"rer\s+[A-Za-z0-9._\-~+/=*]{16,}", re.IGNORECASE),
)

PRIVATE_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9])(?:/home|/Users|/root)/[^\s\]})>'\"]+"),
    re.compile(r"(?<![A-Za-z0-9])~/(?:[^\s\]})>'\"]+)", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:)?\\\\Users\\\\[^\s\]})>'\"]+", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])/tmp/(?:hermes|scope|pytest|tmp)[^\s\]})>'\"]*", re.IGNORECASE),
)

TOOL_TRACE_LINE_RE = re.compile(r"\bTool execution trace(?:\s*\(([^)]*)\))?:[^\n\r]*(?:[\n\r]|$)", re.IGNORECASE)

ATTACHMENT_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\[Image attached at:\s*.*\]\s*$", re.IGNORECASE),
    re.compile(r"^\[inline image/[^\]]*data omitted\]\s*$", re.IGNORECASE),
    re.compile(r"^\[screenshot\]\s*$", re.IGNORECASE),
)

INLINE_ATTACHMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[Image attached at:\s*[^\]]*\]", re.IGNORECASE),
    re.compile(r"\[inline image/[^\]]*data omitted\]", re.IGNORECASE),
    re.compile(r"\[screenshot\]", re.IGNORECASE),
    re.compile(r"(?:[A-Za-z]:)?[^\s\]]*[/\\]image_cache[/\\]img_[A-Za-z0-9_-]+\.(?:jpe?g|png|webp|gif)\b", re.IGNORECASE),
)


def sanitize_capture_text(text: Any) -> str:
    """Remove gateway attachment markers before capture/journal storage.

    The LLM may receive images through Hermes' native vision path, but Scope
    Recall should not persist local cache paths or inline-image placeholders as
    memory material. Keep the user's surrounding text so a screenshot question
    can still be represented without leaking `/image_cache/img_*.jpg` paths.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return ""
    kept_lines: list[str] = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if any(pattern.match(stripped) for pattern in ATTACHMENT_LINE_PATTERNS):
            continue
        sanitized_line = line.rstrip()
        for pattern in INLINE_ATTACHMENT_PATTERNS:
            sanitized_line = pattern.sub("", sanitized_line)
        sanitized_line = re.sub(r"[ \t]{2,}", " ", sanitized_line).strip()
        if sanitized_line:
            kept_lines.append(sanitized_line)
    sanitized = "\n".join(kept_lines).strip()
    return re.sub(r"\n{3,}", "\n\n", sanitized)


def contains_secret_like_text(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def redact_secret_like_text(text: Any) -> str:
    cleaned = clean_text(text)
    if not cleaned:
        return ""
    redacted = cleaned
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def redact_private_paths(text: Any) -> str:
    cleaned = clean_text(text)
    if not cleaned:
        return ""
    redacted = cleaned
    for pattern in INLINE_ATTACHMENT_PATTERNS:
        redacted = pattern.sub("[REDACTED_PATH]", redacted)
    for pattern in PRIVATE_PATH_PATTERNS:
        redacted = pattern.sub("[REDACTED_PATH]", redacted)
    return redacted


def sanitize_report_text(text: Any) -> str:
    """Redact sensitive details for report/evidence surfaces.

    This is stricter than normal capture sanitization: user-visible and durable
    audit surfaces should not echo raw tool stdout, plaintext secrets, local
    filesystem paths, or gateway attachment cache paths.
    """
    cleaned = sanitize_capture_text(text)
    if not cleaned:
        return ""

    def _tool_summary(match: re.Match[str]) -> str:
        tool = (match.group(1) or "").strip()
        suffix = f" ({tool})" if tool else ""
        raw_line = match.group(0)
        markers: list[str] = []
        if contains_secret_like_text(raw_line):
            markers.append("[REDACTED_SECRET]")
        if redact_private_paths(raw_line) != clean_text(raw_line):
            markers.append("[REDACTED_PATH]")
        marker_suffix = " " + " ".join(markers) if markers else ""
        return f"Tool execution summary{suffix}: output omitted{marker_suffix}"

    redacted = TOOL_TRACE_LINE_RE.sub(_tool_summary, cleaned)
    redacted = redact_secret_like_text(redacted)
    redacted = redact_private_paths(redacted)
    return clean_text(redacted)


def _configured_patterns(config: dict[str, Any] | None) -> tuple[str, ...]:
    """Return additive safety skip patterns plus operator-configured patterns.

    Runtime wrapper and secret-hygiene patterns are safety gates, not ordinary
    preferences. Keep the built-in gates active even when an older config.json
    carries its own capture_skip_patterns list from a previous release.
    """
    patterns = list(DEFAULT_CAPTURE_SKIP_PATTERNS)
    if not config:
        return tuple(patterns)
    raw = config.get("capture_skip_patterns")
    configured: tuple[str, ...]
    if not raw:
        configured = ()
    elif isinstance(raw, str):
        configured = (raw,)
    elif isinstance(raw, (list, tuple)):
        configured = tuple(str(item) for item in raw if str(item).strip())
    else:
        configured = ()
    for pattern in configured:
        normalized = _normalize_skip_pattern(pattern)
        if normalized and normalized not in patterns:
            patterns.append(normalized)
    return tuple(patterns)


@lru_cache(maxsize=64)
def _compiled_configured_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, flags=re.IGNORECASE | re.MULTILINE))
        except re.error:
            continue
    return tuple(compiled)


def _normalize_skip_pattern(pattern: str) -> str:
    """Fix common config escaping mistakes so patterns actually match.

    Hermes config UI stores patterns through JSON serialization, which can
    cause patterns like ``^[CONTEXT`` to become doubly-escaped ``^\\\\[CONTEXT``
    after a full save/load round-trip.  This function detects and repairs the
    most common breakage: double backslashes before regex meta-characters.
    """
    if not pattern:
        return ""
    # Try as-is first
    try:
        re.compile(pattern)
        return pattern  # valid regex already
    except re.error:
        pass
    # Common fix: compress double backslashes to single before meta chars
    repaired = re.sub(r"\\\\(?=[\\\[\](){}.*+?|^$])", r"\\", pattern)
    if repaired == pattern:
        return ""  # unfixable, discard
    try:
        re.compile(repaired)
        return repaired
    except re.error:
        return ""  # still broken after repair, discard


def should_capture_text(text: Any, config: dict[str, Any] | None = None) -> CaptureFilterResult:
    cleaned = sanitize_capture_text(text)
    if not cleaned:
        return CaptureFilterResult(False, "empty")
    if is_trivial(cleaned):
        return CaptureFilterResult(False, "trivial")

    max_chars = int((config or {}).get("capture_hard_max_chars") or 4000)
    if max_chars > 0 and len(cleaned) > max_chars:
        return CaptureFilterResult(False, "too-long")

    if contains_secret_like_text(cleaned):
        return CaptureFilterResult(False, "secret-like-content")

    for pattern in _compiled_configured_patterns(_configured_patterns(config)):
        if pattern.search(cleaned):
            return CaptureFilterResult(False, f"skip-pattern:{pattern.pattern}")

    return CaptureFilterResult(True, "")
