"""Doctor checks for journal backlog, retry/dead-letter queues, quarantine history, and digest failure patterns.

The report is diagnostic only: it classifies recovery work so operators can decide when replay is safe."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

try:
    from .digest_durable_work import (
        JOURNAL_DURABLE_DOMAIN,
        JOURNAL_DURABLE_OWNER_ROLES,
        JOURNAL_DURABLE_POLICY_VERSION,
        journal_durable_health,
        unavailable_digest_health,
    )
    from .doctor_common import coerce_int
    from .journal_recovery import classify_rejection_reason
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from digest_durable_work import (  # type: ignore
        JOURNAL_DURABLE_DOMAIN,
        JOURNAL_DURABLE_OWNER_ROLES,
        JOURNAL_DURABLE_POLICY_VERSION,
        journal_durable_health,
        unavailable_digest_health,
    )
    from doctor_common import coerce_int
    from journal_recovery import classify_rejection_reason

def journal_enabled_from_config(config: dict[str, Any]) -> bool:
    raw_journal = config.get("journal")
    journal_config: dict[str, Any] = raw_journal if isinstance(raw_journal, dict) else {}
    value = journal_config.get("enabled", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def journal_backlog_age_hours(oldest_created_at: str) -> float:
    if not oldest_created_at:
        return 0.0
    try:
        from datetime import datetime, timezone

        created = datetime.fromisoformat(str(oldest_created_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def _schema_unavailable_block(
    *,
    missing_columns: list[str],
    recommendation: str,
    status: str = "schema_missing",
) -> dict[str, Any]:
    """Fail-closed doctor block: missing columns are not numeric zero."""

    return {
        "status": status,
        "available": False,
        "count": None,
        "missing_columns": list(missing_columns),
        "recommendation": recommendation,
    }


_DEFERRED_REQUIRED_COLUMNS = ("deferred_run_id", "defer_count")


def _deferred_unavailable_block(
    *,
    missing_columns: list[str],
    recommendation: str,
    status: str = "schema_missing",
) -> dict[str, Any]:
    """Deferred backlog is a single capability: unmeasured fields stay null."""

    return {
        **_schema_unavailable_block(
            missing_columns=missing_columns,
            recommendation=recommendation,
            status=status,
        ),
        "oldest_deferred_age_hours": None,
        "repeat_deferred_count": None,
        "max_defer_count": None,
    }


def _journal_entry_columns(conn: sqlite3.Connection) -> set[str] | None:
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(journal_entries)")}
    except Exception:
        return None


def _deferred_backlog_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Bounded, non-content deferred-queue metrics for doctor.

    Counts currently deferred overflow, the oldest visible deferral age, and a
    repeated-deferral/churn signal. Missing columns are ``schema_missing`` so a
    pre-migration database cannot look like a healthy zero backlog.
    """

    recommendation = (
        "Migrate journal_entries with the current plugin schema so deferred "
        "cursor health can be counted."
    )
    columns = _journal_entry_columns(conn)
    if columns is None:
        return _deferred_unavailable_block(
            missing_columns=list(_DEFERRED_REQUIRED_COLUMNS),
            recommendation=recommendation,
            status="unknown",
        )
    missing = [name for name in _DEFERRED_REQUIRED_COLUMNS if name not in columns]
    if missing:
        return _deferred_unavailable_block(
            missing_columns=missing,
            recommendation=recommendation,
        )
    count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM journal_entries
            WHERE (processed_run_id IS NULL OR processed_run_id = '')
              AND COALESCE(deferred_run_id, '') != ''
            """
        ).fetchone()[0]
        or 0
    )
    oldest_row = conn.execute(
        """
        SELECT deferred_at FROM journal_entries
        WHERE (processed_run_id IS NULL OR processed_run_id = '')
          AND COALESCE(deferred_run_id, '') != ''
          AND COALESCE(deferred_at, '') != ''
        ORDER BY deferred_at ASC LIMIT 1
        """
    ).fetchone()
    oldest_at = str(oldest_row[0]) if oldest_row else ""
    repeat_deferred_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM journal_entries
            WHERE (processed_run_id IS NULL OR processed_run_id = '')
              AND COALESCE(defer_count, 0) >= 2
            """
        ).fetchone()[0]
        or 0
    )
    max_defer_count = int(
        conn.execute(
            """
            SELECT COALESCE(MAX(defer_count), 0) FROM journal_entries
            WHERE processed_run_id IS NULL OR processed_run_id = ''
            """
        ).fetchone()[0]
        or 0
    )
    return {
        "status": "available",
        "available": True,
        "count": count,
        "oldest_deferred_age_hours": round(journal_backlog_age_hours(oldest_at), 3),
        "repeat_deferred_count": repeat_deferred_count,
        "max_defer_count": max_defer_count,
    }


def _retryable_failure_metrics(
    conn: sqlite3.Connection, *, threshold: int
) -> dict[str, Any]:
    """Bounded retryable-failure health without row contents."""

    recommendation = (
        "Migrate journal_entries with the current plugin schema so durable "
        "retryable-failure health can be counted."
    )
    columns = _journal_entry_columns(conn)
    if columns is None:
        return {
            **_schema_unavailable_block(
                missing_columns=["retryable_failures"],
                recommendation=recommendation,
                status="unknown",
            ),
            "pending_entries": None,
            "threshold": max(1, int(threshold or 3)),
        }
    if "retryable_failures" not in columns:
        return {
            **_schema_unavailable_block(
                missing_columns=["retryable_failures"],
                recommendation=recommendation,
            ),
            "pending_entries": None,
            "threshold": max(1, int(threshold or 3)),
        }
    pending = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM journal_entries
            WHERE (processed_run_id IS NULL OR processed_run_id = '')
              AND COALESCE(retryable_failures, 0) > 0
            """
        ).fetchone()[0]
        or 0
    )
    return {
        "status": "available",
        "available": True,
        "count": pending,
        "pending_entries": pending,
        "threshold": max(1, int(threshold or 3)),
    }


def classify_reason_counts(reason_counts: dict[str, int]) -> dict[str, int]:
    category_counts: dict[str, int] = {}
    for reason, count in reason_counts.items():
        category = classify_rejection_reason(reason)
        category_counts[category] = category_counts.get(category, 0) + int(count)
    return dict(sorted(category_counts.items()))


def _json_dict(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(str(raw))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def journal_report(hermes_home: Path, *, enabled: bool = True, journal_config: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Build a read-only health report for journal digest and recovery debt.

    The function intentionally reports categories, samples, and thresholds rather than repairing anything: operators need to know whether backlog is ordinary work, auth/quota dead letter, quarantine, or replayable debt. Missing defer or retryable budget columns fail closed in ``digest_health`` without changing the separate top-level backlog/orphan status contract."""
    journal_config = journal_config or {}
    recommendations: list[str] = []
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"
    if not enabled:
        return {
            "enabled": False,
            "status": "disabled",
            "durable_work": unavailable_digest_health(
                domain_type=JOURNAL_DURABLE_DOMAIN,
                policy_version=JOURNAL_DURABLE_POLICY_VERSION,
                reason_code="disabled_by_config",
                storage_dir=storage_dir,
                domain_roles=JOURNAL_DURABLE_OWNER_ROLES,
            ),
        }, {"ok": True, "failures": []}, recommendations
    if not db_path.exists():
        return {
            "enabled": True,
            "status": "missing",
            "path": str(db_path),
            "durable_work": unavailable_digest_health(
                domain_type=JOURNAL_DURABLE_DOMAIN,
                policy_version=JOURNAL_DURABLE_POLICY_VERSION,
                reason_code="truth_database_absent",
                storage_dir=storage_dir,
                domain_roles=JOURNAL_DURABLE_OWNER_ROLES,
            ),
        }, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, recommendations

    required_tables = {"journal_entries", "journal_digest_runs", "memory_journal_sources", "journal_rejections"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(required_tables - tables)
            if missing:
                recommendations.append("Initialize scope-recall with the current plugin or run journal digest once to create the journal/provenance schema.")
                return {
                    "enabled": True,
                    "path": str(db_path),
                    "status": "schema_missing",
                    "missing_tables": missing,
                    "durable_work": unavailable_digest_health(
                        domain_type=JOURNAL_DURABLE_DOMAIN,
                        policy_version=JOURNAL_DURABLE_POLICY_VERSION,
                        reason_code="schema_missing",
                        storage_dir=storage_dir,
                        domain_roles=JOURNAL_DURABLE_OWNER_ROLES,
                    ),
                }, {"ok": False, "failures": [f"journal tables missing: {missing}"]}, recommendations

            durable_health = journal_durable_health(
                conn,
                storage_dir=storage_dir,
            )

            total_entries = int(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0])
            unprocessed_entries = int(
                conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id IS NULL OR processed_run_id = ''").fetchone()[0]
            )
            processed_entries = max(0, total_entries - unprocessed_entries)
            digest_runs = int(conn.execute("SELECT COUNT(*) FROM journal_digest_runs").fetchone()[0])
            source_links = int(conn.execute("SELECT COUNT(*) FROM memory_journal_sources").fetchone()[0])
            rejections = int(conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0])
            if "memories" in tables:
                orphan_sources = int(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM memory_journal_sources AS s
                        LEFT JOIN memories AS m ON m.id = s.memory_id
                        WHERE m.id IS NULL
                        """
                    ).fetchone()[0]
                )
            else:
                orphan_sources = 0
            deferred_metrics = _deferred_backlog_metrics(conn)
            retryable_failures_threshold = max(
                1, coerce_int(journal_config.get("retryable_failures_quarantine"), 3)
            )
            retryable_metrics = _retryable_failure_metrics(
                conn, threshold=retryable_failures_threshold
            )
            oldest_unprocessed = conn.execute(
                """
                SELECT created_at FROM journal_entries
                WHERE processed_run_id IS NULL OR processed_run_id = ''
                ORDER BY created_at ASC LIMIT 1
                """
            ).fetchone()
            unprocessed_by_role = {
                str(row["role"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT role, COUNT(*) AS count
                    FROM journal_entries
                    WHERE processed_run_id IS NULL OR processed_run_id = ''
                    GROUP BY role
                    ORDER BY role
                    """
                )
            }
            contamination_counts: dict[str, dict[str, int]] = {}
            for marker in ("image_cache/img_", "[Image attached at:", "[inline image/", "/tmp/hermes", ".hermes/"):
                contamination_counts[marker] = {
                    "all": int(conn.execute("SELECT COUNT(*) FROM journal_entries WHERE content LIKE ?", (f"%{marker}%",)).fetchone()[0]),
                    "unprocessed": int(
                        conn.execute(
                            "SELECT COUNT(*) FROM journal_entries WHERE (processed_run_id IS NULL OR processed_run_id = '') AND content LIKE ?",
                            (f"%{marker}%",),
                        ).fetchone()[0]
                    ),
                    "tool_unprocessed": int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM journal_entries
                            WHERE (processed_run_id IS NULL OR processed_run_id = '') AND role = 'tool' AND content LIKE ?
                            """,
                            (f"%{marker}%",),
                        ).fetchone()[0]
                    ),
                }
            last_run = conn.execute(
                """
                SELECT id, started_at, finished_at, status, extractor, processed_entries, inserted, updated, skipped
                FROM journal_digest_runs
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            digest_status_counts = {
                str(row["status"] or "unknown"): int(row["count"])
                for row in conn.execute(
                    "SELECT COALESCE(status, 'unknown') AS status, COUNT(*) AS count FROM journal_digest_runs GROUP BY COALESCE(status, 'unknown') ORDER BY status"
                )
            }
            digest_extractor_counts = {
                str(row["extractor"] or "unknown"): {"runs": int(row["runs"]), "processed_entries": int(row["processed_entries"] or 0)}
                for row in conn.execute(
                    """
                    SELECT COALESCE(extractor, 'unknown') AS extractor, COUNT(*) AS runs, COALESCE(SUM(processed_entries), 0) AS processed_entries
                    FROM journal_digest_runs
                    GROUP BY COALESCE(extractor, 'unknown')
                    ORDER BY extractor
                    """
                )
            }
            unresolved_quarantine_run_ids = {
                str(row["id"] or "")
                for row in conn.execute(
                    """
                    SELECT DISTINCT d.id
                    FROM journal_digest_runs AS d
                    JOIN journal_entries AS e ON e.processed_run_id = d.id
                    JOIN journal_rejections AS r ON r.journal_entry_id = e.id AND r.run_id = d.id
                    LEFT JOIN memory_journal_sources AS s ON s.journal_entry_id = e.id
                    WHERE d.extractor = 'llm-quarantine'
                      AND (r.reason LIKE 'retry-exhausted:%' OR r.reason LIKE 'dead-letter:%')
                      AND s.memory_id IS NULL
                    """
                )
            }
            recent_runs = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, started_at, status, extractor, processed_entries, inserted, updated, skipped, metadata,
                           CASE
                               WHEN json_valid(metadata) THEN COALESCE(json_extract(metadata, '$.operator_classification'), '')
                               ELSE ''
                           END AS operator_classification
                    FROM journal_digest_runs
                    ORDER BY started_at DESC
                    LIMIT 25
                    """
                )
            ]
            recent_status_counts: dict[str, int] = {}
            recent_extractor_counts: dict[str, int] = {}
            recent_no_insert_risk_runs = 0
            recent_no_insert_risk_streak = 0
            recent_no_insert_explicit_skip_runs = 0
            recent_no_insert_reasons: dict[str, int] = {}
            streak_open = True
            for row in recent_runs:
                recent_status_counts[str(row.get("status") or "unknown")] = recent_status_counts.get(str(row.get("status") or "unknown"), 0) + 1
                recent_extractor_counts[str(row.get("extractor") or "unknown")] = recent_extractor_counts.get(str(row.get("extractor") or "unknown"), 0) + 1
                metadata = _json_dict(row.pop("metadata", ""))
                no_insert_reason = str(metadata.get("no_insert_reason") or "").strip()
                raw_productive = metadata.get("productive_writes")
                try:
                    productive_writes = int(raw_productive) if raw_productive is not None else int(row.get("inserted") or 0) + int(row.get("updated") or 0)
                except (TypeError, ValueError):
                    productive_writes = int(row.get("inserted") or 0) + int(row.get("updated") or 0)
                health_flags = metadata.get("health_flags") if isinstance(metadata.get("health_flags"), list) else []
                row["productive_writes"] = productive_writes
                row["no_insert_reason"] = no_insert_reason
                row["health_flags"] = health_flags
                is_risk_run = False
                if int(row.get("processed_entries") or 0) > 0 and productive_writes == 0 and no_insert_reason:
                    is_resolved_quarantine = str(row.get("extractor") or "") == "llm-quarantine" and str(row.get("id") or "") not in unresolved_quarantine_run_ids
                    operator_classification = str(row.get("operator_classification") or metadata.get("operator_classification") or "").strip()
                    is_operator_no_durable = operator_classification in {"no_durable_memory", "no_replay"} or no_insert_reason.startswith("operator_review")
                    if no_insert_reason == "explicit_skip" or is_resolved_quarantine or is_operator_no_durable:
                        recent_no_insert_explicit_skip_runs += 1
                    else:
                        is_risk_run = True
                        recent_no_insert_risk_runs += 1
                        recent_no_insert_reasons[no_insert_reason] = recent_no_insert_reasons.get(no_insert_reason, 0) + 1
                if streak_open:
                    if is_risk_run:
                        recent_no_insert_risk_streak += 1
                    else:
                        streak_open = False
            recent_no_insert_reasons = dict(sorted(recent_no_insert_reasons.items()))
            rejection_reason_counts = {
                str(row["reason"] or ""): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT COALESCE(reason, '') AS reason, COUNT(*) AS count
                    FROM journal_rejections
                    GROUP BY COALESCE(reason, '')
                    ORDER BY reason
                    """
                )
            }
            retry_exhausted_reason_counts = {
                str(row["reason"] or ""): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT COALESCE(reason, '') AS reason, COUNT(*) AS count
                    FROM journal_rejections
                    WHERE reason LIKE 'retry-exhausted:%'
                    GROUP BY COALESCE(reason, '')
                    ORDER BY reason
                    """
                )
            }
            dead_letter_reason_counts = {
                str(row["reason"] or ""): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT COALESCE(reason, '') AS reason, COUNT(*) AS count
                    FROM journal_rejections
                    WHERE reason LIKE 'dead-letter:%'
                    GROUP BY COALESCE(reason, '')
                    ORDER BY reason
                    """
                )
            }
            retry_replay_candidate_reason_counts = {
                str(row["reason"] or ""): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT COALESCE(r.reason, '') AS reason, COUNT(*) AS count
                    FROM journal_rejections AS r
                    JOIN journal_entries AS e ON e.id = r.journal_entry_id
                    LEFT JOIN memory_journal_sources AS s ON s.journal_entry_id = e.id
                    WHERE r.reason LIKE 'retry-exhausted:%'
                      AND COALESCE(e.processed_run_id, '') != ''
                      AND r.run_id = e.processed_run_id
                      AND s.memory_id IS NULL
                    GROUP BY COALESCE(r.reason, '')
                    ORDER BY reason
                    """
                )
            }
            dead_letter_replay_candidate_reason_counts = {
                str(row["reason"] or ""): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT COALESCE(r.reason, '') AS reason, COUNT(*) AS count
                    FROM journal_rejections AS r
                    JOIN journal_entries AS e ON e.id = r.journal_entry_id
                    LEFT JOIN memory_journal_sources AS s ON s.journal_entry_id = e.id
                    WHERE r.reason LIKE 'dead-letter:%'
                      AND COALESCE(e.processed_run_id, '') != ''
                      AND r.run_id = e.processed_run_id
                      AND s.memory_id IS NULL
                    GROUP BY COALESCE(r.reason, '')
                    ORDER BY reason
                    """
                )
            }
            rejection_categories = classify_reason_counts(rejection_reason_counts)
            retry_exhausted_categories = classify_reason_counts(retry_exhausted_reason_counts)
            dead_letter_categories = classify_reason_counts(dead_letter_reason_counts)
            retry_replay_candidate_categories = classify_reason_counts(retry_replay_candidate_reason_counts)
            dead_letter_replay_candidate_categories = classify_reason_counts(dead_letter_replay_candidate_reason_counts)
            historical_retry_exhausted_rejections = sum(retry_exhausted_reason_counts.values())
            historical_dead_letter_rejections = sum(dead_letter_reason_counts.values())
            retry_replay_candidates = sum(retry_replay_candidate_reason_counts.values())
            dead_letter_replay_candidates = sum(dead_letter_replay_candidate_reason_counts.values())
            retry_exhausted_rejections = retry_replay_candidates
            dead_letter_rejections = dead_letter_replay_candidates
            quarantine_runs = len(unresolved_quarantine_run_ids)
            historical_quarantine_runs = int(
                conn.execute("SELECT COUNT(*) FROM journal_digest_runs WHERE extractor = 'llm-quarantine'").fetchone()[0]
            )
            fallback_runs = int(
                conn.execute("SELECT COUNT(*) FROM journal_digest_runs WHERE extractor IN ('heuristic-fallback', 'llm-fallback') OR status = 'ok_with_fallback'").fetchone()[0]
            )
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting journal/provenance status.")
        return {
            "enabled": True,
            "path": str(db_path),
            "status": "error",
            "error": str(exc),
            "durable_work": unavailable_digest_health(
                domain_type=JOURNAL_DURABLE_DOMAIN,
                policy_version=JOURNAL_DURABLE_POLICY_VERSION,
                reason_code="health_read_error",
                state="needs_repair",
                storage_dir=storage_dir,
                domain_roles=JOURNAL_DURABLE_OWNER_ROLES,
            ),
        }, {"ok": False, "failures": [f"journal health error: {exc}"]}, recommendations

    failures: list[str] = []
    warn_entries = max(0, coerce_int(journal_config.get("backlog_warn_entries"), 500))
    fail_entries = max(0, coerce_int(journal_config.get("backlog_fail_entries"), 3000))
    max_age_hours = max(0, coerce_int(journal_config.get("backlog_max_age_hours"), 72))
    max_entries_per_digest = max(1, coerce_int(journal_config.get("max_entries_per_digest"), 500))
    no_insert_fail_streak = max(0, coerce_int(journal_config.get("no_insert_fail_streak"), 3))
    dynamic_threshold = max(0, coerce_int(journal_config.get("dynamic_backlog_threshold"), warn_entries or 500))
    ceiling = max(max_entries_per_digest, coerce_int(journal_config.get("max_entries_per_digest_ceiling"), max_entries_per_digest))
    if unprocessed_entries >= max(dynamic_threshold, 1):
        recommended_batch_size = min(ceiling, max(max_entries_per_digest, unprocessed_entries))
    else:
        recommended_batch_size = max_entries_per_digest
    estimated_runs_to_clear = 0 if unprocessed_entries == 0 else max(1, (unprocessed_entries + recommended_batch_size - 1) // recommended_batch_size)
    oldest_value = oldest_unprocessed["created_at"] if oldest_unprocessed else ""
    backlog_age = journal_backlog_age_hours(oldest_value)
    contaminated_unprocessed = sum(item["unprocessed"] for item in contamination_counts.values())
    contaminated_tool_unprocessed = sum(item["tool_unprocessed"] for item in contamination_counts.values())
    if orphan_sources:
        failures.append(f"memory_journal_sources contains {orphan_sources} orphan link(s)")
        recommendations.append("Run hygiene/repair or delete orphan memory_journal_sources before release.")
    if unprocessed_entries:
        recommendations.append("Run scripts/journal-digest.py to promote staged journal entries into durable memories.")
    if warn_entries and unprocessed_entries >= warn_entries:
        recommendations.append(
            f"Journal backlog has {unprocessed_entries} unprocessed entrie(s); increase/dynamically adjust max_entries_per_digest and verify digest throughput."
        )
    if fail_entries and unprocessed_entries > fail_entries:
        failures.append(f"journal backlog has {unprocessed_entries} unprocessed entrie(s), above fail threshold {fail_entries}")
    if max_age_hours and backlog_age > max_age_hours:
        failures.append(f"journal backlog oldest unprocessed entry is {backlog_age:.1f}h old, above threshold {max_age_hours}h")
    if contaminated_unprocessed:
        recommendations.append(
            f"Journal backlog contains {contaminated_unprocessed} unprocessed attachment/path marker hit(s); verify tool trace hygiene and sanitize_capture_text coverage."
        )
    if contaminated_tool_unprocessed:
        recommendations.append(
            f"Tool trace hygiene: {contaminated_tool_unprocessed} unprocessed tool trace marker hit(s) remain; run digest/cleanup after deploying sanitized ingestion."
        )
    if deferred_metrics.get("available") is False:
        recommendations.append(str(deferred_metrics.get("recommendation") or ""))
    elif deferred_metrics.get("count"):
        recommendations.append(
            f"Journal digest has {deferred_metrics['count']} budget-deferred entrie(s); later runs should resume the per-session cursor rather than reload the same prefix."
        )
    if deferred_metrics.get("available") is True and deferred_metrics.get("repeat_deferred_count"):
        recommendations.append(
            f"Journal digest has {deferred_metrics['repeat_deferred_count']} repeatedly deferred entrie(s); inspect fat-session chunk budgets and cursor progress."
        )
    pending_retryable = False
    if retryable_metrics.get("available") is False:
        recommendations.append(str(retryable_metrics.get("recommendation") or ""))
    elif retryable_metrics.get("pending_entries"):
        pending_retryable = True
        recommendations.append(
            f"Journal backlog has {retryable_metrics['pending_entries']} unprocessed entrie(s) with durable retryable LLM failures; they leave the FIFO head after {retryable_metrics['threshold']} cross-run failure(s) and remain replayable."
        )
    digest_health_status = "ready"
    digest_health_reasons: list[str] = []
    if deferred_metrics.get("available") is False:
        digest_health_reasons.append("deferred_schema_unavailable")
        digest_health_status = "unknown"
    if retryable_metrics.get("available") is False:
        digest_health_reasons.append("retryable_schema_unavailable")
        digest_health_status = "unknown"
    if pending_retryable:
        digest_health_reasons.append("pending_retryable_failures")
    recent_bad_runs = sum(recent_status_counts.get(status, 0) for status in ("error", "retry_scheduled", "dead_letter"))
    recent_fallback_runs = recent_status_counts.get("ok_with_fallback", 0) + recent_extractor_counts.get("heuristic-fallback", 0)
    recent_quarantine_runs = sum(
        1
        for row in recent_runs
        if str(row.get("extractor") or "") == "llm-quarantine"
        and str(row.get("id") or "") in unresolved_quarantine_run_ids
    )
    if recent_bad_runs or recent_quarantine_runs:
        digest_health_status = "degraded"
        digest_health_reasons.append("recent_digest_failures_or_quarantine")
        recommendations.append("Journal digest recently failed or quarantined LLM batches; inspect retry/dead-letter health before relying on automated summaries.")
    if recent_fallback_runs:
        digest_health_status = "degraded"
        digest_health_reasons.append("recent_heuristic_fallback")
        recommendations.append("Journal digest recently used heuristic fallback; verify LLM extractor health and quality flags.")
    if recent_no_insert_risk_streak:
        digest_health_status = "degraded"
        digest_health_reasons.append("recent_no_productive_write_risk")
        recommendations.append(
            f"Journal digest has a current streak of {recent_no_insert_risk_streak} run(s) with no productive writes for provider/schema/quality reasons; inspect no_insert_reason before relying on automated summaries."
        )
    if no_insert_fail_streak and recent_no_insert_risk_streak >= no_insert_fail_streak:
        failures.append(
            f"journal digest has a consecutive streak of {recent_no_insert_risk_streak} no productive writes run(s), at or above fail streak {no_insert_fail_streak}"
        )
    if quarantine_runs:
        digest_health_reasons.append("historical_llm_quarantine")
        recommendations.append(f"Journal digest has {quarantine_runs} historical llm-quarantine run(s); replay or classify them through retry/dead-letter tooling.")
    if retry_exhausted_rejections or dead_letter_rejections:
        digest_health_reasons.append("historical_retry_or_dead_letter_rejections")
        recommendations.append(
            f"Journal rejections include retry/dead-letter evidence (retry_exhausted={retry_exhausted_rejections}, dead_letter={dead_letter_rejections}); add replay/cleanup before declaring digest fully healthy."
        )
    if retry_replay_candidates:
        digest_health_reasons.append("retry_replay_queue_nonempty")
        recommendations.append(f"Journal recovery queue has {retry_replay_candidates} retry-exhausted entrie(s) eligible for replay; run scripts/journal.recovery.py dry-run/apply then journal-digest.")
    if dead_letter_replay_candidates:
        digest_health_reasons.append("dead_letter_replay_queue_nonempty")
        recommendations.append(f"Journal recovery queue has {dead_letter_replay_candidates} dead-letter entrie(s); only replay after fixing auth/quota/config root cause.")
    auth_or_quota = (
        retry_replay_candidate_categories.get("auth", 0)
        + retry_replay_candidate_categories.get("quota", 0)
        + dead_letter_replay_candidate_categories.get("auth", 0)
        + dead_letter_replay_candidate_categories.get("quota", 0)
    )
    parse_or_timeout = (
        retry_replay_candidate_categories.get("parse", 0)
        + retry_replay_candidate_categories.get("timeout", 0)
        + dead_letter_replay_candidate_categories.get("parse", 0)
        + dead_letter_replay_candidate_categories.get("timeout", 0)
    )
    low_value = retry_replay_candidate_categories.get("low_value", 0) + dead_letter_replay_candidate_categories.get("low_value", 0)
    unknown = retry_replay_candidate_categories.get("unknown", 0) + dead_letter_replay_candidate_categories.get("unknown", 0)
    if auth_or_quota:
        recommendations.append("Journal rejection categories include auth/quota failures; fix provider credentials, permissions, or rate limits before replaying dead letters.")
    if parse_or_timeout:
        recommendations.append("Journal rejection categories include timeout/parse failures; dry-run replay is reasonable after extractor/network/schema root cause is fixed.")
    if low_value:
        recommendations.append("Journal rejection categories include low-value/noise entries; keep them rejected as evidence instead of replaying by default.")
    if unknown:
        recommendations.append("Journal rejection categories include unknown reasons; inspect samples before replay or cleanup.")

    payload = {
        "enabled": True,
        "path": str(db_path),
        "status": "ready" if not failures else "needs_repair",
        "tables": sorted(required_tables),
        "entries": {
            "total": total_entries,
            "processed": processed_entries,
            "unprocessed": unprocessed_entries,
            "oldest_unprocessed": oldest_value,
        },
        "backlog": {
            "unprocessed_by_role": dict(sorted(unprocessed_by_role.items())),
            "oldest_unprocessed_age_hours": round(backlog_age, 3),
            "contamination_counts": contamination_counts,
            "thresholds": {"warn_entries": warn_entries, "fail_entries": fail_entries, "max_age_hours": max_age_hours},
            "batch_policy": {
                "max_entries_per_digest": max_entries_per_digest,
                "dynamic_backlog_threshold": dynamic_threshold,
                "max_entries_per_digest_ceiling": ceiling,
                "recommended_batch_size": recommended_batch_size,
                "estimated_runs_to_clear": estimated_runs_to_clear,
            },
            "deferred": deferred_metrics,
            "retryable_failures": retryable_metrics,
        },
        "digest_runs": digest_runs,
        "digest_health": {
            "status": digest_health_status,
            "reasons": digest_health_reasons,
            "status_counts": digest_status_counts,
            "extractor_counts": digest_extractor_counts,
            "recent_status_counts": recent_status_counts,
            "recent_extractor_counts": recent_extractor_counts,
            "recent_no_insert_risk_runs": recent_no_insert_risk_runs,
            "recent_no_insert_risk_streak": recent_no_insert_risk_streak,
            "recent_no_insert_explicit_skip_runs": recent_no_insert_explicit_skip_runs,
            "recent_no_insert_reasons": recent_no_insert_reasons,
            "no_insert_fail_streak": no_insert_fail_streak,
            "fallback_runs": fallback_runs,
            "llm_quarantine_runs": quarantine_runs,
            "historical_llm_quarantine_runs": historical_quarantine_runs,
            "retry_exhausted_rejections": retry_exhausted_rejections,
            "dead_letter_rejections": dead_letter_rejections,
            "historical_retry_exhausted_rejections": historical_retry_exhausted_rejections,
            "historical_dead_letter_rejections": historical_dead_letter_rejections,
            "rejection_categories": rejection_categories,
            "retry_exhausted_categories": retry_replay_candidate_categories,
            "dead_letter_categories": dead_letter_replay_candidate_categories,
            "historical_retry_exhausted_categories": retry_exhausted_categories,
            "historical_dead_letter_categories": dead_letter_categories,
            "recovery_queue": {
                "retry_exhausted_candidates": retry_replay_candidates,
                "dead_letter_candidates": dead_letter_replay_candidates,
                "retry_exhausted_categories": retry_replay_candidate_categories,
                "dead_letter_categories": dead_letter_replay_candidate_categories,
            },
            "recent_runs": recent_runs[:10],
        },
        "last_digest_run": dict(last_run) if last_run else {},
        "durable_work": durable_health,
        "source_links": source_links,
        "rejections": rejections,
        "orphan_source_links": orphan_sources,
    }
    seen_recommendations: set[str] = set()
    unique_recommendations: list[str] = []
    for item in recommendations:
        text = str(item or "").strip()
        if not text or text in seen_recommendations:
            continue
        seen_recommendations.add(text)
        unique_recommendations.append(item)
    return payload, {"ok": not failures, "failures": failures}, unique_recommendations
