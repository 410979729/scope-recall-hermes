"""Truth-store path and permission hardening contracts."""

from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

import scope_recall.doctor_sqlite as doctor_sqlite
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.truth_connection import (
    TruthDatabaseConnectionError,
    connect_truth_database,
    truth_storage_permissions,
)


def test_truth_connection_preserves_sqlite_memory_database_semantics():
    conn = connect_truth_database(":memory:", mode="rwc")
    try:
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()
        conn.execute("CREATE TABLE marker(value TEXT)")
        conn.execute("INSERT INTO marker(value) VALUES ('ready')")
        stored = conn.execute("SELECT value FROM marker").fetchone()
    finally:
        conn.close()

    assert foreign_keys is not None and int(foreign_keys[0]) == 1
    assert stored is not None and stored[0] == "ready"


def test_truth_connection_handles_sqlite_uri_metacharacters(tmp_path):
    metacharacters = "# percent" if os.name == "nt" else "? # percent"
    db_path = tmp_path / f"memory {metacharacters}.sqlite3"

    conn = connect_truth_database(db_path, mode="rwc")
    try:
        conn.execute("CREATE TABLE marker(value TEXT)")
        conn.commit()
    finally:
        conn.close()

    assert db_path.is_file()


def test_mutable_truth_connection_does_not_resolve_paths_before_no_follow(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "memory.sqlite3"

    def forbid_resolve(self, *args, **kwargs):
        raise AssertionError("mutable truth path must not resolve symlinks")

    monkeypatch.setattr(Path, "resolve", forbid_resolve)
    conn = connect_truth_database(db_path, mode="rwc")
    conn.close()

    assert db_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX O_NOFOLLOW contract")
def test_mutable_truth_connection_rejects_database_symlink(tmp_path):
    target = tmp_path / "target.sqlite3"
    sqlite_conn = connect_truth_database(target, mode="rwc")
    sqlite_conn.close()
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(target)

    report = truth_storage_permissions(link)
    assert report["status"] == "unsafe"
    assert report["symlink"] is True
    with pytest.raises(TruthDatabaseConnectionError, match="symlink"):
        connect_truth_database(link, mode="rw")


@pytest.mark.skipif(os.name == "nt", reason="Windows uses inherited ACLs, not POSIX mode bits")
def test_mutable_truth_connection_hardens_directory_and_database_modes(tmp_path):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(mode=0o777)
    os.chmod(storage_dir, 0o777)
    db_path = storage_dir / "memory.sqlite3"

    conn = connect_truth_database(db_path, mode="rwc")
    conn.close()

    assert stat.S_IMODE(storage_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_sqlite_doctor_fails_closed_on_unsafe_posix_permissions(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()
    db_path = storage_dir / "memory.sqlite3"
    conn = connect_truth_database(db_path, mode="rwc")
    try:
        ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        doctor_sqlite,
        "truth_storage_permissions",
        lambda _path: {
            "status": "unsafe",
            "ok": False,
            "platform_policy": "posix-owner-only",
            "directory_mode": "0755",
            "database_mode": "0644",
        },
        raising=False,
    )

    payload, check, recommendations = doctor_sqlite.sqlite_report(tmp_path)

    assert payload["storage_permissions"]["status"] == "unsafe"
    assert check["ok"] is False
    assert any("permissions" in failure.lower() for failure in check["failures"])
    assert any("0600" in item and "0700" in item for item in recommendations)


def test_sqlite_doctor_fails_closed_on_incomplete_factual_freshness_coverage(
    tmp_path,
):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()
    db_path = storage_dir / "memory.sqlite3"
    conn = connect_truth_database(db_path, mode="rwc")
    conn.row_factory = doctor_sqlite.sqlite3.Row
    try:
        ensure_schema(conn)
        store_row(
            conn,
            memory_id="doctor-untracked-fact",
            scope_id="shared-scope",
            platform="telegram",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="doctor-freshness",
            source="tool-store",
            target="ops",
            content="Doctor untracked factual freshness sentinel.",
            metadata='{"memory_type":"factual","lifecycle":"promoted"}',
        )
        conn.execute(
            "DELETE FROM fact_freshness WHERE subject_id = 'doctor-untracked-fact'"
        )
        conn.commit()
    finally:
        conn.close()

    payload, check, recommendations = doctor_sqlite.sqlite_report(tmp_path)

    assert payload["fact_freshness"]["coverage"] == {
        "factual_memories": 1,
        "tracked_memory_facts": 0,
        "coverage_percent": 0.0,
    }
    assert check["ok"] is False
    assert any(
        "freshness coverage" in failure.lower() for failure in check["failures"]
    )
    assert any("backfill" in item.lower() for item in recommendations)
