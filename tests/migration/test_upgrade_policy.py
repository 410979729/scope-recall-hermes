"""Upgrade admission, rollback snapshots and bounded lock waits."""

import errno
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from scope_recall.adapters.clients import install_codex_scope_recall
from scope_recall.core import storage, store_upgrade
from scope_recall.core.backup_primitives import BackupError
from scope_recall.core.file_lock import advisory_file_lock
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.maintenance import cli
from scope_recall.maintenance.doctor_report import DoctorReport
from scope_recall.maintenance.doctor_store import check_storage
from scope_recall.runtime import worker_entry, worker_watchdog
from v11_support import downgrade_store


def old_store(root, version=1111):
    project = root / "TEST-project"
    project.mkdir()
    config, core = install_codex_scope_recall(root / "TEST-install", project_root=project)
    downgrade_store(core.storage.path, version)
    return config, core


def worker_config(root, core):
    binding = core.storage.binding
    path = root / "TEST-worker.json"
    path.write_text(
        json.dumps(
            {
                "binding": {
                    "agent_id": binding.agent_id,
                    "installation_id": binding.installation_id,
                    "data_directory": str(binding.data_directory),
                    "scope_ids": sorted(binding.scope_ids),
                    "test_mode": binding.test_mode,
                },
                "session_id": "TEST-upgrade",
                "owner_id": "TEST-upgrade",
                "allowed_scope_ids": sorted(binding.scope_ids),
                "actor_origin": "human_direct",
                "supervisor_enabled": False,
                "auxiliary": {"external_embedding": False, "external_consolidation": False},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("latest", [1111, SCHEMA_VERSION])
def test_failed_upgrade_keeps_successful_and_latest_snapshots(tmp_path, monkeypatch, latest):
    _, core = old_store(tmp_path, 1108)
    core.storage.initialize()
    directory = core.storage.path.parent / "upgrade-snapshots"
    rollback = next(directory.glob("*.sqlite3"))
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute("CREATE TABLE test_latest_truth(value TEXT)")
        conn.execute("INSERT INTO test_latest_truth VALUES ('TEST latest truth')")
        conn.commit()
    if latest == 1111:
        downgrade_store(core.storage.path, latest)
    else:
        monkeypatch.setattr(storage, "UPGRADE_CHAIN", (*storage.UPGRADE_CHAIN, latest))
        monkeypatch.setattr(storage, "SCHEMA_VERSION", latest + 1)

    def fail(self, conn):
        conn.execute("CREATE TABLE test_uncommitted(value TEXT)")
        raise OSError(errno.ENOSPC, "TEST disk full")

    monkeypatch.setattr(storage.SQLiteStorage, "_migrate_store", fail)
    for _ in range(2):
        with pytest.raises(OSError):
            core.storage.initialize()
        snapshots = list(directory.glob("*.sqlite3"))
        assert len(snapshots) == 2 and rollback in snapshots
        current = next(p for p in snapshots if p != rollback)
        with closing(sqlite3.connect(current)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == latest
            assert conn.execute("SELECT value FROM test_latest_truth").fetchone()[0] == "TEST latest truth"


def test_completed_upgrade_recovers_snapshot_role_before_next_attempt(tmp_path, monkeypatch):
    _, core = old_store(tmp_path, 1108)
    core.storage.initialize()
    downgrade_store(core.storage.path, 1111)
    atomic = store_upgrade.atomic_json
    fail_promotion = [True]

    def write(path, value):
        if fail_promotion[0] and value.get("upgrade_role") == "rollback":
            raise OSError(errno.ENOSPC, "TEST manifest promotion failed")
        return atomic(path, value)

    monkeypatch.setattr(store_upgrade, "atomic_json", write)
    core.storage.initialize()
    fail_promotion[0] = False
    current = storage.SCHEMA_VERSION
    monkeypatch.setattr(storage, "UPGRADE_CHAIN", (*storage.UPGRADE_CHAIN, current))
    monkeypatch.setattr(storage, "SCHEMA_VERSION", current + 1)

    def fail(*args):
        raise OSError(errno.ENOSPC, "TEST next migration failed")

    monkeypatch.setattr(storage.SQLiteStorage, "_migrate_store", fail)
    with pytest.raises(OSError):
        core.storage.initialize()
    schemas = []
    for path in (core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"):
        with closing(sqlite3.connect(path)) as conn:
            schemas.append(conn.execute("PRAGMA user_version").fetchone()[0])
    assert sorted(schemas) == [1111, current]


def test_failed_upgrade_doctor_recommends_retry_before_restore(tmp_path, monkeypatch):
    _, core = old_store(tmp_path)

    def fail(self, conn):
        raise OSError(errno.ENOSPC, "TEST disk full")

    monkeypatch.setattr(storage.SQLiteStorage, "_migrate_store", fail)
    with pytest.raises(OSError):
        core.storage.initialize()
    report = DoctorReport(host="codex", status="degraded")
    assert not check_storage(report, core.storage.binding, core.storage.path.parent)
    assert "reopen" in str(report) and "retry" in str(report)
    assert "restore --snapshot" not in str(report)


def test_paused_watchdog_does_not_admit_schema_maintenance(tmp_path, monkeypatch, capsys):
    _, core = old_store(tmp_path)
    path = worker_config(tmp_path, core)
    monkeypatch.setattr(worker_watchdog, "operator_paused", lambda config: True)
    calls = []
    monkeypatch.setattr(worker_watchdog, "_run_once", lambda *args, **kwargs: calls.append(kwargs) or 0)
    assert worker_watchdog.run(path, Path(sys.executable), cleanup_config=False) == 0
    assert not calls
    capsys.readouterr()


def test_paused_maintenance_child_does_not_open_store(tmp_path, monkeypatch):
    from scope_recall.runtime import scheduling

    _, core = old_store(tmp_path)
    monkeypatch.setattr(scheduling, "operator_paused", lambda config: True)
    output = StringIO()
    assert (
        worker_entry.run_upgrade_worker(worker_config(tmp_path, core), output=output, paused=scheduling.operator_paused)
        == 0
    )
    assert json.loads(output.getvalue())["status"] == "paused"
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1111


def test_foreground_upgrade_has_no_background_deadline(tmp_path, monkeypatch, capsys):
    _, core = old_store(tmp_path)
    original = storage.SQLiteStorage.initialize
    budgets = []
    migrate = storage.SQLiteStorage._migrate_store
    now = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def slow_step(self, conn):
        now[0] += 181
        migrate(self, conn)

    def initialize(self, **kwargs):
        budgets.append(kwargs.get("remaining_seconds"))
        return original(self, **kwargs)

    monkeypatch.setattr(storage.SQLiteStorage, "initialize", initialize)
    monkeypatch.setattr(storage.SQLiteStorage, "_migrate_store", slow_step)
    code = cli.main(["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "TEST-install")])
    capsys.readouterr()
    assert code == 0 and budgets == [None]
    assert store_upgrade.read_upgrade_state(core.storage.path.parent)["seconds"] >= 181


def test_interrupted_foreground_upgrade_rolls_back(tmp_path, monkeypatch):
    _, core = old_store(tmp_path)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        before = tuple(conn.iterdump())

    def interrupt(self, conn):
        conn.execute("CREATE TABLE test_interrupted(value TEXT)")
        raise KeyboardInterrupt()

    monkeypatch.setattr(storage.SQLiteStorage, "_migrate_store", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "TEST-install")])
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert tuple(conn.iterdump()) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1111


def test_migration_lock_wait_uses_time_left_after_snapshot(tmp_path, monkeypatch):
    _, core = old_store(tmp_path)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    from scope_recall.core import backup_primitives

    backup = store_upgrade.backup_sqlite
    elapsed = [100.0]
    controlled = SimpleNamespace(monotonic=lambda: elapsed[0])
    for module in (storage, store_upgrade, backup_primitives):
        monkeypatch.setattr(module, "time", controlled)

    def slow_snapshot(*args, **kwargs):
        result = backup(*args, **kwargs)
        elapsed[0] += 0.08
        return result

    def inspect_wait(self, conn):
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] < 100
        raise RuntimeError("TEST inspected remaining lock budget")

    monkeypatch.setattr(store_upgrade, "backup_sqlite", slow_snapshot)
    monkeypatch.setattr(storage.SQLiteStorage, "_migrate_store", inspect_wait)
    with pytest.raises(RuntimeError, match="inspected remaining"):
        core.storage.initialize(remaining_seconds=0.15)


def test_post_commit_wal_switch_never_waits_for_readers(tmp_path, monkeypatch):
    _, core = old_store(tmp_path)
    seen = []

    def inspect(conn):
        seen.append(conn.execute("PRAGMA busy_timeout").fetchone()[0])

    monkeypatch.setattr(storage, "_ensure_wal", inspect)
    core.storage.initialize(remaining_seconds=30)
    assert seen == [0]


def test_schema_maintenance_reuses_worker_ownership_lock(tmp_path, monkeypatch, capsys):
    _, core = old_store(tmp_path)
    calls = []

    def spawn(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("TEST duplicate worker admitted")

    monkeypatch.setattr(worker_watchdog, "_spawn_worker", spawn)
    with advisory_file_lock(core.storage.path.parent / "runtime-worker.lock", timeout_seconds=0):
        with ThreadPoolExecutor(1) as pool:
            code = pool.submit(
                worker_watchdog._run_once,
                worker_config(tmp_path, core),
                Path(sys.executable),
                cleanup_config=False,
                job_kind="upgrade",
            ).result(timeout=5)
    assert code == 75 and not calls
    capsys.readouterr()


@pytest.mark.parametrize("version", [1105, 1106, 1107, 1108, 1109, 10815])
def test_doctor_uses_restore_schema_rules_for_completed_upgrades(tmp_path, version):
    _, core = old_store(tmp_path, version if version != 10815 else storage.SCHEMA_VERSION)
    if version == 10815:
        with closing(sqlite3.connect(core.storage.path)) as conn:
            conn.execute("PRAGMA user_version=10815")
    core.storage.initialize(repair_header=True)
    report = DoctorReport(host="codex", status="degraded")
    assert check_storage(report, core.storage.binding, core.storage.path.parent)
    assert "cannot safely restore" in str(report) and "restore --snapshot" not in str(report)
    assert str(version) in str(report)


def test_doctor_orders_successful_cross_schema_undo_by_actual_versions(tmp_path):
    _, core = old_store(tmp_path)
    core.storage.initialize()
    report = DoctorReport(host="codex", status="degraded")
    assert check_storage(report, core.storage.binding, core.storage.path.parent)
    text = str(report)
    assert f"current store schema {SCHEMA_VERSION}" in text and "snapshot schema 1111" in text
    assert text.index("--backup-dir") < text.index("After it reports prepared") < text.index("snapshot schema 1111")
    assert "--apply" in text


def test_background_deadline_doctor_names_unbounded_foreground_command(tmp_path, monkeypatch):
    _, core = old_store(tmp_path)
    now = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def expire(self, conn):
        now[0] += 181
        raise BackupError("backup deadline exceeded")

    monkeypatch.setattr(storage.SQLiteStorage, "_migrate_store", expire)
    with pytest.raises(BackupError):
        core.storage.initialize(remaining_seconds=180)
    report = DoctorReport(host="codex", status="degraded")
    assert not check_storage(report, core.storage.binding, core.storage.path.parent)
    assert "upgrade-store" in str(report) and "without a total time limit" in str(report)


def test_interrupted_maintenance_doctor_names_foreground_command(tmp_path):
    _, core = old_store(tmp_path)
    state = {
        "status": "interrupted",
        "phase": "snapshot",
        "reason": "process_interrupted",
        "schema_before": 1111,
        "schema_target": SCHEMA_VERSION,
        "snapshot_verified": False,
    }
    (core.storage.path.parent / store_upgrade.UPGRADE_STATE).write_text(json.dumps(state), encoding="utf-8")
    report = DoctorReport(host="codex", status="degraded")
    assert not check_storage(report, core.storage.binding, core.storage.path.parent)
    assert "upgrade-store" in str(report) and "without a total time limit" in str(report)


@pytest.fixture
def long_reader(tmp_path, monkeypatch):
    _, core = old_store(tmp_path)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    reader = sqlite3.connect(core.storage.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM source_events").fetchone()
    original = storage.SQLiteStorage._migrate_store

    def spill(self, conn):
        conn.execute("PRAGMA cache_size=5")
        conn.execute("CREATE TABLE test_spill(value BLOB)")
        conn.executemany("INSERT INTO test_spill VALUES (zeroblob(8192))", [()] * 128)
        original(self, conn)

    monkeypatch.setattr(storage.SQLiteStorage, "_migrate_store", spill)
    try:
        yield core, reader, original
    finally:
        reader.close()


def test_delete_reader_blocks_spilled_pages_with_bounded_rollback(long_reader):
    core, reader, _ = long_reader
    began = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        core.storage.initialize(remaining_seconds=0.15)
    assert time.monotonic() - began < 0.5
    assert reader.execute("PRAGMA user_version").fetchone()[0] == 1111
    assert not reader.execute("SELECT 1 FROM sqlite_master WHERE name='test_spill'").fetchall()
    report = DoctorReport(host="codex", status="degraded")
    assert not check_storage(report, core.storage.binding, core.storage.path.parent)
    assert "upgrade_blocked_by_reader" in str(report) and "read transactions" in str(report)


def test_released_delete_reader_allows_retry(long_reader, monkeypatch):
    core, reader, original = long_reader
    with pytest.raises(sqlite3.OperationalError):
        core.storage.initialize(remaining_seconds=0.15)
    reader.rollback()
    monkeypatch.setattr(storage.SQLiteStorage, "_migrate_store", original)
    assert core.storage.initialize(remaining_seconds=2).schema_version == storage.SCHEMA_VERSION
