"""Window, excluded-tail, reference-closure, hash, and epoch row contracts."""

from __future__ import annotations

from pathlib import Path

from scope_recall.journal_source_restore_rows import journal_semantic_record

from journal_source_restore_support import (
    DIGEST_EXCLUDED_END,
    DIGEST_EXCLUDED_START,
    JOURNAL_EXCLUDED_END,
    JOURNAL_EXCLUDED_START,
    REFERENCED_DIGEST_ID,
    apply_kwargs,
    approved_digest_rows,
    approved_journal_rows,
    build_source_restore_pair,
    checkpoint_sqlite_file,
    compute_digest_run_set_digest,
    count_rows,
    dangling_processed_run_count,
    journal_content_hash,
    open_fixture_connection,
    plan_kwargs,
    rebind_source_expectations,
    select_digest_window,
)
def _run(**kwargs):
    from scope_recall.journal_source_restore import run_journal_source_restore

    return run_journal_source_restore(**kwargs)


def _excluded_kwargs(pair) -> dict[str, str]:
    return {
        "journal_excluded_start": JOURNAL_EXCLUDED_START,
        "journal_excluded_end": JOURNAL_EXCLUDED_END,
        "digest_excluded_start": DIGEST_EXCLUDED_START,
        "digest_excluded_end": DIGEST_EXCLUDED_END,
    }


def test_malformed_journal_window_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["journal_created_at_start"] = "not-an-rfc3339-timestamp"
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "journal_window_invalid"


def test_naive_journal_window_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["journal_created_at_start"] = "2026-03-01T00:00:00"
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "journal_window_invalid"


def test_exact_start_row_is_selected_and_exact_end_is_not(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is True
    assert receipt["journal_selected_count"] == 19
    conn = open_fixture_connection(pair.source_path)
    try:
        at_end = int(
            conn.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE created_at = ?",
                (pair.journal_created_at_end,),
            ).fetchone()[0]
        )
        at_start = int(
            conn.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE created_at = ?",
                (pair.journal_created_at_start,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert at_end >= 1
    assert at_start == 0


def test_excluded_tail_missing_from_target_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = {**plan_kwargs(pair), **_excluded_kwargs(pair)}
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "excluded_tail_missing"
    assert receipt["verdict"] != "ready"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_excluded_tail_conflicting_target_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    tail_hash = journal_content_hash("synthetic-after-window")
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute(
            """
            INSERT INTO journal_entries(
                scope_id, shared_scope_id, session_id, turn_number, role, content,
                content_hash, created_at, metadata
            ) VALUES (
                'scope-outside', 'other-shared', 'after', 1, 'user',
                'different-excluded-tail-body', ?, '2026-03-02T00:00:00+00:00', '{}'
            )
            """,
            (tail_hash,),
        )
        conn.commit()
    finally:
        conn.close()
    kwargs = {**plan_kwargs(pair), **_excluded_kwargs(pair)}
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "excluded_tail_conflict"
    assert receipt["verdict"] != "ready"


def test_before_window_rows_remain_outside_unit_of_work(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is True
    assert receipt["journal_selected_count"] == 19
    assert receipt["digest_run_selected_count"] == 2


def _narrow_digest_window_past_referenced(pair) -> dict:
    kwargs = plan_kwargs(pair)
    kwargs["digest_started_at_start"] = "2026-03-01T02:30:00+00:00"
    conn = open_fixture_connection(pair.source_path)
    try:
        selected = select_digest_window(
            conn,
            start=kwargs["digest_started_at_start"],
            end=kwargs["digest_started_at_end"],
        )
    finally:
        conn.close()
    kwargs["expected_digest_run_count"] = len(selected)
    kwargs["expected_digest_run_set_digest"] = compute_digest_run_set_digest(selected)
    return kwargs


def test_target_existing_exact_referenced_digest_closes_control(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    referenced = approved_digest_rows()[0]
    target = open_fixture_connection(pair.target_path)
    try:
        target.execute(
            """
            INSERT INTO journal_digest_runs(
                id, started_at, finished_at, status, extractor, interval_label,
                processed_entries, inserted, updated, skipped, error, metadata
            ) VALUES (
                :id, :started_at, :finished_at, :status, :extractor, :interval_label,
                :processed_entries, :inserted, :updated, :skipped, :error, :metadata
            )
            """,
            referenced,
        )
        target.commit()
    finally:
        target.close()
    receipt = _run(**_narrow_digest_window_past_referenced(pair))
    assert receipt["ok"] is True
    assert receipt["verdict"] == "ready"
    assert dangling_processed_run_count(pair.target_path) == 0


def test_absent_referenced_digest_control_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.source_path)
    try:
        conn.execute("DELETE FROM journal_digest_runs WHERE id = ?", (REFERENCED_DIGEST_ID,))
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.source_path)
    rebound = rebind_source_expectations(pair)
    receipt = _run(**plan_kwargs(rebound))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "dangling_digest_reference"


def test_conflicting_target_referenced_digest_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    target = open_fixture_connection(pair.target_path)
    try:
        target.execute(
            """
            INSERT INTO journal_digest_runs(
                id, started_at, status, extractor, interval_label, metadata
            ) VALUES (?, '2020-01-01T00:00:00+00:00', 'other', 'heuristic', '', '{}')
            """,
            (REFERENCED_DIGEST_ID,),
        )
        target.commit()
    finally:
        target.close()
    receipt = _run(**_narrow_digest_window_past_referenced(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "dangling_digest_reference"


def test_content_hash_mismatch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    sample = approved_journal_rows()[0]
    conn = open_fixture_connection(pair.source_path)
    try:
        conn.execute(
            """
            UPDATE journal_entries SET content = ?
            WHERE scope_id = ? AND session_id = ? AND turn_number = ?
              AND role = ? AND content_hash = ?
            """,
            (
                "corrupted-body-does-not-match-hash",
                sample["scope_id"],
                sample["session_id"],
                sample["turn_number"],
                sample["role"],
                sample["content_hash"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.source_path)
    rebound = rebind_source_expectations(pair)
    receipt = _run(**plan_kwargs(rebound))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "journal_content_hash_mismatch"
    assert receipt["verdict"] != "ready"


def test_target_epoch_includes_fts_operator_and_sequence(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    from scope_recall.journal_source_restore import compute_target_epoch as production_epoch

    epoch = production_epoch(pair.target_path)
    assert "memories_fts" in epoch["tables"]
    assert "operator_operations" in epoch["tables"]
    assert "journal_entries" in epoch["sqlite_sequence"]


def test_memories_fts_epoch_drift_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute(
            "INSERT INTO memories_fts(memory_id, content, summary) VALUES (?, ?, ?)",
            ("mem-fts-drift", "fts-drift", "fts-drift"),
        )
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.target_path)
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _run(**apply_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_epoch_stale"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_operator_operations_epoch_drift_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute(
            """
            INSERT INTO operator_operations(
                operation_id, operation_kind, target_ref, request_fingerprint,
                before_json, result_json, backup_path, status, receipt_state,
                receipt_path, receipt_attempts, receipt_last_error,
                committed_at, updated_at
            ) VALUES (
                'op-epoch-drift', 'probe.drift', '', ?, '{}', '{}', '',
                'committed', 'pending', '', 0, '',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            )
            """,
            ("a" * 64,),
        )
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.target_path)
    receipt = _run(**apply_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_epoch_stale"


def test_sqlite_sequence_epoch_drift_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute(
            "UPDATE sqlite_sequence SET seq = seq + 50 WHERE name = 'journal_entries'"
        )
        if conn.total_changes == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES ('journal_entries', 99)"
            )
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.target_path)
    receipt = _run(**apply_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_epoch_stale"

def test_journal_semantic_record_defaults_missing_defer_count() -> None:
    from scope_recall.journal_source_restore_rows import journal_semantic_record

    row = {
        "scope_id": "scope-approved",
        "shared_scope_id": "shared-scope-approved",
        "platform": "cli",
        "user_id": "synthetic-user",
        "chat_id": "synthetic-chat",
        "thread_id": "",
        "gateway_session_key": "gw-synthetic",
        "agent_identity": "synthetic-agent",
        "agent_workspace": "hermes",
        "session_id": "session-01",
        "turn_number": 1,
        "role": "user",
        "content": "synthetic-approved-01",
        "content_hash": journal_content_hash("synthetic-approved-01"),
        "created_at": "2026-03-01T01:01:00+00:00",
        "processed_run_id": "",
        "processed_at": None,
        "metadata": "{}",
        "extraction_attempts": 0,
        "deferred_run_id": "",
        "deferred_at": None,
    }
    record = journal_semantic_record(row)
    assert record["defer_count"] == 0


def test_insert_missing_rows_defaults_old_source_defer_count(tmp_path: Path) -> None:
    from scope_recall.journal_source_restore_rows import insert_missing_rows
    from scope_recall.journal_store import ensure_journal_schema
    from scope_recall.sql_store import ensure_schema

    conn = open_fixture_connection(tmp_path / "target.sqlite3")
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        row = {
            "id": 42,
            "scope_id": "scope-old",
            "shared_scope_id": "shared-old",
            "platform": "cli",
            "user_id": "synthetic-user",
            "chat_id": "synthetic-chat",
            "thread_id": "",
            "gateway_session_key": "gw-synthetic",
            "agent_identity": "synthetic-agent",
            "agent_workspace": "hermes",
            "session_id": "old-session",
            "turn_number": 1,
            "role": "user",
            "content": "old-source-missing-defer-count",
            "content_hash": journal_content_hash("old-source-missing-defer-count"),
            "created_at": "2026-03-01T01:01:00+00:00",
            "processed_run_id": "",
            "processed_at": None,
            "metadata": "{}",
            "extraction_attempts": 1,
            "deferred_run_id": "defer-old",
            "deferred_at": "2026-03-01T01:40:00+00:00",
        }
        inserted, _digest_inserted, remapping, _evidence = insert_missing_rows(
            conn, journals=[row], digests=[]
        )
        conn.commit()
        assert inserted == 1
        stored = conn.execute(
            "SELECT defer_count, deferred_run_id FROM journal_entries "
            "WHERE scope_id = 'scope-old'"
        ).fetchone()
        assert int(stored["defer_count"] or 0) == 0
        assert stored["deferred_run_id"] == "defer-old"
        assert remapping is True
    finally:
        conn.close()


def test_restore_resets_target_session_cursor_so_remapped_rows_stay_loadable(
    tmp_path: Path,
) -> None:
    """Do not copy source session cursors. Restore remaps IDs and may merge.

    A leftover high resume_after_id plus a later unprocessed row would hide
    remapped IDs on the wrap side. Reset to cursor-zero so they load.
    """

    from scope_recall.journal_source_restore_rows import insert_missing_rows
    from scope_recall.journal_store import (
        ensure_journal_schema,
        load_session_digest_state,
        load_unprocessed_journal_entries,
        upsert_session_digest_state,
    )
    from scope_recall.sql_store import ensure_schema

    conn = open_fixture_connection(tmp_path / "target.sqlite3")
    try:
        ensure_schema(conn)
        ensure_journal_schema(conn)
        conn.execute(
            """
            INSERT INTO journal_entries(
                id, scope_id, shared_scope_id, platform, user_id, chat_id, thread_id,
                gateway_session_key, agent_identity, agent_workspace, session_id,
                turn_number, role, content, content_hash, created_at,
                processed_run_id, metadata
            ) VALUES (
                2000, 'scope-old', 'shared-old', 'cli', 'synthetic-user',
                'synthetic-chat', '', 'gw-synthetic', 'synthetic-agent', 'hermes',
                'old-session', 99, 'user', 'later-target-row', ?,
                '2026-03-01T02:00:00+00:00', '', '{}'
            )
            """,
            (journal_content_hash("later-target-row"),),
        )
        upsert_session_digest_state(
            conn,
            scope_id="scope-old",
            session_id="old-session",
            resume_after_id=1016,
            run_id="preexisting-target",
        )
        row = {
            "id": 42,
            "scope_id": "scope-old",
            "shared_scope_id": "shared-old",
            "platform": "cli",
            "user_id": "synthetic-user",
            "chat_id": "synthetic-chat",
            "thread_id": "",
            "gateway_session_key": "gw-synthetic",
            "agent_identity": "synthetic-agent",
            "agent_workspace": "hermes",
            "session_id": "old-session",
            "turn_number": 1,
            "role": "user",
            "content": "remapped-restored-row",
            "content_hash": journal_content_hash("remapped-restored-row"),
            "created_at": "2026-03-01T01:01:00+00:00",
            "processed_run_id": "",
            "processed_at": None,
            "metadata": "{}",
            "extraction_attempts": 0,
            "deferred_run_id": "",
            "deferred_at": None,
            "defer_count": 2,
        }
        inserted, _digest_inserted, remapping, _evidence = insert_missing_rows(
            conn, journals=[row], digests=[]
        )
        conn.commit()
        assert inserted == 1
        assert remapping is True
        cursor = load_session_digest_state(
            conn, scope_id="scope-old", session_id="old-session"
        )
        assert cursor is None or int(cursor["resume_after_id"] or 0) == 0
        restored_id = int(
            conn.execute(
                "SELECT id FROM journal_entries WHERE content_hash = ?",
                (row["content_hash"],),
            ).fetchone()[0]
        )
        assert restored_id != 42
        loaded = load_unprocessed_journal_entries(
            conn, scope_ids=["scope-old"], limit=50
        )
        assert restored_id in {int(entry.id) for entry in loaded}
    finally:
        conn.close()

def test_journal_semantic_record_defaults_missing_retryable_failures() -> None:
    """Old source snapshots need not already have the retryable_failures column."""

    row = {
        "scope_id": "scope",
        "shared_scope_id": "shared",
        "platform": "cli",
        "user_id": "user",
        "chat_id": "chat",
        "thread_id": "",
        "gateway_session_key": "",
        "agent_identity": "default",
        "agent_workspace": "hermes",
        "session_id": "s",
        "turn_number": 1,
        "role": "user",
        "content": "old snapshot without retryable column",
        "content_hash": "abc",
        "created_at": "2026-01-01T00:00:00+00:00",
        "processed_run_id": "",
        "processed_at": None,
        "metadata": "{}",
        "extraction_attempts": 0,
        "deferred_run_id": "",
        "deferred_at": None,
    }
    record = journal_semantic_record(row)
    assert record["retryable_failures"] == 0
    assert "retryable_failures" not in row


def test_journal_semantic_record_preserves_retryable_failures() -> None:
    row = {
        "scope_id": "scope",
        "shared_scope_id": "shared",
        "platform": "cli",
        "user_id": "user",
        "chat_id": "chat",
        "thread_id": "",
        "gateway_session_key": "",
        "agent_identity": "default",
        "agent_workspace": "hermes",
        "session_id": "s",
        "turn_number": 1,
        "role": "user",
        "content": "approved snapshot already has retryable_failures",
        "content_hash": "abc",
        "created_at": "2026-01-01T00:00:00+00:00",
        "processed_run_id": "",
        "processed_at": None,
        "metadata": "{}",
        "extraction_attempts": 0,
        "deferred_run_id": "",
        "deferred_at": None,
        "retryable_failures": 4,
    }
    record = journal_semantic_record(row)
    assert record["retryable_failures"] == 4
