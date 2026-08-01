"""HTTP utility helpers for hosted providers and operator scripts.

Network errors should be returned with sanitized diagnostics so credentials and private paths do not leak into tool output."""

from __future__ import annotations

from typing import Any

try:  # Support package imports and direct plugin scripts.
    from .capture_filters import redact_secret_like_text
except ImportError:  # pragma: no cover - direct script import style
    from capture_filters import redact_secret_like_text


def redact_sensitive(text: Any) -> str:
    """Redact HTTP/provider diagnostics with the canonical capture taxonomy."""

    return redact_secret_like_text(text).replace(
        "[REDACTED_SECRET]",
        "[REDACTED]",
    )


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
