"""Pure journal-digest admission policy. No SQL, no apply, no LLM."""

from __future__ import annotations

import re

from ...capture_filters import should_capture_text
from ...gating import clean_text
from ...journal_candidates import JournalDigestCandidate, _looks_like_historical_template_noise

JOURNAL_TARGETS = {"user", "memory", "project", "ops", "general"}



LOW_VALUE_NOTIFICATION_RE = re.compile(
    r"\b(?:webhook|web\s+hook|bot\s+(?:push|message|status)|notification|push\s+message|"
    r"sign[-\s]?in|check[-\s]?in|subscription|subscribed|unsubscribe|qas)\b|"
    r"(?:通知|推送|机器人消息|签到|签入|登录提醒|订阅(?:更新|通知)?)",
    re.IGNORECASE,
)
LOW_VALUE_LOG_RE = re.compile(
    r"\b(?:docker\s+logs?|journalctl|kubectl\s+logs?|stack\s+trace|traceback|stderr|stdout|"
    r"shell\s+(?:prompt|output)|terminal\s+output|command\s+output|tool\s+(?:execution\s+)?summary|tool\s+result)\b|"
    r"(?:工具执行摘要|工具结果|命令输出|终端输出|日志输出|堆栈|调用栈)",
    re.IGNORECASE,
)
LOW_VALUE_PROGRESS_RE = re.compile(
    r"\b(?:backup\s+path|temporary\s+file|run\s+result|task\s+progress|no\s+action\s+required|"
    r"one[-\s]?off|status\s+update)\b|(?:临时文件|备份路径|任务进度|一次性|无需处理|状态更新)",
    re.IGNORECASE,
)
EPHEMERAL_RELEASE_STATE_RE = re.compile(
    r"(?:session\s+[`'\"]?\d{8}_[0-9a-f_]+|\bHEAD\s*=|\borigin/main\b|\bgit\s+status\b|"
    r"\b(?:pushed|local|closed|open)\s+(?:commits?|issues?)\b|\bissue\s*#?\d+\b|#\d+\s+`|"
    r"\b(?:commit|tag|branch)\s+[`'\"]?[0-9a-f]{7,40}\b|\b[0-9a-f]{7,40}\b.*\b(?:commit|HEAD|origin/main)\b|"
    r"\b\d+\s+passed\b|\bpyright\b.*\b(?:warning|error)s?\b|\bruff\s+(?:pass|passed|全通过)\b|"
    r"(?:未\s*commit|未\s*push|未\s*tag|不\s*tag|不\s*release|已关闭\s*issue|记录时状态|发布候选|当前进度))",
    re.IGNORECASE,
)
TRANSIENT_PHASE_GATE_RE = re.compile(
    r"(?:当前阶段|这个阶段|现阶段|下一步|继续下一步|不要急着|先(?:进行)?阶段性?验证|先验证|再进(?:入)?\s*[A-Z]\d|进入\s*[A-Z]\d|"
    r"阶段性验收|全量\s*pytest|live\s+doctor|rollout\s+profiles\s+dry-run|可选复审|"
    r"current\s+phase|next\s+step|phase[-\s]?gate|before\s+entering\s+[A-Z]\d|run\s+full\s+pytest|live\s+doctor)",
    re.IGNORECASE,
)
# Bare should/must/requires/必须/应该 must not promote tool_trace or log
# candidates. Keep verified/rollback/guardrail and real preference/constraint
# tokens, matching digest_state pre-extractor HIGH_VALUE.
HIGH_VALUE_DURABLE_SIGNAL_RE = re.compile(
    r"\b(?:preference|prefers|constraint|policy|api\s+boundary|environment\s+fact|"
    r"root\s+cause|fix|workaround|verification|verified|reusable|workflow|procedure|"
    r"runbook|pitfall|design\s+decision|stable|rollback|guardrail)\b|"
    r"(?:偏好|约束|边界|环境事实|根因|修复|验证|可复用|流程|步骤|规程|坑|设计决策|稳定|回滚|防护)",
    re.IGNORECASE,
)


def _has_high_value_durable_signal(text: str) -> bool:
    return bool(HIGH_VALUE_DURABLE_SIGNAL_RE.search(text or ""))


def _low_value_promotion_reason(candidate: JournalDigestCandidate) -> str:
    """Return a rejection reason for obvious journal-digest promotion noise.

    Capture filters protect raw journal ingestion, but an LLM digest can rephrase
    webhook/log/tool noise into a plausible durable fact.  This second gate is
    intentionally conservative: only obvious notification/log/progress shapes are
    blocked, and root-cause/fix/workflow/preference/constraint signals still pass.
    """
    text = clean_text(candidate.content)
    if not text:
        return "low-value-empty"
    if "[REDACTED_PATH]" in text or "Artifact anchors:" in text or "artifact anchors:" in text:
        return "low-value-redacted-path-or-artifact-anchor"
    if candidate.target == "project" and re.search(r"(?:当前系统现状|当前技术现状|当前系统状态|当前状态|技术债务|current status|current state|technical debt)", text, re.IGNORECASE):
        return "low-value-stale-status-snapshot"
    has_value_signal = _has_high_value_durable_signal(text)
    if candidate.memory_type == "tool_trace" and not has_value_signal:
        return "low-value-tool-trace"
    tag_set = {str(tag).strip().lower() for tag in candidate.tags or []}
    if TRANSIENT_PHASE_GATE_RE.search(text) and (
        candidate.memory_type in {"decision", "summary", "workflow"}
        or candidate.target == "project"
        or tag_set & {"phase-gate", "project-management", "status", "progress"}
    ):
        return "low-value-transient-phase-gate"
    stable_release_knowledge = has_value_signal and candidate.memory_type in {"constraint", "pitfall", "procedure", "workflow"} and candidate.target != "project"
    if EPHEMERAL_RELEASE_STATE_RE.search(text) and not stable_release_knowledge:
        return "low-value-ephemeral-release-or-issue-state"
    if LOW_VALUE_NOTIFICATION_RE.search(text) and not has_value_signal:
        return "low-value-notification"
    if LOW_VALUE_LOG_RE.search(text) and not has_value_signal:
        return "low-value-log-or-tool-summary"
    if LOW_VALUE_PROGRESS_RE.search(text) and not has_value_signal:
        return "low-value-progress"
    return ""

def _candidate_rejection_reason(candidate: JournalDigestCandidate) -> str:
    if candidate.target not in JOURNAL_TARGETS:
        return "invalid-target"
    if len(candidate.content) < 40:
        return "too-short"
    if _looks_like_historical_template_noise(candidate.content):
        return "historical-template-noise"
    lowered = candidate.content.lower()
    if "operations workflow summary from journal digest:" in lowered or "journal digest memory" in lowered:
        return "historical-template-noise"
    capture_result = should_capture_text(candidate.content)
    if not capture_result.allowed:
        return f"capture-filter:{capture_result.reason or 'blocked'}"
    low_value_reason = _low_value_promotion_reason(candidate)
    if low_value_reason:
        return low_value_reason
    return ""


def _candidate_allowed(candidate: JournalDigestCandidate) -> bool:
    return not _candidate_rejection_reason(candidate)
