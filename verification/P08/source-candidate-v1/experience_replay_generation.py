"""Generate review-only Experience replay case drafts from playbooks.

Replay case drafts are maintenance artifacts: they help operators turn useful
playbooks into benchmark fixtures, but they do not write benchmark files or mutate
SQLite state.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .capture_filters import sanitize_report_text

REPLAY_CASE_DRAFT_SCHEMA_VERSION = "experience_replay_case_draft.v1"
_DRAFTABLE_STATUSES = frozenset({"candidate", "reviewed", "promoted"})
_ALLOWED_DECISIONS = frozenset({"direct_reuse", "guided_reuse", "no_reuse"})


def _json_loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _scope_predicate(accessible_scope_ids: Sequence[str]) -> tuple[str, list[str]]:
    scopes = [str(scope_id) for scope_id in accessible_scope_ids if str(scope_id)]
    if not scopes:
        return "0", []
    placeholders = ",".join("?" for _ in scopes)
    return f"(scope_id IN ({placeholders}) OR shared_scope_id IN ({placeholders}))", [*scopes, *scopes]


def _normalized_phrase(text: Any) -> str:
    lowered = sanitize_report_text(str(text or "")).strip().lower()
    lowered = re.sub(r"[^0-9a-z\u4e00-\u9fff_. -]+", " ", lowered)
    return " ".join(lowered.split())


def _append_unique(items: list[str], value: str, *, limit: int) -> None:
    cleaned = _normalized_phrase(value)
    if not cleaned or cleaned in items:
        return
    items.append(cleaned)
    del items[limit:]


def _required_terms(row: sqlite3.Row, *, limit: int = 8) -> list[str]:
    terms: list[str] = []
    verification = _json_loads(row["verification"], [])
    if isinstance(verification, list):
        for item in verification:
            text = _normalized_phrase(item)
            if "pytest" in text:
                _append_unique(terms, "pytest", limit=limit)
            if "dry-run" in text and "json" in text:
                _append_unique(terms, "dry-run json", limit=limit)
            _append_unique(terms, text, limit=limit)
    steps = _json_loads(row["steps"], [])
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, Mapping):
                text = _normalized_phrase(step.get("evidence_required") or step.get("action") or "")
            else:
                text = _normalized_phrase(step)
            if "pytest" in text:
                _append_unique(terms, "pytest", limit=limit)
            if "dry-run" in text and "json" in text:
                _append_unique(terms, "dry-run json", limit=limit)
            _append_unique(terms, text, limit=limit)
    if not terms:
        _append_unique(terms, row["title"], limit=limit)
    return terms


def _evidence_refs(row: sqlite3.Row) -> list[str]:
    playbook_ref = f"playbook:{row['id']}"
    refs = [playbook_ref]
    raw_refs = _json_loads(row["evidence_anchors"], [])
    if isinstance(raw_refs, list):
        for item in raw_refs:
            ref = sanitize_report_text(str(item or "").strip())
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def _expected_decision(row: sqlite3.Row) -> str:
    if str(row["status"]) == "candidate":
        return "guided_reuse"
    reuse_policy = _json_loads(row["reuse_policy"], {})
    if isinstance(reuse_policy, Mapping):
        decision = str(reuse_policy.get("default_decision") or "").strip().lower()
        if decision in _ALLOWED_DECISIONS:
            return decision
    return "guided_reuse"


def _draft_from_row(row: sqlite3.Row) -> dict[str, Any]:
    query = " ".join(
        part
        for part in [sanitize_report_text(row["title"]), sanitize_report_text(row["trigger"]), sanitize_report_text(row["goal"])]
        if part
    )
    return {
        "schema_version": REPLAY_CASE_DRAFT_SCHEMA_VERSION,
        "id": f"draft-{row['id']}",
        "source_playbook_id": str(row["id"]),
        "query": query,
        "baseline_text": "I will solve the task from scratch without Experience guidance.",
        "required_terms": _required_terms(row),
        "expected_decision": _expected_decision(row),
        "expected_playbook_id": str(row["id"]),
        "evidence_refs": _evidence_refs(row),
        "requires_operator_review": True,
        "metadata": {
            "task_class": sanitize_report_text(row["task_class"]),
            "source_status": sanitize_report_text(row["status"]),
            "confidence": float(row["confidence"] or 0.0),
        },
    }


def generate_replay_case_drafts(
    conn: sqlite3.Connection,
    *,
    accessible_scope_ids: Sequence[str],
    limit: int = 20,
    statuses: Sequence[str] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Build replay-case drafts for active candidate/reviewed/promoted playbooks.

    The function is intentionally read-only and safe for SQLite query-only
    connections. Operators can review the returned drafts before copying them
    into a benchmark case file.
    """
    allowed_statuses = {str(status).strip() for status in (statuses or _DRAFTABLE_STATUSES) if str(status).strip()}
    allowed_statuses &= _DRAFTABLE_STATUSES
    if not allowed_statuses:
        return {"ok": True, "dry_run": dry_run, "count": 0, "drafts": [], "skipped": [{"reason": "no_draftable_statuses"}]}
    scope_sql, scope_params = _scope_predicate(accessible_scope_ids)
    placeholders = ",".join("?" for _ in sorted(allowed_statuses))
    rows = conn.execute(
        f"""
        SELECT * FROM procedural_playbooks
        WHERE {scope_sql} AND status IN ({placeholders})
        ORDER BY status = 'promoted' DESC, confidence DESC, updated_at DESC
        LIMIT ?
        """,
        [*scope_params, *sorted(allowed_statuses), max(1, min(100, int(limit or 20)))],
    ).fetchall()
    drafts = [_draft_from_row(row) for row in rows]
    return {
        "ok": True,
        "dry_run": dry_run,
        "count": len(drafts),
        "drafts": drafts,
        "statuses": sorted(allowed_statuses),
    }
