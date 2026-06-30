"""Heuristics for classifying whether text is a reusable Experience playbook candidate.

The classifier favors precision over recall so generic chat commands and transient status updates do not pollute procedural memory."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExperienceClassification:
    task_class: str
    title: str
    domain: str
    reusable_action: str
    matched_rule: str


_WORD_RE = re.compile(r"[\w\u4e00-\u9fff-]+", re.UNICODE)


def _norm(text: str) -> str:
    return text.lower()


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = _norm(text)
    return any(token.lower() in lowered for token in tokens)


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


SCOPE_RECALL_TOKENS = ("scope-recall", "scope recall", "scoperecall")
DOC_TOKENS = ("docs", "doc", "readme", "documentation", "文档", "发布说明", "链接")
RELEASE_TOKENS = ("release", "publish", "pypi", "push", "version", "tag", "发布", "推送", "版本", "发版")
JOURNAL_TOKENS = ("journal", "backlog", "digest", "dead-letter", "dead letter", "recovery", "日志", "积压", "摘要", "恢复", "死信")
MEMORY_QUALITY_TOKENS = ("memory quality", "lint", "candidate", "promotion", "promote", "质量", "候选", "治理", "记忆")
HERMES_TOKENS = ("hermes", "gateway", "telegram", "discord", "systemd", "profile", "provider")
GITHUB_RELEASE_TOKENS = ("github", "gh ", "pull request", " pr", "tag", "pypi", "release")


def classify_experience_task(*, text: str, goal: str = "") -> ExperienceClassification:
    """Classify an extracted task into stable reusable playbook identity.

    Titles are intentionally template-based (`<domain>：<reusable action>`) and
    must not echo raw user wording such as "继续昨天的" or "进度如何". The raw user
    phrasing belongs in the playbook trigger/evidence, not in title identity.
    """

    combined = f"{goal}\n{text}"
    if _has_any(combined, SCOPE_RECALL_TOKENS):
        if _has_any(combined, JOURNAL_TOKENS):
            return ExperienceClassification(
                task_class="journal_backlog_drain",
                title="scope-recall：journal backlog 清理",
                domain="scope-recall",
                reusable_action="journal backlog 清理",
                matched_rule="scope_recall_journal",
            )
        if _has_any(combined, DOC_TOKENS):
            return ExperienceClassification(
                task_class="scope_recall_docs_quality",
                title="scope-recall：文档质量检查",
                domain="scope-recall",
                reusable_action="文档质量检查",
                matched_rule="scope_recall_docs",
            )
        if _has_any(combined, RELEASE_TOKENS):
            return ExperienceClassification(
                task_class="scope_recall_release_closeout",
                title="scope-recall：发布收口",
                domain="scope-recall",
                reusable_action="发布收口",
                matched_rule="scope_recall_release",
            )
        if _has_any(combined, MEMORY_QUALITY_TOKENS):
            return ExperienceClassification(
                task_class="scope_recall_memory_quality_governance",
                title="scope-recall：记忆质量治理",
                domain="scope-recall",
                reusable_action="记忆质量治理",
                matched_rule="scope_recall_memory_quality",
            )
        return ExperienceClassification(
            task_class="scope_recall_quality_check",
            title="scope-recall：质量检查",
            domain="scope-recall",
            reusable_action="质量检查",
            matched_rule="scope_recall_general",
        )
    if _has_any(combined, GITHUB_RELEASE_TOKENS) and _has_any(combined, RELEASE_TOKENS):
        return ExperienceClassification(
            task_class="github_release_publish",
            title="GitHub：release 发布核验",
            domain="GitHub",
            reusable_action="release 发布核验",
            matched_rule="github_release",
        )
    if _has_any(combined, HERMES_TOKENS):
        return ExperienceClassification(
            task_class="hermes_operations",
            title="Hermes：运行维护",
            domain="Hermes",
            reusable_action="运行维护",
            matched_rule="hermes_operations",
        )
    if _word_count(goal) <= 2 and _word_count(text) <= 12:
        return ExperienceClassification(
            task_class="agent_verified_task",
            title="Agent：已验证任务流程",
            domain="Agent",
            reusable_action="已验证任务流程",
            matched_rule="generic_low_context",
        )
    return ExperienceClassification(
        task_class="agent_verified_task",
        title="Agent：已验证任务流程",
        domain="Agent",
        reusable_action="已验证任务流程",
        matched_rule="generic_verified_task",
    )
