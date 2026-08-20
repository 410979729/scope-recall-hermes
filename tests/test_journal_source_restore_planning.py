"""Planning and binding contracts for journal source-restore."""

from __future__ import annotations

from pathlib import Path

from journal_source_restore_support import (
    JOURNAL_WINDOW_END,
    REFERENCED_DIGEST_ID,
    apply_kwargs,
    approved_journal_rows,
    build_source_restore_pair,
    checkpoint_sqlite_file,
    count_rows,
    dangling_processed_run_count,
    nontarget_snapshot,
    open_fixture_connection,
    plan_kwargs,
    rebind_source_expectations,
    sqlite_sidecars,
)

from scope_recall.maintenance_lease import acquire_activation_lease, release_activation_lease


def _run(**kwargs):
    from scope_recall.journal_source_restore import run_journal_source_restore

    return run_journal_source_restore(**kwargs)


def test_dry_run_is_query_only_and_reports_counts_without_mutation(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before_target = nontarget_snapshot(pair.target_path)
    journal_before = count_rows(pair.target_path, "journal_entries")
    digest_before = count_rows(pair.target_path, "journal_digest_runs")
    source_mtime = pair.source_path.stat().st_mtime_ns

    receipt = _run(**plan_kwargs(pair))

    assert receipt["ok"] is True
    assert receipt["dry_run"] is True
    assert receipt["stage"] == "plan"
    assert receipt["verdict"] == "ready"
    assert receipt["journal_selected_count"] == 19
    assert receipt["digest_run_selected_count"] == 2
    assert receipt["journal_set_digest"] == pair.expected_journal_set_digest
    assert receipt["digest_run_set_digest"] == pair.expected_digest_run_set_digest
    assert receipt["journal_conflict_count"] == 0
    assert receipt["digest_run_conflict_count"] == 0
    assert receipt.get("journal_inserted_count", 0) == 0
    assert "content" not in receipt
    assert "metadata" not in receipt
    assert count_rows(pair.target_path, "journal_entries") == journal_before
    assert count_rows(pair.target_path, "journal_digest_runs") == digest_before
    assert nontarget_snapshot(pair.target_path) == before_target
    assert pair.source_path.stat().st_mtime_ns == source_mtime


def test_same_path_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["target_path"] = pair.source_path
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "same_path"
    assert count_rows(pair.source_path, "journal_entries") >= 19


def test_unhealthy_source_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    pair.source_path.write_bytes(b"not-a-sqlite-database")
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_unhealthy"


def test_nonempty_source_wal_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    wal_path, _shm, _journal = sqlite_sidecars(pair.source_path)
    wal_path.write_bytes(b"dirty-wal-bytes")
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_wal_present"


def test_zero_byte_source_sidecars_are_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    wal_path, shm_path, _journal = sqlite_sidecars(pair.source_path)
    wal_path.write_bytes(b"")
    shm_path.write_bytes(b"")
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_wal_present"
    assert receipt["verdict"] != "ready"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_source_sha256_mismatch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["expected_source_sha256"] = "0" * 64
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_sha256_mismatch"


def test_source_schema_digest_mismatch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["expected_schema_digest"] = "0" * 64
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_schema_digest_mismatch"


def test_source_user_version_mismatch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["expected_user_version"] = 1
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_user_version_mismatch"


def test_unbound_before_window_rows_do_not_by_themselves_refuse(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    source = open_fixture_connection(pair.source_path)
    try:
        total_journals = int(source.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0])
        total_digests = int(source.execute("SELECT COUNT(*) FROM journal_digest_runs").fetchone()[0])
        before_window = int(
            source.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE created_at < ?",
                (pair.journal_created_at_start,),
            ).fetchone()[0]
        )
    finally:
        source.close()
    assert total_journals > 19
    assert total_digests > 2
    assert before_window >= 1
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is True
    assert receipt["journal_selected_count"] == 19
    assert receipt["digest_run_selected_count"] == 2


def test_widened_window_without_new_approval_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["journal_created_at_end"] = "2026-03-03T00:00:00+00:00"
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] in {
        "selection_count_mismatch",
        "journal_set_digest_mismatch",
    }
    assert count_rows(pair.target_path, "journal_entries") == count_rows(
        pair.target_path, "journal_entries"
    )


def test_expected_journal_count_mismatch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["expected_journal_count"] = 18
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "selection_count_mismatch"


def test_journal_set_digest_mismatch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["expected_journal_set_digest"] = "0" * 64
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "journal_set_digest_mismatch"


def test_digest_run_set_digest_mismatch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["expected_digest_run_set_digest"] = "0" * 64
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "digest_run_set_digest_mismatch"


def test_planning_does_not_call_now_iso_or_mutate_when_apply_gates_absent(
    tmp_path: Path, monkeypatch
) -> None:
    pair = build_source_restore_pair(tmp_path)

    def boom() -> str:
        raise AssertionError("now_iso must not run during source-restore planning")

    monkeypatch.setattr("scope_recall.sql_store.now_iso", boom)
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is True
    assert receipt["dry_run"] is True


def test_apply_kwargs_without_confirmation_still_plan_cleanly(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = apply_kwargs(pair)
    kwargs["dry_run"] = True
    kwargs["maintenance_confirmed"] = False
    lease = acquire_activation_lease(pair.target_path)
    try:
        receipt = _run(**kwargs)
    finally:
        release_activation_lease(lease)
    assert receipt["ok"] is True
    assert receipt["dry_run"] is True


def test_half_open_end_bound_excludes_end_timestamp(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    assert kwargs["journal_created_at_end"] == JOURNAL_WINDOW_END
    receipt = _run(**kwargs)
    assert receipt["ok"] is True
    assert receipt["journal_selected_count"] == 19


def test_canonical_journal_set_digest_golden_vector() -> None:
    from journal_source_restore_oracles import (
        GOLDEN_DIGEST_DIGEST,
        GOLDEN_DIGEST_VECTOR,
        GOLDEN_EPOCH_DIGEST,
        GOLDEN_EPOCH_PAYLOAD,
        GOLDEN_JOURNAL_DIGEST,
        GOLDEN_JOURNAL_VECTOR,
        independent_digest_run_set_digest,
        independent_epoch_digest,
        independent_journal_set_digest,
    )
    from scope_recall.journal_source_restore import (
        compute_digest_run_set_digest,
        compute_journal_set_digest,
    )

    assert independent_journal_set_digest(GOLDEN_JOURNAL_VECTOR) == GOLDEN_JOURNAL_DIGEST
    assert compute_journal_set_digest(list(GOLDEN_JOURNAL_VECTOR)) == GOLDEN_JOURNAL_DIGEST
    assert independent_digest_run_set_digest(GOLDEN_DIGEST_VECTOR) == GOLDEN_DIGEST_DIGEST
    assert compute_digest_run_set_digest(list(GOLDEN_DIGEST_VECTOR)) == GOLDEN_DIGEST_DIGEST
    assert independent_epoch_digest(GOLDEN_EPOCH_PAYLOAD) == GOLDEN_EPOCH_DIGEST


def test_independent_target_epoch_matches_production_on_fixture(tmp_path: Path) -> None:
    from journal_source_restore_oracles import independent_target_epoch
    from journal_source_restore_support import build_source_restore_pair
    from scope_recall.journal_source_restore import compute_target_epoch

    pair = build_source_restore_pair(tmp_path)
    independent = independent_target_epoch(pair.target_path)
    production = compute_target_epoch(pair.target_path)
    assert independent["epoch_digest"] == production["epoch_digest"]
    assert independent["tables"]["memories_fts"]["count"] == production["tables"]["memories_fts"]["count"]
    assert independent["tables"]["operator_operations"]["count"] == production["tables"]["operator_operations"]["count"]
    assert independent["sqlite_sequence"] == production["sqlite_sequence"]


def test_equal_journal_window_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["journal_created_at_end"] = kwargs["journal_created_at_start"]
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "journal_window_invalid"
    assert count_rows(pair.target_path, "journal_entries") == 1


def test_reversed_digest_window_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    start = kwargs["digest_started_at_start"]
    kwargs["digest_started_at_start"] = kwargs["digest_started_at_end"]
    kwargs["digest_started_at_end"] = start
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "digest_window_invalid"


def test_empty_journal_window_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = plan_kwargs(pair)
    kwargs["journal_created_at_start"] = "   "
    receipt = _run(**kwargs)
    assert receipt["ok"] is False
    assert receipt["error_code"] == "journal_window_invalid"


def test_nonempty_source_rollback_journal_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    _wal, _shm, journal = sqlite_sidecars(pair.source_path)
    journal.write_bytes(b"dirty-rollback-journal")
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_wal_present"


def test_zero_byte_source_journal_sidecar_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    _wal, _shm, journal = sqlite_sidecars(pair.source_path)
    journal.write_bytes(b"")
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_wal_present"
    assert receipt["verdict"] != "ready"


def test_zero_byte_wal_and_nonzero_shm_are_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    wal_path, shm_path, _journal = sqlite_sidecars(pair.source_path)
    wal_path.write_bytes(b"")
    shm_path.write_bytes(b"nonzero-shm-bytes")
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_wal_present"
    assert receipt["verdict"] != "ready"


def test_symlinked_source_wal_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    wal_path, _shm, _journal = sqlite_sidecars(pair.source_path)
    target = tmp_path / "wal-bytes.bin"
    target.write_bytes(b"symlink-wal")
    try:
        wal_path.symlink_to(target)
    except OSError as exc:
        import pytest

        pytest.skip(f"symlink unavailable: {exc}")
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "source_wal_present"


def test_source_content_mutation_after_inspection_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    pair = build_source_restore_pair(tmp_path)
    from scope_recall import journal_source_restore as jsr

    sample = approved_journal_rows()[0]
    original = jsr._inspect_source

    def inspect_then_mutate(source):
        info = original(source)
        conn = open_fixture_connection(source)
        try:
            conn.execute(
                """
                UPDATE journal_entries
                SET content = ?
                WHERE scope_id = ? AND session_id = ? AND turn_number = ?
                  AND role = ? AND content_hash = ?
                """,
                (
                    "mutated-after-inspect",
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
        return info

    monkeypatch.setattr(jsr, "_inspect_source", inspect_then_mutate)
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] in {
        "source_snapshot_changed",
        "journal_content_hash_mismatch",
    }
    assert receipt["verdict"] != "ready"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_omitted_referenced_digest_is_refused_on_plan(tmp_path: Path) -> None:
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
    receipt = _run(**plan_kwargs(rebound))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "dangling_digest_reference"
    assert receipt["verdict"] != "ready"
    assert dangling_processed_run_count(pair.target_path) == 0
    assert count_rows(pair.target_path, "journal_digest_runs") == 0


def test_journal_identity_conflict_is_not_ready(tmp_path: Path) -> None:
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
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["verdict"] == "not_ready"
    assert receipt["error_code"] == "journal_logical_conflict"
    assert receipt["journal_conflict_count"] == 1


def test_digest_identity_conflict_is_not_ready(tmp_path: Path) -> None:
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
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["verdict"] == "not_ready"
    assert receipt["error_code"] == "digest_logical_conflict"
    assert receipt["digest_run_conflict_count"] == 1


def test_target_schema_digest_mismatch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute("CREATE TABLE jsr_probe_schema(id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.target_path)
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_schema_digest_mismatch"
    assert receipt["verdict"] != "ready"


def test_target_user_version_mismatch_is_refused(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    conn = open_fixture_connection(pair.target_path)
    try:
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    finally:
        conn.close()
    checkpoint_sqlite_file(pair.target_path)
    receipt = _run(**plan_kwargs(pair))
    assert receipt["ok"] is False
    assert receipt["error_code"] == "target_user_version_mismatch"
    assert receipt["verdict"] != "ready"
