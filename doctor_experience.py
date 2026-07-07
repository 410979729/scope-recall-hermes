"""Doctor checks for Experience Kernel playbooks, promotion debt, duplicate groups, and nightly digest health.

These checks surface operational debt without auto-promoting or rewriting playbook state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

try:
    from .doctor_common import redact_secret_like_text
    from .freshness import fact_freshness_report
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from doctor_common import redact_secret_like_text
    from freshness import fact_freshness_report

def experience_config_summary(config: dict[str, Any]) -> dict[str, Any]:
    raw_experience = config.get("experience")
    experience_config: dict[str, Any] = raw_experience if isinstance(raw_experience, dict) else {}
    keys = (
        "enabled",
        "prefetch_enabled",
        "auto_promotion_enabled",
        "auto_promotion_limit_sessions",
        "auto_promote_low_risk",
        "promotion_min_entries",
        "promotion_min_tool_entries",
        "promotion_require_verification",
    )
    return {key: experience_config.get(key) for key in keys if key in experience_config}


def _json_value(raw: Any) -> Any:
    if raw in (None, ""):
        return None
    try:
        return json.loads(str(raw))
    except Exception:
        return None


def _list_count(raw: Any) -> int:
    value = _json_value(raw)
    if isinstance(value, list):
        return sum(1 for item in value if item not in (None, "", [], {}))
    return 0


def _scope_values(row: sqlite3.Row | dict[str, Any]) -> set[str]:
    return {value for value in (str(row["scope_id"] or ""), str(row["shared_scope_id"] or "")) if value}


def _duplicate_playbook_groups(rows: list[sqlite3.Row], *, limit: int = 10) -> list[dict[str, Any]]:
    """Return duplicate playbook groups within overlapping owner/shared scopes only."""

    by_title: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (str(row["task_class"] or ""), str(row["title"] or ""))
        by_title.setdefault(key, []).append(row)
    duplicate_groups: list[dict[str, Any]] = []
    for (task_class, title), group_rows in by_title.items():
        remaining = list(group_rows)
        while remaining:
            component = [remaining.pop(0)]
            changed = True
            while changed:
                changed = False
                component_scopes = set().union(*(_scope_values(item) for item in component))
                rest: list[sqlite3.Row] = []
                for row in remaining:
                    if component_scopes & _scope_values(row):
                        component.append(row)
                        changed = True
                    else:
                        rest.append(row)
                remaining = rest
            if len(component) <= 1:
                continue
            duplicate_groups.append(
                {
                    "task_class": redact_secret_like_text(task_class),
                    "title": redact_secret_like_text(title),
                    "count": len(component),
                    "statuses": redact_secret_like_text(",".join(str(row["status"] or "") for row in component)),
                }
            )
    duplicate_groups.sort(key=lambda item: (-int(item["count"]), str(item["title"])))
    return duplicate_groups[: max(0, int(limit))]


def _replay_case_count(raw_metadata: Any) -> int:
    metadata = _json_value(raw_metadata)
    if not isinstance(metadata, dict):
        return 0
    for key in ("replay_cases", "replay_case_ids", "required_replay_cases"):
        value = metadata.get(key)
        if isinstance(value, list):
            return sum(1 for item in value if item not in (None, "", [], {}))
        if isinstance(value, dict) and isinstance(value.get("cases"), list):
            return sum(1 for item in value["cases"] if item not in (None, "", [], {}))
    return 0


def _experience_maturity_payload(
    promoted_rows: list[sqlite3.Row],
    *,
    promoted_missing_verified_at: int,
    misleading_runs: int,
    unresolved_misleading_runs: int,
    stale_runs: int,
    unresolved_stale_runs: int,
) -> dict[str, Any]:
    promoted_total = len(promoted_rows)
    with_evidence = sum(1 for row in promoted_rows if _list_count(row["evidence_anchors"]) > 0)
    with_replay = sum(1 for row in promoted_rows if _replay_case_count(row["metadata"]) > 0)
    with_verified = max(0, promoted_total - promoted_missing_verified_at)
    missing_evidence = max(0, promoted_total - with_evidence)
    missing_replay = max(0, promoted_total - with_replay)
    if not promoted_total:
        status = "no_promoted_playbooks"
    elif missing_replay:
        status = "needs_replay_coverage"
    elif missing_evidence:
        status = "needs_evidence_anchors"
    elif promoted_missing_verified_at:
        status = "needs_verification_feedback"
    elif unresolved_misleading_runs or unresolved_stale_runs:
        status = "needs_feedback_review"
    else:
        status = "ready"
    return {
        "status": status,
        "promoted_total": promoted_total,
        "promoted_with_evidence_anchors": with_evidence,
        "promoted_missing_evidence_anchors": missing_evidence,
        "promoted_with_replay_cases": with_replay,
        "promoted_missing_replay_cases": missing_replay,
        "promoted_with_last_verified_at": with_verified,
        "promoted_missing_last_verified_at": promoted_missing_verified_at,
        "feedback": {
            "stale": stale_runs,
            "misleading": misleading_runs,
            "unresolved_stale": unresolved_stale_runs,
            "unresolved_misleading": unresolved_misleading_runs,
        },
    }


def experience_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Report Experience playbook health, duplicate groups, review debt, and promotion readiness.

    This is deliberately advisory: it tells the operator where Experience automation needs attention without changing playbook lifecycle state."""
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    required_tables = {
        "task_episodes",
        "procedural_playbooks",
        "procedural_playbooks_fts",
        "playbook_versions",
        "experience_runs",
        "reflection_events",
        "fact_freshness",
        "skill_anchors",
        "skill_conflicts",
    }
    if not db_path.exists():
        return {"enabled": True, "status": "missing", "path": str(db_path)}, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, [
            "Initialize scope-recall with the current plugin to create Experience Kernel tables."
        ]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(required_tables - tables)
            if missing:
                recommendations.append("Initialize scope-recall with the current plugin so ensure_schema() creates Experience Kernel tables.")
                return {
                    "enabled": True,
                    "path": str(db_path),
                    "status": "schema_missing",
                    "missing_tables": missing,
                }, {"ok": False, "failures": [f"experience tables missing: {missing}"]}, recommendations
            playbook_total = int(conn.execute("SELECT COUNT(*) FROM procedural_playbooks").fetchone()[0])
            playbook_by_status = {
                redact_secret_like_text(row["status"]): int(row["count"])
                for row in conn.execute("SELECT status, COUNT(*) AS count FROM procedural_playbooks GROUP BY status")
            }
            run_total = int(conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0])
            run_by_outcome = {
                redact_secret_like_text(row["outcome"]): int(row["count"])
                for row in conn.execute("SELECT outcome, COUNT(*) AS count FROM experience_runs GROUP BY outcome")
            }
            stale_facts = int(conn.execute("SELECT COUNT(*) FROM fact_freshness WHERE status IN ('stale', 'needs_live_check')").fetchone()[0])
            freshness_report = fact_freshness_report(conn)
            promoted_missing_verified_at = int(
                conn.execute("SELECT COUNT(*) FROM procedural_playbooks WHERE status = 'promoted' AND COALESCE(last_verified_at, '') = ''").fetchone()[0]
            )
            promoted_rows = conn.execute(
                """
                SELECT id, evidence_anchors, metadata, last_verified_at
                FROM procedural_playbooks
                WHERE status = 'promoted'
                ORDER BY updated_at DESC, id ASC
                """
            ).fetchall()
            duplicate_rows = conn.execute(
                """
                SELECT task_class, title, status, scope_id, shared_scope_id
                FROM procedural_playbooks
                WHERE status NOT IN ('superseded', 'quarantined')
                ORDER BY task_class, title, updated_at DESC, id ASC
                """
            ).fetchall()
            duplicate_groups = _duplicate_playbook_groups(duplicate_rows, limit=10)
            misleading_runs = int(conn.execute("SELECT COUNT(*) FROM experience_runs WHERE outcome = 'misleading'").fetchone()[0])
            stale_runs = int(conn.execute("SELECT COUNT(*) FROM experience_runs WHERE outcome = 'stale'").fetchone()[0])
            unresolved_feedback = {
                str(row["outcome"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT r.outcome, COUNT(*) AS count
                    FROM experience_runs AS r
                    JOIN procedural_playbooks AS p ON p.id = r.playbook_id
                    WHERE r.outcome IN ('misleading', 'stale')
                      AND p.status NOT IN ('quarantined', 'superseded')
                    GROUP BY r.outcome
                    """
                ).fetchall()
            }
            unresolved_misleading_runs = int(unresolved_feedback.get("misleading", 0))
            unresolved_stale_runs = int(unresolved_feedback.get("stale", 0))
            maturity_payload = _experience_maturity_payload(
                promoted_rows,
                promoted_missing_verified_at=promoted_missing_verified_at,
                misleading_runs=misleading_runs,
                unresolved_misleading_runs=unresolved_misleading_runs,
                stale_runs=stale_runs,
                unresolved_stale_runs=unresolved_stale_runs,
            )
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting Experience Kernel status.")
        return {"enabled": True, "path": str(db_path), "status": "error", "error": str(exc)}, {"ok": False, "failures": [f"experience health error: {exc}"]}, recommendations

    needs_review_count = int(playbook_by_status.get("needs_review", 0))
    promoted_count = int(playbook_by_status.get("promoted", 0))
    quarantined_count = int(playbook_by_status.get("quarantined", 0))
    needs_review_ratio = (needs_review_count / playbook_total) if playbook_total else 0.0
    if needs_review_ratio >= 0.5 and playbook_total:
        recommendations.append(f"Experience promotion funnel is review-heavy ({needs_review_count}/{playbook_total} needs_review); tighten promotion scoring and dedupe candidates.")
    if duplicate_groups:
        recommendations.append(f"Experience playbooks contain {len(duplicate_groups)} duplicate title/task-class group(s); run dedupe/merge review before auto-promotion.")
    failures: list[str] = []
    if promoted_missing_verified_at:
        message = f"{promoted_missing_verified_at} promoted playbook(s) lack last_verified_at; require verification feedback before direct reuse."
        failures.append(message)
        recommendations.append(message)
    if int(maturity_payload.get("promoted_missing_evidence_anchors") or 0):
        message = (
            f"{maturity_payload.get('promoted_missing_evidence_anchors')} promoted playbook(s) lack evidence anchors; "
            "keep them guided/reviewed until source journal or verification anchors are attached."
        )
        failures.append(message)
        recommendations.append(message)
    if int(maturity_payload.get("promoted_missing_replay_cases") or 0):
        message = (
            f"{maturity_payload.get('promoted_missing_replay_cases')} promoted playbook(s) lack replay coverage; "
            "add positive and negative replay cases before relying on reusable experience quality."
        )
        failures.append(message)
        recommendations.append(message)
    if unresolved_misleading_runs or unresolved_stale_runs:
        message = (
            f"Experience feedback includes unresolved stale/misleading outcomes "
            f"(stale={unresolved_stale_runs}/{stale_runs}, misleading={unresolved_misleading_runs}/{misleading_runs}); "
            "quarantine or review affected playbooks."
        )
        failures.append(message)
        recommendations.append(message)
    if int(freshness_report.get("needs_live_check") or 0):
        recommendations.append(
            f"Fact freshness has {freshness_report.get('needs_live_check')} stale/needs-live-check fact(s); "
            "run live validation before treating those operational facts as current."
        )

    payload = {
        "enabled": True,
        "path": str(db_path),
        "status": "ready" if not failures else "needs_attention",
        "tables": sorted(required_tables),
        "playbooks": {"total": playbook_total, "by_status": dict(sorted(playbook_by_status.items()))},
        "promotion_funnel": {
            "needs_review": needs_review_count,
            "promoted": promoted_count,
            "quarantined": quarantined_count,
            "needs_review_ratio": round(needs_review_ratio, 3),
            "duplicate_groups": duplicate_groups,
            "promoted_missing_last_verified_at": promoted_missing_verified_at,
            "feedback": {
                "stale": stale_runs,
                "misleading": misleading_runs,
                "unresolved_stale": unresolved_stale_runs,
                "unresolved_misleading": unresolved_misleading_runs,
            },
        },
        "runs": {"total": run_total, "by_outcome": dict(sorted(run_by_outcome.items()))},
        "maturity": maturity_payload,
        "stale_facts": stale_facts,
        "fact_freshness": freshness_report,
    }
    return payload, {"ok": not failures, "failures": failures}, recommendations


def nightly_digest_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Report recent nightly digest status, fallback usage, and failure/quarantine debt.

    The report helps operators decide whether automated digest output is trustworthy before relying on generated memories."""
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    required_tables = {"nightly_digest_runs"}
    if not db_path.exists():
        return {"enabled": True, "status": "missing", "path": str(db_path)}, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, [
            "Initialize scope-recall or restore memory.sqlite3 before trusting nightly digest status."
        ]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(required_tables - tables)
            if missing:
                return {
                    "enabled": True,
                    "path": str(db_path),
                    "status": "not_initialized",
                    "missing_tables": missing,
                }, {"ok": True, "failures": []}, ["Run scripts/nightly-digest.py once if this deployment uses nightly digest consolidation."]
            total_runs = int(conn.execute("SELECT COUNT(*) FROM nightly_digest_runs").fetchone()[0])
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, digest_date, started_at, finished_at, extractor, model, dry_run,
                           status, inserted, updated, skipped, deleted, error,
                           CASE
                               WHEN json_valid(metadata) THEN COALESCE(json_extract(metadata, '$.operator_classification'), '')
                               ELSE ''
                           END AS operator_classification
                    FROM nightly_digest_runs
                    ORDER BY started_at DESC
                    LIMIT 10
                    """
                )
            ]
            by_status = {
                redact_secret_like_text(row["status"]): int(row["count"])
                for row in conn.execute("SELECT status, COUNT(*) AS count FROM nightly_digest_runs GROUP BY status")
            }
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting nightly digest status.")
        return {"enabled": True, "path": str(db_path), "status": "error", "error": str(exc)}, {"ok": False, "failures": [f"nightly digest health error: {exc}"]}, recommendations

    for row in rows:
        row["error"] = redact_secret_like_text(row.get("error") or "")

    latest = rows[0] if rows else {}
    latest_status = str(latest.get("status") or "")
    consecutive_errors = 0
    for row in rows:
        if str(row.get("status") or "") != "error":
            break
        consecutive_errors += 1

    recent_errors = [row for row in rows if str(row.get("status") or "") == "error"]
    recent_fallbacks = [
        row
        for row in rows
        if "fallback" in str(row.get("status") or "")
        and str(row.get("operator_classification") or "") not in {"accepted_fallback", "handled", "no_replay"}
    ]
    historical_recent_fallbacks = [row for row in rows if "fallback" in str(row.get("status") or "")]
    failures: list[str] = []
    if latest_status == "error":
        failures.append(f"latest nightly digest run failed: {latest.get('error') or latest.get('started_at')}")
    if consecutive_errors >= 3:
        failures.append(f"nightly digest has {consecutive_errors} consecutive error run(s)")
    if recent_fallbacks:
        recommendations.append("Nightly digest recently used fallback; inspect extractor/model timeout and provider health before relying on automated summaries.")
    if recent_errors and latest_status != "error":
        recommendations.append("Recent nightly digest errors exist but the latest run recovered; keep monitoring timeout/fallback trends.")

    status = "ready"
    if failures:
        status = "needs_attention"
    elif recent_fallbacks or recent_errors:
        status = "degraded"

    payload = {
        "enabled": True,
        "path": str(db_path),
        "status": status,
        "tables": sorted(required_tables),
        "runs": {"total": total_runs, "by_status": dict(sorted(by_status.items()))},
        "latest_run": latest,
        "recent_runs": rows,
        "recent_open_fallbacks": len(recent_fallbacks),
        "recent_historical_fallbacks": len(historical_recent_fallbacks),
        "consecutive_errors": consecutive_errors,
    }
    return payload, {"ok": not failures, "failures": failures}, recommendations
