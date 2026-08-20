"""Unique reuse for recall scoring defaults, freshness hints, and CJK scope tokens.

Orchestrator and ``RecallService`` helpers must import these values from here.
Do not copy the constants back into ``recall.py``.
"""

from __future__ import annotations

import re

FRESHNESS_HINTS = {
    "current",
    "currently",
    "latest",
    "new",
    "newest",
    "now",
    "recent",
    "recently",
    "today",
    "updated",
    "当前",
    "目前",
    "现在",
    "如今",
    "最新",
}

FRESHNESS_BASE_WEIGHT = 0.03
FRESHNESS_STEP_WEIGHT = 0.015
FRESHNESS_MAX_WEIGHT = 0.06
FRESHNESS_ABSOLUTE_BONUS_CAP = 0.06
FRESHNESS_RELATIVE_BONUS_RATIO = 0.12

TEMPORAL_DURABLE_TYPES = {
    "constraint",
    "decision",
    "environment_fact",
    "fact",
    "factual",
    "memory",
    "ops",
    "ops_procedure",
    "preference",
    "procedure",
    "project",
    "project_fact",
    "resource",
    "user_preference",
    "workflow",
}
TEMPORAL_EPISODIC_TYPES = {"episodic", "summary"}
TEMPORAL_TEMPORARY_TYPES = {"scratch", "temporary", "temporary_state", "tool_trace"}

DEFAULT_MIN_SCORE = 0.18
DEFAULT_VECTOR_ONLY_MIN_SCORE = 0.70
INTENT_UNMATCHED_BM25_FACTOR = 0.25
DEFAULT_METADATA_WEIGHT = 0.08
DEFAULT_ENTITY_WEIGHT = 0.06
DEFAULT_ENTITY_DISTANCE_WEIGHT = 0.04

ENTITY_SCOPE_STOPWORDS = {
    "anchors",
    "api",
    "always",
    "artifact",
    "base",
    "how",
    "is",
    "url",
    "uri",
    "current",
    "latest",
    "like",
    "likes",
    "our",
    "prod",
    "project",
    "response",
    "recovery",
    "releases",
    "rollout",
    "style",
    "tell",
    "what",
    "when",
    "where",
    "which",
    "who",
    "deploy",
    "deployment",
    "rollback",
    "run",
    "runbook",
    "command",
    "production",
    "procedure",
    "worker",
    "queue",
    "drain",
    "server",
    "service",
    "services",
    "systemctl",
    "memory",
    "scope-recall",
    "scope",
    "recall",
    "use",
}

CJK_SCOPE_QUERY_SUBJECT_RE = re.compile(
    r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,8}?)"
    r"(?=(?:现在|目前|当前|最近|在哪里|在哪|哪儿|何处|的|是什么|怎么|如何|是否|有没有))"
)
CJK_SCOPE_PRONOUNS = {"我们", "你们", "他们", "她们", "它们", "大家", "自己"}
CJK_SCOPE_QUERY_PREFIXES = (
    "麻烦请告诉我",
    "麻烦告诉我",
    "可以告诉我",
    "请告诉我",
    "我想知道",
    "帮我查一下",
    "能告诉我",
    "帮我看看",
    "告诉我",
    "帮我查",
    "请问",
)
SHORT_CJK_NAME = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,6}$")


def is_short_cjk_name(value: str) -> bool:
    """Return whether a declared entity looks like a short CJK proper name."""

    return bool(SHORT_CJK_NAME.fullmatch(str(value or "").strip()))


def normalize_cjk_scope_subject(value: str) -> str:
    """Remove conversational prefixes without weakening the named subject."""

    subject = str(value or "").strip()
    for prefix in CJK_SCOPE_QUERY_PREFIXES:
        if subject.startswith(prefix) and len(subject) - len(prefix) >= 2:
            return subject[len(prefix) :]
    return subject
