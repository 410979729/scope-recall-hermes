"""Going back to a backup with ``scope-recall restore``: what was forgotten since stays forgotten.

Each store is a TEST installation in the test's own folder, with synthetic messages only.
"""

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from scope_recall.adapters.clients import transcript
from scope_recall.adapters.clients.config import install_codex_scope_recall
from scope_recall.adapters.hermes.shared_entries import read_shared_payload
from scope_recall.contracts import ContractError, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core import restore as core_restore
from scope_recall.core.restore import InstallationMaintenance, begin_restore, export_deletion_ledger, ledger_digest
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.writer_lease import TruthWriterLease
from scope_recall.maintenance import cli, restore
from scope_recall.maintenance.doctor import run_doctor
from scope_recall.maintenance.shared import main as shared_main
from scope_recall.maintenance.shared import store_binding
from v11_support import downgrade_store, source_event

SECRET = "TEST-forgotten the door code is TEST-4711"


@dataclass
class Store:
    core: MemoryCore
    ctx: TrustedContext
    scope: str
    home: Path
    tmp: Path
    said: int = 0

    @property
    def data(self) -> Path:
        return self.ctx.binding.data_directory

    def say(self, text):
        self.said += 1
        saved = self.core.record_event(
            self.ctx,
            source_event(source_event_key=f"TEST-restore/{self.said}", content=text),
            scope_id=self.scope,
            remaining_seconds=10,
        )
        return self.core.source(self.ctx, saved.event_refs[0].ref, 1)

    def forget(self, source):
        self.say(f"删除 {source.ref}")
        request = dict(
            protocol_version="1.1",
            target_refs=[source.ref],
            mode="delete",
            expected_revisions={source.ref: source.revision},
        )
        return self.core.forget(self.ctx, request, remaining_seconds=10)

    def words(self, text):
        """Whether any of the store's files holds ``text``."""
        found = False
        for name in ("memory.sqlite3", "memory.sqlite3-wal", "memory.sqlite3-shm"):
            path = self.data / name
            found = found or (path.is_file() and text.encode() in path.read_bytes())
        return found

    def target(self):
        return ["--host", "codex", "--instance-root", str(self.home)]


@pytest.fixture
def store(tmp_path):
    project = tmp_path / "TEST-project"
    project.mkdir()
    config, core = install_codex_scope_recall(tmp_path / "TEST-install", project_root=project)
    scope = config.audience_scopes["owner_private"]
    ctx = TrustedContext(config.to_binding(), "TEST-session", frozenset({scope}), "human_direct")
    return Store(core, ctx, scope, tmp_path / "TEST-install", tmp_path)


def run(capsys, *arguments):
    code = cli.main(list(arguments))
    return code, json.loads(capsys.readouterr().out)


def backup(capsys, store, name):
    output = store.tmp / "TEST-snapshots" / f"{name}.sqlite3"
    code, out = run(capsys, "backup", "--database", str(store.data / "memory.sqlite3"), "--output", str(output))
    assert code == 0 and out["quick_check"] == "ok", out
    return output


def fenced(store):
    """Whether every normal open of the store refuses, as it does while a restore is pending."""
    try:
        store.core.status(store.ctx)
    except ContractError as exc:
        return exc.code == "RESTORE_UNVERIFIED"
    return False


def test_going_back_to_a_backup_keeps_what_was_forgotten_since(store, capsys):
    kept = store.say("TEST-kept the meeting moved to Thursday")
    secret = store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-before-forget")
    store.forget(secret)
    later = store.say("TEST-later a message said after the backup")
    assert store.core.source(store.ctx, secret.ref, 1) is None

    before = (store.data / "memory.sqlite3").read_bytes()
    code, plan = run(capsys, "restore", *store.target(), "--snapshot", str(snapshot))
    assert code == 0 and (plan["status"], plan["action"], plan["restore_pending"]) == ("planned", "restore", False)
    assert plan["backup_required"] and (store.data / "memory.sqlite3").read_bytes() == before

    code, done = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(store.tmp / "TEST-kept"),
        "--apply",
    )
    assert code == 0, done
    assert (done["status"], done["restore_pending"], done["wal_truncated"], done["purges_queued"]) == (
        "restored",
        False,
        True,
        1,
    )
    assert store.core.source(store.ctx, secret.ref, 1) is None
    assert not store.core.search_sources(store.ctx, "TEST-4711", history=True)
    assert not store.words(SECRET), "the snapshot's copy of the forgotten words left the store's files"
    assert store.core.source(store.ctx, kept.ref, 1) is not None, "what the backup held and nobody forgot is back"
    assert store.core.source(store.ctx, later.ref, 1) is None, "what came after the backup is not"
    assert not (store.data / restore.LEDGER_FILENAME).exists()
    with closing(sqlite3.connect(done["backup"]["path"])) as kept_copy:
        assert kept_copy.execute("PRAGMA quick_check").fetchone()[0] == "ok", "the store as it was is kept"
    with closing(sqlite3.connect(store.data / "memory.sqlite3")) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_store_closed_for_a_restore_without_a_ledger_beside_it_is_reopened(store, capsys):
    """``rollback --apply`` closed a store holding sources this way and kept no ledger: every host refused it and no
    command opened it again.  ``restore`` reads the ledger through the fence from the store, which nothing replaced,
    and finishes; given a snapshot of the closed store itself, the store reopens as it was."""
    secret = store.say(SECRET)
    store.forget(secret)
    kept = store.say("TEST-kept a message the store holds")
    authority = InstallationMaintenance(
        TrustedContext(store.ctx.binding, "TEST-closer", store.ctx.binding.scope_ids, "host_generated")
    )
    ledger = export_deletion_ledger(store.core.storage, authority)
    begin_restore(store.core.storage, authority, expected_ledger_sha256=ledger_digest(ledger))
    assert fenced(store)
    itself = backup(capsys, store, "TEST-closed-store")

    code, done = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(itself),
        "--backup-dir",
        str(store.tmp / "TEST-kept"),
        "--apply",
    )
    assert code == 0 and (done["status"], done["ledger_kept"], done["restore_pending"]) == ("restored", False, False)
    assert not fenced(store)
    assert store.core.source(store.ctx, kept.ref, 1) is not None
    assert store.core.source(store.ctx, secret.ref, 1) is None


class _CopyCutShort:
    """The snapshot's connection, whose copy over the store stops after its first page, as a crash would stop it."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def backup(self, target):
        def stop(status, remaining, total):
            raise KeyboardInterrupt

        self._connection.backup(target, pages=1, progress=stop)


@pytest.mark.parametrize("stage", ["open", "copy", "replay"])
def test_a_run_cut_short_keeps_the_store_closed_and_the_next_run_finishes_it(store, capsys, monkeypatch, stage):
    secret = store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-before-forget")
    store.forget(secret)
    later = store.say("TEST-later a message said after the backup")
    arguments = ["restore", *store.target(), "--snapshot", str(snapshot)]
    actual = restore.connect_truth_database

    if stage == "open":

        def refusing(path, *args, **kwargs):
            if kwargs.get("mode") == "rw":
                raise sqlite3.OperationalError("TEST disk I/O error")
            return actual(path, *args, **kwargs)

        monkeypatch.setattr(restore, "connect_truth_database", refusing)
        code, out = run(capsys, *arguments, "--backup-dir", str(store.tmp / "TEST-kept"), "--apply")
        assert (code, out["error"], out["restore_pending"]) == (2, "OperationalError", True)
    else:
        if stage == "copy":

            def cut(path, *args, **kwargs):
                connection = actual(path, *args, **kwargs)
                return _CopyCutShort(connection) if kwargs.get("mode") == "ro" else connection

            monkeypatch.setattr(restore, "connect_truth_database", cut)
        else:

            def interrupted(*args, **kwargs):
                raise KeyboardInterrupt

            monkeypatch.setattr(restore, "replay_deletion_ledger", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cli.main([*arguments, "--backup-dir", str(store.tmp / "TEST-kept"), "--apply"])
        capsys.readouterr()
    monkeypatch.undo()
    assert fenced(store) and (store.data / restore.LEDGER_FILENAME).is_file()
    with closing(sqlite3.connect(f"{(store.data / 'memory.sqlite3').as_uri()}?mode=ro", uri=True)) as conn:
        held = conn.execute("SELECT count(*) FROM source_events WHERE event_id=?", (later.ref,)).fetchone()[0]
    assert held == (1 if stage != "replay" else 0), "a copy cut short leaves the store as it was"
    assert "restore_pending" in run_doctor(host="codex", instance_root=store.home).capability_gaps

    code, plan = run(capsys, *arguments)
    assert code == 0 and (plan["ledger_kept"], plan["backup_required"], plan["restore_pending"]) == (True, False, True)
    code, done = run(capsys, *arguments, "--apply")
    assert code == 0 and done["status"] == "restored" and "backup" not in done, done
    assert not fenced(store) and store.core.source(store.ctx, secret.ref, 1) is None
    assert not store.words(SECRET)
    assert "restore_pending" not in run_doctor(host="codex", instance_root=store.home).capability_gaps


def _another_store(capsys, store, tmp_path):
    project = tmp_path / "TEST-other-project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "TEST-other-install", project_root=project)
    output = tmp_path / "TEST-snapshots" / "TEST-another-store.sqlite3"
    assert cli.main(["backup", "--database", str(core.storage.path), "--output", str(output)]) == 0
    capsys.readouterr()
    return output


def _live_copy(capsys, store, tmp_path):
    copy = tmp_path / "TEST-live-copy.sqlite3"
    with closing(sqlite3.connect(store.data / "memory.sqlite3")) as source, closing(sqlite3.connect(copy)) as target:
        source.backup(target)
        target.execute("PRAGMA journal_mode=WAL")
    return copy


def _damaged(capsys, store, tmp_path):
    """A snapshot cut short, as a copy that stopped half way leaves one."""
    copy = backup(capsys, store, "TEST-damaged")
    data = copy.read_bytes()
    page = int.from_bytes(data[16:18], "big")
    copy.write_bytes(data[: len(data) // page // 2 * page])
    return copy


def _older(capsys, store, tmp_path):
    copy = backup(capsys, store, "TEST-older")
    downgrade_store(copy, 1109)
    return copy


@pytest.mark.parametrize(
    ("make", "error"),
    [
        (_another_store, "snapshot_identity"),
        (_live_copy, "snapshot_not_a_backup"),
        (_damaged, "snapshot_damaged"),
        (_older, "snapshot_schema"),
    ],
)
def test_a_snapshot_that_is_not_a_whole_backup_of_this_store_is_refused_before_anything_changes(
    store, capsys, tmp_path, make, error
):
    store.say(SECRET)
    snapshot = make(capsys, store, tmp_path)
    before = (store.data / "memory.sqlite3").read_bytes()
    code, out = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(tmp_path / "TEST-kept"),
        "--apply",
    )
    assert (code, out.get("error"), out["restore_pending"]) == (2, error, False), out
    assert (store.data / "memory.sqlite3").read_bytes() == before and not (tmp_path / "TEST-kept").exists()


def test_a_store_some_process_still_writes_or_holds_open_is_left_alone(store, capsys, monkeypatch):
    snapshot = backup(capsys, store, "TEST-snapshot")
    arguments = ["restore", *store.target(), "--snapshot", str(snapshot), "--backup-dir", str(store.tmp / "TEST-kept")]
    lease = TruthWriterLease(store.data, role="provider")
    assert lease.acquire()["status"] == "acquired"
    try:
        code, out = run(capsys, *arguments, "--apply")
    finally:
        lease.release()
    assert (code, out["error"], out["restore_pending"]) == (2, "store_busy", False)

    class Running:
        pid = os.getpid()
        host_adapter = "codex"

    monkeypatch.setattr(restore, "live_records", lambda data_directory: [Running()])
    code, out = run(capsys, *arguments, "--apply")
    assert (code, out["error"], out["restore_pending"]) == (2, "store_open", False)
    assert "codex" in out["hint"]

    monkeypatch.undo()
    code, out = run(capsys, "restore", *store.target(), "--snapshot", str(snapshot), "--apply")
    assert (code, out["error"], out["restore_pending"]) == (2, "backup_required", False)
    assert not (store.data / restore.LEDGER_FILENAME).exists()


def test_a_newer_release_prepares_and_the_release_that_reads_the_snapshot_finishes(store, capsys, monkeypatch):
    """Going back across a schema step: the release that wrote the latest store exports its ledger and closes it,
    and the release that reads the snapshot's schema replays that ledger over the snapshot without reading the store
    it cannot open.  Here the newer release is this one with its schema one step on, the store stamped with that
    schema before the older release finishes."""
    secret = store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-older-release")
    store.forget(secret)
    newer = SCHEMA_VERSION + 1
    monkeypatch.setattr(restore, "SCHEMA_VERSION", newer)
    monkeypatch.setattr(core_restore, "SCHEMA_VERSION", newer)
    arguments = ["restore", *store.target(), "--snapshot", str(snapshot)]
    code, plan = run(capsys, *arguments)
    assert code == 0 and plan["action"] == "prepare"
    code, prepared = run(capsys, *arguments, "--backup-dir", str(store.tmp / "TEST-kept"), "--apply")
    assert code == 0 and (prepared["status"], prepared["ledger_schema"], prepared["restore_pending"]) == (
        "prepared",
        newer,
        True,
    )
    assert fenced(store) and Path(prepared["backup"]["path"]).is_file()

    monkeypatch.undo()
    with closing(sqlite3.connect(store.data / "memory.sqlite3")) as conn:
        conn.execute("UPDATE instance_meta SET schema_version=? WHERE singleton=1", (newer,))
        conn.execute(f"PRAGMA user_version={newer}")
        conn.commit()
    code, done = run(capsys, *arguments, "--apply")
    assert code == 0 and (done["status"], done["ledger_kept"], done["ledger_schema"]) == ("restored", True, newer), done
    assert store.core.source(store.ctx, secret.ref, 1) is None and not store.words(SECRET)


def test_a_shared_store_is_restored_by_its_root(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(transcript, "workbuddy_projects", lambda: tmp_path / "TEST-workbuddy-projects")
    root = (tmp_path / "TEST-shared").resolve()
    assert shared_main(["init-shared", "--root", str(root), "--agent-id", "TEST-agent"]) == 0
    homes = {host: (tmp_path / f"TEST-{host}-home").resolve() for host in ("workbuddy", "dsh")}

    def attach(host, name, *grants):
        homes[host].mkdir(exist_ok=True)
        argv = ["attach", "--host", host, "--instance-root", str(homes[host]), "--root", str(root)]
        assert shared_main([*argv, "--entry", host, "--display-name", name, *grants]) == 0

    attach("workbuddy", "WorkBuddy")
    capsys.readouterr()
    payload = read_shared_payload(root)
    binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
    ctx = TrustedContext(binding, "TEST-session", binding.scope_ids, "human_direct", entry_id="workbuddy")
    shared = Store(MemoryCore(CoreConfig(binding)), ctx, payload["scope_ids"][0], homes["workbuddy"], tmp_path)
    secret = shared.say(SECRET)
    snapshot = backup(capsys, shared, "TEST-shared-before-forget")
    shared.forget(secret)
    attach("dsh", "DeepSeek Harness", "--grants-like", "workbuddy", "--capture-like", "workbuddy")
    capsys.readouterr()
    arguments = [
        "restore",
        "--root",
        str(root),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(tmp_path / "TEST-kept"),
    ]

    entry = ["--host", "workbuddy", "--instance-root", str(homes["workbuddy"])]
    code, out = run(capsys, "restore", *entry, "--snapshot", str(snapshot), "--apply")
    assert (code, out["error"]) == (2, "shared_store_entry")
    code, out = run(capsys, *arguments, "--apply")
    assert (code, out["error"], out["restore_pending"]) == (2, "snapshot_entries", False), out
    assert "dsh" in out["hint"]
    assert shared_main(["detach", "--instance-root", str(homes["dsh"])]) == 0
    capsys.readouterr()
    code, done = run(capsys, *arguments, "--apply")
    assert code == 0 and (done["status"], done["installation_kind"]) == ("restored", "shared"), done
    assert shared.core.source(ctx, secret.ref, 1) is None and not shared.words(SECRET)
