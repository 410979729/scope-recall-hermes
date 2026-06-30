"""Memory quality lint rules for active secrets, pollution, and low-signal durable rows.

Quality findings are review evidence and should distinguish active problems from archived historical data."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any, Sequence

from .capture_filters import contains_secret_like_text, sanitize_report_text
from .gating import compact_text

TEMPLATE_PREFIXES = (
    "Journal digest memory",
    "Operations workflow summary",
)
PATH_CACHE_PATTERNS = (
    "/tmp/",
    "image_cache/",
    "audio_cache/",
    "hermes-results",
)
ATTACHMENT_MARKERS = (
    "MEDIA:",
    "[ATTACHMENT",
    "attachment://",
    "sandbox:/mnt/data/",
)
STALE_REVIEW_VALUES = {"stale-review", "stale_review", "stale review"}
QUALITY_RULES = {
    "template_prefix",
    "raw_attachment_marker",
    "cache_or_tmp_path",
    "overlong_transcript",
    "stale_review_active",
    "missing_memory_type",
    "secret_like_content",
}


def _load_metadata(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_archived(metadata: dict[str, Any]) -> bool:
    return str(metadata.get("lifecycle") or "").strip().lower() == "archived"


def _is_active_profile_memory(metadata: dict[str, Any]) -> bool:
    lifecycle = str(metadata.get("lifecycle") or "promoted").strip().lower()
    return lifecycle not in {"archived", "candidate", "scratch"}


def _has_any(text: str, needles: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def _looks_like_transcript(text: str) -> bool:
    lowered = text.lower()
    markers = sum(
        1
        for marker in (
            "tool execution trace",
            "python -m pytest",
            "pytest ",
            "git status",
            "ruff check",
            "pyright",
            "traceback",
            "stdout",
            "stderr",
        )
        if marker in lowered
    )
    if len(text) >= 2400:
        return markers >= 1
    return len(text) >= 900 and markers >= 2


def lint_memory_row(row: sqlite3.Row) -> list[str]:
    metadata = _load_metadata(row["metadata"])
    if not _is_active_profile_memory(metadata):
        return []
    content = str(row["content"] or "")
    summary = str(row["summary"] or "")
    text = f"{summary}\n{content}"
    rules: list[str] = []
    if any(content.startswith(prefix) or summary.startswith(prefix) for prefix in TEMPLATE_PREFIXES):
        rules.append("template_prefix")
    if _has_any(text, ATTACHMENT_MARKERS):
        rules.append("raw_attachment_marker")
    if _has_any(text, PATH_CACHE_PATTERNS):
        rules.append("cache_or_tmp_path")
    if _looks_like_transcript(content):
        rules.append("overlong_transcript")
    expires_at = str(metadata.get("expires_at") or metadata.get("freshness") or "").strip().lower()
    if expires_at in STALE_REVIEW_VALUES or str(metadata.get("review_status") or "").strip().lower() in STALE_REVIEW_VALUES:
        rules.append("stale_review_active")
    if not str(metadata.get("memory_type") or metadata.get("type") or metadata.get("category") or "").strip():
        rules.append("missing_memory_type")
    if contains_secret_like_text(content):
        rules.append("secret_like_content")
    return rules


def memory_quality_report(conn: sqlite3.Connection, *, sample_limit: int = 8) -> dict[str, Any]:
    """Build the active memory quality report for secrets, pollution, and low-value rows.

    The report distinguishes active issues from archived history so dashboards do not overstate current risk."""
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "memories" not in tables:
        return {
            "status": "schema_missing",
            "active_rows": 0,
            "active_lint_hits": 0,
            "by_rule": {},
            "samples": [],
            "rules": sorted(QUALITY_RULES),
        }
    rows = conn.execute(
        """
        SELECT id, scope_id, source, target, content, summary, updated_at, metadata
        FROM memories
        ORDER BY updated_at DESC, id ASC
        """
    ).fetchall()
    by_rule: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    active_rows = 0
    for row in rows:
        metadata = _load_metadata(row["metadata"])
        if not _is_active_profile_memory(metadata):
            continue
        active_rows += 1
        rules = lint_memory_row(row)
        if not rules:
            continue
        by_rule.update(rules)
        if len(samples) < max(0, int(sample_limit)):
            content = str(row["content"] or "")
            samples.append(
                {
                    "id": str(row["id"]),
                    "scope_id": str(row["scope_id"] or ""),
                    "source": str(row["source"] or ""),
                    "target": str(row["target"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                    "rules": rules,
                    "preview": sanitize_report_text(compact_text(content, 220)),
                }
            )
    active_lint_hits = sum(by_rule.values())
    if active_lint_hits:
        status = "needs_review"
    else:
        status = "ready"
    return {
        "status": status,
        "active_rows": active_rows,
        "active_lint_hits": active_lint_hits,
        "by_rule": dict(sorted(by_rule.items())),
        "samples": samples,
        "rules": sorted(QUALITY_RULES),
    }
