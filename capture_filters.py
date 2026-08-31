"""Capture hygiene filters for rejecting low-value, secret-like, or path-heavy text before it reaches durable storage.

These filters are intentionally conservative because they sit before SQLite truth and journal evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

try:  # Support package imports and the repository's direct manual scripts.
    from .gating import clean_text, is_trivial
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
except ImportError:  # pragma: no cover - exercised by manual script import style
    from gating import clean_text, is_trivial
    from secret_patterns import (
        COMMON_SECRET_PATTERN_VALUES,
        PEM_PRIVATE_KEY_BEGIN_RE,
        SECRET_ASSIGNMENT_RE,
        TOKEN_ASSIGNMENT_RE,
        contains_secret_like_text,
        is_safe_token_metric_key,
        is_sensitive_mapping_key,
        secret_scan_shadow,
    )


@dataclass(frozen=True)
class CaptureFilterResult:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class TransportNoiseDecision:
    """Pure, content-only classification for transport and recovery wrappers."""

    blocked: bool
    reason_codes: tuple[str, ...] = ()


_TRANSPORT_PREFIX_CHARS = 512
_UNICODE_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\ufe58": "-",
        "\ufe63": "-",
        "\uff0d": "-",
    }
)
_MARKDOWN_LEADER_RE = re.compile(
    r"(?m)^[ \t]*(?:(?:>[ \t]*)+|(?:[-*+][ \t]+)|(?:\d{1,3}[.)][ \t]+))+"
)
_TRANSPORT_ROLE_LEADER_RE = re.compile(
    r"(?im)^[ \t]*(?:system|assistant|user|message|note|context|handoff)[ \t]*:[ \t]*"
)
_TRANSPORT_NOISE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "context_compaction_wrapper",
        re.compile(
            r"(?:^|\n)[^\w\n]{0,24}\[\s*context\s+compaction\b|"
            r"(?:^|\n)\s*earlier\s+turns?\s+were\s+compacted\b",
            re.IGNORECASE,
        ),
    ),
    (
        "reference_only_wrapper",
        re.compile(
            r"(?:^|\n)\s*(?:\[\s*reference\s+only\s*\]|#{1,6}\s*reference\s+only|reference\s+only)(?=\s|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "context_compression_continuation",
        re.compile(r"(?:^|\n)\s*conversation\s+continues?\s+after\s+context\s+compression", re.IGNORECASE),
    ),
    (
        "conversation_history_wrapper",
        re.compile(r"(?:^|\n)\s*the\s+conversation\s+history\s+below\s+is\s+intact\b", re.IGNORECASE),
    ),
    (
        "interrupted_turn_wrapper",
        re.compile(r"(?:^|\n)\s*your\s+previous\s+turn\s+was\s+interrupted\b", re.IGNORECASE),
    ),
    (
        "processing_handoff_wrapper",
        re.compile(r"(?:^|\n)\s*finish\s+processing\s+those\s+results\s+and\s+summarize\s+what\s+was\s+accomplished\b", re.IGNORECASE),
    ),
    (
        "telegram_history_wrapper",
        re.compile(r"(?:^|\n)\s*\[?\s*recent\s+telegram\s+chat\s+history\b", re.IGNORECASE),
    ),
    (
        "historical_task_snapshot_wrapper",
        re.compile(r"(?:^|\n)\s*(?:#{1,6}\s*)?historical\s+task\s+snapshot\b", re.IGNORECASE),
    ),
    (
        "background_process_wrapper",
        re.compile(r"(?:^|\n)\s*\[?\s*important\s*:\s*background\s+process\b", re.IGNORECASE),
    ),
    (
        "skill_library_wrapper",
        re.compile(r"(?:^|\n)\s*review\s+the\s+conversation\s+above\s+and\s+update\s+the\s+skill\s+library", re.IGNORECASE),
    ),
    (
        "tool_execution_wrapper",
        re.compile(r"(?:^|\n)\s*(?:\[[^\]\n]{0,48}\]\s*)?tool\s+execution\s+wrapper\b", re.IGNORECASE),
    ),
    (
        "gateway_recovery_wrapper",
        re.compile(r"(?:^|\n)\s*(?:gateway\s+recovery\s+wrapper\b|\[\s*system\s+note\s*:\s*gateway\s+recovered\b)", re.IGNORECASE),
    ),
    (
        "system_note_wrapper",
        re.compile(r"(?:^|\n)\s*\[\s*system\s+note\s*:", re.IGNORECASE),
    ),
    (
        "native_compaction_wrapper",
        re.compile(r"(?:^|\n)\s*(?:encrypted|native)\s+(?:context\s+)?compaction\s+(?:marker|wrapper)\b", re.IGNORECASE),
    ),
    (
        "preserved_task_wrapper",
        re.compile(r"(?:^|\n)\s*\[\s*your\s+active\s+task\s+list\s+was\s+preserved\s+across\s+context\s+compression\s*\]", re.IGNORECASE),
    ),
)


def _transport_prefix(text: Any) -> str:
    """Normalize only the bounded structural prefix used by wrapper rules."""

    if text is None:
        return ""
    if isinstance(text, bytes):
        raw = text.decode("utf-8", errors="replace")
    else:
        raw = str(text)
    prefix = raw.lstrip("\ufeff\x00 \t\r\n")[:_TRANSPORT_PREFIX_CHARS]
    prefix = prefix.translate(_UNICODE_DASH_TRANSLATION)
    # Telegram/Markdown can quote or list-prefix every wrapper line. Removing
    # only structural leaders preserves the prose itself for anchored rules.
    prefix = _MARKDOWN_LEADER_RE.sub("", prefix)
    # Transport envelopes frequently prefix the first line with a bounded role
    # label (for example ``System:``). Removing only that structural label
    # keeps an inline wrapper marker at the same trusted beginning boundary.
    prefix = _TRANSPORT_ROLE_LEADER_RE.sub("", prefix)
    return prefix.casefold()


def classify_transport_noise(text: Any) -> TransportNoiseDecision:
    """Identify transport wrappers without treating ordinary discussion as noise.

    Rules inspect a bounded beginning of the text. This catches quoted/listed
    wrappers while allowing legitimate prose that merely discusses compaction,
    gateways, or tool execution later in a durable memory.
    """

    prefix = _transport_prefix(text)
    if not prefix:
        return TransportNoiseDecision(False, ())
    reasons = tuple(
        code for code, pattern in _TRANSPORT_NOISE_RULES if pattern.search(prefix)
    )
    return TransportNoiseDecision(bool(reasons), reasons)


_TRANSPORT_REASON_LABELS = {
    "context_compaction_wrapper": "CONTEXT COMPACTION",
    "reference_only_wrapper": "REFERENCE ONLY",
    "context_compression_continuation": "Conversation continues after context compression",
    "conversation_history_wrapper": "conversation history wrapper",
    "interrupted_turn_wrapper": "previous turn interrupted",
    "processing_handoff_wrapper": "finish processing results",
    "telegram_history_wrapper": "Recent Telegram",
    "historical_task_snapshot_wrapper": "Historical Task Snapshot",
    "background_process_wrapper": "Background process",
    "skill_library_wrapper": "skill library",
    "tool_execution_wrapper": "tool execution wrapper",
    "gateway_recovery_wrapper": "System note / gateway recovery wrapper",
    "system_note_wrapper": "System note",
    "native_compaction_wrapper": "native compaction marker",
    "preserved_task_wrapper": "active task list",
}


DEFAULT_CAPTURE_SKIP_PATTERNS: tuple[str, ...] = (
    r"^## Active Task(?:\n|\r|$)",
    r"^## Remaining Work(?:\n|\r|$)",
    r"call the memory tool .*output only the raw json",
    r"reply with ok and nothing else",
    r"^\s*you are an ai assistant",
    r"<available_skills>[\s\S]*?</available_skills>",
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

    transport = classify_transport_noise(cleaned)
    if transport.blocked:
        labels = [
            _TRANSPORT_REASON_LABELS.get(code, code)
            for code in transport.reason_codes
        ]
        return CaptureFilterResult(False, f"transport-noise:{','.join(labels)}")

    max_chars = int((config or {}).get("capture_hard_max_chars") or 4000)
    if max_chars > 0 and len(cleaned) > max_chars:
        return CaptureFilterResult(False, "too-long")

    if contains_secret_like_text(cleaned):
        return CaptureFilterResult(False, "plaintext_secret_rejected")

    for pattern in _compiled_configured_patterns(_configured_patterns(config)):
        if pattern.search(cleaned):
            return CaptureFilterResult(False, f"skip-pattern:{pattern.pattern}")

    return CaptureFilterResult(True, "")
