from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scope_recall.activation_transaction import (
    capture_activation_state,
    committed_activation_receipt,
)
from scope_recall.maintenance_lease import (
    activation_lease_path,
    install_activation_lease_authorizer,
)


def _write_lease(db_path: Path, token: str = "lease-token") -> Path:
    path = activation_lease_path(db_path)
    path.write_text(
        json.dumps({"kind": "scope-recall-activation-maintenance", "token": token}),
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
