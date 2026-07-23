"""Read-only temporal fact, evolution, and reflection health telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .temporal_facts import fact_fts_integrity_status
from .truth_connection import connect_truth_database

_VISIBLE_LIFECYCLE_SQL = (
    "LOWER(COALESCE(json_extract(metadata, '$.lifecycle'), '')) "
    "NOT IN ('archived','obsolete','superseded','candidate','rejected')"
)
_FACT_MEMORY_TYPES = (
    "fact",
    "factual",
    "preference",
    "project",
    "resource",
    "constraint",
)


def _config_bool(config: Mapping[str, Any], section: str, key: str) -> bool:
    raw_section = config.get(section)
    values = raw_section if isinstance(raw_section, Mapping) else {}
    return values.get(key) is True


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _foreign_key_integrity(conn: sqlite3.Connection, *, sample_limit: int = 100) -> dict[str, Any]:
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    enabled = int(row[0]) == 1 if row is not None else False
    bounded_limit = max(1, int(sample_limit))
    sample: list[dict[str, Any]] = []
    violation_count = 0
    for item in conn.execute("PRAGMA foreign_key_check"):
        violation_count += 1
        if len(sample) >= bounded_limit:
            continue
        sample.append(
            {
                "table": str(item[0] or ""),
                "rowid": item[1],
                "parent": str(item[2] or ""),
                "foreign_key_id": int(item[3]),
            }
        )
    return {
        "enabled": enabled,
        "violation_count": violation_count,
        "violations_truncated": violation_count > bounded_limit,
        "violation_sample": sample,
    }


def _claim_coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in _FACT_MEMORY_TYPES)
    eligible = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM memories
            WHERE {_VISIBLE_LIFECYCLE_SQL}
              AND LOWER(COALESCE(json_extract(metadata, '$.memory_type'), ''))
                  IN ({placeholders})
            """,
            _FACT_MEMORY_TYPES,
        ).fetchone()[0]
    )
    claimed = int(
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.id)
            FROM memories AS m
            JOIN fact_claims AS c ON c.memory_id = m.id
            WHERE {_VISIBLE_LIFECYCLE_SQL.replace('metadata', 'm.metadata')}
              AND LOWER(COALESCE(json_extract(m.metadata, '$.memory_type'), ''))
                  IN ({placeholders})
            """,
            _FACT_MEMORY_TYPES,
        ).fetchone()[0]
    )
    return {
        "eligible_memory_count": eligible,
        "claimed_memory_count": claimed,
        "coverage_rate": 1.0 if eligible == 0 else claimed / eligible,
    }


def _single_current_overlaps(conn: sqlite3.Connection, now_iso: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT scope_id, fact_key
                FROM fact_claims
                WHERE cardinality = 'single'
                  AND status IN ('current', 'uncertain')
                  AND valid_from <= ?
                  AND (valid_to IS NULL OR valid_to = '' OR valid_to > ?)
                  AND recorded_at <= ?
                  AND (retired_at IS NULL OR retired_at = '' OR retired_at > ?)
                GROUP BY scope_id, fact_key
                HAVING COUNT(*) > 1
            )
            """,
            (now_iso, now_iso, now_iso, now_iso),
        ).fetchone()[0]
    )


def _open_interval_conflicts(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT scope_id, fact_key
                FROM fact_claims
                WHERE cardinality = 'single'
                  AND status IN ('current', 'uncertain')
                  AND (valid_to IS NULL OR valid_to = '')
                GROUP BY scope_id, fact_key
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    )


def _sourceless_claims(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM fact_claims AS c
            WHERE TRIM(COALESCE(c.source_type, '')) = ''
               OR (
                    TRIM(COALESCE(c.source_ref, '')) = ''
                    AND NOT EXISTS (
                        SELECT 1
                        FROM fact_claim_evidence AS e
                        WHERE e.claim_id = c.claim_id
                    )
               )
            """
        ).fetchone()[0]
    )


def _review_debt(conn: sqlite3.Connection, tables: set[str]) -> dict[str, int]:
    receipt_review = 0
    if "fact_action_receipts" in tables:
        receipt_review = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM fact_action_receipts
                WHERE effective_action = 'review' OR status = 'review'
                """
            ).fetchone()[0]
        )
    candidate_review = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM memories
            WHERE LOWER(COALESCE(json_extract(metadata, '$.lifecycle'), '')) = 'candidate'
              AND (
                    LOWER(COALESCE(json_extract(metadata, '$.fact_evolution.action'), '')) = 'review'
                 OR LOWER(COALESCE(json_extract(metadata, '$.evolution.action'), '')) = 'review'
                 OR LOWER(COALESCE(json_extract(metadata, '$.candidate_status'), '')) = 'needs_review'
              )
            """
        ).fetchone()[0]
    )
    return {
        "receipt_review_count": receipt_review,
        "candidate_review_count": candidate_review,
        "total": receipt_review + candidate_review,
    }


def _recent_receipts(
    conn: sqlite3.Connection,
    tables: set[str],
    cutoff_iso: str,
) -> dict[str, Any]:
    if "fact_action_receipts" not in tables:
        return {
            "window_days": 30,
            "applied_or_replayed": 0,
            "review": 0,
            "failure": 0,
            "rollback_observability": "transactional_failures_are_not_persisted",
        }
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status IN ('applied', 'replayed') THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'review' OR effective_action = 'review' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status NOT IN ('applied', 'replayed', 'review') THEN 1 ELSE 0 END)
        FROM fact_action_receipts
        WHERE created_at >= ?
        """,
        (cutoff_iso,),
    ).fetchone()
    return {
        "window_days": 30,
        "applied_or_replayed": int(row[0] or 0),
        "review": int(row[1] or 0),
        "failure": int(row[2] or 0),
        "rollback_observability": "transactional_failures_are_not_persisted",
    }


def _mental_model_debt(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM memories
            WHERE LOWER(COALESCE(json_extract(metadata, '$.memory_type'), '')) = 'mental_model'
              AND (
                    LOWER(COALESCE(json_extract(metadata, '$.lifecycle'), '')) = 'candidate'
                 OR LOWER(COALESCE(json_extract(metadata, '$.candidate_status'), '')) = 'needs_review'
              )
            """
        ).fetchone()[0]
    )


def temporal_evolution_report(
    hermes_home: Path,
    runtime_config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Return bounded counts from SQLite without running schema or repair writes."""

    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    enabled = {
        "fact_evolution": _config_bool(runtime_config, "fact_evolution", "enabled"),
        "temporal_queries": _config_bool(runtime_config, "temporal_queries", "enabled"),
        "reflection": _config_bool(runtime_config, "reflection", "enabled"),
        "reflection_write_candidates": _config_bool(
            runtime_config, "reflection", "write_candidates"
        ),
    }
    recommendations: list[str] = []
    if not db_path.exists():
        return (
            {"status": "missing", "path": str(db_path), "features": enabled},
            {"ok": True, "failures": []},
            recommendations,
        )

    conn: sqlite3.Connection | None = None
    foreign_key_integrity: dict[str, Any] = {
        "enabled": False,
        "violation_count": 0,
        "violations_truncated": False,
        "violation_sample": [],
    }
    fact_fts_integrity: dict[str, Any] = {
        "membership_sets_checked": True,
        "set_differences": {},
        "current": False,
    }
    try:
        conn = connect_truth_database(db_path, mode="ro")
        conn.execute("PRAGMA query_only=ON")
        before = conn.total_changes
        tables = _tables(conn)
        required = {
            "memories",
            "fact_claims",
            "fact_claim_evidence",
            "fact_action_receipts",
        }
        missing = sorted(required - tables)
        if missing:
            any_enabled = any(enabled.values())
            if any_enabled:
                recommendations.append(
                    "Temporal/reflection features are enabled but fact schema tables are missing; disable the gates or initialize the current provider before use."
                )
            else:
                recommendations.append(
                    "Fact schema tables are not initialized; apply the current provider schema before enabling temporal or reflection features."
                )
            payload = {
                "status": "schema_missing",
                "path": str(db_path),
                "features": enabled,
                "missing_tables": missing,
                "write_delta": conn.total_changes - before,
            }
            failures = ["enabled temporal/reflection feature lacks fact schema"] if any_enabled else []
            return payload, {"ok": not failures, "failures": failures}, recommendations

        foreign_key_integrity = _foreign_key_integrity(conn)
        fact_fts_integrity = fact_fts_integrity_status(
            conn,
            verify_membership_sets=True,
            sample_limit=20,
        )
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        cutoff_iso = (now - timedelta(days=30)).isoformat()
        coverage = _claim_coverage(conn)
        overlaps = _single_current_overlaps(conn, now_iso)
        open_conflicts = _open_interval_conflicts(conn)
        sourceless = _sourceless_claims(conn)
        review_debt = _review_debt(conn, tables)
        recent = _recent_receipts(conn, tables, cutoff_iso)
        mental_model_debt = _mental_model_debt(conn)
        write_delta = conn.total_changes - before
    except Exception as exc:
        return (
            {"status": "error", "path": str(db_path), "error": str(exc)},
            {"ok": False, "failures": [f"temporal evolution doctor error: {exc}"]},
            ["Repair or restore SQLite before relying on temporal/reflection health telemetry."],
        )
    finally:
        if conn is not None:
            conn.close()

    failures: list[str] = []
    if not bool(foreign_key_integrity["enabled"]):
        failures.append("SQLite truth connection has PRAGMA foreign_keys disabled")
    if int(foreign_key_integrity["violation_count"]):
        suffix = "+" if bool(foreign_key_integrity["violations_truncated"]) else ""
        failures.append(
            "SQLite foreign-key violations: "
            f"{foreign_key_integrity['violation_count']}{suffix}"
        )
        recommendations.append(
            "Foreign-key violations require an explicit reviewed repair plan; do not silently delete orphan rows."
        )
    if not bool(fact_fts_integrity.get("current")):
        differences = fact_fts_integrity.get("set_differences")
        difference_map = differences if isinstance(differences, Mapping) else {}
        mismatch_counts = [
            f"{name}={int(summary.get('count') or 0)}"
            for name, summary in difference_map.items()
            if isinstance(summary, Mapping) and int(summary.get("count") or 0)
        ]
        missing_tables = [
            str(item) for item in fact_fts_integrity.get("missing_tables", [])
        ]
        detail = ", ".join(mismatch_counts)
        if missing_tables:
            detail = "missing tables: " + ", ".join(missing_tables)
        failures.append(
            "fact FTS membership is not current"
            + (f" ({detail})" if detail else "")
        )
        recommendations.append(
            "Run the reviewed fact FTS repair path; equal row counts do not prove claim-id membership identity."
        )
    if overlaps:
        failures.append(f"single-value current overlap groups: {overlaps}")
    if open_conflicts:
        failures.append(f"single-value open interval conflicts: {open_conflicts}")
    if sourceless:
        failures.append(f"claims without usable source provenance: {sourceless}")
    if write_delta:
        failures.append(f"read-only temporal doctor wrote {write_delta} row changes")
    if coverage["eligible_memory_count"] and coverage["coverage_rate"] < 1.0:
        recommendations.append(
            "Eligible factual memories are not fully represented in fact_claims; review deterministic backfill candidates before enabling current/as-of behavior broadly."
        )
    if review_debt["total"]:
        recommendations.append(
            f"Fact evolution review debt exists ({review_debt['total']}); drain reviewed candidates before widening auto-apply gates."
        )
    if mental_model_debt:
        recommendations.append(
            f"Mental-model candidate review debt exists ({mental_model_debt}); review candidates before activation."
        )

    payload = {
        "status": "needs_repair" if failures else "ready",
        "path": str(db_path),
        "features": enabled,
        "foreign_key_integrity": foreign_key_integrity,
        "fact_fts_integrity": fact_fts_integrity,
        "claim_coverage": coverage,
        "single_current_overlap_groups": overlaps,
        "open_interval_conflict_groups": open_conflicts,
        "claims_without_source": sourceless,
        "evolution_review_debt": review_debt,
        "recent_evolution": recent,
        "mental_model_candidate_debt": mental_model_debt,
        "write_delta": write_delta,
    }
    return payload, {"ok": not failures, "failures": failures}, recommendations


__all__ = ["temporal_evolution_report"]
