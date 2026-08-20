"""Immutable source opener, sidecar, and snapshot-identity contracts."""

from __future__ import annotations

from pathlib import Path

from journal_source_restore_support import (
    convert_to_wal_header_main_only,
    file_identity,
    ordinary_readonly_open,
    plan_kwargs,
    rebind_source_expectations,
    sqlite_sidecars,
    build_source_restore_pair,
)


def _run(**kwargs):
    from scope_recall.journal_source_restore import run_journal_source_restore

    return run_journal_source_restore(**kwargs)


def test_ordinary_ro_materializes_sidecars_on_wal_header_main(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    convert_to_wal_header_main_only(pair.source_path)
    assert all(not sidecar.exists() for sidecar in sqlite_sidecars(pair.source_path))
    conn = ordinary_readonly_open(pair.source_path)
    try:
        assert str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower() == "ok"
    finally:
        conn.close()
    wal_path, shm_path, journal_path = sqlite_sidecars(pair.source_path)
    assert wal_path.exists() or shm_path.exists() or journal_path.exists()
    for sidecar in (wal_path, shm_path, journal_path):
        if sidecar.exists():
            sidecar.unlink()


def test_immutable_opener_reads_wal_header_main_without_sidecars(tmp_path: Path) -> None:
    from scope_recall.journal_source_restore import open_immutable_source_connection

    pair = build_source_restore_pair(tmp_path)
    convert_to_wal_header_main_only(pair.source_path)
    before = file_identity(pair.source_path)
    conn = open_immutable_source_connection(pair.source_path)
    try:
        assert str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower() == "ok"
        assert str(conn.execute("PRAGMA integrity_check(1)").fetchone()[0]).lower() == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        conn.close()
    after = file_identity(pair.source_path)
    assert after == before
    assert all(not sidecar.exists() and not sidecar.is_symlink() for sidecar in sqlite_sidecars(pair.source_path))


def test_plan_wal_header_main_only_source_keeps_bytes_and_no_sidecars(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    convert_to_wal_header_main_only(pair.source_path)
    rebound = rebind_source_expectations(pair)
    before = file_identity(rebound.source_path)
    receipt = _run(**plan_kwargs(rebound))
    after = file_identity(rebound.source_path)
    assert receipt["ok"] is True
    assert receipt["verdict"] == "ready"
    assert receipt["journal_selected_count"] == 19
    assert after == before
    assert all(
        not sidecar.exists() and not sidecar.is_symlink()
        for sidecar in sqlite_sidecars(rebound.source_path)
    )
    assert all(
        not sidecar.exists()
        for sidecar in sqlite_sidecars(rebound.target_path)
    )


def test_compute_target_epoch_on_wal_header_target_creates_no_sidecars(
    tmp_path: Path,
) -> None:
    from scope_recall.journal_source_restore import compute_target_epoch

    pair = build_source_restore_pair(tmp_path)
    convert_to_wal_header_main_only(pair.target_path)
    assert all(not sidecar.exists() and not sidecar.is_symlink() for sidecar in sqlite_sidecars(pair.target_path))
    before = file_identity(pair.target_path)
    epoch = compute_target_epoch(pair.target_path)
    after = file_identity(pair.target_path)
    assert epoch["epoch_digest"]
    assert epoch["file_sha256"] == before["sha256"]
    assert after == before
    assert all(
        not sidecar.exists() and not sidecar.is_symlink()
        for sidecar in sqlite_sidecars(pair.target_path)
    )


def test_plan_wal_header_main_only_target_creates_no_sidecars(tmp_path: Path) -> None:
    """W133 B1: dry-run inspection of a checkpointed WAL-header target is zero-side-effect."""

    pair = build_source_restore_pair(tmp_path)
    convert_to_wal_header_main_only(pair.target_path)
    before_sidecars = sqlite_sidecars(pair.target_path)
    assert all(not sidecar.exists() and not sidecar.is_symlink() for sidecar in before_sidecars)
    before = file_identity(pair.target_path)
    receipt = _run(**plan_kwargs(pair))
    after = file_identity(pair.target_path)
    after_sidecars = sqlite_sidecars(pair.target_path)
    assert receipt["ok"] is True
    assert receipt["verdict"] == "ready"
    assert receipt["dry_run"] is True
    assert after == before
    assert all(not sidecar.exists() and not sidecar.is_symlink() for sidecar in after_sidecars)


def test_preflight_ledger_lookup_on_wal_header_target_creates_no_sidecars(
    tmp_path: Path,
) -> None:
    """W157: committed-ledger preflight must not dest-open a main-only target."""

    from scope_recall.journal_source_restore import _lookup_committed_operation

    pair = build_source_restore_pair(tmp_path)
    convert_to_wal_header_main_only(pair.target_path)
    assert all(not sidecar.exists() and not sidecar.is_symlink() for sidecar in sqlite_sidecars(pair.target_path))
    before = file_identity(pair.target_path)
    lookup = _lookup_committed_operation(pair.target_path, "op_jsr_absent")
    after = file_identity(pair.target_path)
    assert lookup.status == "absent"
    assert lookup.row is None
    assert after == before
    assert all(
        not sidecar.exists() and not sidecar.is_symlink()
        for sidecar in sqlite_sidecars(pair.target_path)
    )
