from __future__ import annotations

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


def experience_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
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
            duplicate_groups = [
                {
                    "task_class": redact_secret_like_text(str(row["task_class"] or "")),
                    "title": redact_secret_like_text(str(row["title"] or "")),
                    "count": int(row["count"]),
                    "statuses": redact_secret_like_text(str(row["statuses"] or "")),
                }
                for row in conn.execute(
                    """
                    SELECT task_class, title, COUNT(*) AS count, GROUP_CONCAT(status, ',') AS statuses
                    FROM procedural_playbooks
                    WHERE status NOT IN ('superseded', 'quarantined')
                    GROUP BY task_class, title
                    HAVING COUNT(*) > 1
                    ORDER BY count DESC, title ASC
                    LIMIT 10
                    """
                )
            ]
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
    if promoted_missing_verified_at:
        recommendations.append(f"{promoted_missing_verified_at} promoted playbook(s) lack last_verified_at; require verification feedback before direct reuse.")
    if unresolved_misleading_runs or unresolved_stale_runs:
        recommendations.append(
            f"Experience feedback includes unresolved stale/misleading outcomes "
            f"(stale={unresolved_stale_runs}/{stale_runs}, misleading={unresolved_misleading_runs}/{misleading_runs}); "
            "quarantine or review affected playbooks."
        )
    if int(freshness_report.get("needs_live_check") or 0):
        recommendations.append(
            f"Fact freshness has {freshness_report.get('needs_live_check')} stale/needs-live-check fact(s); "
            "run live validation before treating those operational facts as current."
        )

    payload = {
        "enabled": True,
        "path": str(db_path),
        "status": "ready",
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
        "stale_facts": stale_facts,
        "fact_freshness": freshness_report,
    }
    return payload, {"ok": True, "failures": []}, recommendations


def nightly_digest_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
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
