"""Going back to a backup with ``scope-recall restore``: what was forgotten since stays forgotten.

Each store is a TEST installation in the test's own folder, with synthetic messages only.
"""

import hashlib
import itertools
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from scope_recall.adapters.clients import transcript
from scope_recall.adapters.clients.config import install_codex_scope_recall
from scope_recall.adapters.hermes.installation import build_installation_manifest
from scope_recall.adapters.hermes.shared_entries import attach_shared_record, read_shared_payload, shared_entry_record
from scope_recall.contracts import ContractError, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core import restore as core_restore
from scope_recall.core.delete_storage import group_digest
from scope_recall.core.restore import InstallationMaintenance, export_deletion_ledger, ledger_digest
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.writer_lease import TruthWriterLease
from scope_recall.maintenance import cli, restore
from scope_recall.maintenance.doctor import run_doctor
from scope_recall.maintenance.shared import init_shared, store_binding
from scope_recall.maintenance.shared import main as shared_main
from test_claims import Clock, capture, initial
from test_deletion import authorize, request
from v11_support import downgrade_store, recall_request, source_event

SECRET = "TEST-forgotten the door code is TEST-4711"
#: Beside the store while a restore is pending, by the names every release that reads the ledger format uses.
MARKER = "restore-required.json"
KEPT_LEDGER = "restore-ledger.json"


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


def test_restore_revalidates_a_warmed_structural_verdict(store, capsys, monkeypatch):
    from scope_recall.core import lexical_index

    kept = store.say("TEST kept amber palette")
    snapshot = backup(capsys, store, "TEST-structural-cache")
    store.say("TEST later silver palette")
    with store.core.storage.read(store.ctx):
        pass
    calls = []
    original = lexical_index.claim_backfill_pending

    def checked(conn):
        calls.append(1)
        return original(conn)

    monkeypatch.setattr(lexical_index, "claim_backfill_pending", checked)
    code, result = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(store.tmp / "TEST-restore-cache-backup"),
        "--apply",
    )
    assert code == 0 and result["status"] == "restored"
    assert store.core.source(store.ctx, kept.ref, 1) is not None
    assert calls


def test_restore_revokes_body_scan_receipts_from_the_snapshot(store, capsys):
    kept = store.say("TEST kept amber palette")
    assert kept.content_secret_scan is not None
    snapshot = backup(capsys, store, "TEST-receipt-revocation")
    code, result = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(store.tmp / "TEST-receipt-backup"),
        "--apply",
    )
    assert code == 0 and result["status"] == "restored"
    assert store.core.source(store.ctx, kept.ref, 1).content_secret_scan is None


def test_disaster_recovery_revokes_body_scan_receipts_before_reopening(store):
    kept = store.say("TEST kept amber palette")
    assert kept.content_secret_scan is not None
    authority = InstallationMaintenance(replace(store.ctx, allowed_scope_ids=store.ctx.binding.scope_ids))
    ledger = export_deletion_ledger(store.core.storage, authority)
    core_restore.begin_restore(
        store.core.storage, authority, expected_ledger_sha256=ledger_digest(ledger), ledger_format=ledger["format"]
    )
    core_restore.finish_disaster_recovery(store.core.storage, authority)
    assert store.core.source(store.ctx, kept.ref, 1).content_secret_scan is None


@pytest.mark.parametrize("session_index", [False, True])
def test_going_back_to_a_backup_keeps_what_was_forgotten_since(store, capsys, session_index):
    kept = store.say("TEST-kept the meeting moved to Thursday")
    secret = store.say(SECRET)
    if not session_index:
        with closing(sqlite3.connect(store.core.storage.path)) as conn:
            conn.execute("DROP INDEX source_session")
    snapshot = backup(capsys, store, "TEST-before-forget")
    with closing(sqlite3.connect(snapshot)) as conn:
        assert bool(conn.execute("PRAGMA index_info(source_session)").fetchall()) is session_index
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
    assert (done["status"], done["restore_pending"], done["purges_queued"]) == ("restored", False, 1)
    assert store.core.source(store.ctx, secret.ref, 1) is None
    assert not store.core.search_sources(store.ctx, "TEST-4711", history=True)
    assert not store.words(SECRET), "the snapshot's copy of the forgotten words left the store's files"
    assert store.core.source(store.ctx, kept.ref, 1) is not None, "what the backup held and nobody forgot is back"
    assert store.core.source(store.ctx, later.ref, 1) is None, "what came after the backup is not"
    assert SECRET not in (store.data / KEPT_LEDGER).read_text(encoding="utf-8"), "the ledger kept is content-free"
    with closing(sqlite3.connect(done["backup"]["path"])) as kept_copy:
        assert kept_copy.execute("PRAGMA quick_check").fetchone()[0] == "ok", "the store as it was is kept"
    with closing(sqlite3.connect(store.data / "memory.sqlite3")) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_store_closed_for_a_restore_without_a_ledger_beside_it_is_reopened(store, capsys):
    """A store closed for a restore with no ledger kept beside it is finished by ``restore``, which reads the ledger
    through the fence from the store nothing has replaced; given a snapshot of the closed store itself, the store
    opens again as it was."""
    secret = store.say(SECRET)
    store.forget(secret)
    kept = store.say("TEST-kept a message the store holds")
    authority = InstallationMaintenance(
        TrustedContext(store.ctx.binding, "TEST-closer", store.ctx.binding.scope_ids, "host_generated")
    )
    ledger = export_deletion_ledger(store.core.storage, authority)
    binding = store.ctx.binding
    marker = dict(
        installation_id=binding.installation_id,
        agent_id=binding.agent_id,
        data_directory=str(binding.data_directory),
        expected_ledger_sha256=ledger_digest(ledger),
    )
    (store.data / MARKER).write_text(json.dumps(marker), encoding="utf-8")
    assert fenced(store) and not (store.data / KEPT_LEDGER).exists()
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


@pytest.mark.parametrize("stage", ["open", "copy", "replay", "truncate"])
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

            if stage == "replay":
                monkeypatch.setattr(restore, "replay_deletion_ledger", interrupted)
            else:
                monkeypatch.setattr(SQLiteStorage, "truncate_wal", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cli.main([*arguments, "--backup-dir", str(store.tmp / "TEST-kept"), "--apply"])
        capsys.readouterr()
    monkeypatch.undo()
    assert fenced(store) and (store.data / KEPT_LEDGER).is_file()
    with closing(sqlite3.connect(f"{(store.data / 'memory.sqlite3').as_uri()}?mode=ro", uri=True)) as conn:
        held = conn.execute("SELECT count(*) FROM source_events WHERE event_id=?", (later.ref,)).fetchone()[0]
    assert held == (1 if stage in ("open", "copy") else 0), "a copy cut short leaves the store as it was"
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
    assert not (store.data / KEPT_LEDGER).exists()


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


@pytest.mark.parametrize("plan_only", [True, False])
def test_shared_snapshot_keeps_registered_scopes_outside_manifest(tmp_path, capsys, monkeypatch, plan_only):
    monkeypatch.setattr(transcript, "workbuddy_projects", lambda: tmp_path / "projects")
    root = (tmp_path / "shared").resolve()
    home = (tmp_path / "home").resolve()
    home.mkdir()
    assert shared_main(["init-shared", "--root", str(root), "--agent-id", "TEST-agent"]) == 0
    assert (
        shared_main(
            [
                "attach",
                "--host",
                "workbuddy",
                "--instance-root",
                str(home),
                "--root",
                str(root),
                "--entry",
                "workbuddy",
                "--display-name",
                "WorkBuddy",
            ]
        )
        == 0
    )
    capsys.readouterr()
    payload = read_shared_payload(root)
    binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
    extra = frozenset({"TEST-extra-one", "TEST-extra-two", "TEST-extra-three"})
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        conn.executemany("INSERT INTO instance_scopes(scope_id) VALUES (?)", [(scope,) for scope in extra])
    expanded = replace(binding, scope_ids=binding.scope_ids | extra)
    core = MemoryCore(CoreConfig(expanded))
    refs = []
    for scope in sorted(extra):
        ctx = TrustedContext(expanded, "TEST-session", frozenset({scope}), "human_direct", entry_id="workbuddy")
        saved = core.record_event(
            ctx,
            source_event(source_event_key=scope, content=f"Synthetic text {scope}"),
            scope_id=scope,
            remaining_seconds=10,
        )
        refs.append((ctx, saved.event_refs[0].ref, scope))
    shared = Store(core, refs[0][0], refs[0][2], home, tmp_path)
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        conn.execute("INSERT INTO instance_scopes(scope_id) VALUES (?)", ("TEST-snapshot-only",))
    snapshot = backup(capsys, shared, "before-upgrade")
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        conn.execute("DELETE FROM instance_scopes WHERE scope_id=?", ("TEST-snapshot-only",))
    for index, (ctx, ref, scope) in enumerate(refs[:2]):
        core.record_event(
            ctx,
            source_event(source_event_key=f"delete-{index}", content=f"delete {ref}"),
            scope_id=scope,
            remaining_seconds=10,
        )
        core.forget(
            ctx,
            {"protocol_version": "1.1", "mode": "delete", "target_refs": [ref], "expected_revisions": {ref: 1}},
            remaining_seconds=10,
        )
        assert core.source(ctx, ref, 1) is None
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        operations = conn.execute(
            "SELECT operation_id,scope_ids_json FROM deletion_operations ORDER BY operation_id"
        ).fetchall()
    assert len(operations) == 2 and all(set(json.loads(scopes)) <= extra for _, scopes in operations)
    newer = SCHEMA_VERSION + 1
    monkeypatch.setattr(restore, "SCHEMA_VERSION", newer)
    monkeypatch.setattr(core_restore, "SCHEMA_VERSION", newer)
    args = ["restore", "--root", str(root), "--snapshot", str(snapshot)]
    if plan_only:
        code, plan = run(capsys, *args)
        assert code == 0 and plan["status"] == "planned", plan
        return
    code, prepared = run(capsys, *args, "--backup-dir", str(tmp_path / "kept"), "--apply")
    assert code == 0 and prepared["status"] == "prepared", prepared
    monkeypatch.undo()
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        conn.execute("UPDATE instance_meta SET schema_version=? WHERE singleton=1", (newer,))
        conn.execute(f"PRAGMA user_version={newer}")
    replay = restore.replay_deletion_ledger
    monkeypatch.setattr(
        restore, "replay_deletion_ledger", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    with pytest.raises(KeyboardInterrupt):
        cli.main([*args, "--apply"])
    capsys.readouterr()
    monkeypatch.setattr(restore, "replay_deletion_ledger", replay)
    code, done = run(capsys, *args, "--apply")
    assert code == 0 and done["status"] == "restored", done
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        assert (
            conn.execute("SELECT operation_id,scope_ids_json FROM deletion_operations ORDER BY operation_id").fetchall()
            == operations
        )
    for ctx, ref, scope in refs[:2]:
        assert core.source(ctx, ref, 1) is None
        assert not shared.words(f"Synthetic text {scope}")
    for ctx, ref, scope in refs[2:]:
        found = core.source(ctx, ref, 1)
        assert found is not None and found.event["content"] == f"Synthetic text {scope}"


def test_shared_restore_resumes_with_withdrawal_and_suppression_in_registered_scope(tmp_path, capsys, monkeypatch):
    from scope_recall.core.source_storage import source_reference

    root = (tmp_path / "shared-protection").resolve()
    init_shared(root=root, agent_id="TEST-agent", test_mode=True)
    stamp = "2026-01-01T00:00:00Z"
    local = build_installation_manifest(tmp_path / "home", agent_id="TEST-agent", test_mode=True)
    attach_shared_record(
        root, shared_entry_record(local, entry_id="agent1", display_name="agent1", attached_at=stamp), now=stamp
    )
    payload = read_shared_payload(root)
    binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
    scope = "TEST-scope"
    assert scope not in binding.scope_ids
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        conn.execute("INSERT INTO instance_scopes(scope_id) VALUES (?)", (scope,))
    expanded = replace(binding, scope_ids=binding.scope_ids | {scope})
    ctx = TrustedContext(
        expanded,
        "TEST-session",
        frozenset({scope}),
        "human_direct",
        entry_id="agent1",
        project_id="TEST-project",
        branch_id="TEST-main",
    )
    core = MemoryCore(CoreConfig(expanded), clock=Clock())
    core.test_sequence = itertools.count(1)
    item, _ = initial(core, ctx)
    muted = capture(core, ctx, "TEST amberobject private note")
    assert any(source.ref == muted.ref for source in core.search_sources(ctx, "amberobject", automatic=True))
    group_key = "TEST-group-protection"
    first_key = "TEST-group-first"
    part = capture(
        core,
        ctx,
        "TEST hiddenfirst group words",
        key=first_key,
        segment=dict(group_key=group_key, index=0, total=2, truncated=False),
    )
    assert part is None
    with core.storage.read(ctx) as tx:
        group_part = tx.deletions.target(source_reference(expanded.installation_id, first_key))
    shared = Store(core, ctx, scope, tmp_path / "home", tmp_path)
    snapshot = backup(capsys, shared, "before-protection")
    capture(core, ctx, "撤回 TEST-project 配色蓝色。", when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx, item.ref) is None
    for target in (muted, group_part):
        authorize(core, ctx, target, mode="suppress")
        core.forget(ctx, request(target, mode="suppress"), remaining_seconds=10)
    digest = group_digest(expanded, scope, ctx.project_id, ctx.branch_id, group_key)
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        assert conn.execute(
            "SELECT suppressed FROM source_group_blocks WHERE group_sha256=?", (digest,)
        ).fetchone() == (1,)
    assert core.source(ctx, muted.ref, 1).suppressed
    args = ["restore", "--root", str(root), "--snapshot", str(snapshot), "--apply"]
    replay = restore.replay_deletion_ledger
    monkeypatch.setattr(
        restore, "replay_deletion_ledger", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    with pytest.raises(KeyboardInterrupt):
        cli.main([*args, "--backup-dir", str(tmp_path / "kept")])
    capsys.readouterr()
    assert (root / MARKER).is_file()
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        assert conn.execute("SELECT suppressed FROM source_events WHERE event_id=?", (muted.ref,)).fetchone() == (0,)
        assert conn.execute("SELECT 1 FROM source_group_blocks WHERE group_sha256=?", (digest,)).fetchone() is None
    monkeypatch.setattr(restore, "replay_deletion_ledger", replay)
    code, done = run(capsys, *args)
    assert code == 0 and done["status"] == "restored", done
    assert core.current_claim(ctx, item.ref) is None
    assert core.claim_history(ctx, item.ref)[-1].state == "retracted"
    assert core.source(ctx, muted.ref, 1).suppressed
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        assert conn.execute(
            "SELECT suppressed FROM source_group_blocks WHERE group_sha256=?", (digest,)
        ).fetchone() == (1,)
    second = capture(
        core,
        ctx,
        "TEST hiddensecond group words",
        key="TEST-group-second",
        segment=dict(group_key=group_key, index=1, total=2, truncated=False),
    )
    assert second is not None and second.suppressed
    for phrase, ref in (("amberobject", muted.ref), ("hiddensecond", second.ref)):
        assert not core.search_sources(ctx, phrase, automatic=True)
        packet = core.recall(ctx, recall_request(query=phrase), deadline_seconds=10)
        assert all(entry.ref != ref for entry in packet.items)


@pytest.mark.parametrize("scopes", [[], ["TEST-not-registered"]])
def test_pending_restore_rejects_ledger_scopes_outside_checked_registry(store, scopes):
    binding = store.ctx.binding
    authority = InstallationMaintenance(TrustedContext(binding, "restore", binding.scope_ids, "host_generated"))
    ledger = export_deletion_ledger(store.core.storage, authority)
    ledger["scope_ids"] = sorted(binding.scope_ids | set(scopes)) if scopes else []
    marker = dict(
        installation_id=binding.installation_id,
        agent_id=binding.agent_id,
        data_directory=str(binding.data_directory),
        expected_ledger_sha256=ledger_digest(ledger),
    )
    (store.data / KEPT_LEDGER).write_text(json.dumps(ledger), encoding="utf-8")
    (store.data / MARKER).write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(ContractError) as error:
        core_restore.pending_restore_scopes(store.core.storage, authority, minimum=binding.scope_ids)
    assert (error.value.code, error.value.field) == ("RESTORE_UNVERIFIED", "ledger_identity")


@pytest.mark.parametrize("missing", ["binding", "registered", "new_entry", "current_binding"])
def test_snapshot_missing_current_scopes_is_refused(tmp_path, capsys, monkeypatch, missing):
    monkeypatch.setattr(transcript, "workbuddy_projects", lambda: tmp_path / "projects")
    root = (tmp_path / "shared").resolve()
    home = (tmp_path / "home").resolve()
    home.mkdir()
    assert shared_main(["init-shared", "--root", str(root), "--agent-id", "TEST-agent"]) == 0
    assert (
        shared_main(
            [
                "attach",
                "--host",
                "workbuddy",
                "--instance-root",
                str(home),
                "--root",
                str(root),
                "--entry",
                "workbuddy",
                "--display-name",
                "WorkBuddy",
            ]
        )
        == 0
    )
    capsys.readouterr()
    payload = read_shared_payload(root)
    binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
    ctx = TrustedContext(binding, "TEST-session", binding.scope_ids, "human_direct", entry_id="workbuddy")
    shared = Store(MemoryCore(CoreConfig(binding)), ctx, payload["scope_ids"][0], home, tmp_path)
    snapshot = backup(capsys, shared, "before-new-scope")
    if missing == "binding":
        with sqlite3.connect(snapshot) as conn:
            conn.execute("DELETE FROM instance_scopes WHERE scope_id=?", (payload["scope_ids"][0],))
    elif missing == "registered":
        with sqlite3.connect(root / "memory.sqlite3") as conn:
            conn.execute("INSERT INTO instance_scopes(scope_id) VALUES (?)", ("TEST-later-scope",))
    elif missing == "current_binding":
        with sqlite3.connect(root / "memory.sqlite3") as conn:
            conn.execute("DELETE FROM instance_scopes WHERE scope_id=?", (payload["scope_ids"][0],))
    else:
        other = (tmp_path / "other").resolve()
        other.mkdir()
        stamp = "2026-01-01T00:00:00Z"
        local = build_installation_manifest(other, agent_id="TEST-agent", test_mode=True)
        record = shared_entry_record(local, entry_id="hermes", display_name="Hermes", attached_at=stamp)
        attach_shared_record(root, record, now=stamp)
        assert set(read_shared_payload(root)["scope_ids"]) - set(payload["scope_ids"])
    code, out = run(
        capsys,
        "restore",
        "--root",
        str(root),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(tmp_path / "kept"),
        "--apply",
    )
    expected = "IDENTITY_UNBOUND" if missing == "current_binding" else "snapshot_scopes"
    assert (code, out["error"], out["restore_pending"]) == (2, expected, False), out
    if missing == "current_binding":
        assert out["field"] == "scope_binding"
    assert not (tmp_path / "kept").exists()


def test_admission_stays_closed_until_the_log_that_held_the_snapshot_is_truncated(store, capsys, monkeypatch):
    """A reader still on an older snapshot holds the truncation back: the copy put the snapshot's pages, forgotten
    words included, into ``memory.sqlite3-wal``, so the store stays closed and the same command finishes later."""
    secret = store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-before-forget")
    store.forget(secret)
    monkeypatch.setattr(restore, "_STORE_SECONDS", 0.5)
    arguments = ["restore", *store.target(), "--snapshot", str(snapshot)]
    reader = sqlite3.connect(store.data / "memory.sqlite3", isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM source_events").fetchone()
        code, out = run(capsys, *arguments, "--backup-dir", str(store.tmp / "TEST-kept"), "--apply")
    finally:
        reader.execute("ROLLBACK")
        reader.close()
    assert (code, out.get("field"), out["restore_pending"]) == (2, "wal_busy", True), out
    assert fenced(store) and (store.data / KEPT_LEDGER).is_file()

    code, done = run(capsys, *arguments, "--apply")
    assert code == 0 and done["status"] == "restored", done
    assert not fenced(store) and store.core.source(store.ctx, secret.ref, 1) is None
    assert not store.words(SECRET)


class _WrittenHalfWay:
    """A file whose writer is stopped half way through its one write."""

    def __init__(self, stream):
        self._stream = stream

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stream.close()
        return False

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def write(self, text):
        self._stream.write(text[: len(text) // 2])
        self._stream.flush()
        raise KeyboardInterrupt


def test_a_marker_cut_short_while_it_is_written_never_strands_the_store(store, capsys, monkeypatch):
    secret = store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-before-forget")
    store.forget(secret)
    actual = Path.open

    def cut(self, mode="r", *args, **kwargs):
        stream = actual(self, mode, *args, **kwargs)
        return _WrittenHalfWay(stream) if "x" in mode and MARKER in self.name else stream

    monkeypatch.setattr(Path, "open", cut)
    arguments = ["restore", *store.target(), "--snapshot", str(snapshot), "--backup-dir"]
    with pytest.raises(KeyboardInterrupt):
        cli.main([*arguments, str(store.tmp / "TEST-kept"), "--apply"])
    capsys.readouterr()
    monkeypatch.undo()
    marker = store.data / MARKER
    assert not marker.exists() or json.loads(marker.read_text(encoding="utf-8")), "a marker is whole or absent"

    code, done = run(capsys, *arguments, str(store.tmp / "TEST-kept-again"), "--apply")
    assert code == 0 and done["status"] == "restored", done
    assert store.core.source(store.ctx, secret.ref, 1) is None and not fenced(store)


@pytest.mark.parametrize("left", ["empty", "partial"])
def test_a_marker_left_partial_or_empty_is_written_again_while_nothing_replaced_the_store(store, capsys, left):
    secret = store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-before-forget")
    store.forget(secret)
    later = store.say("TEST-later a message said after the backup")
    binding = store.ctx.binding
    whole = json.dumps(
        dict(
            installation_id=binding.installation_id,
            agent_id=binding.agent_id,
            data_directory=str(binding.data_directory),
            expected_ledger_sha256="0" * 64,
        )
    )
    (store.data / MARKER).write_text("" if left == "empty" else whole[: len(whole) // 2], encoding="utf-8")
    assert fenced(store)

    arguments = ["restore", *store.target(), "--snapshot", str(snapshot)]
    code, plan = run(capsys, *arguments)
    assert code == 0 and (plan["restore_pending"], plan["ledger_kept"], plan["backup_required"]) == (True, False, True)
    code, done = run(capsys, *arguments, "--backup-dir", str(store.tmp / "TEST-kept"), "--apply")
    assert code == 0 and done["status"] == "restored", done
    assert not fenced(store) and store.core.source(store.ctx, secret.ref, 1) is None
    assert store.core.source(store.ctx, later.ref, 1) is None
    with closing(sqlite3.connect(done["backup"]["path"])) as kept:
        held = kept.execute("SELECT count(*) FROM source_events WHERE event_id=?", (later.ref,)).fetchone()[0]
    assert held == 1, "the store as it was when it was closed is kept"


def test_a_damaged_marker_beside_a_ledger_newer_than_the_store_is_refused(store, capsys, monkeypatch):
    """A run stopped after its copy leaves the snapshot in the store and the ledger beside it; with the marker then
    damaged, only that ledger knows what was forgotten since, so the marker is not written again from the store."""
    secret = store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-before-forget")
    store.forget(secret)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(restore, "replay_deletion_ledger", interrupted)
    arguments = ["restore", *store.target(), "--snapshot", str(snapshot)]
    with pytest.raises(KeyboardInterrupt):
        cli.main([*arguments, "--backup-dir", str(store.tmp / "TEST-kept"), "--apply"])
    capsys.readouterr()
    monkeypatch.undo()
    (store.data / MARKER).write_text("{", encoding="utf-8")
    before = (store.data / "memory.sqlite3").read_bytes()

    code, out = run(capsys, *arguments, "--backup-dir", str(store.tmp / "TEST-kept-again"), "--apply")
    assert (code, out.get("field"), out["restore_pending"]) == (2, "marker_damaged", True), out
    assert (store.data / "memory.sqlite3").read_bytes() == before and fenced(store)


@pytest.mark.parametrize("change", ["write", "replace", "replace_before_digest"])
def test_the_snapshot_copied_is_the_one_checked(store, capsys, monkeypatch, change):
    """Whatever happens to the snapshot's file once it is opened, what is copied over the store is what was checked
    and what the receipt's digest describes: another SQLite writer is kept out, and a file moved over the snapshot,
    before its digest is taken or after, is not the one read.  Windows refuses to move a file over one held open; POSIX
    moves it, and the handles opened before go on reading the file they opened."""
    kept = store.say("TEST-kept the meeting moved to Thursday")
    secret = store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-checked")
    checked = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    store.say("TEST-injected a message the checked snapshot does not hold")
    other = backup(capsys, store, "TEST-other")
    store.forget(secret)
    # The digest is taken after the checks and the ledger, the fence after the digest.
    hooked = "export_deletion_ledger" if change == "replace_before_digest" else "begin_restore"
    actual = getattr(restore, hooked)

    def changed_meanwhile(*args, **kwargs):
        try:
            if change == "write":
                with closing(sqlite3.connect(snapshot, timeout=0.2)) as conn:
                    conn.execute("UPDATE source_events SET content='TEST-injected' WHERE event_id=?", (kept.ref,))
                    conn.commit()
            else:
                os.replace(other, snapshot)
        except (sqlite3.OperationalError, PermissionError):
            pass
        return actual(*args, **kwargs)

    monkeypatch.setattr(restore, hooked, changed_meanwhile)
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
    assert code == 0 and done["status"] == "restored" and done["snapshot_sha256"] == checked, done
    assert store.core.source(store.ctx, kept.ref, 1).event["content"] == "TEST-kept the meeting moved to Thursday"
    assert not store.words("TEST-injected")


def test_a_snapshot_that_is_the_store_under_another_name_is_refused(store, capsys):
    store.say(SECRET)
    database = store.data / "memory.sqlite3"
    with closing(sqlite3.connect(database)) as conn:
        # Out of WAL mode, the store's own header reads as a backup snapshot's.
        conn.execute("PRAGMA journal_mode=DELETE")
    linked = store.tmp / "TEST-linked.sqlite3"
    os.link(database, linked)
    before = database.read_bytes()
    code, out = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(linked),
        "--backup-dir",
        str(store.tmp / "TEST-kept"),
        "--apply",
    )
    assert (code, out.get("error"), out["restore_pending"]) == (2, "snapshot_is_the_store", False), out
    assert database.read_bytes() == before and not (store.tmp / "TEST-kept").exists()


def test_a_store_whose_file_has_another_name_is_refused(store, capsys):
    """Another name for the store's file is no backup: it changes with the store, and a process that opens the store
    through it keeps a write-ahead log of its own and never sees the marker."""
    store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-snapshot")
    database = store.data / "memory.sqlite3"
    os.link(database, store.tmp / "TEST-second-name.sqlite3")
    before = database.read_bytes()
    code, out = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(store.tmp / "TEST-kept"),
        "--apply",
    )
    assert (code, out.get("error"), out["restore_pending"]) == (2, "store_links", False), out
    assert database.read_bytes() == before and not (store.tmp / "TEST-kept").exists()


def test_a_snapshot_the_file_system_cannot_tell_from_the_store_is_refused(store, capsys, monkeypatch):
    """Where the file system cannot say whether two names are one file, the snapshot may be the store's own file: the
    restore is refused rather than run on a guess."""
    store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-snapshot")

    def unsupported(first, second):
        raise NotImplementedError("TEST file identity")

    monkeypatch.setattr(os.path, "samefile", unsupported)
    code, out = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(store.tmp / "TEST-kept"),
        "--apply",
    )
    assert (code, out.get("error"), out["restore_pending"]) == (2, "file_identity_unknown", False), out
    assert not (store.tmp / "TEST-kept").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses to move a file over one held open")
def test_a_snapshot_replaced_as_it_is_opened_is_refused(store, capsys, monkeypatch):
    """SQLite opens the snapshot by its path after the command opened it as a file, which the digest is read from: a
    file moved over the path in between would be copied while the digest described the file opened first."""
    store.say(SECRET)
    snapshot = backup(capsys, store, "TEST-checked")
    store.say("TEST-injected a message the checked snapshot does not hold")
    other = backup(capsys, store, "TEST-other")
    actual = restore.connect_truth_database

    def moved_first(path, *args, **kwargs):
        if Path(path).name == snapshot.name:
            os.replace(other, snapshot)
        return actual(path, *args, **kwargs)

    monkeypatch.setattr(restore, "connect_truth_database", moved_first)
    database = store.data / "memory.sqlite3"
    before = database.read_bytes()
    code, out = run(
        capsys,
        "restore",
        *store.target(),
        "--snapshot",
        str(snapshot),
        "--backup-dir",
        str(store.tmp / "TEST-kept"),
        "--apply",
    )
    assert (code, out.get("error"), out["restore_pending"]) == (2, "snapshot_replaced", False), out
    assert database.read_bytes() == before and not (store.tmp / "TEST-kept").exists()
