"""Small constructors for journal digest result payloads.

Centralizing these shapes keeps doctor/dashboard/release tests aligned when digest status fields evolve."""

from __future__ import annotations

from collections import Counter
from typing import Any


def no_unprocessed_journal_result(*, run_id: str, requested_extractor: str, extractor_used: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "no_unprocessed_journal",
        "run_id": run_id,
        "processed_entries": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "extractor_requested": requested_extractor,
        "extractor_used": extractor_used,
    }


def journal_digest_metadata(
    *,
    total_candidates: int,
    total_loaded_entries: int,
    actions: list[dict[str, Any]],
    requested_extractor: str,
    extractor_used: str,
    extractor_counts: Counter[str],
    extractor_errors: list[Any],
    quarantine_counts: Counter[str],
    backlog_before: int,
    effective_limit: int,
    retention_days: int,
    pruned_entries: int,
    backlog_after: int | None = None,
    productive_writes: int = 0,
    no_insert_reason: str = "",
    health_flags: list[str] | None = None,
    recommended_next_limit: int | None = None,
    candidate_status_counts: Counter[str] | None = None,
) -> dict[str, Any]:
    after = backlog_before if backlog_after is None else int(backlog_after)
    status_counts = Counter(candidate_status_counts or {})
    return {
        "candidate_count": total_candidates,
        "loaded_entries": total_loaded_entries,
        "actions": actions[:50],
        "extractor_requested": requested_extractor,
        "extractor_used": extractor_used,
        "extractor_counts": dict(extractor_counts),
        "extractor_errors": extractor_errors[:5],
        "quarantine_counts": dict(quarantine_counts),
        "backlog_before": backlog_before,
        "backlog_after": after,
        "backlog_delta": after - backlog_before,
        "limit_entries": effective_limit,
        "recommended_next_limit": effective_limit if recommended_next_limit is None else int(recommended_next_limit),
        "retention_days": retention_days,
        "pruned_journal_entries": pruned_entries,
        "productive_writes": int(productive_writes),
        "no_insert_reason": no_insert_reason,
        "health_flags": list(health_flags or []),
        "candidate_status_counts": dict(status_counts),
    }


def journal_digest_success_result(
    *,
    dry_run: bool,
    run_id: str,
    total_loaded_entries: int,
    processed_entry_count: int,
    total_candidates: int,
    counts: Counter[str],
    requested_extractor: str,
    extractor_used: str,
    quarantine_counts: Counter[str],
    backlog_before: int,
    effective_limit: int,
    pruned_entries: int,
    actions: list[dict[str, Any]],
    backlog_after: int | None = None,
    productive_writes: int | None = None,
    no_insert_reason: str = "",
    health_flags: list[str] | None = None,
    recommended_next_limit: int | None = None,
    candidate_status_counts: Counter[str] | None = None,
) -> dict[str, Any]:
    productive = counts.get("inserted", 0) + counts.get("updated", 0) if productive_writes is None else int(productive_writes)
    after = backlog_before if backlog_after is None else int(backlog_after)
    status_counts = Counter(candidate_status_counts or {})
    return {
        "ok": True,
        "status": "dry_run" if dry_run else "ok",
        "run_id": run_id,
        "processed_entries": total_loaded_entries if dry_run else processed_entry_count,
        "loaded_entries": total_loaded_entries,
        "candidates": total_candidates,
        "inserted": counts.get("inserted", 0),
        "updated": counts.get("updated", 0),
        "skipped": counts.get("skipped", 0),
        "extractor_requested": requested_extractor,
        "extractor_used": extractor_used,
        "quarantine_counts": dict(quarantine_counts),
        "backlog_before": backlog_before,
        "backlog_after": after,
        "backlog_delta": after - backlog_before,
        "limit_entries": effective_limit,
        "recommended_next_limit": effective_limit if recommended_next_limit is None else int(recommended_next_limit),
        "pruned_journal_entries": pruned_entries,
        "productive_writes": productive,
        "no_insert_reason": no_insert_reason,
        "health_flags": list(health_flags or []),
        "candidate_status_counts": dict(status_counts),
        "actions": actions[:50],
    }


def journal_digest_receipt_fields(
    *,
    total_loaded_entries: int,
    total_candidates: int,
    counts: Counter[str],
    quarantine_counts: Counter[str],
    extractor_errors: list[Any],
    backlog_before: int,
    backlog_after: int,
    effective_limit: int,
    recommended_next_limit: int | None = None,
    candidate_status_counts: Counter[str] | None = None,
) -> dict[str, Any]:
    """Summarize whether a digest run actually produced durable memory.

    A run that reviewed entries and intentionally found nothing durable is different from
    a run that produced nothing because extraction failed, parsing failed, or everything
    was quarantined. Doctor/dashboard use these fields as the error-budget surface.
    """
    productive_writes = int(counts.get("inserted", 0) or 0) + int(counts.get("updated", 0) or 0)
    status_counts = Counter(candidate_status_counts or {})
    health_flags: list[str] = []
    if backlog_after > 0:
        health_flags.append("backlog_remaining")
    if backlog_after >= backlog_before and total_loaded_entries > 0:
        health_flags.append("backlog_not_decreasing")
    if quarantine_counts:
        health_flags.append("quarantine")
    if extractor_errors:
        health_flags.append("extractor_error")
    if total_loaded_entries > 0 and productive_writes == 0:
        health_flags.append("no_productive_write")
        if quarantine_counts:
            no_insert_reason = "quarantine"
        elif extractor_errors:
            no_insert_reason = "provider_or_schema_risk"
        elif total_candidates > 0 or int(status_counts.get("filtered", 0) or 0) > 0:
            no_insert_reason = "filtered_or_rejected"
        else:
            no_insert_reason = "explicit_skip"
    else:
        no_insert_reason = ""
    return {
        "backlog_after": int(backlog_after),
        "backlog_delta": int(backlog_after) - int(backlog_before),
        "productive_writes": productive_writes,
        "no_insert_reason": no_insert_reason,
        "health_flags": health_flags,
        "recommended_next_limit": effective_limit if recommended_next_limit is None else int(recommended_next_limit),
    }


def nightly_no_candidate_fallback(*, fallback_events: list[dict[str, Any]], candidate_count: int) -> bool:
    if not fallback_events or candidate_count != 0:
        return False
    degraded_no_candidate_kinds = {"llm_empty_skipped", "llm_parse_skipped", "llm_empty_no_candidates", "llm_parse_no_candidates"}
    return any(str(event.get("kind") or "") in degraded_no_candidate_kinds for event in fallback_events)


def nightly_status_payload(*, dry_run: bool, fallback_events: list[dict[str, Any]], candidate_count: int) -> tuple[bool, str, str | None]:
    no_candidate_fallback = nightly_no_candidate_fallback(fallback_events=fallback_events, candidate_count=candidate_count)
    status = "dry_run" if dry_run else ("error" if no_candidate_fallback else ("ok_with_fallback" if fallback_events else "ok"))
    error = "LLM extraction degraded and produced no durable candidates; check provider/schema before relying on nightly digest." if no_candidate_fallback else None
    return not no_candidate_fallback, status, error


def nightly_digest_result(
    *,
    ok: bool,
    status: str,
    run_id: str,
    digest_date: str,
    source_db: str,
    sessions: int,
    task_sessions: int,
    candidate_count: int,
    counts: Counter[str],
    requested_extractor: str,
    extractor_used: str,
    fallback_events: list[dict[str, Any]],
    model: str,
    error: str | None,
    actions: list[dict[str, Any]],
    quality_counts: Counter[str] | None = None,
    pollution_counts: Counter[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "status": status,
        "run_id": run_id,
        "digest_date": digest_date,
        "source_db": source_db,
        "sessions": sessions,
        "task_sessions": task_sessions,
        "candidates": candidate_count,
        "quality_counts": dict(quality_counts or Counter()),
        "pollution_counts": dict(pollution_counts or Counter()),
        "quarantined": counts.get("quarantined", 0),
        "inserted": counts.get("inserted", 0),
        "updated": counts.get("updated", 0),
        "skipped": counts.get("skipped", 0),
        "deleted": counts.get("deleted", 0),
        "extractor": requested_extractor,
        "extractor_used": extractor_used,
        "extractor_fallbacks": fallback_events[:20],
        "model": model,
        "error": error,
        "actions": actions[:50],
    }


def nightly_digest_metadata(
    *,
    sessions: int,
    task_sessions: int,
    extractor_used: str,
    fallback_events: list[dict[str, Any]],
    quality_counts: Counter[str] | None = None,
    pollution_counts: Counter[str] | None = None,
) -> dict[str, Any]:
    return {
        "sessions": sessions,
        "task_sessions": task_sessions,
        "extractor_used": extractor_used,
        "extractor_fallbacks": fallback_events[:20],
        "quality_counts": dict(quality_counts or Counter()),
        "pollution_counts": dict(pollution_counts or Counter()),
    }
