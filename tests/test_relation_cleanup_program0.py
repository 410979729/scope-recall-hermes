"""Program 0 operator-cleanup fencing, replay, and recovery contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import scope_recall.relation_cleanup as cleanup
from scope_recall.relation_containment import enqueue_relation_focus_work
from scope_recall.relation_frequency_maintenance import (
    drain_relation_frequency_work,
)
from scope_recall.sql_store import ensure_schema


_NOW = "2026-08-26T00:00:00+00:00"
_REASON = "retire exact legacy relation work fixture"


def _truth_fixture(tmp_path: Path) -> Path:
    storage = tmp_path / "storage"
    storage.mkdir()
    path = storage / "memory.sqlite3"
    conn = sqlite3.connect(path)
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES('focus', 'scope-a', 'fixture', 'project',
                 'bounded cleanup fixture', 'fixture', ?, ?, '{}')
        """,
        (_NOW, _NOW),
    )
    conn.execute(
        """
        INSERT INTO relation_rebuild_queue(
            scope_id, focus_memory_id, requested_updated_at, reason,
            status, available_at, created_at, updated_at
        ) VALUES('scope-a', 'focus', ?, 'legacy fixture',
                 'pending', ?, ?, ?)
        """,
        (_NOW, _NOW, _NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return path


def _plan(path: Path) -> dict[str, object]:
    return cleanup.run_legacy_relation_cleanup(
        path,
        scope_ids=["scope-a"],
    )


def _apply(
    path: Path,
    plan: dict[str, object],
    *,
    operation_id: str = "relation-cleanup-fixture",
) -> dict[str, object]:
    return cleanup.run_legacy_relation_cleanup(
        path,
        apply=True,
        maintenance_confirmed=True,
        scope_ids=["scope-a"],
        expected_plan_sha256=str(plan["plan_sha256"]),
        operation_id=operation_id,
        reason=_REASON,
    )


def test_cleanup_apply_is_backup_first_committed_and_idempotently_replayed(
    tmp_path: Path,
) -> None:
    path = _truth_fixture(tmp_path)
    plan = _plan(path)
    assert plan["status"] == "planned"
    assert plan["rebuild_queue_count"] == 1

    applied = _apply(path, plan)

    assert applied["status"] == "committed"
    assert applied["receipt_state"] == "mirrored"
    assert applied["replayed"] is False
    assert applied["maintenance_lease_released"] is True
    assert applied["activation_guards_removed"] is True
    backup = path.parent / Path(str(applied["backup_path"]))
    assert backup.is_file()
    assert backup.name.startswith("relation-cleanup.")

    replayed = _apply(path, plan)
    assert replayed["status"] == "committed"
    assert replayed["receipt_state"] == "mirrored"
    assert replayed["replayed"] is True
    assert replayed["backup_path"] == applied["backup_path"]
    assert list(backup.parent.glob("*.sqlite3")) == [backup]

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM relation_rebuild_queue").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM operator_operations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM relation_work_dispositions").fetchone()[0] == 1
    finally:
        conn.close()


def test_cleanup_does_not_restore_ready_while_focus_work_remains(
    tmp_path: Path,
) -> None:
    path = _truth_fixture(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        drained = drain_relation_frequency_work(
            conn,
            change_limit=100,
            focus_limit=100,
            backfill_limit=100,
            relation_candidate_cap=100,
            wall_clock_seconds=5.0,
            commit=True,
        )
        assert int(drained["focused_memories"]) == 1
        assert enqueue_relation_focus_work(
            conn,
            memory_id="focus",
            work_generation=2,
            work_revision="cleanup-focus-debt",
            scope_ids=["scope-a"],
            max_attempts=5,
        ) is True
        conn.execute(
            """
            UPDATE relation_scope_containment
            SET state='blocked', reason_code='operator_cleanup_required'
            WHERE scope_id='scope-a'
            """
        )
        conn.commit()
    finally:
        conn.close()

    plan = _plan(path)
    applied = _apply(path, plan, operation_id="focus-debt-fixture")

    assert applied["status"] == "committed"
    assert applied["restored_scope_count"] == 0
    verifier = sqlite3.connect(path)
    try:
        state = verifier.execute(
            """
            SELECT state, reason_code
            FROM relation_scope_containment WHERE scope_id='scope-a'
            """
        ).fetchone()
        assert tuple(state) == ("blocked", "operator_cleanup_required")
        assert verifier.execute(
            """
            SELECT status FROM relation_focus_work
            WHERE memory_id='focus'
            """
        ).fetchone()[0] == "pending"
    finally:
        verifier.close()


def test_cleanup_replay_retries_receipt_and_reports_authoritative_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _truth_fixture(tmp_path)
    plan = _plan(path)
    real_mirror = cleanup.mirror_operator_receipt

    def fail_mirror(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise OSError("injected receipt mirror failure")

    monkeypatch.setattr(cleanup, "mirror_operator_receipt", fail_mirror)
    applied = _apply(path, plan, operation_id="receipt-retry-fixture")
    assert applied["status"] == "committed_receipt_debt"
    assert applied["receipt_state"] == "pending"
    assert applied["receipt_error"] == "OSError"

    monkeypatch.setattr(cleanup, "mirror_operator_receipt", real_mirror)
    replayed = _apply(path, plan, operation_id="receipt-retry-fixture")
    assert replayed["status"] == "committed"
    assert replayed["receipt_state"] == "mirrored"
    assert replayed["replayed"] is True


def test_committed_guard_cleanup_failure_is_returned_as_fail_closed_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _truth_fixture(tmp_path)
    plan = _plan(path)

    def fail_guard_cleanup(_conn: sqlite3.Connection) -> list[str]:
        raise sqlite3.OperationalError("injected guard cleanup failure")

    monkeypatch.setattr(cleanup, "remove_activation_guard_triggers", fail_guard_cleanup)
    result = _apply(path, plan, operation_id="guard-debt-fixture")

    assert result["status"] == "committed_cleanup_debt"
    assert result["receipt_state"] == "mirrored"
    assert result["maintenance_lease_released"] is False
    assert result["activation_guards_removed"] is False
    assert "activation_guards_and_lease_retained" in result["cleanup_debt"]

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM relation_rebuild_queue").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM operator_operations").fetchone()[0] == 1
    finally:
        conn.close()


def test_cleanup_revalidates_truth_identity_after_acquiring_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _truth_fixture(tmp_path)
    plan = _plan(path)
    real_revalidate = cleanup._revalidate_database_target

    def reject_changed_identity(
        candidate: Path,
        *,
        expected_identity: tuple[int, int],
    ) -> Path:
        real_revalidate(candidate, expected_identity=expected_identity)
        raise RuntimeError("injected identity replacement")

    monkeypatch.setattr(
        cleanup,
        "_revalidate_database_target",
        reject_changed_identity,
    )
    with pytest.raises(RuntimeError, match="failed before commit"):
        _apply(path, plan, operation_id="identity-drift-fixture")

    assert not (path.parent / ".activation-maintenance.json").exists()
    assert not (path.parent / "backups").exists()


def test_cleanup_refuses_windows_junction_backup_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _truth_fixture(tmp_path)
    plan = _plan(path)
    real_isjunction = getattr(cleanup.os.path, "isjunction", lambda _value: False)

    def report_backup_junction(value: object) -> bool:
        candidate = Path(value)
        return candidate.name == "backups" or bool(real_isjunction(value))

    monkeypatch.setattr(cleanup.os.path, "isjunction", report_backup_junction)
    with pytest.raises(RuntimeError, match="failed before commit"):
        _apply(path, plan, operation_id="junction-fixture")

    assert not (path.parent / ".activation-maintenance.json").exists()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM relation_rebuild_queue").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM operator_operations").fetchone()[0] == 0
    finally:
        conn.close()
