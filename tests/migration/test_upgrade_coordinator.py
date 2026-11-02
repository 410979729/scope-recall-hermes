"""Automatic upgrades preserve a rollback copy before changing stored format."""

import errno
import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from scope_recall.adapters.clients import CodexHookHandler
from scope_recall.adapters.clients.config import install_codex_scope_recall
from scope_recall.contracts import ContractError, TrustedContext
from scope_recall.core import backup_primitives, storage, store_upgrade
from scope_recall.core.file_lock import advisory_file_lock
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.truth_connection import connect_truth_database
from scope_recall.maintenance import cli
from scope_recall.maintenance.doctor_report import DoctorReport
from scope_recall.maintenance.doctor_store import check_storage
from scope_recall.maintenance.restore import apply_restore
from v11_support import downgrade_store, source_event


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_migrations_run_on_the_locked_live_connection(tmp_path, monkeypatch, journal):
    _, core, _ = _old_store(tmp_path)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute(f"PRAGMA journal_mode={journal}")
    original = SQLiteStorage._migrate_store
    backup = store_upgrade.backup_sqlite

    def snapshot(source, destination, **kwargs):
        with closing(sqlite3.connect(source, timeout=0)) as peer:
            with pytest.raises(sqlite3.OperationalError):
                peer.execute("BEGIN IMMEDIATE")
            assert peer.execute("PRAGMA user_version").fetchone()[0] == 1111
        return backup(source, destination, **kwargs)

    def migrate(self, conn):
        assert Path(conn.execute("PRAGMA database_list").fetchone()[2]) == self.path
        assert conn.in_transaction
        with closing(sqlite3.connect(self.path, timeout=0)) as peer:
            with pytest.raises(sqlite3.OperationalError):
                peer.execute("BEGIN IMMEDIATE")
        original(self, conn)

    monkeypatch.setattr(SQLiteStorage, "_migrate_store", migrate)
    monkeypatch.setattr(store_upgrade, "backup_sqlite", snapshot)
    core.storage.initialize(remaining_seconds=30)


def test_upgrade_never_hashes_the_live_database(tmp_path, monkeypatch):
    _, core, _ = _old_store(tmp_path)
    digest = backup_primitives.sha256

    def snapshot_only(path):
        assert Path(path) != core.storage.path
        return digest(path)

    monkeypatch.setattr(backup_primitives, "sha256", snapshot_only)
    core.storage.initialize(remaining_seconds=30)


def test_upgrade_has_one_snapshot_copy_and_no_database_publication(tmp_path, monkeypatch):
    _, core, _ = _old_store(tmp_path)
    copies = []
    backup = store_upgrade.backup_sqlite

    def record(source, destination, **kwargs):
        copies.append((Path(source), Path(destination)))
        return backup(source, destination, **kwargs)

    monkeypatch.setattr(store_upgrade, "backup_sqlite", record)
    core.storage.initialize(remaining_seconds=30)
    assert len(copies) == 1 and copies[0][0] == core.storage.path


def test_initialize_deadline_includes_connection_and_coordinator_waits(tmp_path, monkeypatch):
    _, core, _ = _old_store(tmp_path)
    opening, locking, migrating = SQLiteStorage._open, storage.advisory_file_lock, SQLiteStorage._migrate_store

    def slow_open(self, *args, **kwargs):
        time.sleep(0.04)
        return opening(self, *args, **kwargs)

    @contextmanager
    def slow_lock(*args, **kwargs):
        time.sleep(0.04)
        with locking(*args, **kwargs):
            yield

    def slow_migrate(self, conn):
        time.sleep(0.06)
        migrating(self, conn)

    monkeypatch.setattr(SQLiteStorage, "_open", slow_open)
    monkeypatch.setattr(storage, "advisory_file_lock", slow_lock)
    monkeypatch.setattr(SQLiteStorage, "_migrate_store", slow_migrate)
    with pytest.raises((ContractError, backup_primitives.BackupError, sqlite3.Error)):
        core.storage.initialize(remaining_seconds=0.1)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1111


def test_large_store_without_budget_is_deferred():
    assert not storage.upgrade_fits(storage.HEAVY_UPGRADE_BYTES, None)


def _worker_config(root, core):
    binding = core.storage.binding
    payload = {
        "binding": {
            "agent_id": binding.agent_id,
            "installation_id": binding.installation_id,
            "data_directory": str(binding.data_directory),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": True,
        },
        "session_id": "TEST-worker",
        "owner_id": "TEST-upgrade",
        "allowed_scope_ids": sorted(binding.scope_ids),
        "actor_origin": "human_direct",
        "supervisor_enabled": False,
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }
    path = root / "TEST-worker.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_worker_performs_maintenance_before_model_runtime(tmp_path, monkeypatch):
    from scope_recall.runtime import worker_entry

    _, core, _ = _old_store(tmp_path)

    def forbidden(config):
        pytest.fail("model runtime started before schema maintenance")

    monkeypatch.setattr(worker_entry, "build_runtime_instance", forbidden)
    output = StringIO()
    assert worker_entry.run_worker(_worker_config(tmp_path, core), output=output) == 0
    assert json.loads(output.getvalue())["job_kind"] == "upgrade"


def test_owned_watchdog_runs_schema_maintenance_child(tmp_path, capsys):
    from scope_recall.runtime import worker_watchdog

    _, core, _ = _old_store(tmp_path)
    result = {}
    assert (
        worker_watchdog._run_once(
            _worker_config(tmp_path, core),
            Path(sys.executable),
            cleanup_config=False,
            job_kind="upgrade",
            result_sink=result,
        )
        == 0
    )
    assert result["job_kind"] == "upgrade" and result["schema_upgrade"]["status"] == "complete"
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_watchdog_gives_upgrade_its_own_window(tmp_path, monkeypatch):
    from scope_recall.runtime import worker_watchdog
    from scope_recall.runtime.store_maintenance import STORE_UPGRADE_SECONDS

    _, core, _ = _old_store(tmp_path)
    windows = []

    def spawn(config, python, deadline, **kwargs):
        windows.append(deadline - time.monotonic())
        raise RuntimeError("TEST stop before child creation")

    monkeypatch.setattr(worker_watchdog, "_spawn_worker", spawn)
    worker_watchdog._run_once(
        _worker_config(tmp_path, core), Path(sys.executable), cleanup_config=False, job_kind="upgrade"
    )
    assert windows and windows[0] > max(125, STORE_UPGRADE_SECONDS - 5)


def test_successful_automatic_upgrades_retain_only_latest_snapshot(tmp_path):
    _, core, _ = _old_store(tmp_path)
    core.storage.initialize(remaining_seconds=30)
    first = list((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))
    downgrade_store(core.storage.path, 1111)
    core.storage.initialize(remaining_seconds=30)
    snapshots = list((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))
    assert len(snapshots) == 1 and snapshots != first


@pytest.mark.parametrize("version", [1108, 1109, 1111])
def test_failed_attempt_does_not_accumulate_automatic_snapshots(tmp_path, monkeypatch, version):
    _, core, _ = _old_store(tmp_path, version)
    core.storage.initialize(remaining_seconds=30)
    prior = list((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))
    downgrade_store(core.storage.path, 1111)

    def fail(self, conn):
        raise RuntimeError("TEST interrupted")

    monkeypatch.setattr(SQLiteStorage, "_migrate_store", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            core.storage.initialize(remaining_seconds=30)
    snapshots = list((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))
    assert len(snapshots) == 2 and prior[0] in snapshots
    state = store_upgrade.read_upgrade_state(core.storage.path.parent)
    assert state["backup_schema"] == 1111
    report = DoctorReport(host="codex", status="degraded")
    assert not check_storage(report, core.storage.binding, core.storage.path.parent)
    assert "retry" in str(report) and "restore --snapshot" not in str(report)


def test_explicit_backup_directory_is_never_rotated(tmp_path):
    _, core, _ = _old_store(tmp_path)
    directory = core.storage.path.parent / "upgrade-snapshots"
    core.storage.initialize(backup_dir=directory)
    manual = list(directory.glob("*.sqlite3"))
    for _ in range(2):
        downgrade_store(core.storage.path, 1111)
        core.storage.initialize()
    assert len(list(directory.glob("*.sqlite3"))) == 2
    assert all(p.exists() for p in manual)


def test_complete_snapshot_survives_interruption_before_progress_record(tmp_path, monkeypatch):
    _, core, _ = _old_store(tmp_path)

    def fail_migration(*args):
        raise RuntimeError("TEST migration interrupted")

    monkeypatch.setattr(SQLiteStorage, "_migrate_store", fail_migration)
    with pytest.raises(RuntimeError):
        core.storage.initialize()
    path = core.storage.path.parent / store_upgrade.UPGRADE_STATE
    state = json.loads(path.read_text(encoding="utf-8"))
    snapshot = Path(state["backup"])
    state.update(status="running", phase="snapshot", snapshot_verified=False)
    path.write_text(json.dumps(state), encoding="utf-8")

    def fail_snapshot(*args, **kwargs):
        raise OSError(errno.ENOSPC, "TEST snapshot failure")

    monkeypatch.setattr(store_upgrade, "backup_sqlite", fail_snapshot)
    with pytest.raises(OSError):
        core.storage.initialize()
    assert snapshot.exists() and store_upgrade._verified_snapshot(snapshot)


def test_snapshot_validation_failure_never_calls_migration(tmp_path, monkeypatch):
    _, core, _ = _old_store(tmp_path)
    before = _logical_content(core.storage.path)
    markers = store_upgrade._snapshot_markers
    monkeypatch.setattr(store_upgrade, "_snapshot_markers", lambda p: (0, 0))
    monkeypatch.setattr(SQLiteStorage, "_migrate_store", lambda *args: pytest.fail("unverified snapshot"))
    with pytest.raises(ContractError, match="backup_schema_mismatch"):
        core.storage.initialize()
    assert _logical_content(core.storage.path) == before
    assert not list((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))
    monkeypatch.setattr(store_upgrade, "_snapshot_markers", markers)


def test_diagnostic_failure_after_commit_does_not_report_failed_upgrade(tmp_path, monkeypatch):
    _, core, _ = _old_store(tmp_path)
    atomic = store_upgrade.atomic_json

    def fail_completed(path, state):
        if state.get("status") == "complete":
            raise OSError(errno.ENOSPC, "TEST diagnostic disk full")
        return atomic(path, state)

    monkeypatch.setattr(store_upgrade, "atomic_json", fail_completed)
    assert core.storage.initialize().schema_version == SCHEMA_VERSION
    assert core.storage.initialize().schema_version == SCHEMA_VERSION


def test_snapshot_quick_check_uses_initialization_deadline(tmp_path, monkeypatch):
    _, core, _ = _old_store(tmp_path)
    before = _logical_content(core.storage.path)
    connect = sqlite3.connect

    class SlowCheck(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA quick_check":
                time.sleep(0.08)
            return super().execute(sql, *args, **kwargs)

    def slow_snapshot(*args, **kwargs):
        kwargs.setdefault("factory", SlowCheck)
        return connect(*args, **kwargs)

    monkeypatch.setattr(backup_primitives.sqlite3, "connect", slow_snapshot)
    with pytest.raises(backup_primitives.BackupError, match="deadline"):
        core.storage.initialize(remaining_seconds=0.05)
    assert _logical_content(core.storage.path) == before


def test_disk_full_cli_error_is_not_reported_as_busy(tmp_path, monkeypatch, capsys):
    _, core, _ = _old_store(tmp_path)

    def fail(self, conn):
        error = sqlite3.OperationalError("TEST disk full")
        error.sqlite_errorcode = sqlite3.SQLITE_FULL
        error.sqlite_errorname = "SQLITE_FULL"
        raise error

    monkeypatch.setattr(SQLiteStorage, "_migrate_store", fail)
    code = cli.main(["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "TEST-install")])
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["error"] == "SQLITE_FULL", result


def test_cli_writer_wait_does_not_limit_foreground_upgrade(tmp_path, monkeypatch, capsys):
    _, core, _ = _old_store(tmp_path)
    acquire, migrate = cli._wait_for_upgrade_lease, SQLiteStorage._migrate_store

    def waited(*args):
        lease = acquire(*args)
        time.sleep(0.08)
        return lease

    def slow_migrate(self, conn):
        time.sleep(0.05)
        migrate(self, conn)

    monkeypatch.setattr(cli, "_wait_for_upgrade_lease", waited)
    monkeypatch.setattr(SQLiteStorage, "_migrate_store", slow_migrate)
    code = cli.main(["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "TEST-install")])
    capsys.readouterr()
    assert code == 0
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize("version", [1108, 1109])
def test_doctor_older_snapshots_require_retry_without_unsafe_restore(tmp_path, version):
    _, core, _ = _old_store(tmp_path, version)
    state = {
        "status": "failed",
        "phase": "migration",
        "schema_before": SCHEMA_VERSION,
        "backup_schema": version,
        "snapshot_verified": True,
        "backup": "TEST snapshot",
        "reason": "SQLITE_FULL",
    }
    (core.storage.path.parent / store_upgrade.UPGRADE_STATE).write_text(json.dumps(state), encoding="utf-8")
    report = DoctorReport(host="codex", status="degraded")
    assert not check_storage(report, core.storage.binding, core.storage.path.parent)
    assert "reopen" in str(report) and "retry" in str(report)
    assert "scope-recall restore --snapshot" not in str(report)


@pytest.mark.parametrize("version", [1108, 1109])
def test_older_snapshot_retry_preserves_deleted_sources(tmp_path, monkeypatch, version):
    _, core, _ = _old_store(tmp_path, SCHEMA_VERSION)
    context = TrustedContext(core.storage.binding, "TEST-delete", core.storage.binding.scope_ids, "human_direct")
    scope = sorted(core.storage.binding.scope_ids)[0]
    item = core.record_event(context, source_event(content="TEST deleted original"), scope_id=scope).event_refs[0]
    core.record_event(
        context, source_event(source_event_key="TEST-delete-auth", content="delete " + item.ref), scope_id=scope
    )
    core.forget(
        context, dict(protocol_version="1.1", target_refs=[item.ref], mode="delete", expected_revisions={item.ref: 1})
    )
    downgrade_store(core.storage.path, version)
    before = _logical_content(core.storage.path)
    migrate = SQLiteStorage._migrate_store

    def fail(self, conn):
        conn.execute("CREATE TABLE test_failed_migration(value TEXT)")
        raise OSError(errno.ENOSPC, "TEST storage failure")

    monkeypatch.setattr(SQLiteStorage, "_migrate_store", fail)
    with pytest.raises(OSError):
        core.storage.initialize()
    assert _logical_content(core.storage.path) == before
    report = DoctorReport(host="codex", status="degraded")
    assert not check_storage(report, core.storage.binding, core.storage.path.parent)
    assert "retry" in str(report) and "reopen the host" in str(report)
    monkeypatch.setattr(SQLiteStorage, "_migrate_store", migrate)
    assert core.storage.initialize().schema_version == SCHEMA_VERSION
    assert core.source(context, item.ref, 1) is None


def test_automatic_open_snapshots_before_upgrading(tmp_path):
    project = tmp_path / "TEST-project"
    project.mkdir()
    _, core = install_codex_scope_recall(tmp_path / "TEST-install", project_root=project)
    downgrade_store(core.storage.path, 1111)
    context = TrustedContext(core.storage.binding, "TEST-upgrade", core.storage.binding.scope_ids, "host_generated")
    with core.storage.write(context) as tx:
        assert tx.status().schema_version == SCHEMA_VERSION
    snapshots = list((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))
    assert len(snapshots) == 1
    with closing(sqlite3.connect(snapshots[0])) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1111
        assert db.execute("SELECT schema_version FROM instance_meta").fetchone()[0] == 1111


def test_current_store_initialization_is_not_reported_as_schema_upgrade(tmp_path):
    _, core, _ = _old_store(tmp_path)
    core.storage.initialize()
    context = TrustedContext(core.storage.binding, "TEST-write", core.storage.binding.scope_ids, "host_generated")

    def write():
        with core.storage.write(context) as tx:
            return tx.status().schema_version

    with advisory_file_lock(core.storage.path.parent / store_upgrade.UPGRADE_LOCK), ThreadPoolExecutor(1) as pool:
        assert pool.submit(write).result(timeout=5) == SCHEMA_VERSION


def _logical_content(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0], tuple(conn.iterdump())


def _old_store(root, version=1111):
    project = root / "TEST-project"
    project.mkdir()
    config, core = install_codex_scope_recall(root / "TEST-install", project_root=project)
    downgrade_store(core.storage.path, version)
    return config, core, project


@pytest.mark.parametrize("failure", ["exception", "disk_full"])
@pytest.mark.parametrize("phase", ["snapshot", "migration"])
@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_failed_upgrade_rolls_back_and_next_open_retries(tmp_path, monkeypatch, failure, phase, journal):
    _, core, _ = _old_store(tmp_path)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute(f"PRAGMA journal_mode={journal}")
    before = _logical_content(core.storage.path)
    migrate, backup = SQLiteStorage._migrate_store, store_upgrade.backup_sqlite

    def broken_step(self, conn):
        conn.execute("CREATE TABLE test_uncommitted(value TEXT)")
        if failure == "disk_full":
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
            conn.execute(f"PRAGMA max_page_count={pages}")
            conn.execute("INSERT INTO test_uncommitted VALUES (zeroblob(8388608))")
            pytest.fail("SQLite did not enforce the synthetic disk limit")
        raise RuntimeError("TEST private source content must not be recorded")

    def broken_snapshot(source, destination, **kwargs):
        backup_primitives.create_output(destination)
        if failure == "disk_full":
            raise OSError(errno.ENOSPC, "TEST disk full")
        raise RuntimeError("TEST private source content must not be recorded")

    monkeypatch.setattr(SQLiteStorage, "_migrate_store", broken_step)
    if phase == "snapshot":
        monkeypatch.setattr(store_upgrade, "backup_sqlite", broken_snapshot)
    with pytest.raises((RuntimeError, OSError, sqlite3.Error)):
        core.storage.initialize()
    assert _logical_content(core.storage.path) == before
    state = store_upgrade.read_upgrade_state(core.storage.path.parent)
    assert state["status"] == "failed"
    assert Path(state["backup"]).exists() == (phase == "migration")
    if failure == "disk_full":
        assert state["reason"] == ("OSError:errno=28" if phase == "snapshot" else "SQLITE_FULL")
    assert state["phase"] == phase
    assert "private source content" not in json.dumps(state)
    report = DoctorReport(host="codex", status="degraded")
    assert not check_storage(report, core.storage.binding, core.storage.path.parent)
    assert "schema_upgrade_failed" in report.capability_gaps
    assert state["reason"] in str(report) and "retry" in str(report) and "restore --snapshot" not in str(report)
    monkeypatch.setattr(SQLiteStorage, "_migrate_store", migrate)
    monkeypatch.setattr(store_upgrade, "backup_sqlite", backup)
    assert core.storage.initialize().schema_version == SCHEMA_VERSION
    assert not list(core.storage.path.parent.glob(".store-upgrade-*.sqlite3"))
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='test_uncommitted'").fetchall()


def test_snapshot_includes_committed_wal_pages(tmp_path):
    _, core, _ = _old_store(tmp_path)
    with closing(sqlite3.connect(core.storage.path)) as keeper:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("CREATE TABLE test_wal_truth(value TEXT)")
        keeper.execute("INSERT INTO test_wal_truth VALUES ('TEST WAL truth')")
        keeper.commit()
        assert Path(str(core.storage.path) + "-wal").stat().st_size > 0
        core.storage.initialize()
    snapshot = next((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))
    with closing(sqlite3.connect(snapshot)) as conn:
        assert conn.execute("SELECT value FROM test_wal_truth").fetchone()[0] == "TEST WAL truth"


def test_registered_test_only_next_schema_upgrades_on_open(tmp_path, monkeypatch):
    _, core, _ = _old_store(tmp_path, SCHEMA_VERSION)

    def advance(conn):
        conn.execute("UPDATE instance_meta SET schema_version=?", (SCHEMA_VERSION + 1,))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    monkeypatch.setattr(storage, "SCHEMA_MIGRATIONS", {**storage.SCHEMA_MIGRATIONS, SCHEMA_VERSION: advance})
    monkeypatch.setattr(storage, "UPGRADE_CHAIN", (*storage.UPGRADE_CHAIN, SCHEMA_VERSION))
    monkeypatch.setattr(storage, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    context = TrustedContext(core.storage.binding, "TEST-upgrade", core.storage.binding.scope_ids, "host_generated")
    with core.storage.write(context) as tx:
        assert tx.status().schema_version == SCHEMA_VERSION + 1
    assert len(list((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))) == 1


def test_restore_from_automatic_upgrade_snapshot(tmp_path, monkeypatch):
    from scope_recall.core import restore as core_restore
    from scope_recall.maintenance import restore

    _, core, _ = _old_store(tmp_path, SCHEMA_VERSION)
    context = TrustedContext(core.storage.binding, "TEST-restore", core.storage.binding.scope_ids, "human_direct")
    event = source_event(content="TEST original memory survives snapshot restore")
    captured = core.record_event(context, event, scope_id=sorted(core.storage.binding.scope_ids)[0])

    def advance(conn):
        conn.execute("UPDATE instance_meta SET schema_version=?", (SCHEMA_VERSION + 1,))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    monkeypatch.setattr(storage, "SCHEMA_MIGRATIONS", {**storage.SCHEMA_MIGRATIONS, SCHEMA_VERSION: advance})
    monkeypatch.setattr(storage, "UPGRADE_CHAIN", (*storage.UPGRADE_CHAIN, SCHEMA_VERSION))
    monkeypatch.setattr(storage, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setattr(restore, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setattr(core_restore, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    with core.storage.write(context):
        pass
    snapshot = next((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))
    result = apply_restore(
        core.storage.binding, snapshot, backup_dir=tmp_path / "TEST-restore-backup", now=datetime.now(UTC)
    )
    assert result["status"] == "prepared", result
    monkeypatch.undo()
    result = apply_restore(core.storage.binding, snapshot, backup_dir=None, now=datetime.now(UTC))
    assert result["status"] == "restored", result
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert core.storage.initialize().schema_version == SCHEMA_VERSION
    restored = core.source(context, captured.event_refs[0].ref, 1)
    assert restored.event["content"] == event["content"]


# Child imports bind this checkout explicitly; no installed plugin or host is used.
_PROBE = r"""
import sys, types
from pathlib import Path
package = types.ModuleType("scope_recall")
package.__path__ = [sys.argv[1]]
sys.modules["scope_recall"] = package
from scope_recall.adapters.clients.config import CONFIG_FILENAME, load_codex_config
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core import store_upgrade
config = load_codex_config(Path(sys.argv[2]) / CONFIG_FILENAME)
storage = SQLiteStorage(config.to_binding(), timeout_seconds=5)
if sys.argv[3] == "hold":
    original = SQLiteStorage._migrate_store
    def held(self, conn):
        conn.execute("PRAGMA cache_size=5")
        conn.execute("CREATE TABLE test_crash(value BLOB)")
        conn.executemany("INSERT INTO test_crash VALUES (zeroblob(8192))", [()] * 128)
        print("ready", flush=True)
        sys.stdin.readline()
        conn.execute("DROP TABLE test_crash")
        original(self, conn)
    SQLiteStorage._migrate_store = held
elif sys.argv[3] == "snapshot":
    original = store_upgrade.backup_sqlite
    def held(source, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.touch()
        print("ready", flush=True)
        sys.stdin.readline()
        raise RuntimeError("TEST interrupted snapshot")
    store_upgrade.backup_sqlite = held
print(storage.initialize().schema_version, flush=True)
"""


def _child(root, mode):
    return subprocess.Popen(
        [sys.executable, "-B", "-X", "utf8", "-c", _PROBE, str(Path(__file__).parents[2]), str(root), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize("journal", ["delete", "wal"])
@pytest.mark.parametrize("phase", ["snapshot", "hold"])
def test_process_death_leaves_original_store_and_retry(tmp_path, journal, phase):
    _, core, _ = _old_store(tmp_path)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute(f"PRAGMA journal_mode={journal}")
    before = _logical_content(core.storage.path)
    child = _child(tmp_path / "TEST-install", phase)
    try:
        assert child.stdout.readline().strip() == "ready", child.stderr.read() if child.poll() is not None else ""
        child.kill()
        # Termination is asynchronous on Windows; keep stdin open until the child is gone.
        child.wait(timeout=10)
        child.communicate(timeout=10)
        # Opening a writable pager performs SQLite's hot-journal crash recovery.
        with closing(connect_truth_database(core.storage.path, mode="rw")) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1111
        assert _logical_content(core.storage.path) == before
        state = store_upgrade.read_upgrade_state(core.storage.path.parent)
        assert state["status"] == "interrupted" and state["snapshot_verified"] == (phase == "hold"), state
        report = DoctorReport(host="codex", status="degraded")
        assert not check_storage(report, core.storage.binding, core.storage.path.parent)
        assert "process_interrupted" in str(report)
        assert core.storage.initialize().schema_version == SCHEMA_VERSION
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_three_process_opens_and_real_hook_during_upgrade(tmp_path):
    config, core, project = _old_store(tmp_path)
    first = _child(tmp_path / "TEST-install", "hold")
    peers = []
    try:
        assert first.stdout.readline().strip() == "ready", first.stderr.read() if first.poll() is not None else ""
        peers = [_child(tmp_path / "TEST-install", "open") for _ in range(2)]
        started = time.monotonic()
        handler = CodexHookHandler(config, core=core)
        result = handler.handle_payload(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "TEST-upgrade-hook",
                "turn_id": "TEST-turn",
                "prompt_id": "TEST-prompt",
                "cwd": str(project),
                "prompt": "What did we decide for TEST-project?",
            }
        )
        assert result == {}, result
        assert time.monotonic() - started < 1.0
        assert "SCHEMA_UNSUPPORTED" in str(handler.diagnostics)
        with pytest.raises(ContractError) as refused:
            with core.storage.read(
                TrustedContext(core.storage.binding, "TEST-read", core.storage.binding.scope_ids, "host_generated")
            ):
                pass
        assert refused.value.field == "upgrade_in_progress"
        first.stdin.write("continue\n")
        first.stdin.flush()
        assert first.communicate(timeout=15)[0].strip() == str(SCHEMA_VERSION)
        for child in peers:
            out, err = child.communicate(timeout=15)
            assert child.returncode == 0 and out.strip() == str(SCHEMA_VERSION), (out, err)
        assert len(list((core.storage.path.parent / "upgrade-snapshots").glob("*.sqlite3"))) == 1
    finally:
        for child in [first, *peers]:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
