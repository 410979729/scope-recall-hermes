"""Unified governance scheduler for Scope Recall maintenance.

The scheduler is an operator-facing coordinator: it summarizes governance debt
and, only when explicitly requested, applies low-risk safe cleanup paths with
audit receipts. It must not silently promote, delete, or rewrite durable memory.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Sequence

from .candidate_promotion import candidate_debt_report
from .freshness import fact_freshness_report
from .governance_cleanup import apply_cleanup
from .forgetting import build_forgetting_report
from .sql_store import ensure_schema

GOVERNANCE_SCHEDULER_SCHEMA_VERSION = "governance_scheduler.v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _safe_count(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] if row is not None else 0)


def _scope_where(scope_ids: Sequence[str] | None, *, column: str = "scope_id") -> tuple[str, list[str]] | None:
    """Return WHERE fragment for an optional scope allowlist.

    None means an explicit global operator report; an empty sequence means fail closed.
    """
    if scope_ids is None:
        return "", []
    scopes = [str(item) for item in scope_ids if str(item)]
    if not scopes:
        return None
    placeholders = ",".join("?" for _ in scopes)
    return f" AND {column} IN ({placeholders})", scopes


def _scope_any_where(scope_ids: Sequence[str] | None, *, columns: Sequence[str]) -> tuple[str, list[str]] | None:
    """Return an AND clause matching any of several scope columns."""
    if scope_ids is None:
        return "", []
    scopes = [str(item) for item in scope_ids if str(item)]
    if not scopes:
        return None
    placeholders = ",".join("?" for _ in scopes)
    clause = " OR ".join(f"{column} IN ({placeholders})" for column in columns)
    return f" AND ({clause})", [param for _column in columns for param in scopes]


def _journal_snapshot(conn: sqlite3.Connection, *, scope_ids: Sequence[str] | None = None) -> dict[str, Any]:
    if not _table_exists(conn, "journal_entries"):
        return {"status": "schema_missing", "unprocessed": 0, "dead_letter": 0, "retry_exhausted": 0}
    scope_filter = _scope_any_where(scope_ids, columns=("scope_id", "shared_scope_id"))
    if scope_filter is None:
        unprocessed = 0
    else:
        scope_sql, scope_params = scope_filter
        unprocessed = _safe_count(conn, f"SELECT COUNT(*) FROM journal_entries WHERE (processed_run_id IS NULL OR processed_run_id = ''){scope_sql}", scope_params)
    dead_letter = 0
    retry_exhausted = 0
    if _table_exists(conn, "journal_rejections"):
        if scope_filter is None:
            dead_letter = 0
            retry_exhausted = 0
        elif scope_ids is None:
            dead_letter = _safe_count(conn, "SELECT COUNT(*) FROM journal_rejections WHERE reason LIKE 'dead-letter:%'")
            retry_exhausted = _safe_count(conn, "SELECT COUNT(*) FROM journal_rejections WHERE reason LIKE 'retry-exhausted:%'")
        else:
            scope_sql, scope_params = _scope_any_where(scope_ids, columns=("e.scope_id", "e.shared_scope_id")) or (" AND 0", [])
            dead_letter = _safe_count(
                conn,
                f"""
                SELECT COUNT(*)
                FROM journal_rejections r
                JOIN journal_entries e ON e.id = r.journal_entry_id
                WHERE r.reason LIKE 'dead-letter:%'{scope_sql}
                """,
                scope_params,
            )
            retry_exhausted = _safe_count(
                conn,
                f"""
                SELECT COUNT(*)
                FROM journal_rejections r
                JOIN journal_entries e ON e.id = r.journal_entry_id
                WHERE r.reason LIKE 'retry-exhausted:%'{scope_sql}
                """,
                scope_params,
            )
    return {
        "status": "debt" if unprocessed or dead_letter or retry_exhausted else "ready",
        "unprocessed": unprocessed,
        "dead_letter": dead_letter,
        "retry_exhausted": retry_exhausted,
    }


def _experience_snapshot(conn: sqlite3.Connection, *, scope_ids: Sequence[str] | None = None) -> dict[str, Any]:
    if not _table_exists(conn, "procedural_playbooks"):
        return {"status": "schema_missing", "playbooks": {}, "runs": {}}
    scope_filter = _scope_any_where(scope_ids, columns=("scope_id", "shared_scope_id"))
    if scope_filter is None:
        playbooks = {}
    else:
        scope_sql, scope_params = scope_filter
        playbooks = {
            str(row["status"] or "unknown"): int(row["count"])
            for row in conn.execute(
                f"""
                SELECT COALESCE(status, 'unknown') AS status, COUNT(*) AS count
                FROM procedural_playbooks
                WHERE 1=1{scope_sql}
                GROUP BY COALESCE(status, 'unknown')
                """,
                scope_params,
            )
        }
    runs = {}
    if _table_exists(conn, "experience_runs"):
        run_scope_filter = _scope_where(scope_ids, column="scope_id")
        if run_scope_filter is None:
            runs = {}
        else:
            scope_sql, scope_params = run_scope_filter
            runs = {
                str(row["outcome"] or "unknown"): int(row["count"])
                for row in conn.execute(
                    f"""
                    SELECT COALESCE(outcome, 'unknown') AS outcome, COUNT(*) AS count
                    FROM experience_runs
                    WHERE 1=1{scope_sql}
                    GROUP BY COALESCE(outcome, 'unknown')
                    """,
                    scope_params,
                )
            }
    debt = int(playbooks.get("needs_review", 0)) + int(playbooks.get("quarantined", 0)) + int(runs.get("stale", 0)) + int(runs.get("misleading", 0))
    return {"status": "debt" if debt else "ready", "playbooks": dict(sorted(playbooks.items())), "runs": dict(sorted(runs.items()))}


def _candidate_snapshot(conn: sqlite3.Connection, *, scope_ids: Sequence[str] | None = None) -> dict[str, Any]:
    if not _table_exists(conn, "memories"):
        return {"status": "schema_missing", "candidate_count": 0, "by_action": {}}
    return candidate_debt_report(conn, scope_ids=scope_ids, limit=1000, sample_limit=8)


def _forgetting_snapshot(conn: sqlite3.Connection, *, accessible_scope_ids: Sequence[str] | None, limit: int) -> dict[str, Any]:
    if not _table_exists(conn, "memories"):
        return {"schema_version": "missing", "total_rows": 0, "soft_archive_candidates": {"count": 0, "items": []}}
    return build_forgetting_report(conn, accessible_scope_ids=accessible_scope_ids, limit=limit)


def _cleanup_snapshot(conn: sqlite3.Connection, *, scope_ids: Sequence[str] | None, dry_run: bool, limit: int) -> dict[str, Any]:
    if not _table_exists(conn, "memories"):
        return {"dry_run": dry_run, "candidate_count": 0, "archived": 0, "items": []}
    return apply_cleanup(conn, scope_ids=scope_ids, dry_run=dry_run, limit=limit, actor="governance.scheduler.py")


def _summary(*, journal: dict[str, Any], candidates: dict[str, Any], experience: dict[str, Any], freshness: dict[str, Any], forgetting: dict[str, Any], cleanup: dict[str, Any]) -> dict[str, Any]:
    raw_soft = forgetting.get("soft_archive_candidates")
    raw_hard = forgetting.get("hard_delete_candidates")
    raw_review = forgetting.get("review_debt")
    soft: dict[str, Any] = raw_soft if isinstance(raw_soft, dict) else {}
    hard: dict[str, Any] = raw_hard if isinstance(raw_hard, dict) else {}
    review: dict[str, Any] = raw_review if isinstance(raw_review, dict) else {}
    raw_candidate_actions = candidates.get("by_action")
    candidate_actions: dict[str, Any] = raw_candidate_actions if isinstance(raw_candidate_actions, dict) else {}
    raw_playbooks = experience.get("playbooks")
    playbooks: dict[str, Any] = raw_playbooks if isinstance(raw_playbooks, dict) else {}
    return {
        "journal_unprocessed": int(journal.get("unprocessed") or 0),
        "journal_dead_letter": int(journal.get("dead_letter") or 0),
        "candidate_count": int(candidates.get("candidate_count") or 0),
        "candidate_promotable": int(candidate_actions.get("promote", 0)),
        "experience_needs_review": int(playbooks.get("needs_review", 0)),
        "fact_needs_live_check": int(freshness.get("needs_live_check") or 0),
        "forgetting_soft_archive_candidates": int(soft.get("count") or 0),
        "forgetting_hard_delete_candidates": int(hard.get("count") or 0),
        "forgetting_review_debt": int(review.get("count") or 0),
        "cleanup_candidates": int(cleanup.get("candidate_count") or 0),
        "cleanup_archived": int(cleanup.get("archived") or 0),
    }


def run_governance_cycle(
    conn: sqlite3.Connection,
    *,
    scope_ids: Sequence[str] | None = None,
    accessible_scope_ids: Sequence[str] | None = None,
    dry_run: bool = True,
    apply_safe: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """Run one governance inspection cycle.

    Default dry-run is query-only compatible. Non-dry-run execution requires
    `apply_safe=True` and only applies low-risk cleanup soft archives through
    the audited cleanup helper.
    """
    if scope_ids is None:
        if accessible_scope_ids is None:
            accessible: list[str] = []
            report_scope_filter: Sequence[str] | None = None
            cleanup_scope_filter: Sequence[str] | None = None
            forgetting_scope_filter: Sequence[str] | None = None
        else:
            accessible = [str(item) for item in accessible_scope_ids if str(item)]
            report_scope_filter = accessible
            cleanup_scope_filter = accessible
            forgetting_scope_filter = accessible
        apply_scopes: list[str] = []
    else:
        requested = [str(item) for item in scope_ids if str(item)]
        if accessible_scope_ids is None:
            selected = requested
        else:
            allowed = {str(item) for item in accessible_scope_ids if str(item)}
            selected = [scope_id for scope_id in requested if scope_id in allowed]
        accessible = list(selected)
        report_scope_filter = selected
        cleanup_scope_filter = selected
        forgetting_scope_filter = selected
        apply_scopes = selected
    if not dry_run and not apply_safe:
        return {
            "schema_version": GOVERNANCE_SCHEDULER_SCHEMA_VERSION,
            "ok": False,
            "dry_run": False,
            "apply_safe": False,
            "error": "apply_requires_apply_safe",
            "applied": {"cleanup": {"archived": 0, "audit_required": True}},
        }
    if not dry_run and apply_safe and not apply_scopes:
        return {
            "schema_version": GOVERNANCE_SCHEDULER_SCHEMA_VERSION,
            "ok": False,
            "dry_run": False,
            "apply_safe": True,
            "error": "apply_requires_scope_id",
            "applied": {"cleanup": {"archived": 0, "audit_required": True}},
        }
    if not dry_run:
        ensure_schema(conn)

    journal = _journal_snapshot(conn, scope_ids=report_scope_filter)
    candidates = _candidate_snapshot(conn, scope_ids=report_scope_filter)
    experience = _experience_snapshot(conn, scope_ids=report_scope_filter)
    freshness = fact_freshness_report(conn, scope_ids=report_scope_filter) if _table_exists(conn, "fact_freshness") else {"status": "schema_missing", "needs_live_check": 0, "by_validator_kind": {}}
    forgetting = _forgetting_snapshot(conn, accessible_scope_ids=forgetting_scope_filter, limit=limit)
    cleanup = _cleanup_snapshot(conn, scope_ids=cleanup_scope_filter, dry_run=(dry_run or not apply_safe), limit=limit)
    summary = _summary(journal=journal, candidates=candidates, experience=experience, freshness=freshness, forgetting=forgetting, cleanup=cleanup)
    action_items: list[str] = []
    if summary["journal_unprocessed"]:
        action_items.append("journal_backlog_scan")
    if summary["journal_dead_letter"]:
        action_items.append("dead_letter_recovery_review")
    if summary["candidate_count"]:
        action_items.append("candidate_memory_triage")
    if summary["experience_needs_review"]:
        action_items.append("experience_feedback_review")
    if summary["fact_needs_live_check"]:
        action_items.append("fact_freshness_live_check")
    if summary["forgetting_review_debt"]:
        action_items.append("forgetting_review_debt")
    if summary["cleanup_candidates"]:
        action_items.append("cleanup_soft_archive_review")

    return {
        "schema_version": GOVERNANCE_SCHEDULER_SCHEMA_VERSION,
        "ok": True,
        "dry_run": bool(dry_run),
        "apply_safe": bool(apply_safe),
        "summary": summary,
        "journal": journal,
        "candidate_memory": candidates,
        "experience": experience,
        "fact_freshness": freshness,
        "forgetting": forgetting,
        "cleanup": cleanup,
        "action_items": action_items,
        "applied": {
            "cleanup": {
                "archived": int(cleanup.get("archived") or 0),
                "batch_id": str(cleanup.get("batch_id") or ""),
                "audit_required": not dry_run,
            }
        },
    }


def run_governance_cycle_for_home(
    hermes_home: str | Path,
    *,
    dry_run: bool = True,
    apply_safe: bool = False,
    limit: int = 200,
    scope_ids: Sequence[str] | None = None,
    accessible_scope_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    home = Path(hermes_home).expanduser()
    db_path = home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return {"schema_version": GOVERNANCE_SCHEDULER_SCHEMA_VERSION, "ok": False, "dry_run": dry_run, "apply_safe": apply_safe, "error": f"SQLite truth DB not found: {db_path}"}
    if dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return run_governance_cycle(conn, scope_ids=scope_ids, accessible_scope_ids=accessible_scope_ids, dry_run=dry_run, apply_safe=apply_safe, limit=limit)
    finally:
        conn.close()
