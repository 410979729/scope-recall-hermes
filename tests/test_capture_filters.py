"""Tests for capture hygiene filters that reject wrappers, secrets, paths, and low-value tool text.

These cases protect the boundary before noisy text enters journal or durable memory."""

from __future__ import annotations

import pytest

from scope_recall.capture_filters import _compiled_configured_patterns, _configured_patterns, redact_secret_like_text, sanitize_capture_text, should_capture_text


def test_recent_telegram_history_wrapper_is_rejected():
    result = should_capture_text("[Recent Telegram chat history in this chat since your last turn]\nJoy: hello")

    assert result.allowed is False
    assert "Recent Telegram" in result.reason


def test_context_compaction_wrapper_is_rejected():
    result = should_capture_text("[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.")

    assert result.allowed is False
    assert "CONTEXT COMPACTION" in result.reason


def test_skill_review_meta_prompt_is_rejected():
    result = should_capture_text("Review the conversation above and update the skill library with anything reusable.")

    assert result.allowed is False
    assert "skill library" in result.reason


def test_configured_skip_patterns_are_precompiled_and_cached():
    config = {"capture_skip_patterns": [r"^NOISY WRAPPER", r"Project\s+Scratch"]}
    patterns = _configured_patterns(config)

    first = _compiled_configured_patterns(patterns)
    second = _compiled_configured_patterns(patterns)

    assert first is second
    assert should_capture_text("NOISY WRAPPER\nignore me", config).allowed is False
    assert should_capture_text("Project Scratch transient note", config).allowed is False


def test_secret_like_text_is_rejected():
    result = should_capture_text("The credential_placeholder = *** should not be retained.")

    assert result.allowed is False
    assert result.reason == "secret-like-content"


def test_secret_assignment_with_is_is_rejected():
    result = should_capture_text("The token is abcdefghijklmnopqrstuvwxyz should not be retained.")

    assert result.allowed is False
    assert result.reason == "secret-like-content"


def test_private_key_redaction_removes_entire_pem_block():
    text = """Use this key:
-----BEGIN PRIVATE KEY-----
notreallybase64butsecretbody
-----END PRIVATE KEY-----
done"""

    redacted = redact_secret_like_text(text)

    assert "[REDACTED_SECRET]" in redacted
    assert "notreallybase64butsecretbody" not in redacted
    assert "END PRIVATE KEY" not in redacted


def test_redaction_removes_partially_masked_provider_keys_from_error_text():
    masked_key = "sk-" + "a" * 5 + "*" * 23 + "5916"
    text = f"Incorrect API key provided: {masked_key}"

    redacted = redact_secret_like_text(text)

    assert masked_key not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_partially_masked_provider_key_is_rejected():
    masked_key = "sk-" + "a" * 5 + "*" * 23 + "5916"
    result = should_capture_text(f"Incorrect API key provided: {masked_key}")

    assert result.allowed is False
    assert result.reason == "secret-like-content"


def test_secret_index_like_multiline_metadata_is_not_cross_line_rejected():
    result = should_capture_text("Secret index: Scope Recall smoke dummy credential\nKind: api_key\nVault ref: vault://smoke/scope-recall/dummy")

    assert result.allowed is True
    assert result.reason == ""


def test_continuation_handoff_line_is_rejected():
    result = should_capture_text("Conversation continues after context compression. Resume the active task from the summary.")

    assert result.allowed is False
    assert "Conversation continues after context compression" in result.reason


def test_context_compaction_active_task_payload_is_rejected():
    result = should_capture_text("## Active Task\n审计 LanceDB/vector 同步、重复与检索质量\n\n## Remaining Work\n进一步优化内容卫生处理")

    assert result.allowed is False
    assert "Active Task" in result.reason


def test_gateway_interruption_system_note_is_rejected():
    result = should_capture_text(
        "[System note: Your previous turn was interrupted before you could process the last tool result(s). "
        "The conversation history contains tool outputs you haven't responded to yet. Please finish processing those results "
        "and summarize what was accomplished, then address the user's new message below.]\n\n查看凌晨2：40左右的聊天记录"
    )

    assert result.allowed is False
    assert "System note" in result.reason


def test_generic_system_note_wrapper_is_rejected():
    result = should_capture_text("[System note: gateway recovered the prior turn and restored tool outputs.]\n\nContinue normally.")

    assert result.allowed is False
    assert "System note" in result.reason


def test_preserved_active_task_list_wrapper_is_rejected():
    result = should_capture_text("[Your active task list was preserved across context compression]\n- [>] diagnose bug")

    assert result.allowed is False
    assert "active task list" in result.reason


def test_background_process_tool_notification_is_rejected():
    result = should_capture_text(
        "[IMPORTANT: Background process proc_abc123 completed (exit code 0). "
        "Command: GH_PROMPT_DISABLED=1 gh auth refresh Output: First copy your one-time code: ABCD-1234]"
    )

    assert result.allowed is False
    assert "Background process" in result.reason


@pytest.mark.parametrize(
    "text",
    [
        "Understood.",
        "Noted.",
        "Acknowledged.",
        "Done.",
        "明白了。",
        "了解。",
        "好的。",
    ],
)
def test_short_assistant_acknowledgements_are_rejected(text):
    result = should_capture_text(text)

    assert result.allowed is False
    assert result.reason == "trivial"


def test_attachment_markers_are_removed_before_capture_filtering():
    text = """现在要我扫码，我去哪扫啊

[Image attached at: /tmp/hermes-home/image_cache/img_ccf883cb57da.jpg]
[inline image/jpeg data omitted]
[screenshot]"""

    sanitized = sanitize_capture_text(text)
    result = should_capture_text(text)

    assert sanitized == "现在要我扫码，我去哪扫啊"
    assert result.allowed is True
    assert result.reason == ""


def test_inline_attachment_marker_preserves_surrounding_text():
    text = "Question before [Image attached at: /tmp/hermes-home/image_cache/img_ccf883cb57da.jpg] after"

    sanitized = sanitize_capture_text(text)
    result = should_capture_text(text)

    assert sanitized == "Question before after"
    assert "image_cache" not in sanitized
    assert result.allowed is True
    assert result.reason == ""


def test_attachment_only_payload_is_rejected_after_sanitizing():
    text = """[Image attached at: /tmp/hermes-home/image_cache/img_ccf883cb57da.jpg]
[inline image/jpeg data omitted]
[screenshot]"""

    sanitized = sanitize_capture_text(text)
    result = should_capture_text(text)

    assert sanitized == ""
    assert result.allowed is False
    assert result.reason == "empty"


def test_ordinary_memory_fact_is_allowed():
    result = should_capture_text("Joy prefers read-only SQLite viewers for inspecting live memory databases.")

    assert result.allowed is True
    assert result.reason == ""
