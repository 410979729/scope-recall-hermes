"""Capture hygiene filters for rejecting low-value, secret-like, or path-heavy text before it reaches durable storage.

These filters are intentionally conservative because they sit before SQLite truth and journal evidence."""

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
    re.compile(r"(?<![A-Za-z0-9_-])s" r"k-(?:(?:proj|ant-api\d{2})-)?[A-Za-z0-9_*]{16,}(?![A-Za-z0-9_-])"),
    re.compile(r"g" r"h[pousr]_[A-Za-z0-9_*_]{20,}"),
    re.compile(r"bea" r"rer\s+[A-Za-z0-9._\-~+/=*]{16,}", re.IGNORECASE),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9.*_-]{8,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9.*_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9_*.-]{16,}\b", re.IGNORECASE),
)

PRIVATE_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Windows drive paths first so `C:/Users/...` is fully redacted before the
    # POSIX `/Users/...` fallback can leave a `C:` fragment behind.
    re.compile(
        r"(?<![A-Za-z0-9])[A-Za-z]:(?:[\\/]+|(?=[^\\/\s\]})>'\"]*[\\/]))"
        r"[^\\/\s\]})>'\"]+(?:\s+[^\\/\s\]})>'\"]+)*"
        r"(?:[\\/]+[^\\/\s\]})>'\"]+(?:\s+[^\\/\s\]})>'\"]+)*)*",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![A-Za-z0-9])\\\\[^\\/\s\]})>'\"]+[\\/][^\s\]})>'\"]+", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])%(?:TEMP|TMP|LOCALAPPDATA|APPDATA|USERPROFILE)%[\\/][^\s\]})>'\"]+", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])(?:/home|/Users|/root)/[^\s\]})>'\"]+"),
    re.compile(r"(?<![A-Za-z0-9])~/(?:[^\s\]})>'\"]+)", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])/tmp/(?:hermes|scope|pytest|tmp)[^\s\]})>'\"]*", re.IGNORECASE),
)

SENSITIVE_MAPPING_KEY_RE = re.compile(
    r"(?:"
    r"(?:^|[_\-\s])(?:authorization|api[_\-\s]?key|access[_\-\s]?token|refresh[_\-\s]?token|"
    r"password|passwd|private[_\-\s]?key|client[_\-\s]?secret|cookie)(?:$|[_\-\s:=])"
    r"|(?:^|[_\-\s])token(?:$|[\s:=])"
    r")",
    re.IGNORECASE,
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

DATA_URL_PREFIX_RE = re.compile(
    r"data:[a-z0-9.+-]+/[a-z0-9.+-]+(?:;[a-z0-9.+_-]+=[^;,\s]+)*;base64,",
    re.IGNORECASE,
)
_BASE64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")


def _base64_chunk_end(text: str, start: int) -> tuple[int, int]:
    """Return the end and alphabet-character count of one base64 chunk."""

    index = start
    while index < len(text) and text[index] in _BASE64_ALPHABET:
        index += 1
    alphabet_count = index - start
    padding = 0
    while index < len(text) and text[index] == "=" and padding < 2:
        index += 1
        padding += 1
    return index, alphabet_count


def _folded_base64_separator_end(text: str, start: int) -> int:
    """Return the first character after a physical or escaped line fold."""

    index = start
    while index < len(text) and text[index] in " \t":
        index += 1
    if index < len(text) and text[index] in "\r\n":
        if text[index] == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            index += 2
        else:
            index += 1
    elif text.startswith("\\r\\n", index):
        index += 4
    elif text.startswith("\\n", index) or text.startswith("\\r", index):
        index += 2
    else:
        return start
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _base64_continuation_has_boundary(text: str, end: int, alphabet_count: int) -> bool:
    """Return whether a folded chunk ends at a data-safe boundary.

    A continuation line is payload when its base64 run reaches end of input,
    another physical/escaped fold, or an explicit URI/container delimiter.
    Long runs followed by inline prose are also payload; a short run followed
    by ordinary whitespace (for example ``Keep this sentence``) is preserved.
    """

    if end >= len(text):
        return True
    if _folded_base64_separator_end(text, end) != end:
        return True
    if text[end] in ",;:.)]}>'\"":
        return True
    return alphabet_count >= 32 and text[end] in " \t"


def strip_inline_data_urls(text: Any) -> str:
    """Remove inline/folded base64 data URLs while preserving surrounding prose.

    Base64 payload characters are consumed until the first character outside the
    alphabet. Physical or escaped line folds are consumed when the next base64
    run reaches a URI/container boundary; prose containing spaces remains.
    """

    raw = str(text or "")
    output: list[str] = []
    cursor = 0
    while True:
        prefix = DATA_URL_PREFIX_RE.search(raw, cursor)
        if prefix is None:
            output.append(raw[cursor:])
            break
        output.append(raw[cursor : prefix.start()])
        payload_end, _payload_chars = _base64_chunk_end(raw, prefix.end())
        while True:
            separator_end = _folded_base64_separator_end(raw, payload_end)
            if separator_end == payload_end:
                break
            continuation_end, continuation_chars = _base64_chunk_end(raw, separator_end)
            if continuation_chars <= 0 or not _base64_continuation_has_boundary(
                raw,
                continuation_end,
                continuation_chars,
            ):
                break
            payload_end = continuation_end
        output.append(" ")
        cursor = payload_end
    return "".join(output)


def sanitize_capture_text(text: Any) -> str:
    """Remove binary data URLs and gateway attachment markers before storage.

    The LLM may receive images through Hermes' native vision path, but Scope
    Recall should never persist their base64 payloads, local cache paths, or
    inline-image placeholders. Surrounding user prose and punctuation remain.
    """
    cleaned = clean_text(strip_inline_data_urls(text))
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


def sanitize_mapping_key(value: Any) -> tuple[str, bool]:
    """Return a safe JSON-object key and whether it was redacted.

    Secret assignments can be smuggled in mapping keys, so recursively cleaning
    values alone is not a storage boundary. Sensitive field names are collapsed
    to a stable marker; other keys still receive ordinary secret/path redaction.
    """

    raw = str(value)
    safe = sanitize_report_text(raw)
    if SENSITIVE_MAPPING_KEY_RE.search(raw):
        safe = "[REDACTED_KEY]"
    if not safe:
        safe = "[REDACTED_KEY]"
    return safe, safe != raw


def sanitize_structured_value(value: Any, *, _depth: int = 0) -> tuple[Any, bool]:
    """Recursively sanitize both keys and values for durable/report JSON.

    The return flag lets operator surfaces accurately report that redaction took
    place. Key collisions created by redaction use ordinal suffixes and never a
    hash of the original secret-bearing key.
    """

    if _depth >= 16:
        return "[REDACTED_DEPTH_LIMIT]", True
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        changed = False
        for item_key, item in value.items():
            safe_key, key_changed = sanitize_mapping_key(item_key)
            candidate = safe_key
            suffix = 2
            while candidate in output:
                candidate = f"{safe_key}#{suffix}"
                suffix += 1
            clean_item, item_changed = sanitize_structured_value(item, _depth=_depth + 1)
            output[candidate] = clean_item
            changed = changed or key_changed or item_changed or candidate != safe_key
        return output, changed
    if isinstance(value, (list, tuple, set)):
        output_list: list[Any] = []
        changed = not isinstance(value, list)
        for item in value:
            clean_item, item_changed = sanitize_structured_value(item, _depth=_depth + 1)
            output_list.append(clean_item)
            changed = changed or item_changed
        return output_list, changed
    if isinstance(value, bytes):
        return "[REDACTED_BINARY]", True
    if isinstance(value, str):
        safe = sanitize_report_text(value)
        return safe, safe != value
    if isinstance(value, (bool, int, float)) or value is None:
        return value, False
    safe = sanitize_report_text(str(value))
    return safe, True


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
        return CaptureFilterResult(False, "plaintext_secret_rejected")

    for pattern in _compiled_configured_patterns(_configured_patterns(config)):
        if pattern.search(cleaned):
            return CaptureFilterResult(False, f"skip-pattern:{pattern.pattern}")

    return CaptureFilterResult(True, "")
