from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .capture_filters import contains_secret_like_text, sanitize_report_text

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
class CandidateDecision:
    action: str
    reason: str
    confidence: float
    importance: float
    memory_type: str
    risk: str = "low"
    lane: str = ""

    def __post_init__(self) -> None:
        if not self.lane:
            object.__setattr__(self, "lane", default_lane_for_decision(self.action, self.reason, self.risk))


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


def classify_candidate_row(row: sqlite3.Row | Mapping[str, Any]) -> CandidateDecision:
    metadata = load_metadata(_row_value(row, "metadata", "{}"))
    conf = _float_meta(metadata, "confidence", 0.0)
    importance = _float_meta(metadata, "importance", 0.0)
    memory_type = str(metadata.get("memory_type") or metadata.get("category") or "").strip().lower()
    target = str(_row_value(row, "target", "") or "").strip().lower()
    source = str(_row_value(row, "source", "") or "").strip().lower()
    text = f"{_row_value(row, 'summary', '')}\n{_row_value(row, 'content', '')}"

    if lifecycle(metadata) != "candidate":
        return CandidateDecision("skip", "not_candidate", conf, importance, memory_type)
    if target not in PROFILE_TARGETS:
        return CandidateDecision("keep_candidate", "target_not_profile_surface", conf, importance, memory_type)
    if contains_secret_like_text(text):
        return CandidateDecision("keep_candidate", "secret_like_content_requires_human_review", conf, importance, memory_type, risk="high")
    if _contains_any(text, REVIEW_TERMS):
        return CandidateDecision("keep_candidate", "high_risk_terms_require_human_review", conf, importance, memory_type, risk="high")
    if memory_type in NOISE_MEMORY_TYPES:
        return CandidateDecision("archive", f"low_value_memory_type:{memory_type or 'unknown'}", conf, importance, memory_type)
    if _contains_any(text, STALE_PROGRESS_TERMS) and memory_type in {"summary", "decision", "project"}:
        return CandidateDecision("archive", "stale_progress_or_release_status", conf, importance, memory_type)
    if memory_type not in STABLE_MEMORY_TYPES:
        return CandidateDecision("keep_candidate", f"unsupported_memory_type:{memory_type or 'unknown'}", conf, importance, memory_type)

    if target == "user" and conf >= 0.78:
        return CandidateDecision("promote", "user_profile_candidate_confident", conf, importance, memory_type)
    if source == "tool-store" and conf >= 0.86 and importance >= 0.55:
        return CandidateDecision("promote", "tool_store_candidate_confident", conf, importance, memory_type)
    if conf >= 0.78 and importance >= 0.55:
        return CandidateDecision("promote", "high_confidence_stable_candidate", conf, importance, memory_type)
    if importance >= 0.82 and conf >= 0.62:
        return CandidateDecision("promote", "high_importance_stable_candidate", conf, importance, memory_type)
    return CandidateDecision("keep_candidate", "below_auto_promotion_threshold", conf, importance, memory_type)


def candidate_rows(conn: sqlite3.Connection, *, limit: int = 1000) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            """
            SELECT id, scope_id, source, target, content, summary, updated_at, metadata
            FROM memories
            WHERE LOWER(COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.lifecycle') ELSE '' END, '')) = 'candidate'
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (max(1, int(limit or 1000)),),
        ).fetchall()
    )


def candidate_debt_report(conn: sqlite3.Connection, *, limit: int = 1000, sample_limit: int = 8) -> dict[str, Any]:
    rows = candidate_rows(conn, limit=limit)
    by_action = {"promote": 0, "archive": 0, "keep_candidate": 0, "skip": 0}
    by_lane: dict[str, int] = {}
    by_target: dict[str, int] = {}
    by_source: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    oldest_updated_at = ""
    newest_updated_at = ""
    for row in rows:
        decision = classify_candidate_row(row)
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
                    "target": target,
                    "source": source,
                    "updated_at": updated_at,
                    "action": decision.action,
                    "lane": decision.lane,
                    "reason": decision.reason,
                    "memory_type": decision.memory_type,
                    "confidence": decision.confidence,
                    "importance": decision.importance,
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
