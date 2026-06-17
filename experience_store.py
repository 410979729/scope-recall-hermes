from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .capture_filters import contains_secret_like_text, redact_secret_like_text
from .experience_models import (
    PLAYBOOK_STATUSES,
    RISKY_CAPABILITY_CLASSES,
    ExperienceValidationError,
    ProceduralPlaybook,
    validate_procedural_playbook,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads_checked(raw: Any, default: Any) -> tuple[Any, bool]:
    """Load a JSON column and report corrupt non-empty payloads.

    SQLite is the truth store, so old/corrupt rows must be surfaced to safety
    decisions instead of silently defaulting to empty structures.
    """

    if raw in (None, ""):
        return default, False
    try:
        return json.loads(str(raw)), False
    except Exception:
        return default, True


def _json_loads(raw: Any, default: Any) -> Any:
    value, _corrupt = _json_loads_checked(raw, default)
    return value


def _scope_predicate(accessible_scope_ids: Sequence[str]) -> tuple[str, list[str]]:
    scopes = [str(scope_id) for scope_id in accessible_scope_ids if str(scope_id)]
    if not scopes:
        return "0", []
    placeholders = ",".join("?" for _ in scopes)
    return f"(scope_id IN ({placeholders}) OR shared_scope_id IN ({placeholders}))", [*scopes, *scopes]


def _run_scope_predicate(accessible_scope_ids: Sequence[str]) -> tuple[str, list[str]]:
    scopes = [str(scope_id) for scope_id in accessible_scope_ids if str(scope_id)]
    if not scopes:
        return "0", []
    placeholders = ",".join("?" for _ in scopes)
    return f"scope_id IN ({placeholders})", scopes


def _reject_secret_like_value(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, str):
        if contains_secret_like_text(value):
            raise ExperienceValidationError(f"secret-like content is not allowed in playbook {path}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if contains_secret_like_text(key_text):
                raise ExperienceValidationError(f"secret-like content is not allowed in playbook {path}.<key>")
            _reject_secret_like_value(item, path=f"{path}.value")
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_secret_like_value(item, path=f"{path}[{index}]")


def _redact_secret_like_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secret_like_text(value)
    if isinstance(value, Mapping):
        return {redact_secret_like_text(str(key)): _redact_secret_like_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_redact_secret_like_value(item) for item in value]
    return value


def _redact_run(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("decision", "outcome", "outcome_reason", "model_name"):
        if key in item:
            item[key] = redact_secret_like_text(item[key])
    if "evidence" in item:
        item["evidence"] = redact_secret_like_text(item["evidence"])
    return item


def _step_dicts(playbook: ProceduralPlaybook) -> list[dict[str, Any]]:
    return [
        {
            "number": step.number,
            "capability_class": step.capability_class,
            "action": step.action,
            "evidence_required": step.evidence_required,
            "why": step.why,
            "previous_mistakes": list(step.previous_mistakes),
        }
        for step in playbook.steps
    ]


def _playbook_payload(playbook: ProceduralPlaybook) -> dict[str, Any]:
    return {
        "schema_version": playbook.schema_version,
        "task_class": playbook.task_class,
        "title": playbook.title,
        "trigger": playbook.trigger,
        "goal": playbook.goal,
        "preconditions": [dict(item) for item in playbook.preconditions],
        "steps": _step_dicts(playbook),
        "pitfalls": [dict(item) for item in playbook.pitfalls],
        "verification": list(playbook.verification),
        "cleanup": list(playbook.cleanup),
        "reuse_policy": dict(playbook.reuse_policy),
        "status": playbook.status,
        "confidence": playbook.confidence,
    }


def _serialize_row(row: sqlite3.Row, *, match_source: str = "", score: float = 0.0) -> dict[str, Any]:
    corrupt_fields: list[str] = []

    def load_json(field: str, default: Any, *, safety_core: bool = False) -> Any:
        value, corrupt = _json_loads_checked(row[field], default)
        if corrupt and safety_core:
            corrupt_fields.append(field)
        return value

    preconditions = load_json("preconditions", [], safety_core=True)
    steps = load_json("steps", [], safety_core=True)
    verification = load_json("verification", [], safety_core=True)
    environment_constraints = load_json("environment_constraints", {}, safety_core=True)
    reuse_policy = load_json("reuse_policy", {}, safety_core=True)
    payload = {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"]),
        "shared_scope_id": str(row["shared_scope_id"] or ""),
        "task_class": str(row["task_class"]),
        "title": str(row["title"]),
        "trigger": str(row["trigger"]),
        "goal": str(row["goal"]),
        "preconditions": preconditions,
        "steps": steps,
        "pitfalls": load_json("pitfalls", []),
        "verification": verification,
        "cleanup": load_json("cleanup", []),
        "evidence_anchors": load_json("evidence_anchors", []),
        "related_skills": load_json("related_skills", []),
        "environment_constraints": environment_constraints,
        "reuse_policy": reuse_policy,
        "status": str(row["status"]),
        "confidence": float(row["confidence"]),
        "success_count": int(row["success_count"]),
        "failure_count": int(row["failure_count"]),
        "stale_count": int(row["stale_count"]),
        "created_from_episode_id": str(row["created_from_episode_id"] or ""),
        "superseded_by": str(row["superseded_by"] or ""),
        "last_used_at": str(row["last_used_at"] or ""),
        "last_verified_at": str(row["last_verified_at"] or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "metadata": load_json("metadata", {}),
        "requires_operator_review": bool(corrupt_fields)
        or any(isinstance(step, Mapping) and str(step.get("capability_class") or "") in RISKY_CAPABILITY_CLASSES for step in steps)
        or str(row["status"]) != "promoted",
    }
    if corrupt_fields:
        payload["payload_corrupt_fields"] = sorted(corrupt_fields)
    if match_source:
        payload["match_source"] = match_source
    if score:
        payload["score"] = round(score, 4)
    return _redact_secret_like_value(payload)


def create_playbook(
    conn: sqlite3.Connection,
    *,
    playbook_id: str | None = None,
    scope_id: str,
    shared_scope_id: str = "",
    payload: Mapping[str, Any],
    status: str = "candidate",
    confidence: float | None = None,
    created_from_episode_id: str = "",
    evidence_anchors: Sequence[Any] | None = None,
    related_skills: Sequence[Any] | None = None,
    environment_constraints: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_payload = dict(payload)
    requested_status = str(status or normalized_payload.get("status") or "candidate").strip().lower()
    if requested_status != "candidate":
        raise ExperienceValidationError(
            f"playbook create cannot create {requested_status!r} directly; only candidate status is accepted; "
            "use review/promote after independent review"
        )
    _reject_secret_like_value(normalized_payload)
    _reject_secret_like_value(evidence_anchors or [], path="evidence_anchors")
    _reject_secret_like_value(related_skills or [], path="related_skills")
    _reject_secret_like_value(environment_constraints or {}, path="environment_constraints")
    _reject_secret_like_value(metadata or {}, path="metadata")
    _reject_secret_like_value(playbook_id or "", path="playbook_id")
    _reject_secret_like_value(created_from_episode_id or "", path="created_from_episode_id")
    normalized_payload["status"] = "candidate"
    if confidence is not None:
        normalized_payload["confidence"] = confidence
    playbook = validate_procedural_playbook(normalized_payload)
    if playbook.status not in PLAYBOOK_STATUSES:
        raise ExperienceValidationError("unsupported playbook status")
    now = _now_iso()
    pid = str(playbook_id or f"pb_{uuid.uuid4().hex}")
    row_values = {
        "id": pid,
        "scope_id": str(scope_id),
        "shared_scope_id": str(shared_scope_id or ""),
        "task_class": playbook.task_class,
        "title": playbook.title,
        "trigger": playbook.trigger,
        "goal": playbook.goal,
        "preconditions": _json_dumps([dict(item) for item in playbook.preconditions]),
        "steps": _json_dumps(_step_dicts(playbook)),
        "pitfalls": _json_dumps([dict(item) for item in playbook.pitfalls]),
        "verification": _json_dumps(list(playbook.verification)),
        "cleanup": _json_dumps(list(playbook.cleanup)),
        "evidence_anchors": _json_dumps(list(evidence_anchors or [])),
        "related_skills": _json_dumps(list(related_skills or [])),
        "environment_constraints": _json_dumps(dict(environment_constraints or {})),
        "reuse_policy": _json_dumps(dict(playbook.reuse_policy)),
        "status": playbook.status,
        "confidence": float(playbook.confidence),
        "created_from_episode_id": str(created_from_episode_id or ""),
        "created_at": now,
        "updated_at": now,
        "metadata": _json_dumps(dict(metadata or {})),
    }
    conn.execute(
        """
        INSERT INTO procedural_playbooks (
            id, scope_id, shared_scope_id, task_class, title, trigger, goal,
            preconditions, steps, pitfalls, verification, cleanup, evidence_anchors,
            related_skills, environment_constraints, reuse_policy, status, confidence,
            created_from_episode_id, created_at, updated_at, metadata
        ) VALUES (
            :id, :scope_id, :shared_scope_id, :task_class, :title, :trigger, :goal,
            :preconditions, :steps, :pitfalls, :verification, :cleanup, :evidence_anchors,
            :related_skills, :environment_constraints, :reuse_policy, :status, :confidence,
            :created_from_episode_id, :created_at, :updated_at, :metadata
        )
        """,
        row_values,
    )
    conn.execute(
        """
        INSERT INTO procedural_playbooks_fts(
            playbook_id, title, trigger, goal, preconditions, steps, pitfalls, verification
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pid,
            playbook.title,
            playbook.trigger,
            playbook.goal,
            row_values["preconditions"],
            row_values["steps"],
            row_values["pitfalls"],
            row_values["verification"],
        ),
    )
    conn.execute(
        """
        INSERT INTO playbook_versions(id, playbook_id, version, change_type, change_reason, snapshot, created_at)
        VALUES (?, ?, 1, 'create', ?, ?, ?)
        """,
        (f"pbv_{uuid.uuid4().hex}", pid, "initial candidate", _json_dumps(_playbook_payload(playbook)), now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM procedural_playbooks WHERE id = ?", (pid,)).fetchone()
    return _serialize_row(row)


def _lexical_score(row: sqlite3.Row, query: str) -> float:
    if not query.strip():
        return 0.01
    haystacks = {
        "task_class": str(row["task_class"]),
        "title": str(row["title"]),
        "trigger": str(row["trigger"]),
        "goal": str(row["goal"]),
        "steps": str(row["steps"]),
        "pitfalls": str(row["pitfalls"]),
        "verification": str(row["verification"]),
    }
    q = query.lower()
    terms = [term for term in q.replace("-", " ").replace("_", " ").split() if len(term) >= 2]
    score = 0.0
    if q and q in haystacks["task_class"].lower():
        score += 4.0
    for field, text in haystacks.items():
        lower = text.lower()
        hits = sum(1 for term in terms if term in lower)
        if hits:
            weight = 2.5 if field in {"title", "trigger", "goal"} else 1.0
            score += hits * weight
    return score


def _fts_query(query: str) -> str:
    terms = re.findall(r"[\w\u4e00-\u9fff]+", str(query or "").lower())
    terms = [term for term in terms if len(term) >= 2]
    deduped = list(dict.fromkeys(terms))[:12]
    return " OR ".join(f'"{term}"' for term in deduped)


def search_playbooks(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    accessible_scope_ids: Sequence[str],
    limit: int = 5,
    task_class: str = "",
    status: str = "",
) -> list[dict[str, Any]]:
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    where = [scope_sql]
    params: list[Any] = list(scope_params)
    if task_class:
        where.append("task_class = ?")
        params.append(task_class)
    if status:
        where.append("status = ?")
        params.append(status)
    rows = conn.execute(
        f"SELECT * FROM procedural_playbooks WHERE {' AND '.join(where)} ORDER BY updated_at DESC",
        params,
    ).fetchall()
    if query.strip():
        fts_query = _fts_query(query)
        if not fts_query or not rows:
            return []
        row_by_id = {str(row["id"]): row for row in rows}
        candidate_ids = list(row_by_id)
        placeholders = ",".join("?" for _ in candidate_ids)
        fts_rows = conn.execute(
            f"""
            SELECT playbook_id
            FROM procedural_playbooks_fts
            WHERE procedural_playbooks_fts MATCH ? AND playbook_id IN ({placeholders})
            """,
            [fts_query, *candidate_ids],
        ).fetchall()
        scored = [(_lexical_score(row_by_id[str(fts_row["playbook_id"])], query), "fts", row_by_id[str(fts_row["playbook_id"])]) for fts_row in fts_rows]
        scored = [item for item in scored if item[0] > 0]
        scored.sort(key=lambda item: (item[0], float(item[2]["confidence"]), str(item[2]["updated_at"])), reverse=True)
        return [_serialize_row(row, match_source=source, score=score) for score, source, row in scored[: max(1, min(50, int(limit or 5)))]]

    scored: list[tuple[float, str, sqlite3.Row]] = []
    for row in rows:
        scored.append((_lexical_score(row, query), "recent", row))
    scored.sort(key=lambda item: (item[0], float(item[2]["confidence"]), str(item[2]["updated_at"])), reverse=True)
    return [_serialize_row(row, match_source=source, score=score) for score, source, row in scored[: max(1, min(50, int(limit or 5)))]]


def inspect_playbook(conn: sqlite3.Connection, *, playbook_id: str, accessible_scope_ids: Sequence[str]) -> dict[str, Any]:
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    row = conn.execute(f"SELECT * FROM procedural_playbooks WHERE id = ? AND {scope_sql}", [playbook_id, *scope_params]).fetchone()
    if row is None:
        return {"found": False, "id": redact_secret_like_text(playbook_id)}
    versions = [
        _redact_secret_like_value(dict(item))
        for item in conn.execute(
            "SELECT id, version, change_type, change_reason, created_at FROM playbook_versions WHERE playbook_id = ? ORDER BY version DESC",
            (playbook_id,),
        ).fetchall()
    ]
    run_scope_sql, run_scope_params = _run_scope_predicate(accessible_scope_ids)
    runs = [
        _redact_run(item)
        for item in conn.execute(
            f"""
            SELECT id, decision, outcome, outcome_reason, started_at, finished_at
            FROM experience_runs
            WHERE playbook_id = ? AND {run_scope_sql}
            ORDER BY started_at DESC LIMIT 20
            """,
            [playbook_id, *run_scope_params],
        ).fetchall()
    ]
    return {"found": True, "playbook": _serialize_row(row), "versions": versions, "runs": runs}


def _next_version(conn: sqlite3.Connection, playbook_id: str) -> int:
    current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM playbook_versions WHERE playbook_id = ?", (playbook_id,)).fetchone()[0]
    return int(current or 0) + 1


def review_playbook(
    conn: sqlite3.Connection,
    *,
    playbook_id: str,
    accessible_scope_ids: Sequence[str],
    action: str,
    reason: str = "",
    superseded_by: str = "",
) -> dict[str, Any]:
    _reject_secret_like_value(playbook_id, path="review.playbook_id")
    action_to_status = {
        "review": "reviewed",
        "reviewed": "reviewed",
        "promote": "promoted",
        "promoted": "promoted",
        "needs_review": "needs_review",
        "quarantine": "quarantined",
        "quarantined": "quarantined",
        "supersede": "superseded",
        "superseded": "superseded",
    }
    status = action_to_status.get(str(action).strip().lower())
    if status is None:
        raise ExperienceValidationError("unsupported review action")
    _reject_secret_like_value(reason, path="review.reason")
    _reject_secret_like_value(superseded_by, path="review.superseded_by")
    inspected = inspect_playbook(conn, playbook_id=playbook_id, accessible_scope_ids=accessible_scope_ids)
    if not inspected.get("found"):
        return {"reviewed": False, "id": playbook_id, "error": "not_found"}
    now = _now_iso()
    version = _next_version(conn, playbook_id)
    conn.execute(
        "UPDATE procedural_playbooks SET status = ?, superseded_by = ?, updated_at = ? WHERE id = ?",
        (status, superseded_by if status == "superseded" else "", now, playbook_id),
    )
    updated = conn.execute("SELECT * FROM procedural_playbooks WHERE id = ?", (playbook_id,)).fetchone()
    conn.execute(
        """
        INSERT INTO playbook_versions(id, playbook_id, version, change_type, change_reason, snapshot, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (f"pbv_{uuid.uuid4().hex}", playbook_id, version, status, reason, _json_dumps(_serialize_row(updated)), now),
    )
    conn.commit()
    return {"reviewed": True, "id": playbook_id, "status": status, "version": version}


def _recompute_confidence(row: sqlite3.Row) -> float:
    success = int(row["success_count"])
    failure = int(row["failure_count"])
    stale = int(row["stale_count"])
    base = float(row["confidence"])
    outcome_score = (success + 1) / (success + failure + 2)
    review_bonus = 0.05 if str(row["status"]) == "promoted" else 0.0
    stale_penalty = min(0.20, 0.05 * stale)
    recent_failure_penalty = 0.15 if failure and (failure >= success) else 0.0
    value = 0.65 * base + 0.30 * outcome_score + review_bonus - stale_penalty - recent_failure_penalty
    return max(0.0, min(0.95, value))


def record_playbook_feedback(
    conn: sqlite3.Connection,
    *,
    playbook_id: str,
    scope_id: str,
    outcome: str,
    accessible_scope_ids: Sequence[str] | None = None,
    decision: str = "guided_reuse",
    evidence: Sequence[Any] | None = None,
    outcome_reason: str = "",
    model_name: str = "",
    tool_call_count: int = 0,
    token_estimate: int = 0,
) -> dict[str, Any]:
    _reject_secret_like_value(playbook_id, path="feedback.playbook_id")
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids if accessible_scope_ids is not None else [scope_id])
    row = conn.execute(f"SELECT * FROM procedural_playbooks WHERE id = ? AND {scope_sql}", [playbook_id, *scope_params]).fetchone()
    if row is None:
        return {"recorded": False, "id": playbook_id, "error": "not_found"}
    normalized_outcome = str(outcome or "unknown").strip().lower()
    if normalized_outcome not in {"success", "partial", "failed", "stale", "misleading", "unknown"}:
        raise ExperienceValidationError("unsupported feedback outcome")
    normalized_decision = str(decision or "guided_reuse").strip().lower()
    if normalized_decision not in {"direct_reuse", "guided_reuse", "no_reuse"}:
        raise ExperienceValidationError("unsupported feedback decision")
    _reject_secret_like_value(normalized_decision, path="feedback.decision")
    _reject_secret_like_value(list(evidence or []), path="feedback.evidence")
    _reject_secret_like_value(outcome_reason, path="feedback.outcome_reason")
    _reject_secret_like_value(model_name, path="feedback.model_name")
    now = _now_iso()
    current_status = str(row["status"])
    if current_status in {"quarantined", "superseded"}:
        return {"recorded": False, "id": playbook_id, "error": "terminal_status", "status": current_status}
    global_update_allowed = str(scope_id) == str(row["scope_id"])
    conn.execute(
        """
        INSERT INTO experience_runs(
            id, playbook_id, scope_id, decision, confidence_at_use, evidence, outcome,
            outcome_reason, model_name, tool_call_count, token_estimate, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"xrun_{uuid.uuid4().hex}",
            playbook_id,
            scope_id,
            normalized_decision,
            float(row["confidence"]),
            _json_dumps(list(evidence or [])),
            normalized_outcome,
            outcome_reason,
            model_name,
            int(tool_call_count or 0),
            int(token_estimate or 0),
            now,
            now,
        ),
    )
    if not global_update_allowed:
        conn.commit()
        return {
            "recorded": True,
            "global_updated": False,
            "id": playbook_id,
            "outcome": normalized_outcome,
            "status": current_status,
            "confidence": float(row["confidence"]),
            "success_count": int(row["success_count"]),
            "failure_count": int(row["failure_count"]),
            "stale_count": int(row["stale_count"]),
        }
    success_delta = 1 if normalized_outcome == "success" else 0
    failure_delta = 1 if normalized_outcome in {"failed", "misleading"} else 0
    stale_delta = 1 if normalized_outcome == "stale" else 0
    new_status = str(row["status"])
    if normalized_outcome in {"failed", "misleading", "stale"}:
        new_status = "needs_review"
    conn.execute(
        """
        UPDATE procedural_playbooks
        SET success_count = success_count + ?, failure_count = failure_count + ?, stale_count = stale_count + ?,
            status = ?, last_used_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (success_delta, failure_delta, stale_delta, new_status, now, now, playbook_id),
    )
    updated = conn.execute("SELECT * FROM procedural_playbooks WHERE id = ?", (playbook_id,)).fetchone()
    new_confidence = _recompute_confidence(updated)
    conn.execute("UPDATE procedural_playbooks SET confidence = ? WHERE id = ?", (new_confidence, playbook_id))
    conn.commit()
    final = conn.execute("SELECT * FROM procedural_playbooks WHERE id = ?", (playbook_id,)).fetchone()
    return {
        "recorded": True,
        "global_updated": True,
        "id": playbook_id,
        "outcome": normalized_outcome,
        "status": str(final["status"]),
        "confidence": float(final["confidence"]),
        "success_count": int(final["success_count"]),
        "failure_count": int(final["failure_count"]),
        "stale_count": int(final["stale_count"]),
    }


def experience_stats(conn: sqlite3.Connection, *, accessible_scope_ids: Sequence[str]) -> dict[str, Any]:
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    rows = conn.execute(f"SELECT id, status FROM procedural_playbooks WHERE {scope_sql}", scope_params).fetchall()
    ids = [str(row["id"]) for row in rows]
    by_status: dict[str, int] = {}
    for row in rows:
        status = redact_secret_like_text(row["status"])
        by_status[status] = by_status.get(status, 0) + 1
    by_outcome: dict[str, int] = {}
    total_runs = 0
    if ids:
        placeholders = ",".join("?" for _ in ids)
        run_scope_sql, run_scope_params = _run_scope_predicate(accessible_scope_ids)
        for row in conn.execute(
            f"""
            SELECT outcome, COUNT(*)
            FROM experience_runs
            WHERE playbook_id IN ({placeholders}) AND {run_scope_sql}
            GROUP BY outcome
            """,
            [*ids, *run_scope_params],
        ):
            outcome = redact_secret_like_text(row[0])
            count = int(row[1])
            by_outcome[outcome] = count
            total_runs += count
    return {
        "playbooks": {"total": len(rows), "by_status": dict(sorted(by_status.items()))},
        "runs": {"total": total_runs, "by_outcome": dict(sorted(by_outcome.items()))},
    }
