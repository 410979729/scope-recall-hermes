"""Tests for Experience playbook synthesis payloads."""
from __future__ import annotations

from scope_recall.experience_models import validate_procedural_playbook
from scope_recall.experience_synthesis import build_experience_playbook_payload


def test_synthesized_playbook_contains_weak_model_operational_sections():
    anchors = [
        {"kind": "user_statement", "journal_entry_id": 1, "summary": "检查 scope-recall release gate。"},
        {"kind": "test_command", "journal_entry_id": 2, "summary": "pytest 12 passed"},
        {"kind": "assistant_closure", "journal_entry_id": 3, "summary": "完成：验证通过。"},
    ]

    payload = build_experience_playbook_payload(
        task_class="scope_recall_release_closeout",
        title="scope-recall：发布收口",
        goal="检查 scope-recall release gate。",
        risk_level="low",
        tool_names=["pytest", "release gate"],
        verification=["测试结果显示通过。", "发布检查通过。"],
        evidence_anchors=anchors,
    )
    playbook = validate_procedural_playbook(payload)

    assert playbook.task_class == "scope_recall_release_closeout"
    assert len(playbook.preconditions) >= 3
    assert len(playbook.steps) == 3
    assert playbook.verification == ("测试结果显示通过。", "发布检查通过。")
    assert playbook.cleanup
    assert payload["reuse_policy"]["source_evidence_anchor_count"] == 3
    assert payload["reuse_policy"]["source_evidence_anchor_kinds"] == ["user_statement", "test_command", "assistant_closure"]
    assert payload["reuse_policy"]["allow_direct_reuse"] is True
    assert any("Joy" in item for item in payload["reuse_policy"]["must_stop_and_ask_joy"])


def test_synthesized_high_risk_playbook_forbids_automatic_mutation():
    payload = build_experience_playbook_payload(
        task_class="github_release_publish",
        title="GitHub：release 发布核验",
        goal="发布并推送仓库。",
        risk_level="high",
        tool_names=["pytest", "gh"],
        verification=["发布检查通过。"],
        evidence_anchors=[{"kind": "repo_state", "journal_entry_id": 2, "summary": "release gate ok"}],
    )
    playbook = validate_procedural_playbook(payload)

    assert playbook.reuse_policy["default_decision"] == "guided_reuse"
    assert playbook.reuse_policy["allow_direct_reuse"] is False
    prohibited = "\n".join(playbook.reuse_policy["prohibited_auto_actions"])
    assert "不得自动执行" in prohibited
    assert any(step.capability_class == "local_write" for step in playbook.steps)
    assert any("Joy" in item for item in playbook.reuse_policy["must_stop_and_ask_joy"])


def test_synthesized_playbook_redacts_private_goal_and_treats_secret_risk_as_guided():
    private_goal = "检查 " + "/".join(["", "home", "a", ".hermes-yuheng", "plugins", "scope-recall"]) + " 并处理 token 相邻问题。"
    payload = build_experience_playbook_payload(
        task_class="hermes_operations",
        title="Hermes：配置核验",
        goal=private_goal,
        risk_level="secret",
        tool_names=["read_file"],
        verification=["验证通过。"],
        evidence_anchors=[{"kind": "tool_command", "journal_entry_id": 2, "summary": "read_file ok"}],
    )
    playbook = validate_procedural_playbook(payload)
    serialized = str(payload)

    assert "/home/a" not in serialized
    assert "[REDACTED_PATH]" in serialized
    assert playbook.reuse_policy["default_decision"] == "guided_reuse"
    assert playbook.reuse_policy["allow_direct_reuse"] is False
    assert any("凭据" in item or "secret" in item.lower() for item in playbook.reuse_policy["must_stop_and_ask_joy"])


def test_synthesized_playbook_marks_missing_verification_for_review():
    payload = build_experience_playbook_payload(
        task_class="scope_recall_release_closeout",
        title="scope-recall：发布收口",
        goal="检查 scope-recall release gate。",
        risk_level="low",
        tool_names=["pytest"],
        verification=[],
        evidence_anchors=[{"kind": "assistant_closure", "journal_entry_id": 3, "summary": "完成但缺少验证输出。"}],
    )
    playbook = validate_procedural_playbook(payload)

    assert playbook.status == "needs_review"
    assert playbook.reuse_policy["default_decision"] == "guided_reuse"
    assert playbook.reuse_policy["allow_direct_reuse"] is False
    assert playbook.verification == ("verification_missing_requires_review",)
    assert any("缺少验证" in item for item in playbook.reuse_policy["must_stop_and_ask_joy"])
