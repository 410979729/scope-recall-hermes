"""Tests for sanitized Experience evidence anchors."""
from __future__ import annotations

from scope_recall.experience_evidence import evidence_anchor_for_entry, extract_evidence_anchors


def _entry(entry_id: int, role: str, content: str, session_id: str = "session-a") -> dict[str, object]:
    return {"id": entry_id, "role": role, "content": content, "session_id": session_id}


def test_evidence_anchor_classifies_and_sanitizes_tool_output():
    anchor = evidence_anchor_for_entry(
        _entry(12, "tool", "python3 -m pytest tests/test_release.py -q -> 12 passed; wrote /home/a/private/output.log; token ghp_abcdefghijklmnopqrst1234567890ABCD")
    )

    assert anchor["kind"] == "test_command"
    assert anchor["role"] == "tool"
    assert anchor["journal_entry_id"] == 12
    assert "pytest" in anchor["summary"]
    assert "/home/a" not in anchor["summary"]
    assert "ghp_" not in anchor["summary"]
    assert "[REDACTED_PATH]" in anchor["summary"]
    assert "[REDACTED_SECRET]" in anchor["summary"]


def test_extract_evidence_anchors_is_bounded_deduped_and_keeps_closure():
    entries = [
        _entry(1, "user", "检查 scope-recall release gate。"),
        _entry(2, "tool", "python3 -m pytest tests/test_release.py -q -> 12 passed"),
        _entry(3, "tool", "python3 -m pytest tests/test_release.py -q -> 12 passed"),
        _entry(4, "tool", "git status --short clean"),
        _entry(5, "assistant", "完成：验证通过，可以收口。"),
        _entry(6, "assistant", "我继续观察下一项。"),
    ]

    anchors = extract_evidence_anchors(entries, limit=4)

    assert [anchor["kind"] for anchor in anchors] == ["user_statement", "test_command", "repo_state", "health_report"]
    assert [anchor["journal_entry_id"] for anchor in anchors] == [1, 2, 4, 5]
    assert len(anchors) == 4


def test_extract_evidence_anchors_omits_uninformative_non_user_observations():
    entries = [
        _entry(1, "assistant", "我继续看一下。"),
        _entry(2, "tool", "plain log without verification signal"),
        _entry(3, "user", "继续处理。"),
    ]

    anchors = extract_evidence_anchors(entries)

    assert anchors == [
        {"kind": "user_statement", "role": "user", "summary": "继续处理。", "journal_entry_id": 3, "session_id": "session-a"}
    ]
