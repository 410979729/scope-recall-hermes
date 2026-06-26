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
) -> dict[str, Any]:
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
        "limit_entries": effective_limit,
        "retention_days": retention_days,
        "pruned_journal_entries": pruned_entries,
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
) -> dict[str, Any]:
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
        "limit_entries": effective_limit,
        "pruned_journal_entries": pruned_entries,
        "actions": actions[:50],
    }


def nightly_no_candidate_fallback(*, fallback_events: list[dict[str, Any]], candidate_count: int) -> bool:
    return bool(fallback_events) and candidate_count == 0 and any(str(event.get("kind") or "").endswith("_no_candidates") for event in fallback_events)


def nightly_status_payload(*, dry_run: bool, fallback_events: list[dict[str, Any]], candidate_count: int) -> tuple[bool, str, str | None]:
    no_candidate_fallback = nightly_no_candidate_fallback(fallback_events=fallback_events, candidate_count=candidate_count)
    status = "dry_run" if dry_run else ("error" if no_candidate_fallback else ("ok_with_fallback" if fallback_events else "ok"))
    error = "LLM extraction fell back to heuristic but no candidates were produced." if no_candidate_fallback else None
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


def nightly_digest_metadata(*, sessions: int, task_sessions: int, extractor_used: str, fallback_events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "sessions": sessions,
        "task_sessions": task_sessions,
        "extractor_used": extractor_used,
        "extractor_fallbacks": fallback_events[:20],
    }
