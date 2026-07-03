"""Memory quality lint rules for active secrets, pollution, and low-signal durable rows.

Quality findings are review evidence and should distinguish active problems from archived historical data."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

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
    r"\appdata\local\temp",
    "/appdata/local/temp",
    "%temp%",
    "%tmp%",
    r"\windows\temp",
    "/windows/temp",
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
HIDDEN_PROFILE_LIFECYCLES = {"archived", "candidate", "scratch", "superseded", "obsolete", "rejected"}
PROFILE_TARGETS = {"user", "memory", "project", "ops"}
STABLE_MEMORY_TYPES = {"factual", "preference", "procedure", "workflow", "pitfall", "decision", "constraint", "project", "resource"}
NOISE_MEMORY_TYPES = {"summary", "episodic", "tool_trace"}
REVIEW_TERMS = (
    "password",
    "token",
    "secret",
    "api key",
    "api_id",
    "api_hash",
    "credential",
    "private key",
    "密钥",
    "密码",
    "凭据",
    "删除",
    "重启",
    "发布",
    "推送",
    "提交",
    "commit",
    "push",
    "tag",
    "release",
    "sudo",
    "systemctl",
)
STALE_PROGRESS_TERMS = (
    "commit `",
    "commit ",
    "pull request",
    "pr #",
    "issue #",
    "run `",
    "pid ",
    "已推送",
    "已发布",
    "工作树",
    "当前仍为",
)


@dataclass(frozen=True)
class MemoryQualityDecision:
    """Read-only quality decision for one memory row or candidate row.

    The decision is shared by lint reports and candidate promotion so every
    future apply path can explain risk, evidence, and reason before changing
    durable memory state.
    """

    action: str
    reason: str
    confidence: float
    importance: float
    memory_type: str
    risk: str = "low"
    target: str = ""
    lifecycle: str = ""
    evidence_refs: tuple[str, ...] = ()
    freshness: str = ""
    validator_kind: str = ""
    redaction_status: str = "clean"


def _load_metadata(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_quality_metadata(raw: object) -> dict[str, Any]:
    """Load memory metadata for quality decisions without mutating callers."""
    return _load_metadata(raw)


def _row_value(row: sqlite3.Row | Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        return row[key]  # type: ignore[index]
    except Exception:
        return default


def _float_meta(metadata: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(metadata.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _lifecycle(metadata: Mapping[str, Any]) -> str:
    return str(metadata.get("lifecycle") or "").strip().lower()


def _memory_type(metadata: Mapping[str, Any]) -> str:
    return str(metadata.get("memory_type") or metadata.get("type") or metadata.get("category") or "").strip().lower()


def _metadata_ref_values(value: Any, *, default_kind: str = "") -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        refs: list[str] = []
        for item in value:
            refs.extend(_metadata_ref_values(item, default_kind=default_kind))
        return refs
    if isinstance(value, tuple):
        refs: list[str] = []
        for item in value:
            refs.extend(_metadata_ref_values(item, default_kind=default_kind))
        return refs
    if isinstance(value, dict):
        kind = str(value.get("kind") or value.get("type") or default_kind or "").strip()
        refs: list[str] = []
        for key in ("id", "journal_entry_id", "memory_id", "source_id", "session_id"):
            raw_id = value.get(key)
            if raw_id in (None, ""):
                continue
            text = str(raw_id).strip()
            if text:
                refs.append(f"{kind}:{text}" if kind else text)
        raw_ids = value.get("ids")
        if isinstance(raw_ids, (list, tuple)):
            for raw_id in raw_ids:
                text = str(raw_id).strip()
                if text:
                    refs.append(f"{kind}:{text}" if kind else text)
        if refs:
            return refs
        for key, item in value.items():
            if key in {"kind", "type", "id", "ids", "journal_entry_id", "memory_id", "source_id", "session_id"}:
                continue
            refs.extend(_metadata_ref_values(item, default_kind=kind))
        return refs
    if value not in (None, ""):
        text = str(value).strip()
        return [text] if text else []
    return []


def _metadata_refs(metadata: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    refs: list[str] = []
    for key in keys:
        refs.extend(_metadata_ref_values(metadata.get(key)))
    seen: set[str] = set()
    unique: list[str] = []
    for ref in refs:
        if ref and ref not in seen:
            seen.add(ref)
            unique.append(ref)
    return tuple(unique[:20])


def _is_archived(metadata: dict[str, Any]) -> bool:
    return str(metadata.get("lifecycle") or "").strip().lower() in HIDDEN_PROFILE_LIFECYCLES


def _is_active_profile_memory(metadata: dict[str, Any]) -> bool:
    lifecycle = str(metadata.get("lifecycle") or "promoted").strip().lower()
    return lifecycle not in HIDDEN_PROFILE_LIFECYCLES


def _has_any(text: str, needles: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def quality_decision_for_memory(row: sqlite3.Row | Mapping[str, Any]) -> MemoryQualityDecision:
    """Return the shared quality decision for a memory row.

    This function is intentionally read-only. It is the contract that candidate
    promotion, lint reports, and later governance schedulers can share before any
    apply path changes lifecycle or visibility.
    """
    metadata = load_quality_metadata(_row_value(row, "metadata", "{}"))
    lifecycle = _lifecycle(metadata)
    confidence = _float_meta(metadata, "confidence", 0.0)
    importance = _float_meta(metadata, "importance", 0.0)
    memory_type = _memory_type(metadata)
    target = str(_row_value(row, "target", "") or "").strip().lower()
    source = str(_row_value(row, "source", "") or "").strip().lower()
    text = f"{_row_value(row, 'summary', '')}\n{_row_value(row, 'content', '')}"
    freshness = str(metadata.get("expires_at") or metadata.get("freshness") or "").strip()
    validator_kind = str(metadata.get("validator_kind") or "").strip()
    evidence_refs = _metadata_refs(metadata, "evidence_refs", "evidence_anchors", "journal_entry_ids", "source_ids")
    redaction_status = "secret_like" if contains_secret_like_text(text) else "clean"

    base = {
        "confidence": confidence,
        "importance": importance,
        "memory_type": memory_type,
        "target": target,
        "lifecycle": lifecycle,
        "evidence_refs": evidence_refs,
        "freshness": freshness,
        "validator_kind": validator_kind,
        "redaction_status": redaction_status,
    }
    if lifecycle != "candidate":
        rules: list[str] = []
        if isinstance(row, sqlite3.Row) or isinstance(row, Mapping):
            try:
                rules = lint_memory_row(row)  # type: ignore[arg-type]
            except Exception:
                rules = []
        if rules:
            return MemoryQualityDecision("needs_review", "active_lint_rules:" + ",".join(sorted(rules)), risk="high" if "secret_like_content" in rules else "medium", **base)
        return MemoryQualityDecision("skip", "not_candidate", **base)
    if target not in PROFILE_TARGETS:
        return MemoryQualityDecision("keep_candidate", "target_not_profile_surface", **base)
    if redaction_status != "clean":
        return MemoryQualityDecision("keep_candidate", "secret_like_content_requires_human_review", risk="high", **base)
    if _has_any(text, REVIEW_TERMS):
        return MemoryQualityDecision("keep_candidate", "high_risk_terms_require_human_review", risk="high", **base)
    if memory_type in NOISE_MEMORY_TYPES:
        return MemoryQualityDecision("archive", f"low_value_memory_type:{memory_type or 'unknown'}", **base)
    if _has_any(text, STALE_PROGRESS_TERMS) and memory_type in {"summary", "decision", "project"}:
        return MemoryQualityDecision("archive", "stale_progress_or_release_status", **base)
    if memory_type not in STABLE_MEMORY_TYPES:
        return MemoryQualityDecision("keep_candidate", f"unsupported_memory_type:{memory_type or 'unknown'}", **base)
    if not evidence_refs:
        return MemoryQualityDecision("keep_candidate", "missing_evidence_anchor", risk="medium", **base)
    if target == "user" and confidence >= 0.78:
        return MemoryQualityDecision("promote", "user_profile_candidate_confident", **base)
    if source == "tool-store" and confidence >= 0.86 and importance >= 0.55:
        return MemoryQualityDecision("promote", "tool_store_candidate_confident", **base)
    if confidence >= 0.78 and importance >= 0.55:
        return MemoryQualityDecision("promote", "high_confidence_stable_candidate", **base)
    if importance >= 0.82 and confidence >= 0.62:
        return MemoryQualityDecision("promote", "high_importance_stable_candidate", **base)
    return MemoryQualityDecision("keep_candidate", "below_auto_promotion_threshold", **base)


def quality_decision_summary(conn: sqlite3.Connection, *, limit: int = 1000) -> dict[str, Any]:
    """Summarize shared quality decisions without mutating SQLite."""
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "memories" not in tables:
        return {"status": "schema_missing", "rows": 0, "by_action": {}, "by_risk": {}, "by_reason": {}, "samples": []}
    rows = conn.execute(
        """
        SELECT id, scope_id, source, target, content, summary, updated_at, metadata
        FROM memories
        ORDER BY updated_at DESC, id ASC
        LIMIT ?
        """,
        (max(1, int(limit or 1000)),),
    ).fetchall()
    by_action: Counter[str] = Counter()
    by_risk: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for row in rows:
        decision = quality_decision_for_memory(row)
        by_action[decision.action] += 1
        by_risk[decision.risk] += 1
        by_reason[decision.reason] += 1
        if decision.action not in {"skip"} and len(samples) < 8:
            samples.append(
                {
                    "id": str(row["id"]),
                    "target": str(row["target"] or ""),
                    "action": decision.action,
                    "reason": decision.reason,
                    "risk": decision.risk,
                    "memory_type": decision.memory_type,
                    "confidence": decision.confidence,
                    "importance": decision.importance,
                    "preview": sanitize_report_text(compact_text(str(row["content"] or ""), 180)),
                }
            )
    return {
        "status": "needs_review" if any(action != "skip" for action in by_action) else "ready",
        "rows": len(rows),
        "by_action": dict(sorted(by_action.items())),
        "by_risk": dict(sorted(by_risk.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "samples": samples,
        "limit": max(1, int(limit or 1000)),
        "truncated": len(rows) >= max(1, int(limit or 1000)),
    }


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
