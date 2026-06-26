from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

try:
    from .doctor_common import coerce_int
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from doctor_common import coerce_int

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


def journal_report(hermes_home: Path, *, enabled: bool = True, journal_config: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    journal_config = journal_config or {}
    recommendations: list[str] = []
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"
    if not enabled:
        return {"enabled": False, "status": "disabled"}, {"ok": True, "failures": []}, recommendations
    if not db_path.exists():
        return {"enabled": True, "status": "missing", "path": str(db_path)}, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, recommendations

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
                }, {"ok": False, "failures": [f"journal tables missing: {missing}"]}, recommendations

            total_entries = int(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0])
            unprocessed_entries = int(
                conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id IS NULL OR processed_run_id = ''").fetchone()[0]
            )
            processed_entries = max(0, total_entries - unprocessed_entries)
            digest_runs = int(conn.execute("SELECT COUNT(*) FROM journal_digest_runs").fetchone()[0])
            source_links = int(conn.execute("SELECT COUNT(*) FROM memory_journal_sources").fetchone()[0])
            rejections = int(conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0])
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
            recent_runs = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, started_at, status, extractor, processed_entries, inserted, updated, skipped
                    FROM journal_digest_runs
                    ORDER BY started_at DESC
                    LIMIT 25
                    """
                )
            ]
            recent_status_counts: dict[str, int] = {}
            recent_extractor_counts: dict[str, int] = {}
            for row in recent_runs:
                recent_status_counts[str(row.get("status") or "unknown")] = recent_status_counts.get(str(row.get("status") or "unknown"), 0) + 1
                recent_extractor_counts[str(row.get("extractor") or "unknown")] = recent_extractor_counts.get(str(row.get("extractor") or "unknown"), 0) + 1
            retry_exhausted_rejections = int(
                conn.execute("SELECT COUNT(*) FROM journal_rejections WHERE reason LIKE 'retry-exhausted:%'").fetchone()[0]
            )
            dead_letter_rejections = int(
                conn.execute("SELECT COUNT(*) FROM journal_rejections WHERE reason LIKE 'dead-letter:%'").fetchone()[0]
            )
            retry_replay_candidates = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM journal_rejections AS r
                    JOIN journal_entries AS e ON e.id = r.journal_entry_id
                    LEFT JOIN memory_journal_sources AS s ON s.journal_entry_id = e.id
                    WHERE r.reason LIKE 'retry-exhausted:%'
                      AND COALESCE(e.processed_run_id, '') != ''
                      AND s.memory_id IS NULL
                    """
                ).fetchone()[0]
            )
            dead_letter_replay_candidates = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM journal_rejections AS r
                    JOIN journal_entries AS e ON e.id = r.journal_entry_id
                    LEFT JOIN memory_journal_sources AS s ON s.journal_entry_id = e.id
                    WHERE r.reason LIKE 'dead-letter:%'
                      AND COALESCE(e.processed_run_id, '') != ''
                      AND s.memory_id IS NULL
                    """
                ).fetchone()[0]
            )
            quarantine_runs = int(
                conn.execute("SELECT COUNT(*) FROM journal_digest_runs WHERE extractor = 'llm-quarantine'").fetchone()[0]
            )
            fallback_runs = int(
                conn.execute("SELECT COUNT(*) FROM journal_digest_runs WHERE extractor IN ('heuristic-fallback', 'llm-fallback') OR status = 'ok_with_fallback'").fetchone()[0]
            )
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting journal/provenance status.")
        return {"enabled": True, "path": str(db_path), "status": "error", "error": str(exc)}, {"ok": False, "failures": [f"journal health error: {exc}"]}, recommendations

    failures: list[str] = []
    warn_entries = max(0, coerce_int(journal_config.get("backlog_warn_entries"), 500))
    fail_entries = max(0, coerce_int(journal_config.get("backlog_fail_entries"), 3000))
    max_age_hours = max(0, coerce_int(journal_config.get("backlog_max_age_hours"), 72))
    max_entries_per_digest = max(1, coerce_int(journal_config.get("max_entries_per_digest"), 500))
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
    digest_health_status = "ready"
    digest_health_reasons: list[str] = []
    recent_bad_runs = sum(recent_status_counts.get(status, 0) for status in ("error", "retry_scheduled", "dead_letter"))
    recent_fallback_runs = recent_status_counts.get("ok_with_fallback", 0) + recent_extractor_counts.get("heuristic-fallback", 0)
    recent_quarantine_runs = recent_extractor_counts.get("llm-quarantine", 0)
    if recent_bad_runs or recent_quarantine_runs:
        digest_health_status = "degraded"
        digest_health_reasons.append("recent_digest_failures_or_quarantine")
        recommendations.append("Journal digest recently failed or quarantined LLM batches; inspect retry/dead-letter health before relying on automated summaries.")
    if recent_fallback_runs:
        digest_health_status = "degraded"
        digest_health_reasons.append("recent_heuristic_fallback")
        recommendations.append("Journal digest recently used heuristic fallback; verify LLM extractor health and quality flags.")
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
        },
        "digest_runs": digest_runs,
        "digest_health": {
            "status": digest_health_status,
            "reasons": digest_health_reasons,
            "status_counts": digest_status_counts,
            "extractor_counts": digest_extractor_counts,
            "recent_status_counts": recent_status_counts,
            "recent_extractor_counts": recent_extractor_counts,
            "fallback_runs": fallback_runs,
            "llm_quarantine_runs": quarantine_runs,
            "retry_exhausted_rejections": retry_exhausted_rejections,
            "dead_letter_rejections": dead_letter_rejections,
            "recovery_queue": {
                "retry_exhausted_candidates": retry_replay_candidates,
                "dead_letter_candidates": dead_letter_replay_candidates,
            },
            "recent_runs": recent_runs[:10],
        },
        "last_digest_run": dict(last_run) if last_run else {},
        "source_links": source_links,
        "rejections": rejections,
        "orphan_source_links": orphan_sources,
    }
    return payload, {"ok": not failures, "failures": failures}, recommendations
