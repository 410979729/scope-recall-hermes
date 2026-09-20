"""The upgrade-store command brings one store forward now, with a snapshot first, and never
takes a running worker's store from it."""
import json
import sqlite3

from scope_recall.adapters.codex.config import install_codex_scope_recall
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.writer_lease import TruthWriterLease
from scope_recall.maintenance import cli
from v11_support import downgrade_store


def _run(capsys, arguments):
    code = cli.main(arguments)
    return code, json.loads(capsys.readouterr().out)


def test_upgrade_store_takes_a_snapshot_and_brings_the_store_forward(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1108)
    backups = tmp_path / "backups"
    code, out = _run(capsys, ["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "install"),
                              "--backup-dir", str(backups)])
    assert code == 0, out
    assert (out["status"], out["schema_before"], out["schema_after"]) == ("upgraded", 1108, SCHEMA_VERSION)
    assert out["journal_mode"] == "wal" and out["seconds"] >= 0
    snapshot = next(backups.glob("memory-1108-*.sqlite3"))
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1108, "the snapshot is the store as it was"
    assert snapshot.with_suffix(".json").is_file()
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    code, out = _run(capsys, ["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "install")])
    assert code == 0 and out["status"] == "current" and out["schema_before"] == SCHEMA_VERSION


def test_upgrade_store_waits_for_the_worker_and_leaves_a_held_store_alone(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "install", project_root=project)
    downgrade_store(core.storage.path, 1108)
    holder = sqlite3.connect(core.storage.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        code, out = _run(capsys, ["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "install"),
                                  "--backup-dir", str(tmp_path / "backups"), "--wait-seconds", "0.5"])
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
    code, out = _run(capsys, ["upgrade-store", "--host", "codex", "--instance-root", str(tmp_path / "install"),
                              "--backup-dir", str(tmp_path / "backups"), "--wait-seconds", "0.5"])

    assert code == 0, out
    assert attempts >= 2
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
