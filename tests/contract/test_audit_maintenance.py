"""New synthetic backup/rollback roots, including actual Windows junctions."""
import os
from pathlib import Path
import sqlite3

import pytest

from maintenance.backup import BackupError, backup_sqlite
from maintenance.rollback import RollbackError, rollback_to_verified_snapshot
from maintenance.migrate_v2 import MigrationError, build_legacy_catalog, migrate_legacy


def database(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE fixture(value TEXT)")
        conn.execute("INSERT INTO fixture VALUES ('TEST isolated source')")
    return path


@pytest.fixture
def junction(tmp_path):
    if os.name != "nt":
        pytest.skip("actual Windows junction")
    import _winapi
    outside = tmp_path / "TEST-outside"
    outside.mkdir()
    link = tmp_path / "TEST-alias"
    _winapi.CreateJunction(str(outside), str(link))
    try:
        yield link, outside
    finally:
        link.rmdir()


def test_backup_and_rollback_reject_junction_source_and_destination(tmp_path, junction):
    alias, outside = junction
    source = database(tmp_path / "TEST-source.sqlite3")
    remote = database(outside / "TEST-remote.sqlite3")
    originals = {path: path.read_bytes() for path in (source, remote)}
    with pytest.raises(BackupError, match="reparse"):
        backup_sqlite(alias / remote.name, tmp_path / "TEST-rejected.sqlite3")
    with pytest.raises(BackupError, match="reparse"):
        backup_sqlite(source, alias / "TEST-rejected.sqlite3")
    with pytest.raises(RollbackError, match="reparse"):
        rollback_to_verified_snapshot(source, alias / remote.name)
    with pytest.raises(RollbackError, match="reparse"):
        rollback_to_verified_snapshot(source, remote, destination=alias / "TEST-rollback.sqlite3")
    with pytest.raises(MigrationError, match="reparse"):
        build_legacy_catalog(alias / remote.name)
    with pytest.raises(MigrationError, match="reparse"):
        migrate_legacy(source, alias / "TEST-migration")
    assert {path: path.read_bytes() for path in originals} == originals
    assert sorted(path.name for path in outside.iterdir()) == [remote.name]


def test_backup_refuses_manifest_junction_before_creating_output(tmp_path, junction):
    alias, _ = junction
    source = database(tmp_path / "TEST-source.sqlite3")
    output = tmp_path / "TEST-backup.sqlite3"
    with pytest.raises(BackupError, match="reparse"):
        backup_sqlite(source, output, manifest=alias / "TEST-manifest.json")
    assert not output.exists()


def test_backup_output_creation_race_never_overwrites_an_existing_file(tmp_path, monkeypatch):
    import maintenance.backup as backup
    source = database(tmp_path / "TEST-source.sqlite3")
    output = tmp_path / "TEST-raced.sqlite3"
    actual_open = backup.os.open

    def raced_open(path, flags, mode=0o777, **kwargs):
        if Path(path) == output:
            output.write_bytes(b"TEST preserve concurrently created file")
        return actual_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(backup.os, "open", raced_open)
    with pytest.raises(BackupError, match="overwrite"):
        backup_sqlite(source, output)
    assert output.read_bytes() == b"TEST preserve concurrently created file"


def test_uri_reserved_filename_roundtrip_and_originals_remain_unchanged(tmp_path):
    source = database(tmp_path / "TEST-source#1.sqlite3")
    before = source.read_bytes()
    snapshot = tmp_path / "TEST-snapshot#2.sqlite3"
    receipt = backup_sqlite(source, snapshot, manifest=tmp_path / "TEST-backup.json")
    assert receipt["quick_check"] == "ok"
    snapshot_before = snapshot.read_bytes()
    rollback = rollback_to_verified_snapshot(source, snapshot, destination=tmp_path / "TEST-copy.sqlite3")
    assert rollback["restored"] is True
    assert source.read_bytes() == before and snapshot.read_bytes() == snapshot_before
    with sqlite3.connect(rollback["rollback_output"]) as conn:
        assert conn.execute("SELECT value FROM fixture").fetchone()[0] == "TEST isolated source"


def test_immutable_migration_refuses_uncheckpointed_wal_without_touching_source(tmp_path):
    source = tmp_path / "TEST-live.sqlite3"
    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE fixture(value TEXT)")
        conn.execute("INSERT INTO fixture VALUES ('TEST committed only in WAL')")
        conn.commit()
        wal = source.with_name(source.name + "-wal")
        before = source.read_bytes(), wal.read_bytes()
        with pytest.raises(MigrationError, match="nonempty WAL"):
            build_legacy_catalog(source)
        target = tmp_path / "TEST-migration-target"
        with pytest.raises(MigrationError, match="nonempty WAL"):
            migrate_legacy(source, target)
        assert not target.exists()
        assert (source.read_bytes(), wal.read_bytes()) == before
        # The authorized backup API includes WAL rows without checkpointing or
        # editing the live source and yields an offline snapshot for migration.
        output = tmp_path / "TEST-offline.sqlite3"
        backup_sqlite(source, output)
        with sqlite3.connect(output) as saved:
            assert saved.execute("SELECT value FROM fixture").fetchone()[0] == "TEST committed only in WAL"
    finally:
        conn.close()
