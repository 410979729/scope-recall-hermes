"""Experience-to-Skill bridge candidate schema.

Skill candidates are review artifacts, not formal Hermes skills. This module only
validates and serializes candidate payloads; it never writes to the user's skill
library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import sqlite3
from typing import Any, Mapping, Sequence

from .capture_filters import contains_secret_like_text, sanitize_report_text
from .experience_models import ExperienceValidationError, RISKY_CAPABILITY_CLASSES
from .experience_store import record_playbook_feedback

SKILL_CANDIDATE_SCHEMA_VERSION = "skill_candidate.v1"
SKILL_CANDIDATE_RISK_CLASSES = frozenset({"low", "medium", "high"})


@dataclass(frozen=True)
class SkillCandidate:
    schema_version: str
    source_playbook_id: str
    title: str
    trigger_conditions: tuple[str, ...]
    steps: tuple[str, ...]
    verification: tuple[str, ...]
    pitfalls: tuple[str, ...]
    risk_class: str
    evidence_refs: tuple[str, ...]
    requires_operator_review: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "source_playbook_id": self.source_playbook_id,
            "title": self.title,
            "trigger_conditions": list(self.trigger_conditions),
            "steps": list(self.steps),
            "verification": list(self.verification),
            "pitfalls": list(self.pitfalls),
            "risk_class": self.risk_class,
            "evidence_refs": list(self.evidence_refs),
            "requires_operator_review": self.requires_operator_review,
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExperienceValidationError(f"{key} must be a non-empty string")
    raw_text = value.strip()
    _reject_secret_like_value(raw_text, path=key)
    return sanitize_report_text(raw_text)


def _require_list(payload: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ExperienceValidationError(f"{key} must be a list")
    if not allow_empty and not value:
        raise ExperienceValidationError(f"{key} must contain at least one item")
    return value


def _text_tuple(values: Sequence[Any], *, field_name: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not allow_empty and not values:
        raise ExperienceValidationError(f"{field_name} must contain at least one item")
    normalized: list[str] = []
    for idx, value in enumerate(values, start=1):
        if not isinstance(value, str) or not value.strip():
            raise ExperienceValidationError(f"{field_name}[{idx}] must be a non-empty string")
        raw_text = value.strip()
        _reject_secret_like_value(raw_text, path=f"{field_name}[{idx}]")
        normalized.append(sanitize_report_text(raw_text))
    return tuple(normalized)


def _reject_secret_like_value(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, str):
        if contains_secret_like_text(value) or "api_key=" in value.lower() or "password=" in value.lower():
            raise ExperienceValidationError(f"secret-like content is not allowed in skill candidate {path}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if contains_secret_like_text(key_text):
                raise ExperienceValidationError(f"secret-like content is not allowed in skill candidate {path}.<key>")
            _reject_secret_like_value(item, path=f"{path}.value")
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for index, item in enumerate(value):
            _reject_secret_like_value(item, path=f"{path}[{index}]")


def validate_skill_candidate(payload: Mapping[str, Any]) -> SkillCandidate:
    """Validate and normalize a review-only skill candidate payload."""
    schema_version = str(payload.get("schema_version") or SKILL_CANDIDATE_SCHEMA_VERSION).strip()
    if schema_version != SKILL_CANDIDATE_SCHEMA_VERSION:
        raise ExperienceValidationError(f"schema_version must be {SKILL_CANDIDATE_SCHEMA_VERSION}")
    source_playbook_id = _require_text(payload, "source_playbook_id")
    title = _require_text(payload, "title")
    trigger_conditions = _text_tuple(_require_list(payload, "trigger_conditions"), field_name="trigger_conditions")
    steps = _text_tuple(_require_list(payload, "steps"), field_name="steps")
    verification = _text_tuple(_require_list(payload, "verification"), field_name="verification")
    pitfalls = _text_tuple(payload.get("pitfalls", []), field_name="pitfalls", allow_empty=True)
    risk_class = str(payload.get("risk_class") or "medium").strip().lower()
    if risk_class not in SKILL_CANDIDATE_RISK_CLASSES:
        raise ExperienceValidationError(f"risk_class must be one of {sorted(SKILL_CANDIDATE_RISK_CLASSES)}")
    evidence_refs = _text_tuple(_require_list(payload, "evidence_refs"), field_name="evidence_refs")
    raw_metadata = payload.get("metadata") or {}
    if not isinstance(raw_metadata, Mapping):
        raise ExperienceValidationError("metadata must be an object when provided")
    _reject_secret_like_value(raw_metadata, path="metadata")
    return SkillCandidate(
        schema_version=schema_version,
        source_playbook_id=source_playbook_id,
        title=title,
        trigger_conditions=trigger_conditions,
        steps=steps,
        verification=verification,
        pitfalls=pitfalls,
        risk_class=risk_class,
        evidence_refs=evidence_refs,
        requires_operator_review=True,
        metadata=dict(raw_metadata),
    )


def _json_loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        value = json.loads(str(raw))
    except Exception:
        return default
    return value


def _scope_predicate(accessible_scope_ids: Sequence[str]) -> tuple[str, list[str]]:
    scopes = [str(scope_id) for scope_id in accessible_scope_ids if str(scope_id)]
    if not scopes:
        return "0", []
    placeholders = ",".join("?" for _ in scopes)
    return f"(scope_id IN ({placeholders}) OR shared_scope_id IN ({placeholders}))", [*scopes, *scopes]


def _step_actions(raw_steps: Any) -> tuple[list[str], list[str]]:
    steps = _json_loads(raw_steps, [])
    if not isinstance(steps, list):
        return [], []
    actions: list[str] = []
    capability_classes: list[str] = []
    for item in steps:
        if isinstance(item, Mapping):
            action = str(item.get("action") or "").strip()
            capability = str(item.get("capability_class") or "").strip()
            if action:
                actions.append(action)
            if capability:
                capability_classes.append(capability)
        elif isinstance(item, str) and item.strip():
            actions.append(item.strip())
    return actions, capability_classes


def _pitfall_texts(raw_pitfalls: Any) -> list[str]:
    pitfalls = _json_loads(raw_pitfalls, [])
    if not isinstance(pitfalls, list):
        return []
    texts: list[str] = []
    for item in pitfalls:
        if isinstance(item, Mapping):
            text = str(item.get("summary") or item.get("pitfall") or item.get("description") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            texts.append(text)
    return texts


def _evidence_refs(playbook_id: str, raw_evidence: Any) -> list[str]:
    refs = [f"playbook:{playbook_id}"]
    raw_refs = _json_loads(raw_evidence, [])
    if isinstance(raw_refs, list):
        for item in raw_refs:
            text = str(item or "").strip()
            if text and text not in refs:
                refs.append(text)
    return refs


def _risk_class(capability_classes: Sequence[str], confidence: float) -> str:
    if any(capability in RISKY_CAPABILITY_CLASSES for capability in capability_classes):
        return "high"
    if confidence >= 0.85 and all(capability in {"read_only", "local_write", ""} for capability in capability_classes):
        return "low"
    return "medium"


def _candidate_from_playbook_row(row: sqlite3.Row) -> SkillCandidate:
    playbook_id = str(row["id"])
    actions, capability_classes = _step_actions(row["steps"])
    payload = {
        "schema_version": SKILL_CANDIDATE_SCHEMA_VERSION,
        "source_playbook_id": playbook_id,
        "title": str(row["title"]),
        "trigger_conditions": [str(row["trigger"])],
        "steps": actions,
        "verification": _json_loads(row["verification"], []),
        "pitfalls": _pitfall_texts(row["pitfalls"]),
        "risk_class": _risk_class(capability_classes, float(row["confidence"] or 0.0)),
        "evidence_refs": _evidence_refs(playbook_id, row["evidence_anchors"]),
        "metadata": {"task_class": str(row["task_class"]), "source_status": str(row["status"]), "success_count": int(row["success_count"] or 0)},
    }
    return validate_skill_candidate(payload)


def _rejection_reason(row: sqlite3.Row, *, min_success_count: int, min_confidence: float) -> str:
    status = str(row["status"] or "")
    if status not in {"reviewed", "promoted"}:
        return "not_reviewed_or_promoted"
    if int(row["failure_count"] or 0) > 0 or int(row["stale_count"] or 0) > 0:
        return "negative_or_stale_feedback"
    if int(row["success_count"] or 0) < min_success_count:
        return "insufficient_success_feedback"
    if float(row["confidence"] or 0.0) < min_confidence:
        return "low_confidence"
    if not _evidence_refs(str(row["id"]), row["evidence_anchors"]):
        return "missing_evidence"
    return ""


def generate_skill_candidates(
    conn: sqlite3.Connection,
    *,
    accessible_scope_ids: Sequence[str],
    limit: int = 20,
    min_success_count: int = 2,
    min_confidence: float = 0.75,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Generate review-only skill candidates from successful Experience playbooks.

    This function is intentionally dry-run/read-only in MVP form; it does not
    create or update formal Hermes skills.
    """
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    rows = conn.execute(
        f"""
        SELECT * FROM procedural_playbooks
        WHERE {scope_sql}
        ORDER BY confidence DESC, success_count DESC, updated_at DESC
        LIMIT ?
        """,
        [*scope_params, max(1, min(100, int(limit or 20)))],
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for row in rows:
        reason = _rejection_reason(row, min_success_count=min_success_count, min_confidence=min_confidence)
        if reason:
            rejected.append({"id": str(row["id"]), "reason": reason})
            continue
        try:
            candidates.append(_candidate_from_playbook_row(row).to_dict())
        except ExperienceValidationError as exc:
            rejected.append({"id": str(row["id"]), "reason": f"invalid_candidate: {exc}"})
    return {
        "ok": True,
        "dry_run": dry_run,
        "count": len(candidates),
        "candidates": candidates,
        "rejected": rejected,
        "min_success_count": min_success_count,
        "min_confidence": min_confidence,
    }


def _skill_names(raw: Any) -> set[str]:
    value = _json_loads(raw, [])
    if not isinstance(value, list):
        return set()
    return {sanitize_report_text(str(item or "").strip()) for item in value if str(item or "").strip()}


def _playbook_rows_for_skill(
    conn: sqlite3.Connection,
    *,
    skill_name: str,
    accessible_scope_ids: Sequence[str],
    limit: int,
) -> list[sqlite3.Row]:
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    anchor_rows = conn.execute("SELECT playbook_id FROM skill_anchors WHERE skill_name = ?", (skill_name,)).fetchall()
    anchored_ids = {str(row["playbook_id"]) for row in anchor_rows}
    rows = conn.execute(
        f"""
        SELECT * FROM procedural_playbooks
        WHERE {scope_sql}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        [*scope_params, max(1, min(200, int(limit or 20) * 5))],
    ).fetchall()
    matched: list[sqlite3.Row] = []
    for row in rows:
        playbook_id = str(row["id"])
        if playbook_id in anchored_ids or skill_name in _skill_names(row["related_skills"]):
            matched.append(row)
        if len(matched) >= max(1, min(100, int(limit or 20))):
            break
    return matched


def record_skill_feedback(
    conn: sqlite3.Connection,
    *,
    skill_name: str,
    scope_id: str,
    outcome: str,
    accessible_scope_ids: Sequence[str] | None = None,
    evidence: Sequence[Any] | None = None,
    outcome_reason: str = "",
    decision: str = "guided_reuse",
    failure_threshold: int = 2,
    limit: int = 20,
) -> dict[str, Any]:
    """Route skill-use feedback back to linked Experience playbooks.

    This is an audit/update bridge only: it records playbook feedback and may mark
    linked playbooks ``needs_review`` after repeated negative outcomes. It never
    edits, deletes, or writes formal Hermes skill files.
    """
    safe_skill_name = sanitize_report_text(str(skill_name or "").strip())
    if not safe_skill_name:
        return {"recorded": False, "error": "missing_skill_name"}
    _reject_secret_like_value(safe_skill_name, path="skill_name")
    scopes = list(accessible_scope_ids if accessible_scope_ids is not None else [scope_id])
    rows = _playbook_rows_for_skill(conn, skill_name=safe_skill_name, accessible_scope_ids=scopes, limit=limit)
    if not rows:
        return {"recorded": False, "error": "no_playbook_for_skill", "skill_name": safe_skill_name, "count": 0, "results": []}
    augmented_evidence: list[Any] = [f"skill:{safe_skill_name}", *list(evidence or [])]
    results: list[dict[str, Any]] = []
    threshold = max(1, int(failure_threshold or 2))
    for row in rows:
        result = record_playbook_feedback(
            conn,
            playbook_id=str(row["id"]),
            scope_id=scope_id,
            accessible_scope_ids=scopes,
            outcome=outcome,
            decision=decision,
            evidence=augmented_evidence,
            outcome_reason=outcome_reason,
            negative_feedback_threshold=threshold,
        )
        results.append(result)
    return {
        "recorded": any(bool(item.get("recorded")) for item in results),
        "skill_name": safe_skill_name,
        "count": len(results),
        "needs_review_count": sum(1 for item in results if item.get("status") == "needs_review"),
        "failure_threshold": threshold,
        "results": results,
    }
