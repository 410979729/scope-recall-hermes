"""Admission policy for automatically generated durable-memory candidates.

Digest extraction is evidence collection, not a grant of durable truth.  This
module labels automatic candidates for later review without changing explicit
operator/tool stores or structured fact-evolution writes.
"""
from __future__ import annotations

import re
from typing import Any

AUTO_DIGEST_SOURCES = frozenset({"journal-digest", "nightly-digest"})
AUTOMATIC_DIGEST_LIFECYCLES = frozenset({"candidate", "promoted"})
_EXPERIENCE_TYPES = frozenset({"procedure", "workflow", "pitfall"})

_CURRENT_MARKER_RE = re.compile(
    r"(?:\bcurrent(?:ly)?\b|\bonline\b|\blive\b|\brunning\b|\bnow\b|"
    r"当前|目前|现状|在线|运行中|已切换|现已|仍为|持续)",
    re.IGNORECASE,
)
_VOLATILE_VALUE_RE = re.compile(
    r"(?:\b(?:NO[- ]?GO|GO)\b|\b(?:enabled|disabled|active|inactive|failed)\b|"
    r"\bPID\s*[=:]?\s*\d+\b|\bport\s*[=:]?\s*\d+\b|\b\d+\.\d+(?:\.\d+)?(?:[-.]?[A-Za-z0-9]+)?\b|"
    r"\b(?:model|provider|version|branch|commit|endpoint|status|state)\b|"
    r"端口|模型|供应商|版本|分支|提交|状态|候选)",
    re.IGNORECASE,
)
_EXPLICIT_SNAPSHOT_RE = re.compile(
    r"(?:持续(?:判定|处于|保持)?\s*NO[- ]?GO|当前(?:已|为|是|状态|配置|版本|模型|端口)|在线(?:状态|版本|模型|端口)|"
    r"现已切换|目前(?:已|为|是|状态|配置|版本|模型|端口)|仍为)",
    re.IGNORECASE,
)
_NORMATIVE_RE = re.compile(r"(?:必须|禁止|不得|应该|应当|需要|要求|\bmust\b|\bshould\b|\brequire(?:s|d)?\b)", re.IGNORECASE)
_CONCRETE_STATE_ASSERTION_RE = re.compile(
    r"(?:\b(?:is|are|equals?|runs?|uses?|listens?)\b|[=:]|是|为|等于|切换为|使用|运行于|监听于)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?;；\n]+")


def is_time_sensitive_snapshot(content: str) -> bool:
    """Return whether text asserts volatile operational state rather than a rule."""

    text = str(content or "").strip()
    if not text:
        return False
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        explicit = _EXPLICIT_SNAPSHOT_RE.search(sentence)
        current = _CURRENT_MARKER_RE.search(sentence)
        volatile = _VOLATILE_VALUE_RE.search(sentence)
        if not explicit and not (current and volatile):
            continue
        assertion_at = explicit.start() if explicit else current.start()  # type: ignore[union-attr]
        normative = _NORMATIVE_RE.search(sentence)
        if normative and normative.start() < assertion_at:
            continue
        if normative and not _CONCRETE_STATE_ASSERTION_RE.search(
            sentence[assertion_at : normative.start()]
        ):
            continue
        return True
    return False


def automatic_admission_metadata(
    *,
    content: str,
    memory_type: str,
    source: str,
    recommended_action: str = "candidate",
    default_lifecycle: str = "candidate",
    structured_evolution: bool = False,
) -> dict[str, Any]:
    """Build metadata that keeps automatic extraction provisional.

    Structured fact-evolution proposals keep their dedicated transaction path;
    explicit/manual stores are outside this policy entirely.
    """

    normalized_source = str(source or "").strip().lower()
    if normalized_source not in AUTO_DIGEST_SOURCES:
        return {}
    normalized_type = str(memory_type or "summary").strip().lower()
    time_sensitive = is_time_sensitive_snapshot(content)
    normalized_lifecycle = str(default_lifecycle or "candidate").strip().lower()
    if normalized_lifecycle not in AUTOMATIC_DIGEST_LIFECYCLES:
        normalized_lifecycle = "candidate"
    if time_sensitive:
        normalized_lifecycle = "candidate"
    route = "fact_evolution" if structured_evolution else (
        "experience_review" if normalized_type in _EXPERIENCE_TYPES else "memory_review"
    )
    admission = {
        "version": 1,
        "source": normalized_source,
        "route": route,
        "time_sensitive": time_sensitive,
        "recommended_action": str(recommended_action or "candidate"),
    }
    if structured_evolution:
        return {"automatic_admission": admission}

    metadata: dict[str, Any] = {
        "automatic_admission": admission,
        "lifecycle": normalized_lifecycle,
        "candidate_status": (
            "promoted"
            if normalized_lifecycle == "promoted"
            else "needs_live_check" if time_sensitive else "needs_review"
        ),
        "candidate_reason": (
            "automatic_time_sensitive_snapshot"
            if time_sensitive
            else f"automatic_{route}"
        ),
    }
    if time_sensitive:
        metadata.update(
            {
                "freshness": "needs_live_check",
                "freshness_status": "needs_live_check",
                "needs_live_check": True,
                "truth_type": "operational_snapshot",
                "validator_kind": "manual",
                "ttl_days": 3,
            }
        )
    return metadata
