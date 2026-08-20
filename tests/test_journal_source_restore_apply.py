"""Apply, lease, backup, transaction, and semantic contracts for journal source-restore."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

from journal_source_restore_support import (
    REFERENCED_DIGEST_ID,
    SHARED_CONTENT_HASH,
    UNREFERENCED_DIGEST_ID,
    apply_kwargs,
    approved_journal_rows,
    build_source_restore_pair,
    checkpoint_sqlite_file,
    compute_target_epoch,
    convert_to_wal_header_main_only,
    count_rows,
    dangling_processed_run_count,
    journal_row_by_identity,
    make_exploding_connection_factory,
    nontarget_snapshot,
    open_fixture_connection,
    rebind_source_expectations,
    sqlite_sequence_value,
    sqlite_sidecars,
)
from scope_recall.maintenance_lease import acquire_activation_lease, release_activation_lease
from scope_recall.writer_lease import ALLOWED_TRUTH_WRITER_ROLES


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(**kwargs):
    from scope_recall.journal_source_restore import run_journal_source_restore

    return run_journal_source_restore(**kwargs)


def _apply(pair, **overrides):
    lease = acquire_activation_lease(pair.target_path)
    try:
        kwargs = apply_kwargs(pair)
        kwargs.update(overrides)
        return _run(**kwargs)
    finally:
        release_activation_lease(lease)


def test_dedicated_writer_role_is_registered() -> None:
    assert "journal_source_restore" in ALLOWED_TRUTH_WRITER_ROLES


def test_apply_requires_confirmation(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _run(**{**apply_kwargs(pair), "maintenance_confirmed": False})
    assert receipt["ok"] is False
    assert receipt["error_code"] == "confirmation_required"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_apply_requires_activation_lease(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _run(**apply_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "activation_lease_required"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_apply_requires_dedicated_writer_role(tmp_path: Path, monkeypatch) -> None:
    pair = build_source_restore_pair(tmp_path)
    monkeypatch.setattr(
        "scope_recall.writer_lease.ALLOWED_TRUTH_WRITER_ROLES",
        frozenset(role for role in ALLOWED_TRUTH_WRITER_ROLES if role != "journal_source_restore"),
    )
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _apply(pair)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "writer_role_unavailable"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_apply_requires_prewrite_backup_path(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _apply(pair, prewrite_backup_path=None)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "prewrite_backup_required"
    assert count_rows(pair.target_path, "journal_entries") == before
    assert not pair.backup_path.exists()


def test_prewrite_backup_failure_refuses_without_mutation(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    pair.backup_path.parent.mkdir(parents=True, exist_ok=True)
    pair.backup_path.write_bytes(b"preexisting-backup")
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _apply(pair)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "prewrite_backup_failed"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_apply_requires_target_epoch(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _apply(pair, expected_target_epoch_digest="")
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_epoch_required"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_foreign_writer_contention_fails_closed(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    storage = pair.target_path.parent
    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage)!r}), role="provider")
        print("STATUS:" + lease.acquire()["status"], flush=True)
        sys.stdin.readline()
        lease.release()
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "STATUS:acquired"
        before = count_rows(pair.target_path, "journal_entries")
        receipt = _apply(pair)
        assert receipt["ok"] is False
        assert receipt["error_code"] == "truth_writer_busy"
        assert count_rows(pair.target_path, "journal_entries") == before
    finally:
        if child.poll() is None:
            child.stdin.write("\n")
            child.stdin.close()
            child.wait(timeout=10)


def test_apply_writer_revalidation_detects_unrelated_committed_change(
    tmp_path: Path, monkeypatch
) -> None:
    """Detect post-initial-capture unrelated committed drift before writer-bound recapture.

    The injected row is committed after the first ``compute_target_epoch``
    and is outside the epoch table shortlist, so detection cannot depend
    on maintaining that list. The later writer-bound file identity/hash
    recapture is the signal this test covers. It does not close the
    acknowledged cooperative-writer window between that hash and
    ``BEGIN IMMEDIATE``, and it does not claim every TOCTOU interleaving
    is impossible.
    """

    pair = build_source_restore_pair(tmp_path)
    from scope_recall import journal_source_restore as jsr

    original_epoch = jsr.compute_target_epoch
    mutated = {"done": False}

    def capture_then_mutate(path):
        epoch = original_epoch(path)
        if mutated["done"] or Path(path) != pair.target_path:
            return epoch
        mutated["done"] = True
        conn = open_fixture_connection(pair.target_path)
        try:
            conn.execute(
                """
                INSERT INTO skill_anchors(
                    id, playbook_id, skill_name, load_policy, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "anchor-toctou",
                    "playbook-toctou",
                    "skill-toctou",
                    "optional_reference",
                    "unrelated-committed-change",
                    "2026-01-23T00:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        checkpoint_sqlite_file(pair.target_path)
        return epoch

    monkeypatch.setattr(jsr, "compute_target_epoch", capture_then_mutate)
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _apply(pair)
    assert mutated["done"] is True
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_epoch_stale"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_stale_target_epoch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute(
            """
            INSERT INTO journal_entries(
                scope_id, shared_scope_id, session_id, turn_number, role, content,
                content_hash, created_at, metadata
            ) VALUES (
                'scope-stale', 'shared-stale', 'stale', 1, 'user', 'stale-row',
                'f' * 64, '2026-01-20T00:00:00+00:00', '{}'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _apply(pair)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_epoch_stale"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_wal_incoherent_target_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before = count_rows(pair.target_path, "journal_entries")
    conn = open_fixture_connection(pair.target_path)
    try:
        mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        assert mode == "wal"
        conn.execute(
            """
            INSERT INTO journal_entries(
                scope_id, shared_scope_id, session_id, turn_number, role, content,
                content_hash, created_at, metadata
            ) VALUES (
                'scope-wal', 'shared-wal', 'wal-tail', 1, 'user', 'wal-tail',
                '1' * 64, '2026-01-21T00:00:00+00:00', '{}'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    wal_path, _shm, _journal = sqlite_sidecars(pair.target_path)
    if not wal_path.is_file() or wal_path.stat().st_size == 0:
        wal_path.write_bytes(b"incoherent-target-wal")
    receipt = _apply(pair)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_wal_incoherent"
    assert count_rows(pair.target_path, "journal_entries") == before + 1


def test_apply_wal_header_main_only_target_is_not_blocked_by_preflight_lookup(
    tmp_path: Path,
) -> None:
    """W157: preflight ledger lookup must not turn a checkpointed WAL-header into incoherent WAL."""

    pair = build_source_restore_pair(tmp_path)
    convert_to_wal_header_main_only(pair.target_path)
    receipt = _apply(
        pair,
        expected_target_epoch_digest=compute_target_epoch(pair.target_path)["epoch_digest"],
        operation_id="op_jsr_wal_header_apply",
    )
    assert receipt["ok"] is True
    assert receipt["error_code"] != "target_wal_incoherent"
    assert receipt["journal_inserted_count"] == 19
    assert count_rows(pair.target_path, "journal_entries") == 20


def test_first_apply_restores_nineteen_and_two(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before_nontarget = nontarget_snapshot(pair.target_path)
    receipt = _apply(pair)
    assert receipt["ok"] is True
    assert receipt["dry_run"] is False
    assert receipt["stage"] == "apply"
    assert receipt["verdict"] == "applied"
    assert receipt["journal_inserted_count"] == 19
    assert receipt["digest_run_inserted_count"] == 2
    assert receipt["journal_already_present_count"] == 0
    assert receipt["remapping_occurred"] is True
    assert receipt["fts_aftercare_required"] is True
    assert receipt["backup_digest"]
    assert pair.backup_path.is_file()
    assert count_rows(pair.target_path, "journal_entries") == 20
    assert count_rows(pair.target_path, "journal_digest_runs") == 2
    assert nontarget_snapshot(pair.target_path) == before_nontarget


def test_second_identical_apply_is_idempotent(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    first = _apply(pair)
    assert first["ok"] is True
    pair_after = apply_kwargs(pair)
    pair_after["expected_target_epoch_digest"] = compute_target_epoch(pair.target_path)[
        "epoch_digest"
    ]
    pair_after["prewrite_backup_path"] = tmp_path / "backups" / "second.sqlite3"
    lease = acquire_activation_lease(pair.target_path)
    try:
        second = _run(**pair_after)
    finally:
        release_activation_lease(lease)
    assert second["ok"] is True
    assert second["journal_inserted_count"] == 0
    assert second["digest_run_inserted_count"] == 0
    assert second["journal_already_present_count"] == 19
    assert second["digest_run_already_present_count"] == 2
    assert second["remapping_occurred"] is False
    assert count_rows(pair.target_path, "journal_digest_runs") == 2


def test_occupied_and_free_source_ids_still_use_autoincrement(tmp_path: Path) -> None:
    occupied = build_source_restore_pair(tmp_path / "occupied", occupy_source_ids=True)
    free = build_source_restore_pair(tmp_path / "free", occupy_source_ids=False)
    occupied_receipt = _apply(occupied)
    free_receipt = _apply(free)
    assert occupied_receipt["ok"] is True
    assert free_receipt["ok"] is True
    sample = approved_journal_rows()[0]
    occupied_row = journal_row_by_identity(
        occupied.target_path,
        scope_id=sample["scope_id"],
        session_id=sample["session_id"],
        turn_number=sample["turn_number"],
        role=sample["role"],
        content_hash=sample["content_hash"],
    )
    free_row = journal_row_by_identity(
        free.target_path,
        scope_id=sample["scope_id"],
        session_id=sample["session_id"],
        turn_number=sample["turn_number"],
        role=sample["role"],
        content_hash=sample["content_hash"],
    )
    assert occupied_row is not None
    assert free_row is not None
    assert int(occupied_row["id"]) != int(sample["id"])
    assert int(free_row["id"]) != int(sample["id"])
    assert sqlite_sequence_value(occupied.target_path, "journal_entries") >= int(occupied_row["id"])
    assert sqlite_sequence_value(free.target_path, "journal_entries") >= int(free_row["id"])


def test_shared_content_hash_rows_all_restore(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    receipt = _apply(pair)
    assert receipt["ok"] is True
    conn = open_fixture_connection(pair.target_path)
    try:
        rows = conn.execute(
            "SELECT scope_id, session_id, turn_number, role, created_at FROM journal_entries "
            "WHERE content_hash = ? ORDER BY created_at",
            (SHARED_CONTENT_HASH,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 3
    identities = {(row["scope_id"], row["session_id"], row["turn_number"], row["role"]) for row in rows}
    assert len(identities) == 3


def test_full_fields_and_timestamps_are_preserved_without_append_path(
    tmp_path: Path, monkeypatch
) -> None:
    pair = build_source_restore_pair(tmp_path)

    def boom(*_args, **_kwargs):
        raise AssertionError("append/sanitize/now path must not run")

    # Patch only the implementation modules. Do not replace
    # ``journal.append_journal_entry``: that name is a re-export used by
    # adjacent writer-lease tests in the same pytest process.
    monkeypatch.setattr("scope_recall.journal_store.append_journal_entry", boom)
    monkeypatch.setattr("scope_recall.journal_store.sanitize_capture_text", boom)
    monkeypatch.setattr("scope_recall.sql_store.now_iso", boom)
    receipt = _apply(pair)
    assert receipt["ok"] is True
    source_sample = approved_journal_rows()[15]
    restored = journal_row_by_identity(
        pair.target_path,
        scope_id=source_sample["scope_id"],
        session_id=source_sample["session_id"],
        turn_number=source_sample["turn_number"],
        role=source_sample["role"],
        content_hash=source_sample["content_hash"],
    )
    assert restored is not None
    assert restored["created_at"] == source_sample["created_at"]
    assert restored["processed_run_id"] == source_sample["processed_run_id"]
    assert restored["processed_at"] == source_sample["processed_at"]
    assert restored["metadata"] == source_sample["metadata"]
    assert int(restored["extraction_attempts"]) == int(source_sample["extraction_attempts"])
    assert restored["deferred_run_id"] == source_sample["deferred_run_id"]
    assert restored["deferred_at"] == source_sample["deferred_at"]
    assert restored["content"] == source_sample["content"]
    empty = [
        row
        for row in approved_journal_rows()
        if row["processed_run_id"] == "" and row["content_hash"] != SHARED_CONTENT_HASH
    ][0]
    empty_restored = journal_row_by_identity(
        pair.target_path,
        scope_id=empty["scope_id"],
        session_id=empty["session_id"],
        turn_number=empty["turn_number"],
        role=empty["role"],
        content_hash=empty["content_hash"],
    )
    assert empty_restored is not None
    assert empty_restored["processed_run_id"] == ""


def test_referenced_and_unreferenced_digest_semantics(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    receipt = _apply(pair)
    assert receipt["ok"] is True
    conn = open_fixture_connection(pair.target_path)
    try:
        referenced = conn.execute(
            "SELECT * FROM journal_digest_runs WHERE id = ?",
            (REFERENCED_DIGEST_ID,),
        ).fetchone()
        unreferenced = conn.execute(
            "SELECT * FROM journal_digest_runs WHERE id = ?",
            (UNREFERENCED_DIGEST_ID,),
        ).fetchone()
        linked = int(
            conn.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE processed_run_id = ?",
                (REFERENCED_DIGEST_ID,),
            ).fetchone()[0]
        )
        empty = int(
            conn.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE processed_run_id = '' "
                "AND created_at >= ? AND created_at < ?",
                (pair.journal_created_at_start, pair.journal_created_at_end),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert referenced is not None
    assert referenced["status"] == "ok"
    assert unreferenced is not None
    assert unreferenced["status"] == "error"
    assert unreferenced["error"] == "synthetic-timeout"
    assert linked == 8
    assert empty == 11


def test_journal_logical_collision_fails_closed(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    sample = approved_journal_rows()[0]
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute(
            """
            INSERT INTO journal_entries(
                scope_id, shared_scope_id, session_id, turn_number, role, content,
                content_hash, created_at, metadata
            ) VALUES (?, 'other-shared', ?, ?, ?, 'different-logical-body', ?, ?, '{}')
            """,
            (
                sample["scope_id"],
                sample["session_id"],
                sample["turn_number"],
                sample["role"],
                sample["content_hash"],
                "2020-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    pair_stale = apply_kwargs(pair)
    pair_stale["expected_target_epoch_digest"] = compute_target_epoch(pair.target_path)[
        "epoch_digest"
    ]
    lease = acquire_activation_lease(pair.target_path)
    try:
        receipt = _run(**pair_stale)
    finally:
        release_activation_lease(lease)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "journal_logical_conflict"
    assert count_rows(pair.target_path, "journal_digest_runs") == 0


def test_digest_logical_collision_fails_closed(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute(
            """
            INSERT INTO journal_digest_runs(
                id, started_at, status, extractor, interval_label, metadata
            ) VALUES (?, '2020-01-01T00:00:00+00:00', 'other', 'heuristic', '', '{}')
            """,
            (REFERENCED_DIGEST_ID,),
        )
        conn.commit()
    finally:
        conn.close()
    pair_stale = apply_kwargs(pair)
    pair_stale["expected_target_epoch_digest"] = compute_target_epoch(pair.target_path)[
        "epoch_digest"
    ]
    lease = acquire_activation_lease(pair.target_path)
    try:
        receipt = _run(**pair_stale)
    finally:
        release_activation_lease(lease)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "digest_logical_conflict"
    assert count_rows(pair.target_path, "journal_entries") == 1


def test_injected_mid_transaction_rollback(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before_journals = count_rows(pair.target_path, "journal_entries")
    before_digests = count_rows(pair.target_path, "journal_digest_runs")
    receipt = _apply(
        pair,
        connection_factory=make_exploding_connection_factory(fail_on_insert=3),
    )
    assert receipt["ok"] is False
    assert receipt["error_code"] == "apply_rolled_back"
    assert count_rows(pair.target_path, "journal_entries") == before_journals
    assert count_rows(pair.target_path, "journal_digest_runs") == before_digests


def test_nontarget_tables_stay_unchanged_and_schema_is_not_bumped(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before = nontarget_snapshot(pair.target_path)
    source_user_version = pair.expected_user_version
    receipt = _apply(pair)
    assert receipt["ok"] is True
    assert nontarget_snapshot(pair.target_path) == before
    conn = open_fixture_connection(pair.target_path)
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    assert user_version == source_user_version


def test_omitted_referenced_digest_is_refused_on_apply(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.source_path)
    try:
        conn.execute(
            "DELETE FROM journal_digest_runs WHERE id = ?",
            (REFERENCED_DIGEST_ID,),
        )
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.source_path)
    rebound = rebind_source_expectations(pair)
    before_journals = count_rows(pair.target_path, "journal_entries")
    before_digests = count_rows(pair.target_path, "journal_digest_runs")
    receipt = _apply(rebound)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "dangling_digest_reference"
    assert count_rows(pair.target_path, "journal_entries") == before_journals
    assert count_rows(pair.target_path, "journal_digest_runs") == before_digests
    assert dangling_processed_run_count(pair.target_path) == 0
    assert not pair.backup_path.exists()


def test_nonempty_target_rollback_journal_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before = count_rows(pair.target_path, "journal_entries")
    _wal, _shm, journal = sqlite_sidecars(pair.target_path)
    journal.write_bytes(b"dirty-target-rollback-journal")
    receipt = _apply(pair)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_wal_incoherent"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_symlinked_target_shm_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    _wal, shm_path, _journal = sqlite_sidecars(pair.target_path)
    target = tmp_path / "shm-bytes.bin"
    target.write_bytes(b"symlink-shm")
    try:
        shm_path.symlink_to(target)
    except OSError as exc:
        import pytest

        pytest.skip(f"symlink unavailable: {exc}")
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _apply(pair)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_wal_incoherent"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_prewrite_backup_fence_blocks_external_write(
    tmp_path: Path, monkeypatch
) -> None:
    pair = build_source_restore_pair(tmp_path)
    from scope_recall import sqlite_backup

    original = sqlite_backup._transfer_online_backup
    state = {"external_committed": None}

    def seam(source_conn: sqlite3.Connection, dest_conn: sqlite3.Connection) -> None:
        original(source_conn, dest_conn)
        ext = sqlite3.connect(os.fspath(pair.target_path), timeout=0.05)
        try:
            ext.execute("BEGIN IMMEDIATE")
            ext.execute(
                """
                INSERT INTO journal_entries(
                    scope_id, shared_scope_id, session_id, turn_number, role,
                    content, content_hash, created_at, metadata
                ) VALUES (
                    'scope-race', 'shared-race', 'race', 1, 'user',
                    'unbacked-mutation', ?, '2026-01-22T00:00:00+00:00', '{}'
                )
                """,
                ("2" * 64,),
            )
            ext.commit()
            state["external_committed"] = True
        except sqlite3.OperationalError:
            state["external_committed"] = False
        finally:
            ext.close()

    monkeypatch.setattr(sqlite_backup, "_transfer_online_backup", seam)
    before_journals = count_rows(pair.target_path, "journal_entries")
    before_nontarget = nontarget_snapshot(pair.target_path)
    receipt = _apply(pair)
    assert state["external_committed"] is False
    assert receipt["ok"] is True
    assert receipt["journal_inserted_count"] == 19
    assert receipt["digest_run_inserted_count"] == 2
    assert count_rows(pair.backup_path, "journal_entries") == before_journals
    assert count_rows(pair.target_path, "journal_entries") == before_journals + 19
    conn = open_fixture_connection(pair.target_path)
    try:
        raced = int(
            conn.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE content = ?",
                ("unbacked-mutation",),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert raced == 0
    assert nontarget_snapshot(pair.target_path) == before_nontarget


def test_exact_existing_row_is_already_present(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    sample = approved_journal_rows()[0]
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute(
            """
            INSERT INTO journal_entries(
                scope_id, shared_scope_id, platform, user_id, chat_id, thread_id,
                gateway_session_key, agent_identity, agent_workspace, session_id,
                turn_number, role, content, content_hash, created_at, processed_run_id,
                processed_at, metadata, extraction_attempts, deferred_run_id, deferred_at
            ) VALUES (
                :scope_id, :shared_scope_id, :platform, :user_id, :chat_id, :thread_id,
                :gateway_session_key, :agent_identity, :agent_workspace, :session_id,
                :turn_number, :role, :content, :content_hash, :created_at, :processed_run_id,
                :processed_at, :metadata, :extraction_attempts, :deferred_run_id, :deferred_at
            )
            """,
            {key: sample[key] for key in sample if key != "id"},
        )
        conn.commit()
    finally:
        conn.close()
    pair_ready = apply_kwargs(pair)
    pair_ready["expected_target_epoch_digest"] = compute_target_epoch(pair.target_path)[
        "epoch_digest"
    ]
    lease = acquire_activation_lease(pair.target_path)
    try:
        receipt = _run(**pair_ready)
    finally:
        release_activation_lease(lease)
    assert receipt["ok"] is True
    assert receipt["journal_already_present_count"] == 1
    assert receipt["journal_inserted_count"] == 18
