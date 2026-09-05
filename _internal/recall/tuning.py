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
CJK_PROJECT_IDENTITY_SEEKING_RE = re.compile(
    r"(?:项目|作品|短剧|短片)(?:名叫什么|叫什么|是什么名字|是什么|名称|叫啥)|"
    r"叫什么名字"
)
CJK_REFERENTIAL_PREFIXES = ("那个", "这个", "哪个", "所谓")
CJK_ACTIVITY_CLAUSE_RE = re.compile(
    r"^([\u4e00-\u9fff]{2,4})"
    r"(?:写|做|拍|编|搞|负责|开发|维护|管理|参与)"
    r"[\u4e00-\u9fff]{0,6}$"
)
CJK_LEADING_ACTIVITY_RE = re.compile(
    r"^(?:负责|开发|维护|管理|参与)[\u4e00-\u9fff]{1,6}$"
)
CJK_SCOPE_TOKEN_RE = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+$")


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


def is_cjk_scope_token(value: str) -> bool:
    """Return whether a scope token is CJK-only, without a length veto."""

    return bool(CJK_SCOPE_TOKEN_RE.fullmatch(str(value or "").strip()))


def is_unquoted_cjk_referential_identity_clause(subject: str, query: str) -> bool:
    """Return whether an unquoted CJK capture is a referential identity clause.

    ``CJK_SCOPE_QUERY_SUBJECT_RE`` treats 2..8 CJK characters before 的 as a
    named scope. Activity or demonstrative clauses that ask for a project's
    identity are not an already-supplied project name. A named actor inside
    that clause is recovered separately. Simple named subjects, quoted
    captures, and non-identity questions stay out of this path.
    """

    normalized = normalize_cjk_scope_subject(subject)
    if not normalized or not CJK_PROJECT_IDENTITY_SEEKING_RE.search(str(query or "")):
        return False
    if any(normalized.startswith(prefix) for prefix in CJK_REFERENTIAL_PREFIXES):
        return True
    return bool(
        CJK_ACTIVITY_CLAUSE_RE.fullmatch(normalized)
        or CJK_LEADING_ACTIVITY_RE.fullmatch(normalized)
    )


def cjk_named_actor_from_referential_identity_clause(
    subject: str, query: str
) -> str | None:
    """Return the explicit actor in an identity-seeking activity clause.

    Demonstrative and leading-activity clauses have no named actor. The
    activity verb and topic stay out of the returned value so they cannot
    impersonate a supplied project name.
    """

    if not is_unquoted_cjk_referential_identity_clause(subject, query):
        return None
    normalized = normalize_cjk_scope_subject(subject)
    if any(normalized.startswith(prefix) for prefix in CJK_REFERENTIAL_PREFIXES):
        return None
    if CJK_LEADING_ACTIVITY_RE.fullmatch(normalized):
        return None
    matched = CJK_ACTIVITY_CLAUSE_RE.fullmatch(normalized)
    if matched is None:
        return None
    actor = str(matched.group(1) or "").strip()
    if not actor or actor in CJK_SCOPE_PRONOUNS:
        return None
    return actor


def explicit_cjk_query_entities_corroborated_in_item(
    entities: set[str], item_text: str
) -> bool:
    """Return whether every explicit query entity is CJK text found in the item.

    Auto-expanded stores often keep Latin aliases only. A CJK actor the
    memory already states can stand in for that missing metadata. Latin
    names and actors absent from the text do not qualify.
    """

    if not entities:
        return False
    text = str(item_text or "")
    for entity in entities:
        token = str(entity or "").strip()
        if not is_cjk_scope_token(token) or token not in text:
            return False
    return True
