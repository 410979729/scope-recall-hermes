"""Quality gates for deciding whether journal or session digest output is durable enough to promote.

The rules reject transcript-shaped, generic, or low-signal summaries before they can become long-lived memory."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

TRANSIENT_PROGRESS_RE = re.compile(
    r"(进度(?:如何|更新|汇报)?\s*#?\d*|停服维护进度|用了哪些工具|使用了哪些工具|工具流水|本轮(?:进度|操作)|当前进度|progress update\s*#?\d*)",
    re.IGNORECASE,
)
RAW_TOOL_TRACE_RE = re.compile(
    r"(\btool_calls\b|\btool_call_id\b|\"output\"\s*:|Traceback \(most recent call last\)|\[OUT-OF-BAND|call_[A-Za-z0-9]{8,}|/tmp/hermes|image_cache/img_|\.pytest_cache|__pycache__)",
    re.IGNORECASE,
)
TRIGGER_RE = re.compile(
    r"(触发|当|如果|遇到|流程|步骤|修复|验证|发布|部署|审计|排障|任务|问题|报错|bug|release|deploy|debug|scope-recall|journal|digest)",
    re.IGNORECASE,
)
VERIFICATION_RE = re.compile(
    r"(\d+\s+passed|release gate ok|pyright.*0 errors|ruff.*passed|ok=true|验证(?:通过|命令|结果)|实测|smoke|doctor.*ok|pytest)",
    re.IGNORECASE,
)
STEP_RE = re.compile(r"(步骤|先.*再|1[.)、]|2[.)、]|坑点|注意|回滚|清理|验证)", re.IGNORECASE)
TOOLCHAIN_ONLY_RE = re.compile(r"(使用工具链|tool-chain|tools_used)", re.IGNORECASE)


@dataclass(frozen=True)
class DigestQuality:
    reusable: bool
    transient_progress: bool
    has_trigger: bool
    has_verification: bool
    contains_raw_tool_trace: bool
    recommended_action: str
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _candidate_text(candidate: Any) -> str:
    parts = [str(getattr(candidate, "content", "") or ""), str(getattr(candidate, "reason", "") or "")]
    commands = getattr(candidate, "commands", []) or []
    verification = getattr(candidate, "verification", []) or []
    if isinstance(commands, list):
        parts.extend(str(item) for item in commands[:8])
    if isinstance(verification, list):
        parts.extend(str(item) for item in verification[:8])
    return "\n".join(part for part in parts if part)


def score_digest_candidate(candidate: Any) -> DigestQuality:
    text = _candidate_text(candidate)
    memory_type = str(getattr(candidate, "memory_type", "") or "").strip().lower()
    confidence = float(getattr(candidate, "confidence", 0.0) or 0.0)
    verification = getattr(candidate, "verification", []) or []
    commands = getattr(candidate, "commands", []) or []

    transient_progress = bool(TRANSIENT_PROGRESS_RE.search(text))
    contains_raw_tool_trace = bool(RAW_TOOL_TRACE_RE.search(text))
    has_trigger = bool(TRIGGER_RE.search(text))
    has_verification = bool(verification) or bool(VERIFICATION_RE.search(text))
    has_steps = bool(STEP_RE.search(text)) or bool(commands)
    reusable = bool(has_trigger and (has_verification or has_steps) and not transient_progress and not contains_raw_tool_trace)

    reasons: list[str] = []
    if transient_progress:
        reasons.append("transient_progress")
    if contains_raw_tool_trace:
        reasons.append("raw_tool_trace")
    if has_trigger:
        reasons.append("has_trigger")
    if has_verification:
        reasons.append("has_verification")
    if has_steps:
        reasons.append("has_steps")

    if contains_raw_tool_trace:
        recommended_action = "reject"
        reasons.append("reject_raw_tool_trace")
    elif transient_progress:
        recommended_action = "reject"
        reasons.append("reject_transient_progress")
    elif TOOLCHAIN_ONLY_RE.search(text) and not has_verification:
        recommended_action = "reject"
        reasons.append("reject_toolchain_without_verification")
    elif memory_type in {"workflow", "procedure", "pitfall"} and reusable:
        recommended_action = "promote"
    elif memory_type == "summary" or confidence < 0.55:
        recommended_action = "candidate"
        reasons.append("needs_review_or_consolidation")
    else:
        recommended_action = "promote" if reusable or has_verification else "candidate"
        if recommended_action == "candidate":
            reasons.append("insufficient_reuse_evidence")

    return DigestQuality(
        reusable=reusable,
        transient_progress=transient_progress,
        has_trigger=has_trigger,
        has_verification=has_verification,
        contains_raw_tool_trace=contains_raw_tool_trace,
        recommended_action=recommended_action,
        reasons=reasons,
    )
