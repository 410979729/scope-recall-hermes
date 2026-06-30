"""General gating, normalization, and compact-text helpers used across capture, recall, and reporting.

Keep these helpers deterministic and side-effect free because many safety checks depend on their exact behavior."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, List, Set

TRIVIAL_RE = re.compile(
    r"^(?:"
    r"ok|okay|kk|k|yes|no|yep|nope|sure|thanks|thank you|thx|ty|got it|roger|"
    r"understood|noted|acknowledged|done|"
    r"hi|hello|hey|yo|早|早安|你好|嗨|在吗|在嗎|谢谢|謝謝|收到|明白|明白了|了解|了解了|好的|好"
    r")(?:[!！,.。?？~\s]*)$",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[a-zA-Z0-9]{2,}|[\u4e00-\u9fff]{2,}")
MEMORY_CONTEXT_RE = re.compile(r"<memory-context>[\s\S]*?</memory-context>\s*", re.IGNORECASE)
SUPERMEMORY_CONTEXT_RE = re.compile(r"<supermemory-context>[\s\S]*?</supermemory-context>\s*", re.IGNORECASE)


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
        return "\n".join(part for part in (stringify_content(item).strip() for item in value) if part)
    return str(value)


def clean_text(text: Any) -> str:
    text = stringify_content(text)
    text = MEMORY_CONTEXT_RE.sub("", text or "")
    text = SUPERMEMORY_CONTEXT_RE.sub("", text)
    return text.strip()


def compact_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "…"


def is_trivial(text: str) -> bool:
    return bool(TRIVIAL_RE.match((text or "").strip()))


def normalize_query(text: str, char_limit: int) -> str:
    return clean_text(text)[:char_limit].strip()


def should_skip_retrieval(query: str, min_length: int) -> bool:
    if not query:
        return True
    if is_trivial(query):
        return True
    if len(query) < min_length:
        return True
    return False


def query_tokens(text: str) -> List[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in WORD_RE.findall(text.lower()):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def stem_token(token: str) -> str:
    if not token.isascii() or not token.isalpha():
        return token
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("ing"):
        stem = token[:-3]
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            stem = stem[:-1]
        return stem
    if len(token) > 3 and token.endswith("ed"):
        stem = token[:-2]
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            stem = stem[:-1]
        return stem
    if len(token) > 4 and token.endswith("es") and not token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-1]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalized_token_set(tokens: List[str]) -> Set[str]:
    normalized: set[str] = set()
    for token in tokens:
        token = token.lower().strip()
        if not token:
            continue
        normalized.add(token)
        normalized.add(stem_token(token))
    return normalized


def build_fts_query(tokens: List[str]) -> str:
    safe = [fts_escape(token) for token in tokens if token]
    if not safe:
        return ""
    return " OR ".join(safe[:12])


def like_terms(query: str, tokens: List[str]) -> List[str]:
    terms = tokens[:6]
    if not terms and query:
        terms = [query[:30]]
    return [term for term in terms if term]


def fts_escape(token: str) -> str:
    return '"' + token.replace('"', ' ') + '"'


def dedup_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


CAPTURE_SKIP_PATTERNS = [
    re.compile(r"review the conversation above and update the skill library", re.IGNORECASE),
    re.compile(r"call the memory tool .*output only the raw json", re.IGNORECASE),
    re.compile(r"reply with ok and nothing else", re.IGNORECASE),
    re.compile(r"^\s*you are an ai assistant", re.IGNORECASE),
    re.compile(r"<available_skills>[\s\S]*?</available_skills>", re.IGNORECASE),
]
SECRET_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|private[_-]?key)\s*[:=]\s*[^\s]+",
    re.IGNORECASE,
)


def should_skip_capture(text: str, config: dict[str, Any] | None = None) -> bool:
    config = config or {}
    text = clean_text(text or "")
    if not text or is_trivial(text):
        return True
    max_chars = int(config.get("capture_hard_max_chars") or 4000)
    if max_chars > 0 and len(text) > max_chars:
        return True
    if SECRET_RE.search(text):
        return True
    for pattern in CAPTURE_SKIP_PATTERNS:
        if pattern.search(text):
            return True
    return False


def config_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
