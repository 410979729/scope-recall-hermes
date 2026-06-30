"""HTTP utility helpers for hosted providers and operator scripts.

Network errors should be returned with sanitized diagnostics so credentials and private paths do not leak into tool output."""

from __future__ import annotations

import re
from typing import Any

SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|private[_-]?key)\s*[:=]\s*['\"]?[^\s,'\"\]}]+['\"]?"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{12,}\b"),
]


def _redact_match(match: re.Match[str]) -> str:
    text = match.group(0)
    if "=" in text:
        return f"{text.split('=', 1)[0]}=[REDACTED]"
    if ":" in text:
        return f"{text.split(':', 1)[0]}: [REDACTED]"
    return "[REDACTED]"


def redact_sensitive(text: Any) -> str:
    redacted = str(text or "")
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted


def chat_completions_endpoint(base_url: str, *, endpoint: str = "", append_v1: bool = True) -> str:
    explicit = str(endpoint or "").strip().rstrip("/")
    if explicit:
        return explicit
    root = str(base_url or "").strip().rstrip("/") or "https://api.openai.com"
    if root.endswith("/chat/completions"):
        return root
    if root.endswith("/v1"):
        return root + "/chat/completions"
    suffix = "/v1/chat/completions" if append_v1 else "/chat/completions"
    return root + suffix


__all__ = ["chat_completions_endpoint", "redact_sensitive"]
