"""Text normalisation shared by capture and recall: host message content to plain text, trivial-turn detection, dedup keys.

Keep these deterministic and side-effect free; safety checks depend on their exact behaviour."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

MEMORY_CONTEXT_RE = re.compile(
    r"<memory-context>[\s\S]*?</memory-context>\s*", re.IGNORECASE
)


SUPERMEMORY_CONTEXT_RE = re.compile(
    r"<supermemory-context>[\s\S]*?</supermemory-context>\s*", re.IGNORECASE
)


def stringify_content(value: Any) -> str:
    """Normalize Hermes/OpenAI structured message content into plain text.

    Hermes may pass message content as OpenAI-style structured parts, for
    example [{"type": "text", "text": "hi"}] or multimodal blocks. Capture and
    recall filters are regex based, so they must receive text rather than raw
    lists/dicts.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        text = value.get("text")
        if text is not None:
            return stringify_content(text)
        content = value.get("content")
        if content is not None:
            return stringify_content(content)
        return " ".join(
            stringify_content(item)
            for key, item in value.items()
            if key not in {"type", "mime_type", "media_type"}
        ).strip()
    if isinstance(value, Iterable):
        return "\n".join(
            part for part in (stringify_content(item).strip() for item in value) if part
        )
    return str(value)


def clean_text(text: Any) -> str:
    text = stringify_content(text)
    text = MEMORY_CONTEXT_RE.sub("", text or "")
    text = SUPERMEMORY_CONTEXT_RE.sub("", text)
    return text.strip()


def dedup_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())
