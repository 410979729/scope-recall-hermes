"""Capture hygiene filters for rejecting low-value, secret-like, or path-heavy text before it reaches durable storage.

These filters are intentionally conservative because they sit before SQLite truth and journal evidence."""

from __future__ import annotations

import re
from typing import Any

from .gating import clean_text
from .secret_patterns import (
    COMMON_SECRET_PATTERN_VALUES,
    PEM_PRIVATE_KEY_BEGIN_RE,
    SECRET_ASSIGNMENT_RE,
    TOKEN_ASSIGNMENT_RE,
    contains_secret_like_text,
    is_safe_token_metric_key,
    is_sensitive_mapping_key,
    secret_scan_shadow,
)


SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    SECRET_ASSIGNMENT_RE,
    *COMMON_SECRET_PATTERN_VALUES,
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
    # The greedy prefix already consumes the entire non-whitespace token.
    # Start there once, instead of rescanning every suffix of long CJK prose.
    re.compile(r"(?<![^\s\]])(?:[A-Za-z]:)?[^\s\]]*[/\\]image_cache[/\\]img_[A-Za-z0-9_-]+\.(?:jpe?g|png|webp|gif)\b", re.IGNORECASE),
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


def sanitize_source_capture_text(text: str) -> str:
    """Remove known transport payloads without normalizing source prose.

    The legacy summary filter below intentionally compacts whitespace. Source
    evidence needs its original indentation, line endings, and punctuation.
    A caller must mark any transport removal as incomplete capture.
    """
    cleaned = strip_inline_data_urls(text)
    for pattern in INLINE_ATTACHMENT_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned


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


def _redact_private_key_blocks(text: str) -> str:
    """Redact complete or truncated PEM private-key blocks fail closed."""

    output: list[str] = []
    cursor = 0
    lowered = text.casefold()
    while match := PEM_PRIVATE_KEY_BEGIN_RE.search(text, cursor):
        output.append(text[cursor : match.start()])
        output.append("[REDACTED_SECRET]")
        end_marker = f"-----END {match.group('label')}-----".casefold()
        end_start = lowered.find(end_marker, match.end())
        if end_start < 0:
            return "".join(output)
        cursor = end_start + len(end_marker)
    output.append(text[cursor:])
    return "".join(output)


def redact_secret_like_text(text: Any) -> str:
    cleaned = clean_text(text)
    if not cleaned:
        return ""
    shadow = secret_scan_shadow(cleaned)
    if shadow != cleaned and contains_secret_like_text(shadow):
        return "[REDACTED_SECRET]"
    redacted = _redact_private_key_blocks(cleaned)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    redacted = TOKEN_ASSIGNMENT_RE.sub(
        lambda match: (
            match.group(0)
            if is_safe_token_metric_key(match.group("key"))
            else "[REDACTED_SECRET]"
        ),
        redacted,
    )
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
    if is_sensitive_mapping_key(raw):
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

