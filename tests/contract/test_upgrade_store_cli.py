"""The upgrade-store command brings one store forward now, with a snapshot first, and never
takes a running worker's store from it."""

import json
import sqlite3
import time

import pytest
from scope_recall.adapters.clients.config import install_codex_scope_recall
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.writer_lease import TruthWriterLease, read_truth_writer_owner, truth_writer_process_snapshot
from scope_recall.maintenance import cli
from scope_recall.maintenance.install import InstallError
from scope_recall.runtime import backup_primitives
from v11_support import downgrade_store


def _run(capsys, arguments):
    code = cli.main(arguments)
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("wait", [30, 0])
def test_upgrade_store_supplies_a_current_stores_missing_session_index(tmp_path, capsys, wait):
    project = tmp_path / "TEST-project"
    project.mkdir()
    instance = tmp_path / "TEST-install"
    _config, core = install_codex_scope_recall(instance, project_root=project)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("DROP INDEX source_session")
    arguments = ["upgrade-store", "--host", "codex", "--instance-root", str(instance), "--wait-seconds", str(wait)]
    for expected in ("added", "present"):
        code, out = _run(capsys, arguments)
        assert code == 0 and out["status"] == "current", out
        assert out["session_index"] == expected and "backup" not in out
        with sqlite3.connect(core.storage.path) as conn:
            assert conn.execute("PRAGMA index_info(source_session)").fetchall()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize("wait", ["nan", "inf", "-inf"])
def test_upgrade_store_refuses_nonfinite_wait_before_acquiring_a_lease(tmp_path, capsys, monkeypatch, wait):
    def unexpected_lease(*args):
        pytest.fail("invalid wait reached the lease loop")

    monkeypatch.setattr(cli, "_wait_for_upgrade_lease", unexpected_lease)
    code, out = _run(
        capsys,
        ["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path), f"--wait-seconds={wait}"],
    )
    assert code == 2 and out == {"status": "error", "error": "wait_seconds must be finite"}


@pytest.mark.parametrize("wait", [float("nan"), float("inf"), float("-inf")])
def test_upgrade_lease_refuses_nonfinite_wait_before_its_first_attempt(tmp_path, monkeypatch, wait):
    def unexpected_acquire(lease):
        pytest.fail("invalid wait reached lease acquisition")

    monkeypatch.setattr(TruthWriterLease, "acquire", unexpected_acquire)
    with pytest.raises(InstallError, match="wait_seconds must be finite"):
        cli._wait_for_upgrade_lease(tmp_path, wait)


def test_upgrade_store_lease_identifies_the_operation(tmp_path):
    lease = cli._wait_for_upgrade_lease(tmp_path, 0)
    try:
        assert read_truth_writer_owner(tmp_path) == {"role": "upgrade_store"}
    finally:
        lease.release()


def test_upgrade_store_refuses_a_snapshot_migrated_during_the_copy(tmp_path, capsys, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1111)
    # More than one backup step lets another connection commit between copied pages.
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("CREATE TABLE synthetic_padding(body BLOB)")
        conn.executemany("INSERT INTO synthetic_padding VALUES (zeroblob(4096))", [()] * 350)
    migrated = False

    def migrate_between_steps():
        nonlocal migrated
        if not migrated:
            migrated = True
            core.storage.initialize()

    monkeypatch.setattr(backup_primitives, "check_backup_deadline", migrate_between_steps)
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )
    assert migrated
    assert code == 2 and (out["status"], out["error"]) == ("not_upgraded", "backup_schema_mismatch"), out
    assert out["schema_before"] == 1111 and out["backup_schema"] == SCHEMA_VERSION
    with sqlite3.connect(out["backup"]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT schema_version FROM instance_meta").fetchone()[0] == SCHEMA_VERSION


def test_upgrade_store_takes_a_snapshot_and_brings_the_store_forward(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1108)
    backups = tmp_path / "backups"
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(backups),
        ],
    )
    assert code == 0, out
    assert (out["status"], out["schema_before"], out["schema_after"]) == ("upgraded", 1108, SCHEMA_VERSION)
    assert out["journal_mode"] == "wal" and out["seconds"] >= 0
    assert out["backup_schema"] == out["backup_recorded_schema"] == 1108
    snapshot = next(backups.glob("memory-1108-*.sqlite3"))
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1108, "the snapshot is the store as it was"
        assert conn.execute("SELECT schema_version FROM instance_meta").fetchone()[0] == 1108
    assert snapshot.with_suffix(".json").is_file()
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    code, out = _run(capsys, ["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "install")])
    assert code == 0 and out["status"] == "current" and out["schema_before"] == SCHEMA_VERSION
    assert "backup" not in out


def test_upgrade_store_waits_for_the_worker_and_leaves_a_held_store_alone(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1108)
    holder = sqlite3.connect(core.storage.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        code, out = _run(
            capsys,
            [
                "upgrade-store",
                "--host",
                "codex",
                "--instance-root",
                str(tmp_path / "install"),
                "--backup-dir",
                str(tmp_path / "backups"),
                "--wait-seconds",
                "0.5",
            ],
        )
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert code == 2 and (out["status"], out["error"]) == ("not_upgraded", "store_busy")
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1108


def test_upgrade_store_retries_the_worker_lease_until_it_is_released(tmp_path, capsys, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1108)
    acquire = TruthWriterLease.acquire
    attempts = 0

    def acquire_after_worker_release(lease):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"status": "busy", "scope": "cross_process", "owner": {"role": "worker"}}
        return acquire(lease)

    monkeypatch.setattr(TruthWriterLease, "acquire", acquire_after_worker_release)
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--wait-seconds",
            "0.5",
        ],
    )

    assert code == 0, out
    assert attempts >= 2
    assert (out["status"], out["schema_before"], out["schema_after"]) == ("upgraded", 1108, SCHEMA_VERSION)


def test_upgrade_store_asks_storage_for_no_more_than_its_wait_when_the_clock_has_not_ticked(
    tmp_path, capsys, monkeypatch
):
    """Before Python 3.13 ``time.monotonic()`` ticks every 15.6 ms on Windows, so the loop's first
    reading is the one the deadline was built from, and ``(started + 30.0) - started`` is
    30.000000000000014 at this clock value.  Storage refuses a timeout above 30, so the command
    took its snapshot and answered INPUT_INVALID / storage_timeout: one CI run in some dozens,
    and any operator whose machine had been up for that long."""
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1108)
    standing = 100.00000000000004
    assert (standing + 30.0) - standing > 30.0
    monkeypatch.setattr(time, "monotonic", lambda: standing)

    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )

    assert code == 0, out
    assert (out["status"], out["schema_before"], out["schema_after"]) == ("upgraded", 1108, SCHEMA_VERSION)


def test_upgrade_store_requires_the_promised_snapshot_before_mutation(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1108)

    code, out = _run(capsys, ["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "install")])

    assert code == 2 and (out["status"], out["error"]) == ("not_upgraded", "backup_required")
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1108


def _stamp_header(path, version, *, application_id=None):
    """What a 2.0 process does to a migrated store it opens: its own layout's header stamp."""
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version={version}")
        if application_id is not None:
            conn.execute(f"PRAGMA application_id={application_id}")


@pytest.mark.parametrize("index_present", [False, True])
def test_a_header_a_2_0_process_overwrote_is_named_and_restamped(tmp_path, capsys, index_present):
    """Every table and row is 3.x and instance_meta records the schema, but the header says
    10815, so every open would fail closed and upgrade-store would call the store unsupported."""
    import pytest
    from scope_recall.contracts import ContractError
    from scope_recall.core.storage import SQLiteStorage
    from scope_recall.maintenance.doctor import run_doctor

    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    if not index_present:
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("DROP INDEX source_session")
    _stamp_header(core.storage.path, 10815)
    with pytest.raises(ContractError) as refused:
        SQLiteStorage(core.storage.binding).initialize()
    assert (refused.value.code, refused.value.field) == ("SCHEMA_UNSUPPORTED", "header_stale:run_upgrade_store")
    report = run_doctor(host="codex", instance_root=tmp_path / "install")
    assert "schema_header_stale" in report.capability_gaps and report.schema_version == 10815
    root = ["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "install")]
    code, out = _run(capsys, root)
    assert code == 2 and out["error"] == "backup_required"
    code, out = _run(capsys, [*root, "--backup-dir", str(tmp_path / "backups")])
    assert code == 0, out
    assert (out["status"], out["schema_after"]) == ("restamped", SCHEMA_VERSION)
    assert out["session_index"] == ("present" if index_present else "added")
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("PRAGMA index_info(source_session)").fetchall()
    assert (out["header_restamped"]["from"], out["header_restamped"]["to"]) == (10815, SCHEMA_VERSION)
    assert "tables_not_in_schema" not in out, "a store holding only its own tables names none"
    with sqlite3.connect(next((tmp_path / "backups").glob("memory-10815-*.sqlite3"))) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10815, "the snapshot is the store as it was"
    assert _run(capsys, root)[1]["status"] == "current"


def test_a_restamped_older_store_is_then_brought_forward(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1108)
    _stamp_header(core.storage.path, 10815)
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )
    assert code == 0, out
    assert out["header_restamped"]["to"] == 1108
    assert (out["status"], out["schema_after"]) == ("upgraded", SCHEMA_VERSION)


def test_a_header_that_is_not_ours_is_still_refused(tmp_path, capsys):
    """Only this product's store, recording a schema this release knows, is restamped."""
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    _stamp_header(core.storage.path, 10815, application_id=0)
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )
    assert code == 2 and (out["status"], out["error"]) == ("unsupported", "schema_not_in_upgrade_chain")
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10815


def test_rows_the_2_0_process_wrote_into_its_own_tables_are_named_not_passed_over(tmp_path, capsys):
    """The process that stamped the header may also have captured turns into its own tables.  They
    are not part of this store; a restamp that said only "restamped" would leave them unseen."""
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT)")
        conn.executemany("INSERT INTO memories(content) VALUES (?)", [("TEST one",), ("TEST two",)])
    _stamp_header(core.storage.path, 10815)
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )
    assert code == 0 and out["status"] == "restamped", out
    assert out["tables_not_in_schema"] == {"memories": 2} and "another program's" in out["warning"]


def test_a_header_in_this_products_own_numbering_is_never_restamped(tmp_path, capsys):
    """Only a 2.x layout's stamp (10000 and up) is another program's; a 3.x number under a newer
    record would be restamped backwards, so such a store stays refused as it was."""
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    _stamp_header(core.storage.path, SCHEMA_VERSION + 1)
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )
    assert code == 2 and "header_restamped" not in out, out
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


def test_a_table_list_that_cannot_be_read_never_costs_the_restamp_its_result(tmp_path, capsys, monkeypatch):
    """The restamp has committed and the snapshot exists: the result naming it is still printed."""
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    _stamp_header(core.storage.path, 10815)

    def locked(database):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "_tables_not_in_schema", locked)
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )
    assert code == 0 and out["status"] == "restamped" and out["backup"]
    assert out["tables_not_in_schema_error"] == "OperationalError: database is locked" and out["warning"]


@pytest.mark.parametrize("marker", ["header", "metadata"])
def test_upgrade_store_checks_both_snapshot_markers_before_writing(tmp_path, capsys, monkeypatch, marker):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1111)
    original = core.storage.path.read_bytes()
    backup = cli.backup_sqlite

    def inconsistent_copy(source, snapshot, **kwargs):
        receipt = backup(source, snapshot, **kwargs)
        with sqlite3.connect(snapshot) as conn:
            if marker == "header":
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            else:
                conn.execute("UPDATE instance_meta SET schema_version=?", (SCHEMA_VERSION,))
        return receipt

    monkeypatch.setattr(cli, "backup_sqlite", inconsistent_copy)
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )
    assert code == 2 and out["error"] == "backup_schema_mismatch", out
    assert core.storage.path.read_bytes() == original
    with sqlite3.connect(out["backup"]) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert truth_writer_process_snapshot(core.storage.path.parent) == {
        "same_process_holder_count": 0,
        "connection_pin_count": 0,
    }


def test_upgrade_store_reads_the_schema_after_obtaining_the_lease(tmp_path, capsys, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1111)
    acquire = TruthWriterLease.acquire
    migrated = False

    def migrate_before_acquiring(lease):
        nonlocal migrated
        if not migrated:
            migrated = True
            core.storage.initialize()
        return acquire(lease)

    monkeypatch.setattr(TruthWriterLease, "acquire", migrate_before_acquiring)
    backups = tmp_path / "backups"
    code, out = _run(
        capsys,
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(tmp_path / "install"),
            "--backup-dir",
            str(backups),
        ],
    )
    assert code == 0 and out["status"] == "current" and out["schema_before"] == SCHEMA_VERSION, out
    assert "backup" not in out and not backups.exists()
