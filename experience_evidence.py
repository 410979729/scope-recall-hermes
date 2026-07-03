"""Evidence-anchor extraction for reusable Experience playbooks.

Evidence anchors are compact, sanitized pointers that explain why a generated
playbook is trustworthy without storing raw tool traces, credentials, or large
transcripts.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .capture_filters import sanitize_report_text
from .gating import compact_text

TEST_COMMAND_RE = re.compile(r"\b(?:python(?:3)?\s+-m\s+pytest|python\s+-m\s+pytest|pytest|ruff|pyright)\b[^\n;。]*", re.IGNORECASE)
HEALTH_RE = re.compile(r"\b(?:doctor|health check|release gate|smoke)\b|健康检查|发布检查|验证", re.IGNORECASE)
REPO_RE = re.compile(r"\b(?:git\s+status|git\s+diff|gh\s+|pull request|\bPR\b|issue\s*#?\d+)\b", re.IGNORECASE)
FILE_RE = re.compile(r"(?:^|\s)(?:scripts|tests|docs|\.github)/[^\s`'\"]+|(?:^|\s)/(?:home|data|etc|var|tmp)/[^\s`'\"]+", re.IGNORECASE)
COMPLETION_RE = re.compile(r"完成|通过|验证完成|验证通过|success|fixed|done|passed", re.IGNORECASE)


def _entry_value(entry: Any, key: str, default: Any = "") -> Any:
    try:
        return entry[key]  # type: ignore[index]
    except Exception:
        if isinstance(entry, Mapping):
            return entry.get(key, default)
        return getattr(entry, key, default)


def _kind_for(role: str, text: str) -> str:
    if role == "user":
        return "user_statement"
    if TEST_COMMAND_RE.search(text):
        return "test_command"
    if HEALTH_RE.search(text):
        return "health_report"
    if REPO_RE.search(text):
        return "repo_state"
    if FILE_RE.search(text):
        return "file_evidence"
    if role == "assistant" and COMPLETION_RE.search(text):
        return "assistant_closure"
    if role == "tool":
        return "tool_output"
    return "observation"


def evidence_anchor_for_entry(entry: Any) -> dict[str, Any]:
    """Build one sanitized evidence anchor for a journal-like entry."""
    role = str(_entry_value(entry, "role", "") or "")
    content = str(_entry_value(entry, "content", "") or "")
    safe = sanitize_report_text(compact_text(content, 260))
    anchor: dict[str, Any] = {
        "kind": _kind_for(role, content),
        "role": role,
        "summary": safe,
    }
    entry_id = _entry_value(entry, "id", "")
    if entry_id not in (None, ""):
        try:
            anchor["journal_entry_id"] = int(entry_id)
        except (TypeError, ValueError):
            anchor["journal_entry_id"] = str(entry_id)
    session_id = str(_entry_value(entry, "session_id", "") or "")
    if session_id:
        anchor["session_id"] = sanitize_report_text(session_id)[:80]
    return anchor


def extract_evidence_anchors(entries: Sequence[Any], *, limit: int = 8) -> list[dict[str, Any]]:
    """Extract bounded, sanitized anchors from a task episode."""
    anchors: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        role = str(_entry_value(entry, "role", "") or "")
        if role not in {"user", "tool", "assistant"}:
            continue
        anchor = evidence_anchor_for_entry(entry)
        if anchor["kind"] in {"observation", "tool_output"} and role != "user":
            continue
        key = (str(anchor.get("kind") or ""), str(anchor.get("summary") or ""))
        if key in seen:
            continue
        seen.add(key)
        anchors.append(anchor)
        if len(anchors) >= max(1, int(limit or 8)):
            break
    return anchors
