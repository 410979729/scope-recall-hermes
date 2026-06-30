"""Tests for normalized journal digest result payloads and metadata fields.

These contracts keep doctor/dashboard status readable when digest paths no-op, succeed, or degrade."""

from __future__ import annotations

from collections import Counter

from scope_recall.digest_run_results import (
    journal_digest_metadata,
    journal_digest_success_result,
    nightly_digest_metadata,
    nightly_digest_result,
    nightly_status_payload,
    no_unprocessed_journal_result,
)


def test_no_unprocessed_journal_result_contract():
    payload = no_unprocessed_journal_result(run_id="run", requested_extractor="llm", extractor_used="llm")

    assert payload == {
        "ok": True,
        "status": "no_unprocessed_journal",
        "run_id": "run",
        "processed_entries": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "extractor_requested": "llm",
        "extractor_used": "llm",
    }


def test_journal_digest_result_and_metadata_preserve_counts_and_limits():
    counts = Counter({"inserted": 1, "updated": 2, "skipped": 3})
    quarantine_counts = Counter({"auth": 4})
    extractor_counts = Counter({"llm": 1})
    actions = [{"action": "insert", "i": i} for i in range(60)]

    result = journal_digest_success_result(
        dry_run=False,
        run_id="run",
        total_loaded_entries=10,
        processed_entry_count=8,
        total_candidates=6,
        counts=counts,
        requested_extractor="llm",
        extractor_used="llm",
        quarantine_counts=quarantine_counts,
        backlog_before=20,
        effective_limit=100,
        pruned_entries=2,
        actions=actions,
    )
    metadata = journal_digest_metadata(
        total_candidates=6,
        total_loaded_entries=10,
        actions=actions,
        requested_extractor="llm",
        extractor_used="llm",
        extractor_counts=extractor_counts,
        extractor_errors=["e"] * 6,
        quarantine_counts=quarantine_counts,
        backlog_before=20,
        effective_limit=100,
        retention_days=7,
        pruned_entries=2,
    )

    assert result["processed_entries"] == 8
    assert result["inserted"] == 1
    assert result["actions"] == actions[:50]
    assert metadata["extractor_errors"] == ["e"] * 5
    assert metadata["actions"] == actions[:50]


def test_nightly_status_payload_and_result_contract():
    fallback_events = [{"kind": "llm_empty_no_candidates"}]
    ok, status, error = nightly_status_payload(dry_run=False, fallback_events=fallback_events, candidate_count=0)

    assert ok is False
    assert status == "error"
    assert error == "LLM extraction fell back to heuristic but no candidates were produced."

    ok2, status2, error2 = nightly_status_payload(dry_run=False, fallback_events=[{"kind": "timeout"}], candidate_count=1)
    assert ok2 is True
    assert status2 == "ok_with_fallback"
    assert error2 is None

    result = nightly_digest_result(
        ok=True,
        status="ok",
        run_id="run",
        digest_date="2026-01-01",
        source_db="state.db",
        sessions=2,
        task_sessions=1,
        candidate_count=3,
        counts=Counter({"inserted": 1, "updated": 1, "skipped": 1, "deleted": 1}),
        requested_extractor="llm",
        extractor_used="heuristic-fallback",
        fallback_events=[{"kind": str(i)} for i in range(25)],
        model="model",
        error=None,
        actions=[{"i": i} for i in range(60)],
    )
    metadata = nightly_digest_metadata(sessions=2, task_sessions=1, extractor_used="heuristic-fallback", fallback_events=[{"kind": str(i)} for i in range(25)])

    assert result["candidates"] == 3
    assert result["quality_counts"] == {}
    assert result["extractor_fallbacks"] == [{"kind": str(i)} for i in range(20)]
    assert result["actions"] == [{"i": i} for i in range(50)]
    assert metadata["quality_counts"] == {}
    assert metadata["extractor_fallbacks"] == [{"kind": str(i)} for i in range(20)]
