"""Candidate-memory promotion planner and debt reporter.

This module classifies unpromoted rows conservatively so operators can dry-run promotion/archive choices before durable memory surfaces change."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .capture_filters import sanitize_report_text
from .memory_quality import HIDDEN_PROFILE_LIFECYCLES, quality_decision_for_memory

@dataclass(frozen=True)
class CandidateDecision:
    action: str
    reason: str
    confidence: float
    importance: float
    memory_type: str
    risk: str = "low"
    lane: str = ""
    evidence_refs: tuple[str, ...] = ()
    conflict_with: str = ""

    def __post_init__(self) -> None:
        if not self.lane:
            object.__setattr__(self, "lane", default_lane_for_decision(self.action, self.reason, self.risk))


class CandidateConflictCheckError(RuntimeError):
    """The durable-memory conflict query could not be evaluated safely."""


def default_lane_for_decision(action: str, reason: str, risk: str = "low") -> str:
    if action == "promote":
        return "promote_safe"
    if action == "archive":
        return "archive_low_value"
    if action == "skip":
        return "skip"
    if risk == "high":
        return "needs_review_high_risk"
    if reason == "below_auto_promotion_threshold":
        return "defer_recent"
    return "needs_review"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(str(raw))
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def lifecycle(metadata: Mapping[str, Any]) -> str:
    return str(metadata.get("lifecycle") or "").strip().lower()


def _float_meta(metadata: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(metadata.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _row_value(row: sqlite3.Row | Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        return row[key]  # type: ignore[index]
    except Exception:
        return default


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _normalized_conflict_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _active_memory_conflict(conn: sqlite3.Connection, row: sqlite3.Row | Mapping[str, Any]) -> str:
    """Return active memory id with same target/content, if any.

    Candidate auto-promotion must not silently duplicate or overwrite an already
    active durable row. This intentionally checks exact normalized text only;
    fuzzy/supersession relation building belongs to later governance phases.
    """
    target = str(_row_value(row, "target", "") or "")
    scope_id = str(_row_value(row, "scope_id", "") or "")
    candidate_id = str(_row_value(row, "id", "") or "")
    candidate_texts = {
        _normalized_conflict_text(str(_row_value(row, "summary", "") or "")),
        _normalized_conflict_text(str(_row_value(row, "content", "") or "")),
    }
    candidate_texts.discard("")
    if not target or not scope_id or not candidate_texts:
        return ""
    hidden_lifecycle_values = tuple(sorted(HIDDEN_PROFILE_LIFECYCLES))
    hidden_placeholders = ", ".join("?" for _ in hidden_lifecycle_values)
    try:
        rows = conn.execute(
            f"""
            SELECT id, summary, content, metadata
            FROM memories
            WHERE id != ?
              AND scope_id = ?
              AND target = ?
              AND LOWER(COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.lifecycle') ELSE '' END, '')) NOT IN ({hidden_placeholders})
            ORDER BY updated_at DESC, id ASC
            LIMIT 200
            """,
            (candidate_id, scope_id, target, *hidden_lifecycle_values),
        ).fetchall()
    except sqlite3.Error as exc:
        raise CandidateConflictCheckError("candidate conflict query failed") from exc
    for active in rows:
        active_texts = {
            _normalized_conflict_text(str(active["summary"] or "")),
            _normalized_conflict_text(str(active["content"] or "")),
        }
        active_texts.discard("")
        if candidate_texts & active_texts:
            return str(active["id"])
    return ""


def classify_candidate_row(row: sqlite3.Row | Mapping[str, Any], conn: sqlite3.Connection | None = None) -> CandidateDecision:
    quality = quality_decision_for_memory(row)
    if quality.action == "promote" and conn is not None:
        try:
            conflict_with = _active_memory_conflict(conn, row)
        except CandidateConflictCheckError:
            return CandidateDecision(
                "keep_candidate",
                "conflict_check_failed",
                quality.confidence,
                quality.importance,
                quality.memory_type,
                risk="high",
                evidence_refs=quality.evidence_refs,
            )
        if conflict_with:
            return CandidateDecision(
                "keep_candidate",
                "active_memory_conflict",
                quality.confidence,
                quality.importance,
                quality.memory_type,
                risk="medium",
                evidence_refs=quality.evidence_refs,
                conflict_with=conflict_with,
            )
    return CandidateDecision(
        quality.action,
        quality.reason,
        quality.confidence,
        quality.importance,
        quality.memory_type,
        risk=quality.risk,
        evidence_refs=quality.evidence_refs,
    )


def _scope_filter_sql(scope_ids: Sequence[str] | None) -> tuple[str, list[str]] | None:
    """Return SQL and params for an explicit scope allowlist.

    ``scope_ids=None`` means the caller intentionally wants a global operator
    report. An explicit empty list is different: fail closed and return no rows.
    """
    if scope_ids is None:
        return "", []
    scopes = [str(scope_id) for scope_id in scope_ids if str(scope_id)]
    if not scopes:
        return None
    placeholders = ",".join("?" for _ in scopes)
    return f" AND scope_id IN ({placeholders})", scopes


def candidate_rows(conn: sqlite3.Connection, *, scope_ids: Sequence[str] | None = None, limit: int = 1000) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    scope_filter = _scope_filter_sql(scope_ids)
    if scope_filter is None:
        return []
    scope_sql, scope_params = scope_filter
    return list(
        conn.execute(
            f"""
            SELECT id, scope_id, source, target, content, summary, updated_at, metadata
            FROM memories
            WHERE LOWER(COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.lifecycle') ELSE '' END, '')) = 'candidate'
              {scope_sql}
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (*scope_params, max(1, int(limit or 1000))),
        ).fetchall()
    )


def candidate_debt_report(
    conn: sqlite3.Connection,
    *,
    scope_ids: Sequence[str] | None = None,
    limit: int = 1000,
    sample_limit: int = 8,
) -> dict[str, Any]:
    """Summarize ordinary candidate-memory debt and a dry-run promotion plan.

    The report lets operators see promotable rows, archive-noise choices, and stale candidates before changing profile behavior."""
    rows = candidate_rows(conn, scope_ids=scope_ids, limit=limit)
    by_action = {"promote": 0, "archive": 0, "keep_candidate": 0, "skip": 0}
    by_lane: dict[str, int] = {}
    by_target: dict[str, int] = {}
    by_source: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    oldest_updated_at = ""
    newest_updated_at = ""
    for row in rows:
        decision = classify_candidate_row(row, conn)
        by_action[decision.action] = by_action.get(decision.action, 0) + 1
        by_lane[decision.lane] = by_lane.get(decision.lane, 0) + 1
        target = str(row["target"] or "")
        source = str(row["source"] or "")
        by_target[target] = by_target.get(target, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        updated_at = str(row["updated_at"] or "")
        if not oldest_updated_at or updated_at < oldest_updated_at:
            oldest_updated_at = updated_at
        if not newest_updated_at or updated_at > newest_updated_at:
            newest_updated_at = updated_at
        if len(samples) < max(0, int(sample_limit)):
            samples.append(
                {
                    "id": str(row["id"]),
                    "scope_id": str(row["scope_id"] or ""),
                    "target": target,
                    "source": source,
                    "updated_at": updated_at,
                    "action": decision.action,
                    "lane": decision.lane,
                    "reason": decision.reason,
                    "memory_type": decision.memory_type,
                    "confidence": decision.confidence,
                    "importance": decision.importance,
                    "evidence_refs": list(decision.evidence_refs),
                    "conflict_with": decision.conflict_with,
                    "summary": sanitize_report_text(str(row["summary"] or ""))[:220],
                }
            )

    oldest_age_hours = 0.0
    if oldest_updated_at:
        try:
            oldest = datetime.fromisoformat(oldest_updated_at.replace("Z", "+00:00"))
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            oldest_age_hours = round((datetime.now(timezone.utc) - oldest).total_seconds() / 3600.0, 3)
        except Exception:
            oldest_age_hours = 0.0

    return {
        "status": "debt" if rows else "ready",
        "candidate_count": len(rows),
        "oldest_updated_at": oldest_updated_at,
        "newest_updated_at": newest_updated_at,
        "oldest_age_hours": oldest_age_hours,
        "by_action": by_action,
        "by_lane": dict(sorted(by_lane.items())),
        "by_target": dict(sorted(by_target.items())),
        "by_source": dict(sorted(by_source.items())),
        "samples": samples,
        "limit": max(1, int(limit or 1000)),
        "truncated": len(rows) >= max(1, int(limit or 1000)),
    }
