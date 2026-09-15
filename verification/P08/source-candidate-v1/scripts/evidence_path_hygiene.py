"""Absolute local-path detection and stable evidence redaction helpers."""

from __future__ import annotations

from collections.abc import Iterator
import json
import re
from typing import Any


REDACTION_MARKER = "<isolated-path>"

# Order matters: extended/device and UNC forms must be consumed before a drive
# fragment inside the same path can match the ordinary drive rule.
ABSOLUTE_LOCAL_PATH_PATTERNS = (
    re.compile(
        r"(?i)\\\\\?\\(?:UNC\\[^\\/\s\"'<>]+\\[^\\/\s\"'<>]+|"
        r"[a-z]:[\\/])[^\r\n\"'<>|]*"
    ),
    re.compile(r"(?i)\\\\\.\\[^\r\n\"'<>|]*"),
    re.compile(
        r"(?i)(?<![:\\/])(?:\\\\|(?<!:)//)"
        r"[^\\/\s\"'<>]+[\\/][^\\/\s\"'<>]+[^\r\n\"'<>|]*"
    ),
    re.compile(r"(?i)(?<![A-Za-z0-9+.-])[a-z]:[\\/][^\s\r\n\"'<>|]*"),
    re.compile(
        r"(?<![A-Za-z0-9+.-])/(?:tmp|home|Users|private/var/folders)"
        r"(?:/[^\s\r\n\"'<>|]*)?"
    ),
)

_QUOTED_ABSOLUTE_PATH = re.compile(
    r"(?P<quote>[\"'])(?P<path>"
    r"(?:\\\\\?\\(?:UNC\\|[A-Za-z]:[\\/])|\\\\\.\\|"
    r"\\\\[^\\/]+[\\/][^\\/]+|[A-Za-z]:[\\/]|"
    r"/(?:tmp|home|Users|private/var/folders)(?:/|$))"
    r".*?)(?P=quote)",
    re.IGNORECASE,
)


def iter_string_values(value: Any) -> Iterator[str]:
    """Yield every decoded string nested in a JSON-compatible value."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from iter_string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_string_values(item)


def _extend_space_separated_path(text: str, end: int) -> int:
    """Include later spaced path segments when they retain a path separator."""

    cursor = end
    while cursor < len(text) and text[cursor] in {" ", "\t"}:
        token_start = cursor
        while token_start < len(text) and text[token_start] in {" ", "\t"}:
            token_start += 1
        token_end = token_start
        while token_end < len(text) and text[token_end] not in " \t\r\n\"'<>|":
            token_end += 1
        token = text[token_start:token_end]
        if not token or not any(separator in token for separator in ("\\", "/")):
            break
        end = token_end
        cursor = token_end
    return end


def _absolute_local_path_spans(value: str) -> list[tuple[int, int]]:
    text = str(value)
    spans = [
        (match.start("path"), match.end("path"))
        for match in _QUOTED_ABSOLUTE_PATH.finditer(text)
    ]
    for pattern in ABSOLUTE_LOCAL_PATH_PATTERNS:
        for match in pattern.finditer(text):
            spans.append(
                (match.start(), _extend_space_separated_path(text, match.end()))
            )
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda item: (item[0], -item[1])):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
            continue
        merged.append((start, end))
    return merged


def find_absolute_local_paths(value: str) -> list[str]:
    """Return local absolute-path matches from one decoded text value."""

    text = str(value)
    return [text[start:end] for start, end in _absolute_local_path_spans(text)]


def redact_absolute_local_paths(value: str) -> str:
    """Replace quoted or whitespace-delimited local paths with one stable marker."""

    text = str(value)
    for start, end in reversed(_absolute_local_path_spans(text)):
        text = text[:start] + REDACTION_MARKER + text[end:]
    return text


def redact_json_strings(value: Any) -> Any:
    """Return a shape-preserving copy with every nested string redacted."""

    if isinstance(value, str):
        return redact_absolute_local_paths(value)
    if isinstance(value, list):
        return [redact_json_strings(item) for item in value]
    if isinstance(value, dict):
        return {
            redact_absolute_local_paths(key) if isinstance(key, str) else key: redact_json_strings(item)
            for key, item in value.items()
        }
    return value


def private_path_match_count(text: str, *, decode_json: bool) -> int:
    """Count decoded JSON matches plus raw-text matches as a second defense."""

    raw = str(text)
    count = len(find_absolute_local_paths(raw))
    if not decode_json:
        return count
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return count
    count += sum(len(find_absolute_local_paths(value)) for value in iter_string_values(payload))
    return count


__all__ = [
    "ABSOLUTE_LOCAL_PATH_PATTERNS",
    "REDACTION_MARKER",
    "find_absolute_local_paths",
    "iter_string_values",
    "private_path_match_count",
    "redact_absolute_local_paths",
    "redact_json_strings",
]
