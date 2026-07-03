"""Quality scoring for automatically synthesized Experience playbooks.

This module is intentionally read-only and storage-free. It answers whether a
closed task episode has enough goal, tool, verification, specificity, and risk
boundary evidence to become a reviewable playbook candidate.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .task_boundary import goal_signal_key, has_failure_signal, is_low_signal_goal, tail_text

SPECIFICITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpython(?:3)?\s+-m\s+pytest\b", re.IGNORECASE),
    re.compile(r"\bpython\s+-m\s+pytest\b", re.IGNORECASE),
    re.compile(r"\bpytest\b", re.IGNORECASE),
    re.compile(r"\b(?:ruff|pyright|doctor|release gate|systemctl|git|gh)\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)(?:scripts|tests|docs|\.github)/[^\s`'\"]+", re.IGNORECASE),
    re.compile(r"(?:^|\s)/(?:home|data|etc|var|tmp)/[^\s`'\"]+"),
    re.compile(r"(?:^|\s)[A-Za-z]:[\\/][^\s`'\"]+", re.IGNORECASE),
    re.compile(r"\b(?:powershell|pwsh|Get-Content|Set-Content|Test-Path)\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+passed\b", re.IGNORECASE),
    re.compile(r"\bok=true\b", re.IGNORECASE),
)


def _entry_value(entry: Any, key: str, default: Any = "") -> Any:
    try:
        return entry[key]  # type: ignore[index]
    except Exception:
        if isinstance(entry, Mapping):
            return entry.get(key, default)
        return getattr(entry, key, default)


def entry_text(entries: Sequence[Any]) -> str:
    return "\n".join(str(_entry_value(entry, "content", "") or "") for entry in entries)


def _contains_any(text: str, tokens: Sequence[str]) -> bool:
    lowered = str(text or "").lower()
    return any(token.lower() in lowered for token in tokens)


def assess_experience_quality(
    entries: Sequence[Any],
    *,
    goal: str,
    tool_names: Sequence[str],
    verification: Sequence[str],
    risk_level: str,
) -> dict[str, Any]:
    """Score whether an episode is safe enough to synthesize/promote.

    Return shape is stable for dashboards and tests:
    `score`, `decision`, `reasons`, `specificity_hits`, and `tool_entry_count`.
    """
    text = entry_text(entries)
    reasons: list[str] = []
    score = 0.0
    normalized_goal = goal_signal_key(goal or "")
    if goal and not is_low_signal_goal(goal) and len(normalized_goal) >= 4:
        score += 0.18
    else:
        reasons.append("weak_goal")
    tool_entry_count = sum(1 for entry in entries if str(_entry_value(entry, "role", "") or "") == "tool")
    concrete_tools = [name for name in tool_names if str(name) != "tool"]
    if tool_entry_count:
        score += 0.12
    else:
        reasons.append("no_tool_evidence")
    if concrete_tools:
        score += 0.12
    else:
        reasons.append("no_concrete_tool_names")
    if verification:
        score += min(0.24, 0.12 + 0.04 * len(verification))
    else:
        reasons.append("no_verification_evidence")
    specificity_hits = sum(1 for pattern in SPECIFICITY_PATTERNS if pattern.search(text))
    if specificity_hits:
        score += min(0.18, 0.08 + 0.04 * specificity_hits)
    else:
        reasons.append("no_specific_commands_or_paths")
    if len(entries) <= 40:
        score += 0.08
    else:
        reasons.append("oversized_episode_window")
    normalized_risk_level = str(risk_level or "low").strip().lower()
    if normalized_risk_level in {"high", "secret"}:
        if _contains_any(text, ("授权", "authorization", "confirm", "确认", "不能自动", "等待")):
            score += 0.08
        else:
            reasons.append("high_risk_without_authorization_boundary")
    elif normalized_risk_level == "low":
        score += 0.08
    if has_failure_signal(tail_text(entries)):
        score = min(score, 0.35)
        reasons.append("final_failure_signal")
    score = round(min(score, 1.0), 3)
    if score < 0.70:
        decision = "reject"
    elif score < 0.85 or normalized_risk_level in {"high", "secret"}:
        decision = "needs_review"
    else:
        decision = "auto_promote_eligible"
    return {"score": score, "decision": decision, "reasons": reasons, "specificity_hits": specificity_hits, "tool_entry_count": tool_entry_count}
