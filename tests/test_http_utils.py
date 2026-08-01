"""Tests for shared HTTP-error credential redaction.

HTTP callers must use the same secret taxonomy as capture and durable-report
boundaries so provider errors cannot reintroduce plaintext credentials.
"""

from __future__ import annotations

import pytest

from scope_recall.http_utils import redact_sensitive


@pytest.mark.parametrize(
    "text",
    [
        "upstream echoed " + "ghp_" + "A" * 10 + "\u200b" + "B" * 14,
        "redis://:" + "C" * 24 + "@localhost/0",
        "Authorization: Bearer:" + "D" * 24,
        "set-cookie: session_id=" + "E" * 24 + "; HttpOnly",
    ],
)
def test_http_redaction_uses_canonical_secret_boundary(text: str) -> None:
    redacted = redact_sensitive(text)

    assert text not in redacted
    assert "[REDACTED]" in redacted


def test_http_redaction_preserves_benign_cookie_prose() -> None:
    text = "Cookie: recipe uses chocolate chips and butter"

    assert redact_sensitive(text) == text
