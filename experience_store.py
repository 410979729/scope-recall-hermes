"""SQLite storage layer for Experience playbooks, feedback, reviews, and promotion ledgers.

This module owns durable playbook state; callers should prefer its transaction-aware helpers over ad-hoc SQL."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .capture_filters import contains_secret_like_text, sanitize_report_text
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


def _sanitize_report_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_report_text(value)
    if isinstance(value, Mapping):
        return {sanitize_report_text(str(key)): _sanitize_report_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_sanitize_report_value(item) for item in value]
    return value


def _redact_run(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("decision", "outcome", "outcome_reason", "model_name"):
        if key in item:
            item[key] = sanitize_report_text(item[key])
    if "evidence" in item:
        item["evidence"] = sanitize_report_text(item["evidence"])
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


def _related_skill_names(value: Any) -> list[str]:
    raw = value
    if isinstance(raw, str):
        raw = _json_loads(raw, [])
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray, str)):
        return []
    names: list[str] = []
    for item in raw:
        name = sanitize_report_text(str(item or "").strip())
        if name and name not in names:
            names.append(name)
    return names


def _sync_skill_anchors_for_playbook(conn: sqlite3.Connection, row: sqlite3.Row, *, reason: str = "") -> None:
    playbook_id = str(row["id"])
    skills = _related_skill_names(row["related_skills"])
    if not skills:
        return
    existing = {
        str(existing_row["skill_name"])
        for existing_row in conn.execute(
            "SELECT skill_name FROM skill_anchors WHERE playbook_id = ?",
            (playbook_id,),
        ).fetchall()
    }
    now = _now_iso()
    safe_reason = sanitize_report_text(str(reason or "playbook promoted"))[:1000]
    for skill_name in skills:
        if skill_name in existing:
            continue
        conn.execute(
            """
            INSERT INTO skill_anchors(id, playbook_id, skill_name, load_policy, reason, created_at)
            VALUES (?, ?, ?, 'optional_reference', ?, ?)
            """,
            (f"ska_{uuid.uuid4().hex}", playbook_id, skill_name, safe_reason, now),
        )


def _skill_governance_for_playbook(conn: sqlite3.Connection, playbook_id: str, related_skills: Sequence[str]) -> dict[str, Any]:
    anchors = [
        {"skill_name": str(row["skill_name"]), "load_policy": str(row["load_policy"]), "reason": str(row["reason"])}
        for row in conn.execute(
            "SELECT skill_name, load_policy, reason FROM skill_anchors WHERE playbook_id = ? ORDER BY skill_name",
            (playbook_id,),
        ).fetchall()
    ]
    open_conflicts = [
        {
            "skill_name": str(row["skill_name"]),
            "conflicting_source": str(row["conflicting_source"]),
            "conflict_summary": sanitize_report_text(str(row["conflict_summary"])),
            "resolution": str(row["resolution"]),
        }
        for row in conn.execute(
            """
            SELECT skill_name, conflicting_source, conflict_summary, resolution
            FROM skill_conflicts
            WHERE playbook_id = ? AND status = 'open'
            ORDER BY created_at DESC
            """,
            (playbook_id,),
        ).fetchall()
    ]
    anchored = {item["skill_name"] for item in anchors}
    missing = [skill for skill in related_skills if skill and skill not in anchored]
    return {"anchors": anchors, "open_conflicts": open_conflicts, "missing_anchors": missing}


def _attach_skill_governance(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    related_skills = _related_skill_names(payload.get("related_skills") or [])
    if not related_skills:
        payload["skill_governance"] = {"anchors": [], "open_conflicts": [], "missing_anchors": []}
        return payload
    payload["skill_governance"] = _skill_governance_for_playbook(conn, str(payload.get("id") or ""), related_skills)
    return payload


def backfill_skill_anchors(conn: sqlite3.Connection, *, limit: int = 1000) -> dict[str, Any]:
    """Ensure existing promoted playbooks with related skills have DB anchors."""

    rows = conn.execute(
        """
        SELECT *
        FROM procedural_playbooks
        WHERE status = 'promoted'
          AND related_skills IS NOT NULL
          AND related_skills NOT IN ('', '[]')
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (max(1, int(limit or 1000)),),
    ).fetchall()
    checked = 0
    backfilled = 0
    for row in rows:
        skills = _related_skill_names(row["related_skills"])
        if not skills:
            continue
        checked += 1
        governance = _skill_governance_for_playbook(conn, str(row["id"]), skills)
        if not governance.get("missing_anchors"):
            continue
        _sync_skill_anchors_for_playbook(conn, row, reason="startup backfill for promoted playbook related_skills")
        backfilled += 1
    if backfilled:
        conn.commit()
    return {"checked": checked, "backfilled": backfilled}


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
    return _sanitize_report_value(payload)


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
    """Create a reviewable Experience playbook row and associated metadata.

    Creation does not imply promotion: lifecycle, quality, and review fields remain explicit so automation cannot skip governance."""
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
    normalized_payload = dict(_sanitize_report_value(normalized_payload))
    safe_evidence_anchors = _sanitize_report_value(list(evidence_anchors or []))
    safe_related_skills = _sanitize_report_value(list(related_skills or []))
    safe_environment_constraints = _sanitize_report_value(dict(environment_constraints or {}))
    safe_metadata = _sanitize_report_value(dict(metadata or {}))
    safe_created_from_episode_id = sanitize_report_text(created_from_episode_id or "")
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
        "evidence_anchors": _json_dumps(safe_evidence_anchors),
        "related_skills": _json_dumps(safe_related_skills),
        "environment_constraints": _json_dumps(safe_environment_constraints),
        "reuse_policy": _json_dumps(dict(playbook.reuse_policy)),
        "status": playbook.status,
        "confidence": float(playbook.confidence),
        "created_from_episode_id": safe_created_from_episode_id,
        "created_at": now,
        "updated_at": now,
        "metadata": _json_dumps(safe_metadata),
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
    return _attach_skill_governance(conn, _serialize_row(row))


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
        return [
            _attach_skill_governance(conn, _serialize_row(row, match_source=source, score=score))
            for score, source, row in scored[: max(1, min(50, int(limit or 5)))]
        ]

    scored: list[tuple[float, str, sqlite3.Row]] = []
    for row in rows:
        scored.append((_lexical_score(row, query), "recent", row))
    scored.sort(key=lambda item: (item[0], float(item[2]["confidence"]), str(item[2]["updated_at"])), reverse=True)
    return [
        _attach_skill_governance(conn, _serialize_row(row, match_source=source, score=score))
        for score, source, row in scored[: max(1, min(50, int(limit or 5)))]
    ]


def _dedupe_group_key(row: sqlite3.Row) -> tuple[str, str]:
    task_class = re.sub(r"\s+", " ", str(row["task_class"] or "").strip().lower())
    title = re.sub(r"\s+", " ", str(row["title"] or "").strip().lower())
    return task_class, title


def _canonical_sort_key(row: sqlite3.Row) -> tuple[int, float, str, str]:
    status_rank = {"promoted": 3, "reviewed": 2, "needs_review": 1, "candidate": 0}.get(str(row["status"]), -1)
    return (status_rank, float(row["confidence"]), str(row["updated_at"]), str(row["id"]))


def find_duplicate_playbooks(
    conn: sqlite3.Connection,
    *,
    accessible_scope_ids: Sequence[str],
    status: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    where = [scope_sql]
    params: list[Any] = list(scope_params)
    if status:
        where.append("status = ?")
        params.append(status)
    else:
        where.append("status IN ('candidate', 'needs_review', 'reviewed', 'promoted')")
    rows = conn.execute(
        f"SELECT * FROM procedural_playbooks WHERE {' AND '.join(where)} ORDER BY updated_at DESC, confidence DESC",
        params,
    ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = _dedupe_group_key(row)
        if not key[0] or not key[1]:
            continue
        grouped.setdefault(key, []).append(row)
    groups: list[dict[str, Any]] = []
    for (_task_class, _title), items in grouped.items():
        remaining = list(items)
        while remaining:
            component = [remaining.pop(0)]
            changed = True
            while changed:
                changed = False
                rest: list[sqlite3.Row] = []
                for row in remaining:
                    if any(_same_owner_scope(row, existing) for existing in component):
                        component.append(row)
                        changed = True
                    else:
                        rest.append(row)
                remaining = rest
            if len(component) < 2:
                continue
            sorted_items = sorted(component, key=_canonical_sort_key, reverse=True)
            canonical = sorted_items[0]
            groups.append(
                {
                    "task_class": str(canonical["task_class"]),
                    "title": str(canonical["title"]),
                    "count": len(sorted_items),
                    "canonical_id": str(canonical["id"]),
                    "items": [_serialize_row(row) for row in sorted_items],
                }
            )
    groups.sort(key=lambda item: (int(item["count"]), str(item["title"])), reverse=True)
    return groups[: max(1, min(100, int(limit or 50)))]


def _fetch_accessible_playbook(conn: sqlite3.Connection, playbook_id: str, accessible_scope_ids: Sequence[str]) -> sqlite3.Row | None:
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    return conn.execute(f"SELECT * FROM procedural_playbooks WHERE id = ? AND {scope_sql}", [playbook_id, *scope_params]).fetchone()


def _owner_scope_values(row: sqlite3.Row) -> set[str]:
    """Return the owner/private and shared scope ids that define a playbook lineage boundary."""

    return {value for value in (str(row["scope_id"] or ""), str(row["shared_scope_id"] or "")) if value}


def _same_owner_scope(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    """Whether two playbooks may be reviewed/merged as the same owner lineage.

    Some bootstrap/core playbooks store the user-level scope in ``scope_id`` while
    auto-generated session playbooks store that same value in ``shared_scope_id``.
    Treating either field intersection as the owner boundary avoids false
    cross-scope mismatches while still blocking unrelated/private scopes.
    """

    return bool(_owner_scope_values(left) & _owner_scope_values(right))


def _normalize_semantic_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _same_playbook_semantics(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    """Whether two playbooks are safe to put in the same lifecycle lineage by default."""

    return (
        _normalize_semantic_key(left["task_class"]) == _normalize_semantic_key(right["task_class"])
        and _normalize_semantic_key(left["title"]) == _normalize_semantic_key(right["title"])
    )


def _semantic_mismatch_payload(source: sqlite3.Row, target: sqlite3.Row) -> dict[str, Any]:
    return {
        "source_id": str(source["id"]),
        "source_task_class": sanitize_report_text(str(source["task_class"] or "")),
        "source_title": sanitize_report_text(str(source["title"] or "")),
        "target_id": str(target["id"]),
        "target_task_class": sanitize_report_text(str(target["task_class"] or "")),
        "target_title": sanitize_report_text(str(target["title"] or "")),
    }


_PLAYBOOK_VALIDATION_JSON_FIELDS = (
    "preconditions",
    "steps",
    "pitfalls",
    "verification",
    "cleanup",
    "reuse_policy",
)


def _validation_payload_from_row(
    row: sqlite3.Row,
    *,
    status: str,
) -> dict[str, Any]:
    """Rebuild the generic validator payload from authoritative SQLite data."""

    payload: dict[str, Any] = {
        "schema_version": "procedural_playbook.v1",
        "task_class": str(row["task_class"]),
        "title": str(row["title"]),
        "trigger": str(row["trigger"]),
        "goal": str(row["goal"]),
        "status": status,
        "confidence": float(row["confidence"]),
    }
    for field in _PLAYBOOK_VALIDATION_JSON_FIELDS:
        try:
            payload[field] = json.loads(str(row[field]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExperienceValidationError(
                f"persisted playbook {field} must contain valid JSON"
            ) from exc
    return payload


def _review_validation_token(
    conn: sqlite3.Connection,
    *,
    source_row: sqlite3.Row,
    canonical_row: sqlite3.Row | None,
    status: str,
    target_superseded_by: str,
    reason: str,
    force_cross_class: bool,
) -> str:
    """Bind one review plan to every authoritative input used by apply."""

    row_fields = (
        "id",
        "scope_id",
        "shared_scope_id",
        "task_class",
        "title",
        "trigger",
        "goal",
        "preconditions",
        "steps",
        "pitfalls",
        "verification",
        "cleanup",
        "evidence_anchors",
        "related_skills",
        "environment_constraints",
        "reuse_policy",
        "status",
        "confidence",
        "superseded_by",
        "updated_at",
        "metadata",
    )

    def row_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {field: row[field] for field in row_fields}

    token_payload = {
        "source": row_payload(source_row),
        "canonical": row_payload(canonical_row),
        "requested_status": status,
        "requested_superseded_by": target_superseded_by,
        "reason": reason,
        "force_cross_class": bool(force_cross_class),
        "next_version": _next_version(conn, str(source_row["id"])),
    }
    encoded = _json_dumps(token_payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _insert_playbook_version(conn: sqlite3.Connection, *, row: sqlite3.Row, change_type: str, reason: str, created_at: str) -> int:
    version = _next_version(conn, str(row["id"]))
    conn.execute(
        """
        INSERT INTO playbook_versions(id, playbook_id, version, change_type, change_reason, snapshot, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (f"pbv_{uuid.uuid4().hex}", str(row["id"]), version, change_type, reason, _json_dumps(_serialize_row(row)), created_at),
    )
    return version


def merge_playbooks(
    conn: sqlite3.Connection,
    *,
    target_id: str,
    source_ids: Sequence[str],
    accessible_scope_ids: Sequence[str],
    reason: str = "",
    dry_run: bool = True,
    force_cross_class: bool = False,
) -> dict[str, Any]:
    """Merge duplicate or superseded Experience playbooks while preserving audit metadata.

    Merging should make the surviving playbook clear and leave enough history to understand why others were superseded."""
    _reject_secret_like_value(target_id, path="merge.target_id")
    _reject_secret_like_value(list(source_ids), path="merge.source_ids")
    _reject_secret_like_value(reason, path="merge.reason")
    safe_target_id = sanitize_report_text(str(target_id or "").strip())
    safe_reason = sanitize_report_text(reason or "")
    normalized_sources = list(dict.fromkeys(sanitize_report_text(str(item or "").strip()) for item in source_ids if str(item or "").strip()))
    if not safe_target_id:
        return {"merged": False, "dry_run": bool(dry_run), "target_id": "", "error": "target_required"}
    if not normalized_sources:
        return {"merged": False, "dry_run": bool(dry_run), "target_id": safe_target_id, "error": "source_required"}
    if safe_target_id in normalized_sources:
        return {"merged": False, "dry_run": bool(dry_run), "target_id": safe_target_id, "error": "self_merge"}
    target = _fetch_accessible_playbook(conn, safe_target_id, accessible_scope_ids)
    if target is None:
        return {"merged": False, "dry_run": bool(dry_run), "target_id": safe_target_id, "error": "target_not_found"}
    source_rows: list[sqlite3.Row] = []
    missing: list[str] = []
    for source_id in normalized_sources:
        row = _fetch_accessible_playbook(conn, source_id, accessible_scope_ids)
        if row is None:
            missing.append(source_id)
        else:
            source_rows.append(row)
    if missing:
        return {"merged": False, "dry_run": bool(dry_run), "target_id": safe_target_id, "error": "source_not_found", "missing_source_ids": missing}
    already_superseded_rows = [row for row in source_rows if str(row["status"]) == "superseded" and str(row["superseded_by"] or "") == safe_target_id]
    needs_change_rows = [row for row in source_rows if row not in already_superseded_rows]
    owner_mismatches = [str(row["id"]) for row in needs_change_rows if not _same_owner_scope(row, target)]
    if owner_mismatches:
        return {
            "merged": False,
            "dry_run": bool(dry_run),
            "target_id": safe_target_id,
            "error": "scope_owner_mismatch",
            "source_ids": owner_mismatches,
        }
    semantic_mismatches = [row for row in needs_change_rows if not _same_playbook_semantics(row, target)]
    if semantic_mismatches and not force_cross_class:
        return {
            "merged": False,
            "dry_run": bool(dry_run),
            "target_id": safe_target_id,
            "error": "semantic_mismatch",
            "mismatches": [_semantic_mismatch_payload(row, target) for row in semantic_mismatches],
        }
    if semantic_mismatches and not safe_reason:
        return {
            "merged": False,
            "dry_run": bool(dry_run),
            "target_id": safe_target_id,
            "error": "force_reason_required",
            "mismatches": [_semantic_mismatch_payload(row, target) for row in semantic_mismatches],
        }
    payload = {
        "merged": False,
        "dry_run": bool(dry_run),
        "target_id": safe_target_id,
        "source_ids": [str(row["id"]) for row in source_rows],
        "changed_source_ids": [str(row["id"]) for row in needs_change_rows],
        "idempotent_source_ids": [str(row["id"]) for row in already_superseded_rows],
        "changed": bool(needs_change_rows),
        "idempotent": not bool(needs_change_rows),
        "reason": safe_reason,
        "target": _serialize_row(target),
        "sources": [_serialize_row(row) for row in source_rows],
    }
    if dry_run:
        return payload
    if not needs_change_rows:
        payload["merged"] = True
        payload["changed"] = False
        payload["idempotent"] = True
        payload["versions"] = []
        return payload
    now = _now_iso()
    versions: list[dict[str, Any]] = []
    conn.execute("UPDATE procedural_playbooks SET updated_at = ? WHERE id = ?", (now, safe_target_id))
    updated_target = conn.execute("SELECT * FROM procedural_playbooks WHERE id = ?", (safe_target_id,)).fetchone()
    versions.append({"playbook_id": safe_target_id, "version": _insert_playbook_version(conn, row=updated_target, change_type="merge", reason=safe_reason, created_at=now)})
    for row in needs_change_rows:
        source_id = str(row["id"])
        conn.execute("UPDATE procedural_playbooks SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ?", (safe_target_id, now, source_id))
        updated_source = conn.execute("SELECT * FROM procedural_playbooks WHERE id = ?", (source_id,)).fetchone()
        versions.append({"playbook_id": source_id, "version": _insert_playbook_version(conn, row=updated_source, change_type="superseded", reason=safe_reason, created_at=now)})
    conn.commit()
    payload["merged"] = True
    payload["versions"] = versions
    return payload


def inspect_playbook(conn: sqlite3.Connection, *, playbook_id: str, accessible_scope_ids: Sequence[str]) -> dict[str, Any]:
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    row = conn.execute(f"SELECT * FROM procedural_playbooks WHERE id = ? AND {scope_sql}", [playbook_id, *scope_params]).fetchone()
    if row is None:
        return {"found": False, "id": sanitize_report_text(playbook_id)}
    versions = [
        _sanitize_report_value(dict(item))
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
    return {"found": True, "playbook": _attach_skill_governance(conn, _serialize_row(row)), "versions": versions, "runs": runs}


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
    dry_run: bool = False,
    force_cross_class: bool = False,
    validated_payload: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Apply an operator review decision to an Experience playbook.

    ``validated_payload`` binds mutation to a prior dry-run over the same rows.
    ``commit=False`` lets operator flows include schema migration, review writes,
    and receipt finalization in one caller-owned transaction.
    """

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
    safe_reason = sanitize_report_text(reason)
    safe_superseded_by = sanitize_report_text(superseded_by)
    source_row = _fetch_accessible_playbook(
        conn, playbook_id, accessible_scope_ids
    )
    if source_row is None:
        return {
            "reviewed": False,
            "dry_run": bool(dry_run),
            "changed": False,
            "id": playbook_id,
            "error": "not_found",
        }
    canonical_row: sqlite3.Row | None = None
    if status == "superseded":
        if not safe_superseded_by:
            return {
                "reviewed": False,
                "dry_run": bool(dry_run),
                "changed": False,
                "id": playbook_id,
                "error": "superseded_by_required",
            }
        if safe_superseded_by == playbook_id:
            return {
                "reviewed": False,
                "dry_run": bool(dry_run),
                "changed": False,
                "id": playbook_id,
                "error": "self_supersede",
            }
        canonical_row = _fetch_accessible_playbook(
            conn, safe_superseded_by, accessible_scope_ids
        )
        if canonical_row is None:
            return {
                "reviewed": False,
                "dry_run": bool(dry_run),
                "changed": False,
                "id": playbook_id,
                "error": "superseded_by_not_found",
                "superseded_by": safe_superseded_by,
            }
        if not _same_owner_scope(source_row, canonical_row):
            return {
                "reviewed": False,
                "dry_run": bool(dry_run),
                "changed": False,
                "id": playbook_id,
                "error": "superseded_by_scope_mismatch",
                "superseded_by": safe_superseded_by,
            }
        if not _same_playbook_semantics(source_row, canonical_row):
            mismatch = _semantic_mismatch_payload(source_row, canonical_row)
            if not force_cross_class:
                return {
                    "reviewed": False,
                    "dry_run": bool(dry_run),
                    "changed": False,
                    "id": playbook_id,
                    "error": "semantic_mismatch",
                    "superseded_by": safe_superseded_by,
                    "mismatch": mismatch,
                }
            if not safe_reason:
                return {
                    "reviewed": False,
                    "dry_run": bool(dry_run),
                    "changed": False,
                    "id": playbook_id,
                    "error": "force_reason_required",
                    "superseded_by": safe_superseded_by,
                    "mismatch": mismatch,
                }
    target_superseded_by = safe_superseded_by if status == "superseded" else ""
    if status == "promoted":
        promotion_payload = _validation_payload_from_row(source_row, status=status)
        _reject_secret_like_value(promotion_payload, path="review.promote")
        validate_procedural_playbook(promotion_payload)
    validation_token = _review_validation_token(
        conn,
        source_row=source_row,
        canonical_row=canonical_row,
        status=status,
        target_superseded_by=target_superseded_by,
        reason=safe_reason,
        force_cross_class=force_cross_class,
    )
    if validated_payload is not None:
        expected_token = str(validated_payload.get("validation_token") or "")
        expected_status = str(validated_payload.get("status") or "")
        expected_id = str(validated_payload.get("id") or "")
        expected_superseded_by = str(
            validated_payload.get("superseded_by") or ""
        )
        if (
            not expected_token
            or expected_token != validation_token
            or expected_status != status
            or expected_id != playbook_id
            or expected_superseded_by != target_superseded_by
        ):
            return {
                "reviewed": False,
                "dry_run": False,
                "changed": False,
                "id": playbook_id,
                "error": "stale_validation",
            }
    if (
        str(source_row["status"]) == status
        and str(source_row["superseded_by"] or "") == target_superseded_by
    ):
        current_version = _next_version(conn, playbook_id) - 1
        result = {
            "reviewed": True,
            "dry_run": bool(dry_run),
            "changed": False,
            "idempotent": True,
            "id": playbook_id,
            "status": status,
            "version": current_version,
            "validation_token": validation_token,
        }
        if status == "superseded":
            result["superseded_by"] = safe_superseded_by
        return result
    if dry_run:
        result = {
            "reviewed": False,
            "dry_run": True,
            "changed": True,
            "id": playbook_id,
            "current_status": str(source_row["status"]),
            "status": status,
            "validation_token": validation_token,
        }
        if status == "superseded":
            result["current_superseded_by"] = str(
                source_row["superseded_by"] or ""
            )
            result["superseded_by"] = safe_superseded_by
        return result

    started_transaction = False
    if not conn.in_transaction:
        if not commit:
            raise ExperienceValidationError(
                "review_playbook(commit=False) requires an active caller-owned transaction"
            )
        conn.execute("BEGIN")
        started_transaction = True

    savepoint = f"review_playbook_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        now = _now_iso()
        version = _next_version(conn, playbook_id)
        conn.execute(
            "UPDATE procedural_playbooks SET status = ?, superseded_by = ?, updated_at = ? WHERE id = ?",
            (status, target_superseded_by, now, playbook_id),
        )
        updated = conn.execute(
            "SELECT * FROM procedural_playbooks WHERE id = ?", (playbook_id,)
        ).fetchone()
        if status == "promoted":
            _sync_skill_anchors_for_playbook(
                conn, updated, reason=safe_reason or "playbook promoted"
            )
        conn.execute(
            """
            INSERT INTO playbook_versions(id, playbook_id, version, change_type, change_reason, snapshot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"pbv_{uuid.uuid4().hex}",
                playbook_id,
                version,
                status,
                safe_reason,
                _json_dumps(_serialize_row(updated)),
                now,
            ),
        )
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_transaction and conn.in_transaction:
            conn.rollback()
        raise
    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    if commit:
        try:
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
    result = {
        "reviewed": True,
        "dry_run": False,
        "changed": True,
        "id": playbook_id,
        "status": status,
        "version": version,
        "validation_token": validation_token,
    }
    if status == "superseded":
        result["superseded_by"] = safe_superseded_by
    return result


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


def _record_skill_conflicts_from_feedback(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    outcome: str,
    outcome_reason: str = "",
    created_at: str,
) -> int:
    if outcome not in {"stale", "misleading"}:
        return 0
    playbook_id = str(row["id"])
    skills = _related_skill_names(row["related_skills"])
    if not skills:
        return 0
    summary = sanitize_report_text(str(outcome_reason or f"Playbook feedback marked this experience as {outcome}."))[:1200]
    inserted = 0
    for skill_name in skills:
        existing = conn.execute(
            """
            SELECT 1 FROM skill_conflicts
            WHERE playbook_id = ? AND skill_name = ? AND status = 'open' AND conflicting_source = 'feedback'
            """,
            (playbook_id, skill_name),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO skill_conflicts(
                id, playbook_id, skill_name, conflicting_source, conflict_summary,
                resolution, status, created_at, metadata
            ) VALUES (?, ?, ?, 'feedback', ?, 'needs_agent_review', 'open', ?, ?)
            """,
            (
                f"sc_{uuid.uuid4().hex}",
                playbook_id,
                skill_name,
                summary,
                created_at,
                _json_dumps({"outcome": outcome}),
            ),
        )
        inserted += 1
    return inserted


def record_experience_preflight_run(
    conn: sqlite3.Connection,
    *,
    playbook: Mapping[str, Any],
    scope_id: str,
    decision: str,
    query: str,
    reasons: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Persist the result of one Experience preflight evaluation.

    Recording the evidence lets later promotion decisions cite the exact gate outcomes that were active at review time."""
    playbook_id = sanitize_report_text(str(playbook.get("id") or "").strip())
    safe_scope_id = sanitize_report_text(str(scope_id or "").strip())
    safe_decision = sanitize_report_text(str(decision or "guided_reuse").strip().lower())
    if not playbook_id or not safe_scope_id:
        return {"recorded": False, "error": "missing_playbook_or_scope"}
    if safe_decision not in {"direct_reuse", "guided_reuse"}:
        return {"recorded": False, "id": playbook_id, "error": "decision_not_reused", "decision": safe_decision}
    _reject_secret_like_value(playbook_id, path="preflight.playbook_id")
    _reject_secret_like_value(safe_scope_id, path="preflight.scope_id")
    _reject_secret_like_value(safe_decision, path="preflight.decision")
    safe_query = sanitize_report_text(query)
    _reject_secret_like_value(safe_query, path="preflight.query")
    safe_reasons = [sanitize_report_text(str(reason)) for reason in list(reasons or [])]
    _reject_secret_like_value(safe_reasons, path="preflight.reasons")
    preconditions_checked = []
    for item in playbook.get("preconditions") or []:
        if isinstance(item, Mapping):
            preconditions_checked.append(
                {
                    "id": sanitize_report_text(str(item.get("id") or "")),
                    "check": sanitize_report_text(str(item.get("check") or "")),
                    "status": "pending_live_check",
                }
            )
    steps_completed = []
    for item in playbook.get("steps") or []:
        if isinstance(item, Mapping):
            steps_completed.append(
                {
                    "number": item.get("number"),
                    "action": sanitize_report_text(str(item.get("action") or "")),
                    "status": "not_started",
                }
            )
    now = _now_iso()
    run_id = f"xrun_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO experience_runs(
            id, playbook_id, scope_id, decision, confidence_at_use,
            preconditions_checked, steps_completed, evidence, outcome, outcome_reason,
            started_at, finished_at, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?)
        """,
        (
            run_id,
            playbook_id,
            safe_scope_id,
            safe_decision,
            float(playbook.get("confidence") or 0.0),
            _json_dumps(preconditions_checked),
            _json_dumps(steps_completed),
            _json_dumps([{"kind": "experience_preflight", "query": safe_query, "reasons": safe_reasons}]),
            "preflight injected; awaiting outcome feedback",
            now,
            now,
            _json_dumps({"source": "experience_preflight", "requires_feedback": True}),
        ),
    )
    conn.commit()
    return {"recorded": True, "run_id": run_id, "id": playbook_id, "decision": safe_decision}


def _record_feedback_reflection_event(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    scope_id: str,
    outcome: str,
    evidence: Any,
    preconditions_checked: Any,
    steps_completed: Any,
    outcome_reason: str,
    created_at: str,
    recommended_status: str = "needs_review",
) -> bool:
    """Record the durable event generated from playbook feedback reflection.

    This keeps feedback-derived learning auditable instead of silently altering playbook quality scores."""
    if outcome not in {"failed", "misleading", "stale"}:
        return False
    mistakes = [
        {
            "signal": "experience_reuse_feedback",
            "outcome": outcome,
            "reason": outcome_reason,
        }
    ]
    root_causes = [
        {
            "type": "needs_live_review",
            "detail": "Playbook reuse produced negative or stale feedback; require operator review before direct reuse.",
        }
    ]
    corrections = [outcome_reason or "Review playbook steps, preconditions, and reuse policy before re-enabling direct reuse."]
    proposed_updates = [
        {
            "playbook_id": str(row["id"]),
            "recommended_status": recommended_status,
            "preconditions_checked": preconditions_checked,
            "steps_completed": steps_completed,
        }
    ]
    conn.execute(
        """
        INSERT INTO reflection_events(
            id, episode_id, playbook_id, scope_id, event_type, outcome, evidence,
            mistakes, root_causes, corrections, proposed_updates, applied_updates, created_at, metadata
        ) VALUES (?, '', ?, ?, 'reuse_feedback', ?, ?, ?, ?, ?, ?, '[]', ?, ?)
        """,
        (
            f"refl_{uuid.uuid4().hex}",
            str(row["id"]),
            scope_id,
            outcome,
            _json_dumps(evidence),
            _json_dumps(mistakes),
            _json_dumps(root_causes),
            _json_dumps(corrections),
            _json_dumps(proposed_updates),
            created_at,
            _json_dumps(
                {
                    "source": "record_playbook_feedback",
                    "task_class": str(row["task_class"]),
                    "title": str(row["title"]),
                    "requires_operator_review": True,
                }
            ),
        ),
    )
    return True


def record_playbook_feedback(
    conn: sqlite3.Connection,
    *,
    playbook_id: str,
    scope_id: str,
    outcome: str,
    accessible_scope_ids: Sequence[str] | None = None,
    decision: str = "guided_reuse",
    evidence: Sequence[Any] | None = None,
    preconditions_checked: Sequence[Any] | None = None,
    steps_completed: Sequence[Any] | None = None,
    outcome_reason: str = "",
    model_name: str = "",
    tool_call_count: int = 0,
    token_estimate: int = 0,
    negative_feedback_threshold: int = 1,
) -> dict[str, Any]:
    """Record user/operator feedback for an Experience playbook.

    Feedback updates both aggregate trust signals and event history so promotion decisions can be explained later instead of depending only on the latest rating."""
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
    _reject_secret_like_value(list(preconditions_checked or []), path="feedback.preconditions_checked")
    _reject_secret_like_value(list(steps_completed or []), path="feedback.steps_completed")
    _reject_secret_like_value(outcome_reason, path="feedback.outcome_reason")
    _reject_secret_like_value(model_name, path="feedback.model_name")
    safe_evidence = _sanitize_report_value(list(evidence or []))
    safe_preconditions_checked = _sanitize_report_value(list(preconditions_checked or []))
    safe_steps_completed = _sanitize_report_value(list(steps_completed or []))
    safe_outcome_reason = sanitize_report_text(outcome_reason)
    safe_model_name = sanitize_report_text(model_name)
    now = _now_iso()
    current_status = str(row["status"])
    if current_status in {"quarantined", "superseded"}:
        return {"recorded": False, "id": playbook_id, "error": "terminal_status", "status": current_status}
    global_update_allowed = str(scope_id) == str(row["scope_id"])
    success_delta = 1 if normalized_outcome == "success" else 0
    failure_delta = 1 if normalized_outcome in {"failed", "misleading"} else 0
    stale_delta = 1 if normalized_outcome == "stale" else 0
    threshold = max(1, int(negative_feedback_threshold or 1))
    negative_feedback_count = int(row["failure_count"]) + failure_delta + int(row["stale_count"]) + stale_delta
    recommended_status = "needs_review" if normalized_outcome in {"failed", "misleading", "stale"} and negative_feedback_count >= threshold else current_status
    conn.execute(
        """
        INSERT INTO experience_runs(
            id, playbook_id, scope_id, decision, confidence_at_use,
            preconditions_checked, steps_completed, evidence, outcome,
            outcome_reason, model_name, tool_call_count, token_estimate, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"xrun_{uuid.uuid4().hex}",
            playbook_id,
            scope_id,
            normalized_decision,
            float(row["confidence"]),
            _json_dumps(safe_preconditions_checked),
            _json_dumps(safe_steps_completed),
            _json_dumps(safe_evidence),
            normalized_outcome,
            safe_outcome_reason,
            safe_model_name,
            int(tool_call_count or 0),
            int(token_estimate or 0),
            now,
            now,
        ),
    )
    reflection_recorded = _record_feedback_reflection_event(
        conn,
        row=row,
        scope_id=scope_id,
        outcome=normalized_outcome,
        evidence=safe_evidence,
        preconditions_checked=safe_preconditions_checked,
        steps_completed=safe_steps_completed,
        outcome_reason=safe_outcome_reason,
        created_at=now,
        recommended_status=recommended_status,
    )
    if not global_update_allowed or normalized_outcome == "unknown":
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
            "reflection_recorded": reflection_recorded,
        }
    new_status = recommended_status
    last_verified_at = now if normalized_outcome == "success" else row["last_verified_at"]
    conn.execute(
        """
        UPDATE procedural_playbooks
        SET success_count = success_count + ?, failure_count = failure_count + ?, stale_count = stale_count + ?,
            status = ?, last_used_at = ?, last_verified_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (success_delta, failure_delta, stale_delta, new_status, now, last_verified_at, now, playbook_id),
    )
    updated = conn.execute("SELECT * FROM procedural_playbooks WHERE id = ?", (playbook_id,)).fetchone()
    opened_conflicts = _record_skill_conflicts_from_feedback(
        conn,
        row,
        outcome=normalized_outcome,
        outcome_reason=safe_outcome_reason,
        created_at=now,
    )
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
        "negative_feedback_threshold": threshold,
        "negative_feedback_count": negative_feedback_count,
        "skill_conflicts_opened": opened_conflicts,
        "reflection_recorded": reflection_recorded,
    }


def experience_stats(conn: sqlite3.Connection, *, accessible_scope_ids: Sequence[str]) -> dict[str, Any]:
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    rows = conn.execute(f"SELECT id, status FROM procedural_playbooks WHERE {scope_sql}", scope_params).fetchall()
    ids = [str(row["id"]) for row in rows]
    by_status: dict[str, int] = {}
    for row in rows:
        status = sanitize_report_text(row["status"])
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
            outcome = sanitize_report_text(row[0])
            count = int(row[1])
            by_outcome[outcome] = count
            total_runs += count
    return {
        "playbooks": {"total": len(rows), "by_status": dict(sorted(by_status.items()))},
        "runs": {"total": total_runs, "by_outcome": dict(sorted(by_outcome.items()))},
    }
