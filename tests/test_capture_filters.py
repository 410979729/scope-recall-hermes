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


def test_secret_assignment_with_is_is_rejected():
    result = should_capture_text("The token is abcdefghijklmnopqrstuvwxyz should not be retained.")

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


def test_ordinary_memory_fact_is_allowed():
    result = should_capture_text("Joy prefers read-only SQLite viewers for inspecting live memory databases.")

    assert result.allowed is True
    assert result.reason == ""
