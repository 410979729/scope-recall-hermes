"""Tests for Experience promotion quality scoring."""
from __future__ import annotations

from scope_recall.experience_quality import assess_experience_quality


def _entry(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def test_experience_quality_auto_promotes_low_risk_verified_specific_task():
    entries = [
        _entry("user", "检查 scope-recall 文档发布说明。"),
        _entry("tool", "python -m pytest tests/test_release.py -q -> 12 passed; ruff ok; release gate ok"),
        _entry("assistant", "完成：验证通过，可以收口。"),
    ]

    quality = assess_experience_quality(
        entries,
        goal="检查 scope-recall 文档发布说明。",
        tool_names=["pytest", "ruff", "release gate"],
        verification=["测试结果显示通过。", "代码静态检查通过。", "发布检查通过。"],
        risk_level="low",
    )

    assert quality["decision"] == "auto_promote_eligible"
    assert quality["score"] >= 0.85
    assert quality["specificity_hits"] >= 3
    assert quality["tool_entry_count"] == 1


def test_experience_quality_rejects_final_failure_even_with_historical_success():
    entries = [
        _entry("user", "修复 Scope Recall 测试。"),
        _entry("tool", "pytest 5 passed"),
        _entry("assistant", "后来发现仍有问题，pyright failed，不能沉淀。"),
    ]

    quality = assess_experience_quality(
        entries,
        goal="修复 Scope Recall 测试。",
        tool_names=["pytest", "pyright"],
        verification=["测试结果显示通过。"],
        risk_level="low",
    )

    assert quality["decision"] == "reject"
    assert quality["score"] <= 0.35
    assert "final_failure_signal" in quality["reasons"]


def test_experience_quality_keeps_high_risk_without_authorization_in_review():
    entries = [
        _entry("user", "发布并推送仓库。"),
        _entry("tool", "pytest 12 passed; release gate ok"),
        _entry("assistant", "完成：检查通过。"),
    ]

    quality = assess_experience_quality(
        entries,
        goal="发布并推送仓库。",
        tool_names=["pytest", "release gate"],
        verification=["测试结果显示通过。"],
        risk_level="high",
    )

    assert quality["decision"] in {"needs_review", "reject"}
    assert "high_risk_without_authorization_boundary" in quality["reasons"]


def test_experience_quality_counts_windows_paths_as_specific_evidence():
    entries = [
        _entry("user", "检查 Windows 上的 Codex 配置。"),
        _entry("tool", r"Get-Content C:\Users\Administrator\.codex\config.toml; pytest 10 passed"),
        _entry("assistant", "完成：验证通过。"),
    ]

    quality = assess_experience_quality(
        entries,
        goal="检查 Windows 上的 Codex 配置。",
        tool_names=["powershell", "pytest"],
        verification=["pytest 10 passed"],
        risk_level="low",
    )

    assert quality["specificity_hits"] >= 3
    assert quality["decision"] == "auto_promote_eligible"
