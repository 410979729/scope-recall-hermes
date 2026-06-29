from __future__ import annotations

import sqlite3

from scope_recall.journal import ensure_journal_schema
from scope_recall.journal_recovery import classify_recovery_candidates, classify_rejection_reason, recovery_report, schedule_replay
from scope_recall.sql_store import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return conn


def _entry(conn: sqlite3.Connection, entry_id: int, *, processed_run_id: str = "run-failed") -> None:
    conn.execute(
        """
        INSERT INTO journal_entries(
            id, scope_id, shared_scope_id, platform, user_id, chat_id, thread_id,
            gateway_session_key, agent_identity, agent_workspace, session_id,
            turn_number, role, content, content_hash, created_at, processed_run_id, processed_at, metadata
        ) VALUES (?, 'scope-a', 'shared-a', 'telegram', 'joy', 'dm', '', '', 'yuheng', 'hermes', 'session-a', ?, 'user', ?, ?, '2026-01-01T00:00:00+00:00', ?, '2026-01-01T00:00:05+00:00', '{}')
        """,
        (entry_id, entry_id, f"entry {entry_id}", f"hash-{entry_id}", processed_run_id),
    )


def _rejection(conn: sqlite3.Connection, entry_id: int, *, reason: str = "retry-exhausted:timeout", run_id: str = "run-failed", created_at: str = "2026-01-01T00:00:05+00:00") -> None:
    conn.execute(
        "INSERT INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at) VALUES (?, ?, ?, '', ?)",
        (entry_id, run_id, reason, created_at),
    )


def test_classify_rejection_reason_categories():
    assert classify_rejection_reason("retry-exhausted:timeout while calling model") == "timeout"
    assert classify_rejection_reason("dead-letter:auth token expired") == "auth"
    assert classify_rejection_reason("retry-exhausted:429 quota exceeded") == "quota"
    assert classify_rejection_reason("retry-exhausted:invalid json schema") == "parse"
    assert classify_rejection_reason("manual-reviewed:low-value empty noise") == "low_value"
    assert classify_rejection_reason("dead-letter:needs operator review") == "unknown"


def test_journal_recovery_reports_and_schedules_retry_exhausted_entries():
    conn = _conn()
    _entry(conn, 1)
    _entry(conn, 2)
    _entry(conn, 3)
    _rejection(conn, 1, reason="retry-exhausted:timeout")
    _rejection(conn, 2, reason="dead-letter:auth")
    _rejection(conn, 3, reason="retry-exhausted:timeout")
    conn.execute(
        "INSERT INTO memory_journal_sources(memory_id, journal_entry_id, run_id, created_at) VALUES ('memory-existing', 3, 'run-failed', '2026-01-01T00:00:06+00:00')"
    )
    conn.commit()

    report = recovery_report(conn, reason_prefixes=["retry-exhausted:"], limit=10)
    assert report["candidate_count"] == 1
    assert report["items"][0]["journal_entry_id"] == 1
    assert report["by_reason"] == {"retry-exhausted:timeout": 1}
    assert report["by_category"] == {"timeout": 1}

    dry = schedule_replay(conn, reason_prefixes=["retry-exhausted:"], limit=10, dry_run=True, batch_id="replay-batch")
    assert dry["candidate_count"] == 1
    assert dry["scheduled"] == 0
    assert dry["by_category"] == {"timeout": 1}
    assert conn.execute("SELECT processed_run_id FROM journal_entries WHERE id = 1").fetchone()[0] == "run-failed"

    applied = schedule_replay(conn, reason_prefixes=["retry-exhausted:"], limit=10, dry_run=False, batch_id="replay-batch")
    assert applied["scheduled"] == 1
    assert applied["by_category"] == {"timeout": 1}
    row = conn.execute("SELECT processed_run_id, processed_at FROM journal_entries WHERE id = 1").fetchone()
    assert row["processed_run_id"] == ""
    assert row["processed_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM journal_rejections WHERE journal_entry_id = 1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_rejections WHERE journal_entry_id = 2").fetchone()[0] == 1
    assert conn.execute("SELECT processed_run_id FROM journal_entries WHERE id = 3").fetchone()[0] == "run-failed"
    audit = conn.execute(
        "SELECT COUNT(*) FROM governance_audit_events WHERE batch_id = 'replay-batch' AND event_type = 'journal_recovery' AND action = 'schedule_replay'"
    ).fetchone()[0]
    assert audit == 1


def test_journal_recovery_can_include_dead_letters_explicitly():
    conn = _conn()
    _entry(conn, 11)
    _rejection(conn, 11, reason="dead-letter:auth")
    conn.commit()

    default_report = recovery_report(conn, reason_prefixes=["retry-exhausted:"], limit=10)
    assert default_report["candidate_count"] == 0

    with_dead_letters = recovery_report(conn, reason_prefixes=["retry-exhausted:", "dead-letter:"], limit=10)
    assert with_dead_letters["candidate_count"] == 1
    assert with_dead_letters["items"][0]["reason"] == "dead-letter:auth"
    assert with_dead_letters["by_category"] == {"auth": 1}


def test_journal_recovery_can_operator_classify_dead_letters_as_no_replay():
    conn = _conn()
    _entry(conn, 41)
    _entry(conn, 42)
    _rejection(conn, 41, reason="dead-letter:auth")
    _rejection(conn, 42, reason="retry-exhausted:timeout")
    conn.commit()

    dry = classify_recovery_candidates(
        conn,
        reason_prefixes=["retry-exhausted:", "dead-letter:"],
        limit=10,
        dry_run=True,
        batch_id="classify-batch",
        reason="root cause fixed elsewhere; historical entries are not useful to replay",
    )
    assert dry["candidate_count"] == 2
    assert dry["classified"] == 0
    assert recovery_report(conn, reason_prefixes=["retry-exhausted:", "dead-letter:"], limit=10)["candidate_count"] == 2

    applied = classify_recovery_candidates(
        conn,
        reason_prefixes=["retry-exhausted:", "dead-letter:"],
        limit=10,
        dry_run=False,
        batch_id="classify-batch",
        reason="root cause fixed elsewhere; historical entries are not useful to replay",
    )
    assert applied["classified"] == 2
    assert recovery_report(conn, reason_prefixes=["retry-exhausted:", "dead-letter:"], limit=10)["candidate_count"] == 0
    reasons = [row[0] for row in conn.execute("SELECT reason FROM journal_rejections ORDER BY journal_entry_id")]
    assert reasons == [
        "operator-classified:no_replay:dead-letter:auth",
        "operator-classified:no_replay:retry-exhausted:timeout",
    ]
    audit = conn.execute(
        "SELECT COUNT(*) FROM governance_audit_events WHERE batch_id = 'classify-batch' AND event_type = 'journal_recovery' AND action = 'classify_no_replay'"
    ).fetchone()[0]
    assert audit == 2


def test_journal_recovery_report_and_dry_run_do_not_rebuild_stale_fts_index():
    conn = _conn()
    _entry(conn, 12)
    _rejection(conn, 12)
    conn.execute("DELETE FROM memories_fts")
    conn.commit()
    before = conn.total_changes

    report = recovery_report(conn, reason_prefixes=["retry-exhausted:"], limit=10)
    dry = schedule_replay(conn, reason_prefixes=["retry-exhausted:"], limit=10, dry_run=True)

    assert conn.total_changes == before
    assert report["candidate_count"] == 1
    assert dry["candidate_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 0


def test_journal_recovery_dry_run_is_query_only(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    try:
        ensure_schema(writer)
        ensure_journal_schema(writer)
        _entry(writer, 13)
        _rejection(writer, 13)
        writer.commit()
    finally:
        writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        dry = schedule_replay(readonly, reason_prefixes=["retry-exhausted:"], limit=10, dry_run=True)
    finally:
        readonly.close()

    assert dry["dry_run"] is True
    assert dry["candidate_count"] == 1
    assert dry["scheduled"] == 0

    verifier = sqlite3.connect(db_path)
    try:
        assert verifier.execute("SELECT processed_run_id FROM journal_entries WHERE id = 13").fetchone()[0] == "run-failed"
    finally:
        verifier.close()


def test_journal_recovery_ignores_stale_rejection_from_previous_run():
    conn = _conn()
    _entry(conn, 21, processed_run_id="run-new")
    _rejection(conn, 21, run_id="run-old", reason="retry-exhausted:timeout")
    conn.commit()

    report = recovery_report(conn, reason_prefixes=["retry-exhausted:"], limit=10)
    applied = schedule_replay(conn, reason_prefixes=["retry-exhausted:"], limit=10, dry_run=False)

    assert report["candidate_count"] == 0
    assert applied["scheduled"] == 0
    assert conn.execute("SELECT processed_run_id FROM journal_entries WHERE id = 21").fetchone()[0] == "run-new"


def test_journal_recovery_dedupes_multiple_rejections_for_same_entry():
    conn = _conn()
    _entry(conn, 31)
    _rejection(conn, 31, run_id="run-failed", reason="retry-exhausted:timeout", created_at="2026-01-01T00:00:05+00:00")
    conn.execute(
        "INSERT OR REPLACE INTO journal_rejections(journal_entry_id, run_id, reason, candidate, created_at) VALUES (31, 'run-failed-duplicate', 'retry-exhausted:again', '', '2026-01-01T00:00:06+00:00')"
    )
    conn.execute("UPDATE journal_entries SET processed_run_id = 'run-failed-duplicate' WHERE id = 31")
    conn.commit()

    report = recovery_report(conn, reason_prefixes=["retry-exhausted:"], limit=10)
    applied = schedule_replay(conn, reason_prefixes=["retry-exhausted:"], limit=10, dry_run=False, batch_id="dedupe-batch")

    assert report["candidate_count"] == 1
    assert report["items"][0]["run_id"] == "run-failed-duplicate"
    assert applied["scheduled"] == 1
    audit_count = conn.execute("SELECT COUNT(*) FROM governance_audit_events WHERE batch_id = 'dedupe-batch'").fetchone()[0]
    assert audit_count == 1
