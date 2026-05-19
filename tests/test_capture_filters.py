from __future__ import annotations

from scope_recall.capture_filters import should_capture_text


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


def test_secret_like_text_is_rejected():
    result = should_capture_text("The credential_placeholder = *** should not be retained.")

    assert result.allowed is False
    assert result.reason == "secret-like-content"


def test_continuation_handoff_line_is_rejected():
    result = should_capture_text("Conversation continues after context compression. Resume the active task from the summary.")

    assert result.allowed is False
    assert "Conversation continues after context compression" in result.reason


def test_context_compaction_active_task_payload_is_rejected():
    result = should_capture_text("## Active Task\n审计 LanceDB/vector 同步、重复与检索质量\n\n## Remaining Work\n进一步优化内容卫生处理")

    assert result.allowed is False
    assert "Active Task" in result.reason


def test_ordinary_memory_fact_is_allowed():
    result = should_capture_text("Joy prefers read-only SQLite viewers for inspecting live memory databases.")

    assert result.allowed is True
    assert result.reason == ""
