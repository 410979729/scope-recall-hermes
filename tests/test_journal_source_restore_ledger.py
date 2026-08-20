"""Transaction, operator-ledger, commit-reconciliation, and authority contracts."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from journal_source_restore_support import (
    apply_kwargs,
    build_source_restore_pair,
    checkpoint_sqlite_file,
    cli_argv,
    compute_target_epoch,
    convert_to_wal_header_main_only,
    count_rows,
    nontarget_snapshot,
    open_fixture_connection,
    sqlite_sidecars,
)
from scope_recall.maintenance_lease import (
    acquire_activation_lease,
    activation_lease_path,
    release_activation_lease,
)
from scope_recall.writer_lease import TruthWriterLease


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "journal.source_restore.py"


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


def test_apply_requires_operation_id(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _apply(pair, operation_id="")
    assert receipt["ok"] is False
    assert receipt["error_code"] == "operation_id_required"
    assert count_rows(pair.target_path, "journal_entries") == before


def test_one_operator_row_in_same_transaction_and_no_new_schema(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    before_schema = open_fixture_connection(pair.target_path)
    try:
        before_master = {
            (str(row["type"]), str(row["name"]))
            for row in before_schema.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        user_version = int(before_schema.execute("PRAGMA user_version").fetchone()[0])
    finally:
        before_schema.close()
    receipt = _apply(pair, operation_id="op_jsr_same_txn")
    assert receipt["ok"] is True
    conn = open_fixture_connection(pair.target_path)
    try:
        rows = conn.execute(
            "SELECT operation_kind, request_fingerprint, result_json FROM operator_operations"
        ).fetchall()
        after_master = {
            (str(row["type"]), str(row["name"]))
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        after_user = int(conn.execute("PRAGMA user_version").fetchone()[0])
        restore_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%source_restore%'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["operation_kind"] == "journal.source_restore"
    assert len(str(rows[0]["request_fingerprint"])) == 64
    result = json.loads(str(rows[0]["result_json"]))
    assert result["mapping_count"] == 19
    assert result["mapping_digest"]
    assert "pairs" in result
    assert after_master == before_master
    assert after_user == user_version
    assert restore_tables == []
    operator_receipts = list((pair.target_path.parent / "receipts").glob("operator.source_restore.*.json"))
    playbook_receipts = list((pair.target_path.parent / "receipts").glob("playbooks.source_restore.*.json"))
    assert operator_receipts
    assert playbook_receipts == []
    schema = json.loads(operator_receipts[0].read_text(encoding="utf-8"))
    assert schema["schema_version"] == "operator_receipt.v1"


def test_public_json_has_no_raw_ids_or_pairs(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    receipt = _apply(pair, operation_id="op_jsr_public")
    rendered = json.dumps(receipt)
    assert receipt["ok"] is True
    assert receipt["mapping_count"] == 19
    assert receipt["mapping_digest"]
    assert "pairs" not in receipt
    assert "id_map" not in receipt
    assert "source_id" not in rendered
    assert "target_id" not in rendered


def test_hashed_remap_survives_receipt_loss(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    receipt = _apply(pair, operation_id="op_jsr_survives")
    assert receipt["ok"] is True
    receipts_dir = pair.target_path.parent / "receipts"
    if receipts_dir.exists():
        for item in receipts_dir.glob("*.json"):
            item.unlink()
    conn = open_fixture_connection(pair.target_path)
    try:
        row = conn.execute(
            "SELECT result_json FROM operator_operations WHERE operation_id = ?",
            ("op_jsr_survives",),
        ).fetchone()
    finally:
        conn.close()
    result = json.loads(str(row["result_json"]))
    assert result["mapping_count"] == 19
    assert len(result["pairs"]) == 19
    assert all(len(item["source"]) == 64 and len(item["target"]) == 64 for item in result["pairs"])


def test_same_operation_id_different_fingerprint_refuses(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    first = _apply(pair, operation_id="op_jsr_retry")
    assert first["ok"] is True
    second_pair = apply_kwargs(pair)
    second_pair["expected_target_epoch_digest"] = compute_target_epoch(pair.target_path)[
        "epoch_digest"
    ]
    second_pair["prewrite_backup_path"] = tmp_path / "backups" / "second.sqlite3"
    second_pair["operation_id"] = "op_jsr_retry"
    lease = acquire_activation_lease(pair.target_path)
    try:
        second = _run(**second_pair)
    finally:
        release_activation_lease(lease)
    assert second["ok"] is False
    assert second["error_code"] == "operation_fingerprint_conflict"


def test_same_operation_id_same_fingerprint_reconciles_without_second_backup(
    tmp_path: Path,
) -> None:
    pair = build_source_restore_pair(tmp_path)
    first = _apply(pair, operation_id="op_jsr_same")
    assert first["ok"] is True
    backup_mtime = pair.backup_path.stat().st_mtime_ns
    journals = count_rows(pair.target_path, "journal_entries")
    kwargs = apply_kwargs(pair)
    kwargs["operation_id"] = "op_jsr_same"
    lease = acquire_activation_lease(pair.target_path)
    try:
        second = _run(**kwargs)
    finally:
        release_activation_lease(lease)
    assert second["ok"] is True
    assert second["verdict"] in {"applied", "committed_reconciled"}
    assert second["journal_inserted_count"] == 19
    assert count_rows(pair.target_path, "journal_entries") == journals
    assert pair.backup_path.stat().st_mtime_ns == backup_mtime


def test_same_operation_reconcile_on_wal_header_target_creates_no_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    """W157: retry lookup of a committed row must keep a main-only WAL-header clean."""

    pair = build_source_restore_pair(tmp_path)
    first = _apply(pair, operation_id="op_jsr_wal_retry")
    assert first["ok"] is True
    convert_to_wal_header_main_only(pair.target_path)
    from scope_recall import journal_source_restore as jsr

    original_open = jsr._open_target_writer
    seen: dict[str, list[bool]] = {}

    def wrapping_open(target, *, lease_token, connection_factory):
        seen["sidecars"] = [
            sidecar.exists() or sidecar.is_symlink()
            for sidecar in sqlite_sidecars(target)
        ]
        return original_open(
            target, lease_token=lease_token, connection_factory=connection_factory
        )

    monkeypatch.setattr(jsr, "_open_target_writer", wrapping_open)
    kwargs = apply_kwargs(pair)
    kwargs["operation_id"] = "op_jsr_wal_retry"
    lease = acquire_activation_lease(pair.target_path)
    try:
        second = _run(**kwargs)
    finally:
        release_activation_lease(lease)
    assert second["ok"] is True
    assert second["verdict"] == "committed_reconciled"
    assert seen["sidecars"] == [False, False, False]


def test_lookup_finds_wal_visible_row_when_sidecars_already_present(
    tmp_path: Path,
) -> None:
    """W157: dirty targets still read WAL-visible committed ledger rows."""

    pair = build_source_restore_pair(tmp_path)
    receipt = _apply(pair, operation_id="op_jsr_wal_visible")
    assert receipt["ok"] is True
    conn = sqlite3.connect(pair.target_path)
    try:
        mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).strip().lower()
        assert mode == "wal"
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("UPDATE operator_operations SET receipt_state = receipt_state")
        conn.commit()
        assert any(sidecar.exists() for sidecar in sqlite_sidecars(pair.target_path))
        from scope_recall.journal_source_restore import _lookup_committed_operation

        lookup = _lookup_committed_operation(pair.target_path, "op_jsr_wal_visible")
        assert lookup.status == "found"
        assert lookup.row is not None
        assert str(lookup.row.get("operation_id") or "") == "op_jsr_wal_visible"
    finally:
        conn.close()


def _pin_wal_visible_sidecars(path: Path) -> sqlite3.Connection:
    """Keep a live WAL pin so committed ledger rows stay sidecar-visible.

    Closing the writer often checkpoints and deletes siblings, which would
    hide the crash-before-checkpoint seam. The caller must close the pin.
    """

    conn = sqlite3.connect(path)
    mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).strip().lower()
    assert mode == "wal"
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("UPDATE operator_operations SET receipt_state = receipt_state")
    conn.commit()
    assert any(sidecar.exists() for sidecar in sqlite_sidecars(path))
    return conn


def test_same_operation_reconciles_from_wal_visible_ledger_after_crash_before_checkpoint(
    tmp_path: Path,
) -> None:
    """A committed apply that died before checkpoint/mirror must retry from WAL."""

    pair = build_source_restore_pair(tmp_path)
    first = _apply(pair, operation_id="op_jsr_wal_crash")
    assert first["ok"] is True
    journals = count_rows(pair.target_path, "journal_entries")
    pin = _pin_wal_visible_sidecars(pair.target_path)
    try:
        assert any(sidecar.exists() for sidecar in sqlite_sidecars(pair.target_path))
        retry = _apply(pair, operation_id="op_jsr_wal_crash")
        assert retry["ok"] is True
        assert retry["error_code"] != "target_wal_incoherent"
        assert retry["verdict"] == "committed_reconciled"
        assert retry["journal_inserted_count"] == 19
        assert count_rows(pair.target_path, "journal_entries") == journals
    finally:
        pin.close()


def test_unknown_operation_with_target_wal_sidecars_fails_closed(tmp_path: Path) -> None:
    """New or unknown apply IDs must still refuse a dirty target WAL."""

    pair = build_source_restore_pair(tmp_path)
    first = _apply(pair, operation_id="op_jsr_wal_known")
    assert first["ok"] is True
    before = count_rows(pair.target_path, "journal_entries")
    pin = _pin_wal_visible_sidecars(pair.target_path)
    try:
        assert any(sidecar.exists() for sidecar in sqlite_sidecars(pair.target_path))
        retry = _apply(pair, operation_id="op_jsr_wal_unknown")
        assert retry["ok"] is False
        assert retry["error_code"] == "target_wal_incoherent"
        assert count_rows(pair.target_path, "journal_entries") == before
    finally:
        pin.close()


def _leave_nonempty_rollback_journal(path: Path) -> None:
    """Leave a non-empty DELETE-mode rollback journal without WAL/SHM.

    The committed main-file row stays readable. A garbage journal would make
    lookup indeterminate and hide the exemption hole.
    """

    wal_path, shm_path, journal = sqlite_sidecars(path)
    conn = sqlite3.connect(path)
    try:
        mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).strip().lower()
        assert mode == "delete"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE operator_operations SET receipt_state = receipt_state")
        assert journal.is_file() and journal.stat().st_size > 0
        payload = journal.read_bytes()
        conn.rollback()
    finally:
        conn.close()
    journal.write_bytes(payload)
    assert journal.is_file() and journal.stat().st_size > 0
    assert not wal_path.exists() and not wal_path.is_symlink()
    assert not shm_path.exists() and not shm_path.is_symlink()


def test_same_operation_with_target_rollback_journal_fails_closed(tmp_path: Path) -> None:
    """A known committed ID plus a rollback journal must not skip the checkpoint gate.

    WAL-visible crash reconciliation is WAL/SHM only. A ``-journal`` sibling,
    even with a readable committed operation_id, proceeds to the existing
    checkpoint gate and refuses ``target_wal_incoherent`` without mutation.
    """

    pair = build_source_restore_pair(tmp_path)
    first = _apply(pair, operation_id="op_jsr_journal_known")
    assert first["ok"] is True
    checkpoint_sqlite_file(pair.target_path)
    journals = count_rows(pair.target_path, "journal_entries")
    digests = count_rows(pair.target_path, "journal_digest_runs")
    operators = count_rows(pair.target_path, "operator_operations")
    backup_mtime = pair.backup_path.stat().st_mtime_ns
    receipts_dir = pair.target_path.parent / "receipts"
    mirrors = list(receipts_dir.glob("operator.source_restore.*.json"))
    assert mirrors
    mirror_mtimes = {path: path.stat().st_mtime_ns for path in mirrors}
    _leave_nonempty_rollback_journal(pair.target_path)
    from scope_recall.journal_source_restore import _lookup_committed_operation

    lookup = _lookup_committed_operation(pair.target_path, "op_jsr_journal_known")
    assert lookup.status == "found"
    assert lookup.row is not None
    assert str(lookup.row.get("operation_id") or "") == "op_jsr_journal_known"
    retry = _apply(pair, operation_id="op_jsr_journal_known")
    assert retry["ok"] is False
    assert retry["error_code"] == "target_wal_incoherent"
    assert retry["verdict"] != "committed_reconciled"
    assert retry.get("receipt_state") != "mirrored"
    assert count_rows(pair.target_path, "journal_entries") == journals
    assert count_rows(pair.target_path, "journal_digest_runs") == digests
    assert count_rows(pair.target_path, "operator_operations") == operators
    assert pair.backup_path.stat().st_mtime_ns == backup_mtime
    after_mirrors = list(receipts_dir.glob("operator.source_restore.*.json"))
    assert {path: path.stat().st_mtime_ns for path in after_mirrors} == mirror_mtimes


def test_filesystem_mirror_omits_pairs_while_private_ledger_retains_them(
    tmp_path: Path,
) -> None:
    """W133 B4: public filesystem mirror omits remap pairs; private ledger keeps them."""

    pair = build_source_restore_pair(tmp_path)
    receipt = _apply(pair, operation_id="op_jsr_mirror_pairs")
    assert receipt["ok"] is True
    conn = open_fixture_connection(pair.target_path)
    try:
        row = conn.execute(
            "SELECT result_json FROM operator_operations WHERE operation_id = ?",
            ("op_jsr_mirror_pairs",),
        ).fetchone()
    finally:
        conn.close()
    private = json.loads(str(row["result_json"]))
    assert private["mapping_count"] == 19
    assert private["mapping_digest"]
    assert len(private["pairs"]) == 19
    assert all(len(item["source"]) == 64 and len(item["target"]) == 64 for item in private["pairs"])
    mirrors = list((pair.target_path.parent / "receipts").glob("operator.source_restore.*.json"))
    assert len(mirrors) == 1
    payload = json.loads(mirrors[0].read_text(encoding="utf-8"))
    rendered = json.dumps(payload, ensure_ascii=False)
    result = payload.get("result") if isinstance(payload, dict) else None
    assert isinstance(result, dict)
    assert result["mapping_count"] == 19
    assert result["mapping_digest"] == private["mapping_digest"]
    assert "pairs" not in result
    assert "pairs" not in payload
    assert "source_id" not in rendered
    assert "target_id" not in rendered


def test_commit_then_raise_is_not_labeled_rollback(tmp_path: Path, monkeypatch) -> None:
    pair = build_source_restore_pair(tmp_path)
    from scope_recall import journal_source_restore as jsr

    original = jsr._open_target_writer

    def wrapping_factory(target, *, lease_token, connection_factory):
        conn = original(target, lease_token=lease_token, connection_factory=connection_factory)
        real_commit = conn.commit

        def commit_then_raise() -> None:
            real_commit()
            raise RuntimeError("injected_post_commit")

        conn.commit = commit_then_raise  # type: ignore[method-assign]
        return conn

    monkeypatch.setattr(jsr, "_open_target_writer", wrapping_factory)
    receipt = _apply(pair, operation_id="op_jsr_commit_raise")
    assert receipt["error_code"] != "apply_rolled_back"
    assert receipt["ok"] is True or receipt["error_code"] in {
        "committed_receipt_debt",
        "commit_outcome_unknown",
    }
    assert count_rows(pair.target_path, "journal_digest_runs") == 2
    conn = open_fixture_connection(pair.target_path)
    try:
        present = conn.execute(
            "SELECT 1 FROM operator_operations WHERE operation_id = ?",
            ("op_jsr_commit_raise",),
        ).fetchone()
    finally:
        conn.close()
    assert present is not None


def test_post_commit_readback_unavailable_is_never_apply_rolled_back(
    tmp_path: Path, monkeypatch
) -> None:
    """W133 B2: durable committed bytes cannot be labeled apply_rolled_back."""

    pair = build_source_restore_pair(tmp_path)
    from scope_recall import journal_source_restore as jsr

    original_open = jsr._open_target_writer

    def wrapping_open(target, *, lease_token, connection_factory):
        conn = original_open(
            target, lease_token=lease_token, connection_factory=connection_factory
        )
        real_commit = conn.commit

        def commit_then_raise() -> None:
            real_commit()
            raise RuntimeError("injected_post_commit_readback_unavailable")

        conn.commit = commit_then_raise  # type: ignore[method-assign]
        return conn

    monkeypatch.setattr(jsr, "_open_target_writer", wrapping_open)
    monkeypatch.setattr(jsr, "_lookup_committed_operation", lambda *_args, **_kwargs: None)
    receipt = _apply(pair, operation_id="op_jsr_ambiguous_commit")
    assert receipt["error_code"] != "apply_rolled_back"
    assert receipt["error_code"] == "commit_outcome_unknown"
    assert receipt["ok"] is False
    assert count_rows(pair.target_path, "journal_entries") == 20
    assert count_rows(pair.target_path, "journal_digest_runs") == 2
    assert count_rows(pair.target_path, "operator_operations") == 1


def test_truth_writer_context_exit_after_commit_is_never_apply_rolled_back(
    tmp_path: Path, monkeypatch
) -> None:
    """W133b: lease-context exit after a real writer release cannot claim rollback."""

    pair = build_source_restore_pair(tmp_path)
    from scope_recall import journal_source_restore as jsr

    original = jsr.holding_truth_writer_lease

    @contextlib.contextmanager
    def exit_after_release(*args, **kwargs):
        with original(*args, **kwargs) as held:
            yield held
        raise RuntimeError("injected_post_commit_truth_writer_context_exit_failure")

    monkeypatch.setattr(jsr, "holding_truth_writer_lease", exit_after_release)
    receipt = _apply(pair, operation_id="op_jsr_postcommit_exit")
    assert receipt["error_code"] != "apply_rolled_back"
    assert receipt["error_code"] == "committed_cleanup_failed"
    assert receipt["ok"] is False
    assert receipt["stage"] == "apply"
    assert receipt["status"] == "manual_recovery_required"
    assert receipt["verdict"] == "applied_cleanup_failed"
    assert receipt["operation_id"] == "op_jsr_postcommit_exit"
    assert receipt["journal_inserted_count"] == 19
    assert receipt["digest_run_inserted_count"] == 2
    assert receipt["mapping_count"] == 19
    assert receipt["mapping_digest"]
    assert receipt["request_fingerprint"]
    assert receipt["receipt_state"] == "mirrored"
    assert count_rows(pair.target_path, "journal_entries") == 20
    assert count_rows(pair.target_path, "journal_digest_runs") == 2
    assert count_rows(pair.target_path, "operator_operations") == 1


def test_post_commit_writer_close_failure_is_not_clean_success(
    tmp_path: Path, monkeypatch
) -> None:
    """W133c: post-commit writer.close failure is committed cleanup, not success."""

    pair = build_source_restore_pair(tmp_path)
    from scope_recall import journal_source_restore as jsr

    original = jsr._open_target_writer

    def close_failure_open(target, *, lease_token, connection_factory):
        conn = original(target, lease_token=lease_token, connection_factory=connection_factory)
        real_close = conn.close

        def close_then_raise() -> None:
            real_close()
            raise RuntimeError("injected_post_commit_writer_close_failure")

        conn.close = close_then_raise  # type: ignore[method-assign]
        return conn

    monkeypatch.setattr(jsr, "_open_target_writer", close_failure_open)
    receipt = _apply(pair, operation_id="op_jsr_postcommit_close")
    assert receipt["ok"] is False
    assert receipt["error_code"] == "committed_cleanup_failed"
    assert receipt["error_code"] != "apply_rolled_back"
    assert receipt["status"] == "manual_recovery_required"
    assert receipt["verdict"] == "applied_cleanup_failed"
    assert receipt["verdict"] != "apply_rolled_back"
    assert receipt["stage"] == "apply"
    assert receipt["operation_id"] == "op_jsr_postcommit_close"
    assert receipt["journal_inserted_count"] == 19
    assert receipt["digest_run_inserted_count"] == 2
    assert receipt["mapping_count"] == 19
    assert receipt["mapping_digest"]
    assert receipt["request_fingerprint"]
    assert receipt["receipt_state"] == "mirrored"
    assert count_rows(pair.target_path, "journal_entries") == 20
    assert count_rows(pair.target_path, "journal_digest_runs") == 2
    assert count_rows(pair.target_path, "operator_operations") == 1


def test_same_operation_receipt_repair_uses_authorized_writer(
    tmp_path: Path, monkeypatch
) -> None:
    """W133 B3: same-operation receipt repair must not use ungated RW."""

    pair = build_source_restore_pair(tmp_path)
    first = _apply(pair, operation_id="op_jsr_repair_auth")
    assert first["ok"] is True
    backup_mtime = pair.backup_path.stat().st_mtime_ns
    journals = count_rows(pair.target_path, "journal_entries")
    from scope_recall import journal_source_restore as jsr

    original_connect = jsr.connect_truth_database
    original_open = jsr._open_target_writer
    calls = {"direct_rw": 0, "authorized_open": 0}

    def tracking_connect(path, *, mode="rw", **kwargs):
        if mode == "rw":
            calls["direct_rw"] += 1
        return original_connect(path, mode=mode, **kwargs)

    def tracking_open(*args, **kwargs):
        calls["authorized_open"] += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr(jsr, "connect_truth_database", tracking_connect)
    monkeypatch.setattr(jsr, "_open_target_writer", tracking_open)
    kwargs = apply_kwargs(pair)
    kwargs["operation_id"] = "op_jsr_repair_auth"
    lease = acquire_activation_lease(pair.target_path)
    try:
        second = _run(**kwargs)
    finally:
        release_activation_lease(lease)
    assert second["ok"] is True
    assert second["verdict"] == "committed_reconciled"
    assert calls["authorized_open"] >= 1
    assert calls["direct_rw"] == 0
    assert count_rows(pair.target_path, "journal_entries") == journals
    assert pair.backup_path.stat().st_mtime_ns == backup_mtime


def test_writer_setup_failure_closes_connection_and_releases_authority(
    tmp_path: Path, monkeypatch
) -> None:
    pair = build_source_restore_pair(tmp_path)
    from scope_recall import journal_source_restore as jsr
    from scope_recall import maintenance_lease

    captured: dict[str, sqlite3.Connection] = {}
    from scope_recall import truth_connection as truth_connection_module

    original_connect = truth_connection_module.connect_truth_database

    def capturing_connect(path, *args, **kwargs):
        conn = original_connect(path, *args, **kwargs)
        captured["conn"] = conn
        return conn

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected_authorizer_failure")

    monkeypatch.setattr(truth_connection_module, "connect_truth_database", capturing_connect)
    monkeypatch.setattr(maintenance_lease, "install_activation_lease_authorizer", boom)
    monkeypatch.setattr(jsr, "install_activation_lease_authorizer", boom)
    before = count_rows(pair.target_path, "journal_entries")
    receipt = _apply(pair, operation_id="op_jsr_authorizer")
    assert receipt["ok"] is False
    assert receipt["error_code"] != ""
    assert count_rows(pair.target_path, "journal_entries") == before
    conn = captured.get("conn")
    assert conn is not None
    try:
        conn.execute("SELECT 1")
        usable = True
    except Exception:
        usable = False
    assert usable is False
    peer = TruthWriterLease(pair.target_path.parent, role="provider")
    acquired = peer.acquire()
    assert acquired["status"] == "acquired"
    peer.release()


def test_backup_equals_fenced_prestate_and_can_restore(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    extra = open_fixture_connection(pair.target_path)
    try:
        extra.execute(
            """
            INSERT INTO journal_entries(
                scope_id, shared_scope_id, session_id, turn_number, role, content,
                content_hash, created_at, metadata
            ) VALUES (
                'scope-prewrite', 'shared-prewrite', 'prewrite', 1, 'user',
                'committed-before-begin', ?, '2026-01-10T00:00:00+00:00', '{}'
            )
            """,
            (__import__("journal_source_restore_support", fromlist=["journal_content_hash"]).journal_content_hash(
                "committed-before-begin"
            ),),
        )
        extra.commit()
    finally:
        extra.close()
    pair_ready = apply_kwargs(pair)
    pair_ready["expected_target_epoch_digest"] = compute_target_epoch(pair.target_path)[
        "epoch_digest"
    ]
    pair_ready["operation_id"] = "op_jsr_backup"
    prestate = compute_target_epoch(pair.target_path)
    before_nontarget = nontarget_snapshot(pair.target_path)
    lease = acquire_activation_lease(pair.target_path)
    try:
        receipt = _run(**pair_ready)
    finally:
        release_activation_lease(lease)
    assert receipt["ok"] is True
    backup_epoch = compute_target_epoch(pair.backup_path)
    assert backup_epoch["tables"]["journal_entries"]["count"] == prestate["tables"]["journal_entries"]["count"]
    assert backup_epoch["tables"]["operator_operations"]["count"] == 0
    assert backup_epoch["sqlite_sequence"] == prestate["sqlite_sequence"]
    assert nontarget_snapshot(pair.backup_path) == before_nontarget
    restored = tmp_path / "restored-from-backup.sqlite3"
    restored.write_bytes(pair.backup_path.read_bytes())
    assert count_rows(restored, "journal_entries") == prestate["tables"]["journal_entries"]["count"]
    assert count_rows(pair.target_path, "journal_entries") == prestate["tables"]["journal_entries"]["count"] + 19


def test_post_invariant_failure_is_not_clean_success(tmp_path: Path, monkeypatch) -> None:
    pair = build_source_restore_pair(tmp_path)
    from scope_recall import journal_source_restore as jsr

    original = jsr.insert_missing_rows

    def insert_then_corrupt(conn, **kwargs):
        result = original(conn, **kwargs)
        conn.execute("INSERT INTO memories_fts(memory_id, content, summary) VALUES ('x','y','z')")
        return result

    monkeypatch.setattr(jsr, "insert_missing_rows", insert_then_corrupt)
    receipt = _apply(pair, operation_id="op_jsr_invariant")
    assert receipt["ok"] is False
    assert receipt["error_code"] != ""
    assert receipt["verdict"] != "applied"
    assert count_rows(pair.target_path, "journal_digest_runs") == 0


def test_cli_release_failure_after_refusal_reports_manual_recovery(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    pair = build_source_restore_pair(tmp_path)
    pair.backup_path.parent.mkdir(parents=True, exist_ok=True)
    pair.backup_path.write_bytes(b"preexisting-backup")
    spec = __import__("importlib.util", fromlist=["spec_from_file_location"]).spec_from_file_location(
        "scope_recall_journal_source_restore_cli_refusal_cleanup",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = __import__("importlib.util", fromlist=["module_from_spec"]).module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "release_activation_lease", lambda _lease: False)
    code = module.main(cli_argv(apply_kwargs(pair), apply=True))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code != 0
    assert payload["error_code"] == "activation_lease_cleanup_failed"
    assert payload["status"] == "manual_recovery_required"
    assert payload.get("secondary_error_code") == "prewrite_backup_failed"
    assert payload["ok"] is False
    assert activation_lease_path(pair.target_path).exists()


def test_process_death_after_commit_recovers_same_operation_id(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = apply_kwargs(pair)
    kwargs["operation_id"] = "op_jsr_death"
    payload_path = tmp_path / "death-apply.json"
    serializable = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in kwargs.items()
    }
    payload_path.write_text(json.dumps(serializable), encoding="utf-8")
    child = tmp_path / "death_after_commit.py"
    child.write_text(
        "\n".join(
            [
                "import json, os, sys",
                "from pathlib import Path",
                "sys.path.insert(0, sys.argv[1])",
                "from scope_recall import journal_source_restore as jsr",
                "from scope_recall.maintenance_lease import acquire_activation_lease, release_activation_lease",
                "def die(*_args, **_kwargs):",
                "    os._exit(17)",
                "jsr._mirror_committed_receipt = die",
                "kwargs = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))",
                "for key in ('source_path', 'target_path', 'prewrite_backup_path'):",
                "    kwargs[key] = Path(kwargs[key])",
                "lease = acquire_activation_lease(kwargs['target_path'])",
                "try:",
                "    jsr.run_journal_source_restore(**kwargs)",
                "finally:",
                "    release_activation_lease(lease)",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, str(child), str(ROOT), str(payload_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    assert completed.returncode == 17
    assert count_rows(pair.target_path, "journal_entries") == 20
    leftover = activation_lease_path(pair.target_path)
    if leftover.exists():
        leftover.unlink()
    retry = _apply(pair, operation_id="op_jsr_death")
    assert retry["ok"] is True
    assert retry["verdict"] == "committed_reconciled"
    assert retry["journal_inserted_count"] == 19
    assert count_rows(pair.target_path, "journal_entries") == 20
    assert not activation_lease_path(pair.target_path).exists()


def test_stdout_failure_after_commit_recovers_same_operation_id(tmp_path: Path) -> None:
    pair = build_source_restore_pair(tmp_path)
    kwargs = apply_kwargs(pair)
    kwargs["operation_id"] = "op_jsr_stdout"
    argv = cli_argv(kwargs, apply=True)
    child = tmp_path / "stdout_after_commit.py"
    child.write_text(
        "\n".join(
            [
                "import importlib.util, os, sys",
                "from pathlib import Path",
                "sys.path.insert(0, sys.argv[1])",
                "script = Path(sys.argv[1]) / 'scripts' / 'journal.source_restore.py'",
                "spec = importlib.util.spec_from_file_location('jsr_cli_stdout', script)",
                "module = importlib.util.module_from_spec(spec)",
                "spec.loader.exec_module(module)",
                "def boom(_payload):",
                "    raise BrokenPipeError('stdout_closed_after_commit')",
                "module._print = boom",
                "raise SystemExit(module.main(sys.argv[2:]))",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, str(child), str(ROOT), *argv],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    assert completed.returncode != 0
    assert count_rows(pair.target_path, "journal_entries") == 20
    leftover = activation_lease_path(pair.target_path)
    if leftover.exists():
        leftover.unlink()
    retry = _apply(pair, operation_id="op_jsr_stdout")
    assert retry["ok"] is True
    assert retry["verdict"] == "committed_reconciled"
    assert retry["journal_inserted_count"] == 19
    assert count_rows(pair.target_path, "journal_entries") == 20
