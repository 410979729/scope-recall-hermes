"""Narrow #39 protection: verified SQLite online backup/health boundary.

Activation/canary may take a verified backup; ordinary startup must not.
These tests never touch live Hermes homes or production DBs.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from scope_recall import activation_transaction
from scope_recall.maintenance_lease import ACTIVATION_GUARD_TRIGGER_PREFIX
from scope_recall.sqlite_backup import (
    SqliteBackupError,
    inspect_sqlite_health,
    logical_fingerprint,
    verified_online_backup,
)
from scope_recall.truth_connection import connect_truth_database


def _write_truth_db(path: Path, *, marker: str = "alpha") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_truth_database(path, mode="rwc")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("DELETE FROM probe")
        conn.execute("INSERT INTO probe(value) VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


def test_verified_online_backup_healthy_path_records_health_and_logical_equivalence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "memory.sqlite3"
    backup = tmp_path / "backups" / "memory.sqlite3"
    _write_truth_db(source, marker="healthy")

    receipt = verified_online_backup(source, backup)

    assert backup.is_file()
    assert receipt["backup_path"] == str(Path(os.path.abspath(backup)))
    assert receipt["source_path"] == str(Path(os.path.abspath(source)))
    assert receipt["source_health"]["ok"] is True
    assert str(receipt["source_health"]["quick_check"]).lower() == "ok"
    assert str(receipt["source_health"]["integrity_check"]).lower() == "ok"
    assert receipt["source_health"]["foreign_key_violation_present"] is False
    assert receipt["backup_health"]["ok"] is True
    assert str(receipt["backup_health"]["quick_check"]).lower() == "ok"
    assert str(receipt["backup_health"]["integrity_check"]).lower() == "ok"
    assert receipt["backup_health"]["foreign_key_violation_present"] is False
    assert receipt["logical_equivalent"] is True
    assert receipt["source_logical_fingerprint"]
    assert receipt["backup_logical_fingerprint"]
    assert (
        receipt["source_logical_fingerprint"]
        == receipt["backup_logical_fingerprint"]
        == receipt["logical_fingerprint"]
    )
    assert logical_fingerprint(source) == logical_fingerprint(backup)

    with sqlite3.connect(backup) as conn:
        value = conn.execute("SELECT value FROM probe").fetchone()[0]
    assert value == "healthy"


def test_logical_fingerprint_keeps_normal_trigger_with_reserved_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    changed = tmp_path / "changed.sqlite3"
    _write_truth_db(source)
    _write_truth_db(changed)

    for path, suffix in ((source, "source"), (changed, "changed")):
        connection = connect_truth_database(path, mode="rw")
        try:
            connection.execute(
                "CREATE TRIGGER audit_trigger AFTER DELETE ON probe "
                f"BEGIN SELECT '{ACTIVATION_GUARD_TRIGGER_PREFIX}{suffix}'; END"
            )
            connection.commit()
        finally:
            connection.close()

    assert logical_fingerprint(source) != logical_fingerprint(changed)


def test_logical_fingerprint_ignores_trigger_in_reserved_namespace(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.sqlite3"
    guarded = tmp_path / "guarded.sqlite3"
    _write_truth_db(baseline)
    _write_truth_db(guarded)

    connection = connect_truth_database(guarded, mode="rw")
    try:
        connection.execute(
            f'CREATE TRIGGER "{ACTIVATION_GUARD_TRIGGER_PREFIX}test" '
            "AFTER DELETE ON probe BEGIN SELECT 1; END"
        )
        connection.commit()
    finally:
        connection.close()

    assert logical_fingerprint(baseline) == logical_fingerprint(guarded)


def test_verified_online_backup_rejects_symlink_truth(tmp_path: Path) -> None:
    real = tmp_path / "real.sqlite3"
    link = tmp_path / "linked.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    _write_truth_db(real)
    try:
        link.symlink_to(real)
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(SqliteBackupError, match="symlink"):
        verified_online_backup(link, backup)
    assert not backup.exists()


def test_verified_online_backup_rejects_non_file_truth(tmp_path: Path) -> None:
    source_dir = tmp_path / "not-a-db"
    source_dir.mkdir()
    backup = tmp_path / "backup.sqlite3"

    with pytest.raises(SqliteBackupError, match="not a file"):
        verified_online_backup(source_dir, backup)
    assert not backup.exists()


def test_verified_online_backup_injected_backup_failure_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    _write_truth_db(source)

    def boom(source_conn, destination_conn):  # noqa: ANN001, ARG001
        raise sqlite3.OperationalError("injected backup failure")

    monkeypatch.setattr(sqlite_backup, "_transfer_online_backup", boom)

    with pytest.raises(SqliteBackupError, match="injected backup failure"):
        verified_online_backup(source, backup)
    assert not backup.exists()


def test_verified_online_backup_logical_mismatch_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    _write_truth_db(source, marker="source-row")

    original_transfer = sqlite_backup._transfer_online_backup

    def corrupt_after_backup(source_conn, destination_conn):  # noqa: ANN001
        original_transfer(source_conn, destination_conn)
        destination_conn.execute(
            "INSERT INTO probe(value) VALUES ('corrupted-destination')"
        )
        destination_conn.commit()

    monkeypatch.setattr(sqlite_backup, "_transfer_online_backup", corrupt_after_backup)

    with pytest.raises(SqliteBackupError, match="logical"):
        verified_online_backup(source, backup)
    assert not backup.exists()


def test_inspect_sqlite_health_reports_unhealthy_for_garbage_file(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"not a sqlite database at all")

    health = inspect_sqlite_health(garbage)
    assert health["ok"] is False
    assert health["path"] == str(Path(os.path.abspath(garbage)))
    assert "quick_check" in health
    assert "integrity_check" in health
    assert "foreign_key_violation_present" in health


def test_inspect_sqlite_health_reports_healthy_triple_and_fk_violation(
    tmp_path: Path,
) -> None:
    healthy = tmp_path / "healthy.sqlite3"
    _write_truth_db(healthy, marker="ok-row")
    health = inspect_sqlite_health(healthy)
    assert health["ok"] is True
    assert str(health["quick_check"]).lower() == "ok"
    assert str(health["integrity_check"]).lower() == "ok"
    assert health["foreign_key_violation_present"] is False

    broken = tmp_path / "fk-broken.sqlite3"
    conn = connect_truth_database(broken, mode="rwc")
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL, "
            "FOREIGN KEY(parent_id) REFERENCES parent(id))"
        )
        conn.execute("INSERT INTO child(id, parent_id) VALUES (1, 999)")
        conn.commit()
    finally:
        conn.close()

    fk_health = inspect_sqlite_health(broken)
    assert fk_health["ok"] is False
    assert fk_health["foreign_key_violation_present"] is True
    assert str(fk_health["quick_check"]).lower() == "ok"
    assert str(fk_health["integrity_check"]).lower() == "ok"
    assert "foreign_key" in str(fk_health.get("error") or "").lower()


def test_inspect_sqlite_health_stops_after_first_fk_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health only needs bounded evidence that at least one FK violation exists."""

    import scope_recall.sqlite_backup as sqlite_backup

    database = tmp_path / "bounded-health.sqlite3"
    database.write_bytes(b"connection is replaced by a bounded fake")

    class Cursor:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self._row = row

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

        def fetchall(self) -> list[tuple[object, ...]]:
            raise AssertionError("foreign_key_check must not materialize every violation")

    class Connection:
        def execute(self, statement: str) -> Cursor:
            if statement == "PRAGMA quick_check":
                return Cursor(("ok",))
            if statement == "PRAGMA integrity_check(1)":
                return Cursor(("ok",))
            if statement == "PRAGMA foreign_key_check":
                return Cursor(("child", 1, "parent", 0))
            raise AssertionError(statement)

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        sqlite_backup,
        "connect_truth_database",
        lambda *args, **kwargs: Connection(),
    )

    health = inspect_sqlite_health(database)

    assert health["quick_check"] == "ok"
    assert health["integrity_check"] == "ok"
    assert health["foreign_key_violation_present"] is True
    assert health["ok"] is False


def test_verified_online_backup_rejects_same_absolute_path(tmp_path: Path) -> None:
    source = tmp_path / "memory.sqlite3"
    _write_truth_db(source)
    same = tmp_path / "nested" / ".." / "memory.sqlite3"

    with pytest.raises(SqliteBackupError, match="same absolute path"):
        verified_online_backup(source, same)


def test_verified_online_backup_refuses_preexisting_destination_without_delete(
    tmp_path: Path,
) -> None:
    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source, marker="source")
    destination.write_text("preexisting-destination-asset", encoding="utf-8")
    before = destination.read_bytes()

    with pytest.raises(SqliteBackupError, match="preexisting destination"):
        verified_online_backup(source, destination)

    assert destination.read_bytes() == before


def test_verified_online_backup_refuses_preexisting_sidecar_without_delete(
    tmp_path: Path,
) -> None:
    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    sidecar = Path(f"{destination}-wal")
    _write_truth_db(source, marker="source")
    sidecar.write_text("preexisting-wal", encoding="utf-8")
    before = sidecar.read_bytes()

    with pytest.raises(SqliteBackupError, match="preexisting .*sidecar|preexisting destination"):
        verified_online_backup(source, destination)

    assert sidecar.exists()
    assert sidecar.read_bytes() == before
    assert not destination.exists()


def test_verified_online_backup_never_opens_public_destination_for_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement between reservation and SQLite open must not be overwritten."""

    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source)
    real_connect = sqlite_backup.connect_truth_database
    final_write_opens: list[str] = []

    def racing_connect(path: str | Path, *args, **kwargs):  # noqa: ANN002, ANN003
        candidate = Path(os.path.abspath(path))
        mode = str(kwargs.get("mode") or "")
        if candidate == Path(os.path.abspath(destination)) and mode == "rw":
            final_write_opens.append(mode)
            destination.unlink()
            _write_truth_db(destination, marker="external-owner")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite_backup, "connect_truth_database", racing_connect)

    receipt = verified_online_backup(source, destination)

    assert receipt["logical_equivalent"] is True
    assert final_write_opens == []
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT value FROM probe").fetchone()[0] == "alpha"


def test_verified_online_backup_preserves_destination_created_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source)
    real_link = os.link
    link_attempts = 0

    def racing_link(source_path, destination_path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        nonlocal link_attempts
        if Path(destination_path) == destination:
            link_attempts += 1
            _write_truth_db(destination, marker="external-publisher")
        return real_link(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(sqlite_backup.os, "link", racing_link)

    with pytest.raises(SqliteBackupError, match="created concurrently|publish"):
        verified_online_backup(source, destination)

    assert link_attempts == 1
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT value FROM probe").fetchone()[0] == "external-publisher"


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing contract")
def test_verified_online_backup_holds_staging_reservation_through_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source, marker="source-owner")
    real_connect = sqlite_backup.connect_truth_database
    attempted_replacements: list[str] = []
    injected_paths: list[Path] = []

    def racing_connect(path: str | Path, *args, **kwargs):  # noqa: ANN002, ANN003
        candidate = Path(os.path.abspath(path))
        if candidate != Path(os.path.abspath(source)) and kwargs.get("mode") == "rw":
            attacker = Path(f"{candidate}.attacker")
            _write_truth_db(attacker, marker="external-before-open")
            injected_paths.extend((candidate, attacker))
            try:
                os.replace(attacker, candidate)
            except PermissionError:
                attempted_replacements.append("rejected")
            else:
                attempted_replacements.append("succeeded")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite_backup, "connect_truth_database", racing_connect)

    try:
        receipt = verified_online_backup(source, destination)
        assert receipt["logical_equivalent"] is True
        assert attempted_replacements == ["rejected"]
        with sqlite3.connect(destination) as connection:
            assert connection.execute("SELECT value FROM probe").fetchone()[0] == "source-owner"
    finally:
        for path in injected_paths:
            path.unlink(missing_ok=True)
            for sidecar in ("-wal", "-shm", "-journal"):
                Path(f"{path}{sidecar}").unlink(missing_ok=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing contract")
def test_verified_online_backup_holds_staging_reservation_through_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source, marker="source-owner")
    real_link = sqlite_backup.os.link
    attempted_replacements: list[str] = []
    injected_paths: list[Path] = [destination]

    def racing_link(source_path, destination_path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        staging = Path(source_path)
        attacker = Path(f"{staging}.attacker")
        _write_truth_db(attacker, marker="external-before-link")
        injected_paths.extend((staging, attacker))
        try:
            os.replace(attacker, staging)
        except PermissionError:
            attempted_replacements.append("rejected")
        else:
            attempted_replacements.append("succeeded")
        return real_link(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(sqlite_backup.os, "link", racing_link)

    try:
        receipt = verified_online_backup(source, destination)
        assert receipt["logical_equivalent"] is True
        assert attempted_replacements == ["rejected"]
        connection = sqlite3.connect(destination)
        try:
            assert connection.execute("SELECT value FROM probe").fetchone()[0] == "source-owner"
        finally:
            connection.close()
    finally:
        for path in injected_paths:
            path.unlink(missing_ok=True)
            for sidecar in ("-wal", "-shm", "-journal"):
                Path(f"{path}{sidecar}").unlink(missing_ok=True)


def test_verified_online_backup_retries_transient_reservation_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source, marker="source-owner")
    real_reserve = sqlite_backup._reserve_staging_path
    real_close = os.close
    captured_descriptors: list[int] = []
    captured_staging: list[Path] = []
    close_attempts = 0

    def capture_reservation(path: Path):  # noqa: ANN202
        staging, descriptor, identity = real_reserve(path)
        captured_descriptors.append(descriptor)
        captured_staging.append(staging)
        return staging, descriptor, identity

    def flaky_close(descriptor: int) -> None:
        nonlocal close_attempts
        if captured_descriptors and descriptor == captured_descriptors[0]:
            close_attempts += 1
            if close_attempts == 1:
                raise OSError("injected reservation descriptor close failure")
        real_close(descriptor)

    monkeypatch.setattr(sqlite_backup, "_reserve_staging_path", capture_reservation)
    monkeypatch.setattr(sqlite_backup.os, "close", flaky_close)

    try:
        receipt = verified_online_backup(source, destination)
        assert receipt["logical_equivalent"] is True
        assert close_attempts == 2
        with pytest.raises(OSError):
            os.fstat(captured_descriptors[0])
        assert all(not path.exists() for path in captured_staging)
    finally:
        for descriptor in captured_descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            real_close(descriptor)
        for path in captured_staging:
            path.unlink(missing_ok=True)


def test_verified_online_backup_keeps_reservation_for_error_cleanup_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source, marker="source-owner")
    real_reserve = sqlite_backup._reserve_staging_path
    real_close = os.close
    captured_descriptors: list[int] = []
    captured_staging: list[Path] = []
    close_attempts = 0

    def capture_reservation(path: Path):  # noqa: ANN202
        staging, descriptor, identity = real_reserve(path)
        captured_descriptors.append(descriptor)
        captured_staging.append(staging)
        return staging, descriptor, identity

    def delayed_close(descriptor: int) -> None:
        nonlocal close_attempts
        if captured_descriptors and descriptor == captured_descriptors[0]:
            close_attempts += 1
            if close_attempts <= 3:
                raise OSError("injected repeated reservation close failure")
        real_close(descriptor)

    monkeypatch.setattr(sqlite_backup, "_reserve_staging_path", capture_reservation)
    monkeypatch.setattr(sqlite_backup.os, "close", delayed_close)

    try:
        with pytest.raises(SqliteBackupError, match="partial cleanup failure"):
            verified_online_backup(source, destination)
        assert close_attempts == 4
        with pytest.raises(OSError):
            os.fstat(captured_descriptors[0])
    finally:
        for descriptor in captured_descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            real_close(descriptor)
        for path in captured_staging:
            path.unlink(missing_ok=True)


def test_verified_online_backup_never_closes_reused_descriptor_after_close_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    unrelated_path = tmp_path / "unrelated.bin"
    _write_truth_db(source, marker="source-owner")
    real_reserve = sqlite_backup._reserve_staging_path
    real_close = os.close
    captured_descriptors: list[int] = []
    captured_staging: list[Path] = []
    unrelated_descriptors: list[int] = []
    close_attempts = 0

    def capture_reservation(path: Path):  # noqa: ANN202
        staging, descriptor, identity = real_reserve(path)
        captured_descriptors.append(descriptor)
        captured_staging.append(staging)
        return staging, descriptor, identity

    def close_then_report_error(descriptor: int) -> None:
        nonlocal close_attempts
        if (
            captured_descriptors
            and descriptor == captured_descriptors[0]
            and close_attempts == 0
        ):
            close_attempts += 1
            real_close(descriptor)
            reused = os.open(unrelated_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            unrelated_descriptors.append(reused)
            assert reused == descriptor
            raise OSError("injected post-close error after fd reuse")
        close_attempts += int(descriptor == captured_descriptors[0])
        real_close(descriptor)

    monkeypatch.setattr(sqlite_backup, "_reserve_staging_path", capture_reservation)
    monkeypatch.setattr(sqlite_backup.os, "close", close_then_report_error)

    try:
        with pytest.raises(
            SqliteBackupError,
            match="identity changed|partial cleanup failure",
        ):
            verified_online_backup(source, destination)
        assert close_attempts == 1
        assert os.fstat(unrelated_descriptors[0]).st_size == 0
    finally:
        for descriptor in unrelated_descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            real_close(descriptor)
        for descriptor in captured_descriptors:
            if descriptor in unrelated_descriptors:
                continue
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            real_close(descriptor)
        for path in captured_staging:
            path.unlink(missing_ok=True)


def test_verified_online_backup_connection_close_failure_does_not_leak_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    class CloseFailingConnection(sqlite3.Connection):
        def close(self) -> None:
            raise OSError("injected destination connection close failure")

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source, marker="source-owner")
    real_connect = sqlite_backup.connect_truth_database
    real_reserve = sqlite_backup._reserve_staging_path
    captured_connections: list[CloseFailingConnection] = []
    captured_descriptors: list[int] = []
    captured_staging: list[Path] = []

    def capture_reservation(path: Path):  # noqa: ANN202
        staging, descriptor, identity = real_reserve(path)
        captured_descriptors.append(descriptor)
        captured_staging.append(staging)
        return staging, descriptor, identity

    def connect_with_failing_close(path: str | Path, *args, **kwargs):  # noqa: ANN002, ANN003
        if kwargs.get("mode") == "rw":
            connection = sqlite3.connect(path, factory=CloseFailingConnection)
            captured_connections.append(connection)
            return connection
        return real_connect(path, *args, **kwargs)

    def fail_transfer(source_conn, destination_conn):  # noqa: ANN001, ARG001
        raise sqlite3.OperationalError("injected transfer failure")

    monkeypatch.setattr(sqlite_backup, "_reserve_staging_path", capture_reservation)
    monkeypatch.setattr(
        sqlite_backup,
        "connect_truth_database",
        connect_with_failing_close,
    )
    monkeypatch.setattr(sqlite_backup, "_transfer_online_backup", fail_transfer)

    try:
        with pytest.raises(
            SqliteBackupError,
            match="connection close|partial cleanup failure",
        ):
            verified_online_backup(source, destination)
        with pytest.raises(OSError):
            os.fstat(captured_descriptors[0])
    finally:
        for connection in captured_connections:
            sqlite3.Connection.close(connection)
        for descriptor in captured_descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            os.close(descriptor)
        for path in captured_staging:
            path.unlink(missing_ok=True)


def test_verified_online_backup_cleans_claimed_placeholder_when_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination created by this call cannot survive an SQLite open failure."""

    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source)
    real_connect = sqlite_backup.connect_truth_database
    failed_candidates: list[Path] = []

    def fail_destination_open(path: str | Path, *args, **kwargs):  # noqa: ANN002, ANN003
        candidate = Path(os.path.abspath(path))
        if candidate != Path(os.path.abspath(source)) and kwargs.get("mode") == "rw":
            failed_candidates.append(candidate)
            candidate.write_bytes(b"partial SQLite open")
            raise sqlite3.OperationalError("injected destination open failure")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite_backup, "connect_truth_database", fail_destination_open)

    with pytest.raises(SqliteBackupError, match="injected destination open failure"):
        verified_online_backup(source, destination)

    assert failed_candidates
    assert not destination.exists()
    assert all(not candidate.exists() for candidate in failed_candidates)
    assert list(destination.parent.glob(f"{destination.name}.scope-recall-stage-*")) == []
    assert not any(path.exists() for path in (
        Path(f"{destination}-wal"),
        Path(f"{destination}-shm"),
        Path(f"{destination}-journal"),
    ))


def test_verified_online_backup_preserves_external_replacement_during_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source)
    replacements: list[tuple[Path, Path]] = []

    def fail_transfer(source_conn, destination_conn):  # noqa: ANN001, ARG001
        raise sqlite3.OperationalError("injected transfer failure after replacement")

    original_cleanup = sqlite_backup._remove_owned_sqlite_artifacts

    def replace_before_cleanup(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        path.unlink(missing_ok=True)
        _write_truth_db(path, marker="external-owner")
        external_sidecar = Path(f"{path}-wal")
        external_sidecar.write_bytes(b"external-sidecar")
        replacements.append((path, external_sidecar))
        return original_cleanup(path, *args, **kwargs)

    monkeypatch.setattr(sqlite_backup, "_transfer_online_backup", fail_transfer)
    monkeypatch.setattr(
        sqlite_backup,
        "_remove_owned_sqlite_artifacts",
        replace_before_cleanup,
    )

    with pytest.raises(SqliteBackupError, match="partial cleanup failure"):
        verified_online_backup(source, destination)

    assert not destination.exists()
    assert len(replacements) == 1
    external_path, external_sidecar = replacements[0]
    assert external_path.is_file()
    with sqlite3.connect(external_path) as connection:
        assert connection.execute("SELECT value FROM probe").fetchone()[0] == "external-owner"
    assert external_sidecar.read_bytes() == b"external-sidecar"


def test_verified_online_backup_partial_cleanup_failure_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scope_recall.sqlite_backup as sqlite_backup

    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _write_truth_db(source)

    def boom(source_conn, destination_conn):  # noqa: ANN001, ARG001
        raise sqlite3.OperationalError("injected transfer failure")

    original_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):  # noqa: ANN001
        if self == destination or str(self).startswith(str(destination)):
            raise OSError("injected unlink failure")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(sqlite_backup, "_transfer_online_backup", boom)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with pytest.raises(SqliteBackupError, match="partial cleanup failure"):
        verified_online_backup(source, destination)


def test_sqlite_backup_receipt_path_mismatch_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes-home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    _write_truth_db(db_path, marker="activation")
    (home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (storage / "config.json").write_text("{}", encoding="utf-8")

    def drifted_receipt(source: Path, destination: Path):
        return {
            "source_path": str(source) + ".drift",
            "backup_path": str(destination),
            "source_health": {
                "ok": True,
                "quick_check": "ok",
                "integrity_check": "ok",
                "foreign_key_violation_present": False,
            },
            "backup_health": {
                "ok": True,
                "quick_check": "ok",
                "integrity_check": "ok",
                "foreign_key_violation_present": False,
            },
            "source_logical_fingerprint": "a" * 64,
            "backup_logical_fingerprint": "a" * 64,
            "logical_fingerprint": "a" * 64,
            "logical_equivalent": True,
        }

    monkeypatch.setattr(activation_transaction, "_sqlite_online_backup", drifted_receipt)

    with pytest.raises(
        activation_transaction.ActivationSnapshotError,
        match="path|receipt",
    ):
        activation_transaction.capture_activation_state(home, writer_quiesced=True)


def test_activation_wrapper_remains_monkeypatchable_and_surfaces_receipt_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes-home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    _write_truth_db(db_path, marker="activation")
    (home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (storage / "config.json").write_text("{}", encoding="utf-8")

    seen: dict[str, Path] = {}
    original = activation_transaction._sqlite_online_backup

    def probing(source: Path, destination: Path):
        seen["source"] = source
        seen["destination"] = destination
        return original(source, destination)

    monkeypatch.setattr(activation_transaction, "_sqlite_online_backup", probing)

    snapshot = activation_transaction.capture_activation_state(
        home, writer_quiesced=True
    )
    assert seen["source"] == db_path
    assert Path(snapshot["sqlite"]["backup_path"]).is_file()

    sqlite_snap = snapshot["sqlite"]
    assert sqlite_snap["source_health"]["ok"] is True
    assert sqlite_snap["backup_health"]["ok"] is True
    assert sqlite_snap["logical_equivalent"] is True
    assert sqlite_snap["logical_fingerprint"]
    assert logical_fingerprint(db_path) == sqlite_snap["source_logical_fingerprint"]
    assert (
        logical_fingerprint(Path(sqlite_snap["backup_path"]))
        == sqlite_snap["backup_logical_fingerprint"]
    )
    assert (
        sqlite_snap["source_logical_fingerprint"]
        == sqlite_snap["backup_logical_fingerprint"]
        == sqlite_snap["logical_fingerprint"]
    )

    receipt = activation_transaction.committed_activation_receipt(
        snapshot,
        plugin_dir=home / "plugins" / "scope-recall",
        previous_plugin_existed=False,
        plugin_backup_path="",
        plugin_replaced=False,
    )
    surfaced = receipt["sqlite"]
    assert surfaced["backup_path"]
    assert surfaced["source_health"]["ok"] is True
    assert surfaced["backup_health"]["ok"] is True
    assert surfaced["logical_equivalent"] is True
    assert surfaced["logical_fingerprint"]
    assert (
        surfaced["source_logical_fingerprint"]
        == surfaced["backup_logical_fingerprint"]
        == surfaced["logical_fingerprint"]
    )


def test_activation_backup_failure_fail_closed_leaves_no_usable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes-home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    db_path = storage / "memory.sqlite3"
    _write_truth_db(db_path)
    (home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (storage / "config.json").write_text("{}", encoding="utf-8")

    def fail_backup(source: Path, destination: Path) -> None:  # noqa: ARG001
        raise activation_transaction.ActivationSnapshotError("injected backup failure")

    monkeypatch.setattr(activation_transaction, "_sqlite_online_backup", fail_backup)

    with pytest.raises(
        activation_transaction.ActivationSnapshotError, match="injected backup failure"
    ):
        activation_transaction.capture_activation_state(home, writer_quiesced=True)

    backups = list((home / "backups").glob("**/memory.sqlite3")) if (home / "backups").exists() else []
    assert backups == []


def test_sqlite_backup_receipt_rejects_nonboolean_logical_equivalent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "memory.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    healthy = {
        "ok": True,
        "quick_check": "ok",
        "integrity_check": "ok",
        "foreign_key_violation_present": False,
    }
    receipt = {
        "source_path": str(source),
        "backup_path": str(destination),
        "source_health": healthy,
        "backup_health": healthy,
        "source_logical_fingerprint": "a" * 64,
        "backup_logical_fingerprint": "a" * 64,
        "logical_fingerprint": "a" * 64,
        "logical_equivalent": "false",
    }

    with pytest.raises(
        activation_transaction.ActivationSnapshotError,
        match="invalid health/fingerprint evidence",
    ):
        activation_transaction._sqlite_backup_receipt_or_verify(
            source,
            destination,
            receipt,
        )


def test_sqlite_backup_module_is_activation_boundary_not_startup_import() -> None:
    """Ordinary startup/reconciliation modules must not own this protection path."""

    import scope_recall.vector_runtime as vector_runtime

    runtime_source = Path(vector_runtime.__file__).read_text(encoding="utf-8")
    assert "sqlite_backup" not in runtime_source
    assert "verified_online_backup" not in runtime_source
