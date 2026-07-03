"""Tests for task-boundary closure classification.

These tests ensure Experience promotion trusts final task closure instead of
isolated historical success tokens.
"""
from __future__ import annotations

from scope_recall.task_boundary import classify_task_closure, extract_final_evidence, is_low_signal_goal


def _entry(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def test_task_closure_final_failure_overrides_historical_success_tokens():
    entries = [
        _entry("user", "修复 scope-recall 测试。"),
        _entry("tool", "python3 -m pytest tests/test_x.py -q -> 5 passed"),
        _entry("assistant", "后来发现仍有问题，pyright failed，不能沉淀。"),
    ]

    closure = classify_task_closure(entries)

    assert closure.state == "failed"
    assert closure.reason == "final_failure_signal"


def test_task_closure_success_requires_final_tail_signal():
    entries = [
        _entry("user", "检查 release gate。"),
        _entry("tool", "python3 -m pytest tests/test_release.py -q -> 12 passed"),
        _entry("assistant", "完成：验证通过，可以收口。"),
    ]

    closure = classify_task_closure(entries)

    assert closure.state == "success"
    assert closure.reason == "final_success_signal"
    assert any("pytest" in item for item in closure.final_evidence)


def test_task_closure_historical_success_without_final_closure_is_uncertain():
    entries = [
        _entry("user", "继续处理。"),
        _entry("tool", "pytest 7 passed"),
        _entry("assistant", "我继续看下一项。"),
    ]

    closure = classify_task_closure(entries)

    assert closure.state == "uncertain"
    assert closure.reason == "success_not_final"


def test_task_closure_local_success_but_pending_closeout_is_uncertain():
    examples = [
        "The test output is green locally; still reviewing packaging before closeout.",
        "pytest passed; release gate pending.",
        "fixed locally but not deployed.",
    ]

    for text in examples:
        closure = classify_task_closure([_entry("user", "修复并收口。"), _entry("assistant", text)])
        assert closure.state == "uncertain"
        assert closure.reason == "final_uncertain_signal"


def test_task_closure_chinese_negative_failure_context_counts_as_success():
    examples = [
        "完成：验证通过，未发现阻塞问题。",
        "完成：测试通过，无失败项。",
        "完成：验证通过，0 个阻塞问题。",
        "完成：未发现任何阻塞或失败问题。",
        "完成：没有阻塞、失败、报错。",
    ]

    for text in examples:
        closure = classify_task_closure([_entry("user", "修复并收口。"), _entry("assistant", text)])
        assert closure.state == "success"
        assert closure.reason == "final_success_signal"

    blocked = classify_task_closure([_entry("user", "修复并收口。"), _entry("assistant", "仍有阻塞问题，不能收口。")])
    assert blocked.state == "failed"
    assert blocked.reason == "final_failure_signal"


def test_task_closure_english_negative_failure_context_is_not_failed():
    examples = [
        ("pyright: 0 errors, 1 warning", {"unknown", "success", "uncertain"}),
        ("doctor: ok=true, failed checks: []", {"success"}),
        ("No failures found; release gate passed", {"success"}),
        ("No blocking failures. pytest 12 passed", {"success"}),
    ]

    for text, allowed_states in examples:
        closure = classify_task_closure([_entry("user", "修复并收口。"), _entry("assistant", text)])
        assert closure.state in allowed_states
        assert closure.state != "failed"


def test_low_signal_goal_detection_matches_experience_policy():
    assert is_low_signal_goal("继续") is True
    assert is_low_signal_goal("进度如何了") is True
    assert is_low_signal_goal("只回答 ok") is True
    assert is_low_signal_goal("修复 scope-recall doctor backlog 指标") is False


def test_extract_final_evidence_keeps_verification_tail_only():
    entries = [
        _entry("tool", "irrelevant early output"),
        _entry("tool", "ruff ok"),
        _entry("assistant", "验证通过。"),
    ]

    evidence = extract_final_evidence(entries)

    assert evidence == ("ruff ok", "验证通过。")


def test_extract_final_evidence_ignores_failed_verification_lines():
    entries = [
        _entry("tool", "pytest -> 5 failed"),
        _entry("assistant", "不能沉淀，仍有失败测试。"),
    ]

    evidence = extract_final_evidence(entries)
    closure = classify_task_closure(entries)

    assert evidence == ()
    assert closure.state == "failed"
    assert closure.reason == "final_failure_signal"
