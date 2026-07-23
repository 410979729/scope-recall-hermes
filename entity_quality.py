"""Conservative entity-quality policy shared by extraction and graph indexing."""
from __future__ import annotations

import re

_PURE_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+")
_GENERIC_ENGLISH_ENTITY_WORDS = frozenset(
    {
        "agent",
        "agents",
        "alert",
        "alerts",
        "current",
        "function",
        "functions",
        "helper",
        "helpers",
        "memory",
        "memories",
        "operation",
        "operations",
        "process",
        "processes",
        "service",
        "services",
        "state",
        "status",
        "system",
        "task",
        "tasks",
        "tool",
        "tools",
        "workflow",
        "workflows",
    }
)
_SENTENCE_MARKERS = (
    "负责", "需要", "必须", "出现", "决定", "验证", "处理", "收口", "让模型",
    "交给", "等于", "淹没", "只负责", "最终", "所有", "当前", "目前",
)
_BAD_PREFIXES = ("所有", "当前", "目前", "最终", "任何", "这条", "这些")
_BAD_SUFFIXES = ("的", "时", "后", "前", "中", "上", "下", "与", "和", "或", "会", "让", "把", "被", "将", "都", "仍", "已")


def entity_is_indexable(value: str) -> bool:
    """Reject sentence fragments while retaining compact names and identifiers."""

    text = str(value or "").strip()
    if not text:
        return False
    english_words = [word.casefold() for word in _ENGLISH_WORD_RE.findall(text)]
    if english_words and all(
        word in _GENERIC_ENGLISH_ENTITY_WORDS for word in english_words
    ):
        return False
    if not _PURE_CJK_RE.fullmatch(text):
        return True
    if len(text) > 8:
        return False
    if text.startswith(_BAD_PREFIXES) or text.endswith(_BAD_SUFFIXES):
        return False
    if any(marker in text for marker in _SENTENCE_MARKERS):
        return False
    return True
