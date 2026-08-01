from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import scope_recall.doctor_sqlite as doctor_sqlite
from scope_recall.activation_transaction import (
    capture_activation_state,
    committed_activation_receipt,
)
from scope_recall.maintenance_lease import (
    activation_lease_path,
    activation_lease_status,
    ensure_activation_guard_triggers,
    install_activation_lease_authorizer,
    recover_stale_activation_lease,
)
from scope_recall.sql_store import ensure_schema


def _write_lease(
    db_path: Path,
    token: str = "lease-token",
    *,
    pid: int | None = None,
) -> Path:
    path = activation_lease_path(db_path)
    payload: dict[str, object] = {
        "kind": "scope-recall-activation-maintenance",
        "token": token,
    }
    if pid is not None:
        payload["pid"] = pid
        payload["created_at"] = "2026-01-01T00:00:00+00:00"
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return path


def test_existing_writer_connection_is_blocked_when_lease_appears(tmp_path):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()
    db_path = storage_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    install_activation_lease_authorizer(conn, db_path)
    conn.execute("CREATE TABLE events(value TEXT NOT NULL)")
    conn.execute("INSERT INTO events(value) VALUES (?)", ("before",))
    conn.commit()

    snapshot = capture_activation_state(tmp_path, writer_quiesced=True)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        conn.execute("INSERT INTO events(value) VALUES (?)", ("blocked",))
    conn.rollback()

    assert conn.execute("SELECT value FROM events ORDER BY rowid").fetchall() == [
        ("before",)
    ]
    conn.close()
    receipt = committed_activation_receipt(
        snapshot,
        plugin_dir=tmp_path / "plugins" / "scope-recall",
        previous_plugin_existed=False,
        plugin_backup_path="",
        plugin_replaced=False,
    )
    assert receipt["status"] == "committed"
    assert receipt["maintenance_lease"]["released"] is True
    assert not activation_lease_path(db_path).exists()


def test_activation_guard_blocks_raw_writer_until_commit_cleanup(
    tmp_path,
):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()
    db_path = storage_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE events(value TEXT NOT NULL)")
    conn.execute("INSERT INTO events(value) VALUES ('before')")
    conn.commit()
    conn.close()

    snapshot = capture_activation_state(tmp_path, writer_quiesced=True)
    raw = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.OperationalError, match="no such function"):
        raw.execute("INSERT INTO events(value) VALUES ('external')")
    raw.rollback()
    raw.close()

    receipt = committed_activation_receipt(
        snapshot,
        plugin_dir=tmp_path / "plugins" / "scope-recall",
        previous_plugin_existed=False,
        plugin_backup_path="",
        plugin_replaced=False,
    )
    assert receipt["status"] == "committed"
    assert receipt["sqlite"]["guards_removed"] is True
    assert receipt["maintenance_lease"]["released"] is True

    raw = sqlite3.connect(db_path)
    raw.execute("INSERT INTO events(value) VALUES ('after')")
    raw.commit()
    assert raw.execute("SELECT value FROM events ORDER BY rowid").fetchall() == [
        ("before",),
        ("after",),
    ]
    raw.close()


def test_matching_activation_capability_can_write_and_nonmatching_cannot(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(db_path) as seed:
        seed.execute("CREATE TABLE events(value TEXT NOT NULL)")
    _write_lease(db_path, token="expected-token")

    denied = sqlite3.connect(db_path)
    install_activation_lease_authorizer(
        denied,
        db_path,
        lease_token="wrong-token",
    )
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        denied.execute("INSERT INTO events(value) VALUES ('denied')")
    denied.close()

    allowed = sqlite3.connect(db_path)
    install_activation_lease_authorizer(
        allowed,
        db_path,
        lease_token="expected-token",
    )
    allowed.execute("INSERT INTO events(value) VALUES ('activation')")
    allowed.commit()
    allowed.close()

    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT value FROM events").fetchall() == [
            ("activation",)
        ]


def test_stale_activation_lease_recovery_is_dry_run_first_and_removes_guards(
    tmp_path,
):
    db_path = tmp_path / "memory.sqlite3"
    owner = sqlite3.connect(db_path)
    owner.row_factory = sqlite3.Row
    ensure_schema(owner)
    owner.execute("CREATE TABLE events(value TEXT NOT NULL)")
    owner.commit()
    _write_lease(db_path, token="stale-token", pid=2_147_483_647)
    install_activation_lease_authorizer(
        owner,
        db_path,
        lease_token="stale-token",
    )
    triggers = ensure_activation_guard_triggers(
        owner,
        db_path,
        lease_token="stale-token",
    )
    owner.commit()
    owner.close()
    assert triggers

    status = activation_lease_status(db_path)
    dry_run = recover_stale_activation_lease(db_path, apply=False)
    assert status["status"] == "stale"
    assert status["owner_liveness"] == "dead"
    assert dry_run["apply"] is False
    assert dry_run["recoverable"] is True
    assert activation_lease_path(db_path).exists()

    applied = recover_stale_activation_lease(
        db_path,
        apply=True,
        operation_id="stale-lease-core-test",
        reason="recorded lease owner is dead",
        backup_path=str(tmp_path / "backup.sqlite3"),
    )

    assert applied["recovered"] is True
    assert applied["guards_removed"] == len(triggers)
    assert not activation_lease_path(db_path).exists()
    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'scope_recall_activation_guard_%'"
        ).fetchone()[0] == 0
        raw.execute("INSERT INTO events(value) VALUES ('after-recovery')")
        raw.commit()
    finally:
        raw.close()


def test_live_activation_lease_is_never_auto_recovered(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE events(value TEXT NOT NULL)")
    _write_lease(db_path, token="live-token", pid=os.getpid())

    status = activation_lease_status(db_path)
    with pytest.raises(RuntimeError, match="not stale"):
        recover_stale_activation_lease(db_path, apply=True)

    assert status["status"] == "active"
    assert status["owner_liveness"] == "alive"
    assert activation_lease_path(db_path).exists()


def test_normal_guard_setup_cleans_orphan_triggers_when_lease_is_absent(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    owner = sqlite3.connect(db_path)
    owner.execute("CREATE TABLE events(value TEXT NOT NULL)")
    owner.commit()
    lease_path = _write_lease(db_path, token="orphan-token", pid=os.getpid())
    install_activation_lease_authorizer(
        owner,
        db_path,
        lease_token="orphan-token",
    )
    created = ensure_activation_guard_triggers(
        owner,
        db_path,
        lease_token="orphan-token",
    )
    owner.commit()
    owner.close()
    lease_path.unlink()

    ordinary = sqlite3.connect(db_path)
    try:
        removed_setup = ensure_activation_guard_triggers(ordinary, db_path)
        ordinary.commit()
        remaining = ordinary.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'scope_recall_activation_guard_%'"
        ).fetchone()[0]
    finally:
        ordinary.close()

    assert created
    assert removed_setup == []
    assert remaining == 0


def test_sqlite_doctor_reports_stale_activation_lease_without_token(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.commit()
    conn.close()
    _write_lease(
        db_path,
        token="doctor-secret-capability",
        pid=2_147_483_647,
    )

    payload, check, recommendations = doctor_sqlite.sqlite_report(tmp_path)

    lease = payload["activation_lease"]
    assert lease["status"] == "stale"
    assert "token" not in lease
    assert "doctor-secret-capability" not in json.dumps(payload)
    assert check["ok"] is False
    assert any("stale activation" in item.lower() for item in check["failures"])
    assert any("recover.activation_lease.py --dry-run" in item for item in recommendations)


def test_activation_lease_recovery_script_creates_backup_ledger_and_receipt(tmp_path):
    home = tmp_path / "hermes-home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    owner = sqlite3.connect(db_path)
    owner.row_factory = sqlite3.Row
    ensure_schema(owner)
    _write_lease(db_path, token="script-stale-token", pid=2_147_483_647)
    install_activation_lease_authorizer(
        owner,
        db_path,
        lease_token="script-stale-token",
    )
    ensure_activation_guard_triggers(
        owner,
        db_path,
        lease_token="script-stale-token",
    )
    owner.commit()
    owner.close()

    script = Path(__file__).resolve().parents[1] / "scripts" / "recover.activation_lease.py"
    common = [sys.executable, str(script), "--hermes-home", str(home)]
    dry_run = subprocess.run(common, text=True, capture_output=True, check=False)
    blocked = subprocess.run(
        [
            *common,
            "--apply",
            "--operation-id",
            "lease-recovery-script-001",
            "--reason",
            "activation owner process was verified dead",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    applied = subprocess.run(
        [
            *common,
            "--apply",
            "--maintenance-confirmed",
            "--operation-id",
            "lease-recovery-script-001",
            "--reason",
            "activation owner process was verified dead",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert dry_run.returncode == 0
    assert json.loads(dry_run.stdout)["status"] == "stale"
    assert blocked.returncode == 2
    assert "maintenance-confirmed" in json.loads(blocked.stdout)["error"]
    assert applied.returncode == 0, applied.stdout + applied.stderr
    payload = json.loads(applied.stdout)
    assert payload["recovered"] is True
    assert payload["receipt"]["receipt_state"] == "mirrored"
    assert not activation_lease_path(db_path).exists()
    assert len(list((storage / "backups").glob("activation-lease-recovery.*.sqlite3"))) == 1
    assert len(list((storage / "receipts").glob("playbooks.recover_stale.*.json"))) == 1
    check = sqlite3.connect(db_path)
    try:
        trigger_count = check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'scope_recall_activation_guard_%'"
        ).fetchone()[0]
        receipt_state = check.execute(
            "SELECT receipt_state FROM operator_operations "
            "WHERE operation_id = 'lease-recovery-script-001'"
        ).fetchone()[0]
    finally:
        check.close()
    assert trigger_count == 0
    assert receipt_state == "mirrored"
