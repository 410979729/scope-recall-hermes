"""Contract tests for full backup bundles, rotation, deletion mirroring, and disaster recovery.

Tests:
1. Bundle completeness, manifest hashing, and credential redaction.
2. Tamper, corruption, and missing member detection.
3. Disaster restore recall equivalence to original synthetic store.
4. Post-backup deletion mirror replay preventing forgotten content resurrection.
5. Rebuild queuing when vector companions are omitted.
6. 7 daily and 4 weekly backup rotation count bounds.
7. Deletion resilience when mirror writes fail.
8. Immediate post-commit deletion mirroring across sub-transactions sharing a connection.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from scope_recall.adapters.clients.config import install_codex_scope_recall
from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore, capture_inbox
from scope_recall.core.delete_storage import group_digest
from scope_recall.core.deletion_mirror import (
    DELETION_MIRROR_FILENAME,
    catchup_deletion_mirror,
    load_mirror_operations,
)
from scope_recall.core.file_lock import advisory_file_lock
from scope_recall.core.installation_copy import installation_copy_id
from scope_recall.core.management import MemoryManagement
from scope_recall.core.management_query import MemoryFilter
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.storage import SQLiteStorage, Transaction
from scope_recall.maintenance.backup_bundle import (
    BackupBundleError,
    create_backup_bundle,
    verify_backup_bundle,
)
from scope_recall.maintenance.disaster_restore import (
    DisasterRestoreError,
    apply_disaster_restore,
    plan_disaster_restore,
)
from scope_recall.maintenance.doctor_report import DoctorReport
from scope_recall.maintenance.doctor_store import check_deletion_mirror
from scope_recall.runtime import scheduling, worker_entry, worker_watchdog
from scope_recall.runtime.backup_rotation import (
    DAILY_LIMIT,
    WEEKLY_LIMIT,
    rotate_backup_bundles,
)
from scope_recall.runtime.instance import build_runtime_instance
from scope_recall.runtime.instance_config import RuntimeInstanceConfig
from scope_recall.runtime.model_budget import default_budget_policy, initialize_auxiliary_budget_ledger
from scope_recall.runtime.scheduling import next_wake
from v11_support import recall_request, source_event

from tests.host.codex.test_remote import store as shared_store


@pytest.fixture(autouse=True)
def _immediate_mirror_attempt_fits_a_slow_runner(monkeypatch):
    """These tests check what reaches the mirror; the runner's disk speed must not decide it."""
    from scope_recall.core import deletion_mirror

    monkeypatch.setattr(deletion_mirror, "IMMEDIATE_MIRROR_SECONDS", 60.0)


def test_an_immediate_mirror_attempt_out_of_time_leaves_durable_catch_up(tmp_path, monkeypatch):
    from scope_recall.core import deletion_mirror

    core, ctx, binding = _create_synthetic_store(tmp_path)
    source = _say(core, ctx, "NameOne note")
    create_backup_bundle(binding=binding, output=tmp_path / "backup.zip")
    monkeypatch.setattr(deletion_mirror, "IMMEDIATE_MIRROR_SECONDS", 0.0)
    _forget(core, ctx, source)
    lag = json.loads((binding.data_directory / "deletion-mirror-lag.json").read_text(encoding="utf-8"))
    assert "immediate deletion mirror budget exceeded" in lag["error"]
    assert load_mirror_operations(tmp_path / DELETION_MIRROR_FILENAME) == []
    assert catchup_deletion_mirror(core.storage, tmp_path)["status"] == "caught_up"
    assert len(load_mirror_operations(tmp_path / DELETION_MIRROR_FILENAME)) == 1
    assert not (binding.data_directory / "deletion-mirror-lag.json").exists()


def test_raw_client_spools_apply_capture_secret_policy(tmp_path):
    from scope_recall.adapters.clients.remote_client import _spool

    _, _, binding = _create_synthetic_store(tmp_path)
    state = tmp_path / "client-state"
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"state_dir": str(state), "token_file": str(tmp_path / "credential.ref")}))
    for prompt in ("NameOne safe note", "password=synthetic-pass-one", "NameOne data:image/png;base64,QUJDRA=="):
        _spool({"state_dir": state}, {"hook_event_name": "UserPromptSubmit", "prompt": prompt}, "2026-01-01T00:00:00Z")
    (state / "spool" / "broken.json").write_bytes(b"{unreadable")
    bundle = tmp_path / "backup.zip"
    receipt = create_backup_bundle(binding=binding, output=bundle, client_configs=(config,))
    with zipfile.ZipFile(bundle) as archive:
        entries = [json.loads(archive.read(name)) for name in archive.namelist() if "/spool/" in name]
    assert len(entries) == 2
    assert "synthetic-pass-one" not in json.dumps(entries)
    assert "base64" not in json.dumps(entries)
    assert receipt["spool_entries_omitted"] == 2
    assert receipt["spool_entries_redacted"] == 1
    assert len(list((state / "spool").glob("*.json"))) == 4


def test_full_backup_omits_private_spools_and_kept_capture_bodies(tmp_path):
    from scope_recall.adapters.clients.remote_client import _spool
    from test_private_mode import PrivateClock

    core, ctx, binding = _create_synthetic_store(tmp_path)
    clock = PrivateClock()
    core.clock = clock
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=binding.scope_ids)))
    owner.private_on(target="session", session=ctx.session_id)
    private_at = clock.utc_now()
    clock.advance(3600)
    owner.private_off(target="session", session=ctx.session_id)
    state = tmp_path / "TEST-client-state"
    config = tmp_path / "TEST-client.json"
    config.write_text(json.dumps(dict(state_dir=str(state), token_file=str(tmp_path / "TEST-credential.ref"))))
    for at, word in [(private_at, "private"), (clock.utc_now(), "public")]:
        _spool(
            dict(state_dir=state),
            dict(hook_event_name="Stop", session_id=ctx.session_id, last_assistant_message=f"TEST {word} spool body"),
            at,
            clock=dict(client_at=at, server_at=at, received_at=at),
        )
        kept = binding.data_directory / "kept-captures" / f"TEST-{word}.json"
        kept.parent.mkdir(exist_ok=True)
        kept.write_text(
            json.dumps(
                dict(
                    context=dict(session_id=ctx.session_id),
                    event=source_event(content=f"TEST {word} kept body", occurred_at=at, recorded_at=at),
                )
            )
        )
    bundle = tmp_path / "TEST-backup.zip"
    create_backup_bundle(binding=binding, output=bundle, client_configs=(config,))
    with zipfile.ZipFile(bundle) as archive:
        bodies = b"".join(archive.read(name) for name in archive.namelist())
    assert b"TEST private spool body" not in bodies
    assert b"TEST private kept body" not in bodies
    assert b"TEST public spool body" in bodies
    assert b"TEST public kept body" in bodies
    assert len(list((state / "spool").glob("*.json"))) == 2


def test_backup_filters_private_inbox_and_restores_the_interval_history(tmp_path):
    from test_private_mode import PrivateClock

    core, ctx, binding = _create_synthetic_store(tmp_path)
    clock = PrivateClock()
    clock.advance(-60)
    core.clock = clock
    event = source_event(
        content="TEST private queued body", occurred_at="2026-09-05T12:05:00Z", recorded_at="2026-09-05T12:05:00Z"
    )
    capture_inbox.enqueue(
        core.storage,
        clock,
        ctx,
        event,
        scope_id=next(iter(ctx.allowed_scope_ids)),
        host_scope=None,
        remaining_seconds=5,
    )
    public = dict(event, source_event_key="TEST-public-queued", content="TEST public queued body")
    capture_inbox.enqueue(
        core.storage,
        clock,
        replace(ctx, session_id="TEST-other-session"),
        public,
        scope_id=next(iter(ctx.allowed_scope_ids)),
        host_scope=None,
        remaining_seconds=5,
    )
    clock.advance(60)
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=binding.scope_ids)))
    owner.private_on(target="session", session=ctx.session_id)
    clock.advance(3600)
    owner.private_off(target="session", session=ctx.session_id)
    bundle = tmp_path / "TEST-backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    with zipfile.ZipFile(bundle) as archive:
        assert b"TEST private queued body" not in archive.read("database.sqlite3")
        assert b"TEST public queued body" in archive.read("database.sqlite3")
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 2
    result = apply_disaster_restore(bundle, tmp_path / "TEST-restored")
    restored = replace(binding, data_directory=Path(result["data_directory"]))
    recovered = MemoryCore(CoreConfig(restored), clock=clock)
    recovered_ctx = replace(ctx, binding=restored)
    assert (
        recovered.record_event(recovered_ctx, event, scope_id=next(iter(ctx.allowed_scope_ids))).error_code
        == "PRIVATE_MODE"
    )
    with recovered.storage.read(recovered_ctx) as tx:
        assert (
            tx._check().execute("SELECT count(*) FROM private_intervals WHERE ends_at IS NOT NULL").fetchone()[0] == 1
        )


def test_shared_backup_checks_the_stored_entry_session_for_remote_spools(shared_store, tmp_path):
    from scope_recall.adapters.clients.config import load_shared_client
    from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
    from scope_recall.adapters.clients.remote_client import _spool
    from test_private_mode import PrivateClock

    root, homes = shared_store
    client = load_shared_client(homes["codex"], "codex")
    clock = PrivateClock()
    core = MemoryCore(CoreConfig(client.to_binding()), clock=clock)
    audience = resolve_runtime_audience(client, "C:/TEST/project1")
    ctx = trusted_context(client, audience, session_id="TEST-session1")
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=ctx.binding.scope_ids)))
    owner.private_on(target="session", session=ctx.session_id)
    private_at = clock.utc_now()
    clock.advance(3600)
    owner.private_off(target="session", session=ctx.session_id)
    state = tmp_path / "TEST-shared-client-state"
    config = tmp_path / "TEST-shared-client.json"
    config.write_text(json.dumps(dict(host="codex", state_dir=str(state), token_file=str(tmp_path / "TEST-token.ref"))))
    for session, at, words in [
        ("TEST-session1", private_at, "TEST private shared spool"),
        ("TEST-session1", clock.utc_now(), "TEST public shared spool"),
        ("TEST-session2", private_at, "TEST unrelated shared spool"),
    ]:
        _spool(
            dict(state_dir=state),
            dict(hook_event_name="Stop", session_id=session, last_assistant_message=words),
            at,
            clock=dict(client_at=at, server_at=at, received_at=at),
        )
    bundle = tmp_path / "TEST-shared-backup.zip"
    create_backup_bundle(binding=core.config.binding, output=bundle, client_configs=(config,))
    with zipfile.ZipFile(bundle) as archive:
        bodies = b"".join(archive.read(name) for name in archive.namelist() if "/spool/" in name)
    assert b"TEST private shared spool" not in bodies
    assert b"TEST public shared spool" in bodies
    assert b"TEST unrelated shared spool" in bodies
    assert len(list((state / "spool").glob("*.json"))) == 3


@pytest.mark.parametrize("kind", ["mirror", "checkpoint"])
def test_mutable_mirror_files_refuse_hard_links_before_writing(tmp_path, kind):
    _, _, binding = _create_synthetic_store(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    victim = tmp_path / "unrelated.bin"
    if kind == "checkpoint":
        conn = sqlite3.connect(victim)
        try:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
            conn.execute("INSERT INTO unrelated VALUES ('NameOne')")
            conn.commit()
        finally:
            conn.close()
    else:
        victim.write_bytes(b"NameOne unrelated file without newline")
    before = victim.read_bytes()
    target = (
        backup_dir / DELETION_MIRROR_FILENAME
        if kind == "mirror"
        else binding.data_directory / "deletion-mirror-checkpoint.sqlite3"
    )
    target.hardlink_to(victim)
    result = catchup_deletion_mirror(SQLiteStorage(binding), backup_dir)
    assert victim.read_bytes() == before
    assert result["status"] == "error"
    assert "link" in result["error"]


def test_noncanonical_explicit_mirror_is_refused_before_extraction(tmp_path):
    _, _, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    mirror = tmp_path / "offsite-journal.jsonl"
    mirror.write_bytes((tmp_path / DELETION_MIRROR_FILENAME).read_bytes())
    destination = tmp_path / "restored"
    for operation in (plan_disaster_restore, apply_disaster_restore):
        with pytest.raises(DisasterRestoreError, match="deletion-mirror.jsonl"):
            operation(bundle, destination, mirror=mirror)
        assert not destination.exists()


@pytest.mark.parametrize("source_os,destination_os", [("Windows", "Linux"), ("Linux", "Windows")])
def test_foreign_os_recovery_refuses_before_extraction(tmp_path, monkeypatch, source_os, destination_os):
    from scope_recall.maintenance import disaster_restore as recovery
    from scope_recall.runtime import backup_bundle as bundles

    _, _, binding = _create_synthetic_store(tmp_path)
    monkeypatch.setattr(bundles.platform, "system", lambda: source_os)
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["source_os"] == source_os
    monkeypatch.setattr(bundles.platform, "system", lambda: destination_os)

    def unpacked_too_soon(*args, **kwargs):
        pytest.fail("database unpacked before source operating-system refusal")

    monkeypatch.setattr(bundles, "_quick_check_database", unpacked_too_soon)
    destination = tmp_path / "restored"
    for operation in (recovery.plan_disaster_restore, recovery.apply_disaster_restore):
        with pytest.raises(DisasterRestoreError, match="same kind of system"):
            operation(bundle, destination)
        assert not destination.exists()


def test_host_binding_failure_keeps_disaster_recovery_fenced(tmp_path, monkeypatch):
    from scope_recall.core.restore import restore_pending
    from scope_recall.maintenance import disaster_restore as recovery

    _, _, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    destination = tmp_path / "restored"

    def invalid_binding(*args):
        raise ValueError("synthetic host configuration failure")

    monkeypatch.setattr(recovery, "load_binding", invalid_binding)
    with pytest.raises(ValueError, match="synthetic host"):
        apply_disaster_restore(bundle, destination)
    assert restore_pending(destination / "data")


def test_recovered_client_spools_survive_the_next_backup(tmp_path):
    _, _, binding = _create_synthetic_store(tmp_path)
    state = tmp_path / "client-state"
    (state / "spool").mkdir(parents=True)
    (state / "spool" / "entry1.json").write_text(json.dumps({"payload": {"prompt": "NameOne safe note"}}))
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"state_dir": str(state), "token_file": str(tmp_path / "credential.ref")}))
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle, client_configs=(config,))
    recovered = apply_disaster_restore(bundle, tmp_path / "restored")
    restored = replace(binding, data_directory=Path(recovered["data_directory"]))
    assert json.loads((restored.data_directory / "recovery-receipt.json").read_text()) == recovered
    second = tmp_path / "second.zip"
    create_backup_bundle(binding=restored, output=second)
    with zipfile.ZipFile(second) as archive:
        assert json.loads(archive.read("clients/client1/spool/entry1.json"))["payload"]["prompt"] == "NameOne safe note"


def test_slow_mirror_does_not_hold_the_truth_writer_lease(tmp_path, monkeypatch):
    from scope_recall.core import deletion_mirror
    from scope_recall.core.writer_lease import truth_writer_process_snapshot

    core, ctx, binding = _create_synthetic_store(tmp_path)
    source = _say(core, ctx, "NameOne note")
    create_backup_bundle(binding=binding, output=tmp_path / "backup.zip")
    original = deletion_mirror._catchup
    observed = []
    entered, release = threading.Event(), threading.Event()

    def slow_device(*args, **kwargs):
        observed.append(bool(truth_writer_process_snapshot(binding.data_directory)["connection_pin_count"]))
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(deletion_mirror, "_catchup", slow_device)
    thread = threading.Thread(target=lambda: _forget(core, ctx, source))
    thread.start()
    try:
        assert entered.wait(10)
        assert observed == [False], "mirror I/O must run after the writer lease closes"
        with core.storage.write(ctx) as tx:
            assert tx.status().memory_epoch > 0
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert observed == [False]


def test_absence_only_rollback_survives_store_loss_and_disaster_restore(tmp_path):
    from scope_recall.core.restore import begin_restore, export_deletion_ledger, ledger_digest, replay_deletion_ledger
    from test_claims import draft

    core, ctx, binding = _create_synthetic_store(tmp_path)
    snapshot = tmp_path / "before-claim.sqlite3"
    with sqlite3.connect(core.storage.path) as source_db, sqlite3.connect(snapshot) as target_db:
        source_db.backup(target_db)
    source_db.close()
    target_db.close()
    source = _say(core, ctx, "TEST-project 配色 蓝色。")
    scope = next(iter(ctx.allowed_scope_ids))
    accepted = core.records.accept_claim_proposals(
        ctx,
        dict(
            protocol_version="1.1",
            source_refs=[f"{source.ref}@1"],
            claim_proposals=[draft(source)],
            resume_proposals=[],
            reference_proposals=[],
        ),
        scope_id=scope,
        remaining_seconds=10,
    )
    claim = accepted.items[0]
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    _say(core, ctx, "撤回 TEST-project 配色蓝色。", key="withdraw1")
    assert core.current_claim(ctx, claim.ref) is None
    authority = InstallationMaintenance(replace(ctx, allowed_scope_ids=binding.scope_ids))
    ledger = export_deletion_ledger(core.storage, authority)
    assert len(ledger["withdrawals"]) == 1
    begin_restore(core.storage, authority, expected_ledger_sha256=ledger_digest(ledger))
    with sqlite3.connect(snapshot) as source_db, sqlite3.connect(core.storage.path) as target_db:
        source_db.backup(target_db)
    source_db.close()
    target_db.close()
    replay_deletion_ledger(core.storage, authority, ledger)
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM deletion_operations").fetchone()[0] == 0
        assert tx._check().execute("SELECT count(*) FROM restored_absence_blocks").fetchone()[0] == 1
    core.storage.path.rename(tmp_path / "lost-store.sqlite3")
    assert not core.storage.path.exists()
    # Recovery has only the bundle and independent mirror, never the rolled-back store.
    restored = apply_disaster_restore(bundle, tmp_path / "restored")
    target = replace(binding, data_directory=Path(restored["data_directory"]))
    recovered_core = MemoryCore(CoreConfig(target))
    assert recovered_core.current_claim(replace(ctx, binding=target), claim.ref) is None


def test_retention_timeout_does_not_repeat_daily_publication(tmp_path, monkeypatch):
    from scope_recall.runtime import backup_upkeep as upkeep
    from scope_recall.runtime.backup_primitives import BackupError

    core, _, binding = _create_synthetic_store(tmp_path)
    (binding.data_directory / "backup-config.json").write_text(json.dumps({"backup_dir": str(tmp_path / "backups")}))
    original = upkeep.rotate_backup_bundles
    calls = []

    def fail_retention(*args, **kwargs):
        calls.append(1)
        raise BackupError("backup deadline exceeded")

    monkeypatch.setattr(upkeep, "rotate_backup_bundles", fail_retention)
    first = datetime(2026, 1, 1, tzinfo=UTC)
    upkeep.backup_if_due(core.storage, None, available_seconds=120, now=first)
    state = json.loads((binding.data_directory / "backup-state.json").read_text())
    assert state["last_daily_backup_utc"] == "2026-01-01T00:00:00Z"
    assert state["retention_pending"]
    upkeep.backup_if_due(core.storage, None, available_seconds=120, now=first + timedelta(minutes=16))
    assert len(list((tmp_path / "backups").glob("*.zip"))) == 1
    monkeypatch.setattr(upkeep, "rotate_backup_bundles", original)
    result = upkeep.backup_if_due(core.storage, None, available_seconds=120, now=first + timedelta(minutes=32))
    assert result["outcome"] == "retained"
    assert not json.loads((binding.data_directory / "backup-state.json").read_text())["retention_pending"]


def test_mirror_upkeep_preserves_a_concurrent_publication_checkpoint(tmp_path, monkeypatch):
    from scope_recall.runtime import backup_upkeep as upkeep
    from scope_recall.runtime.backup_primitives import atomic_json

    core, _, binding = _create_synthetic_store(tmp_path)
    directory = binding.data_directory
    (directory / "backup-config.json").write_text(json.dumps({"backup_dir": str(tmp_path / "backups")}))
    published = {
        "last_daily_backup_utc": "2026-01-01T00:00:00Z",
        "last_bundle": "bundle1.zip",
        "retention_pending": True,
    }

    def publish_while_mirroring(*args, **kwargs):
        atomic_json(directory / "backup-state.json", published)
        return {"status": "caught_up", "caught_up": 1}

    monkeypatch.setattr(upkeep, "catchup_deletion_mirror", publish_while_mirroring)
    upkeep.mirror_if_due(core.storage, None, available_seconds=10)
    state = json.loads((directory / "backup-state.json").read_text())
    assert all(state.get(key) == value for key, value in published.items())


def test_retention_resumes_verified_archives_after_timeout(tmp_path, monkeypatch):
    from scope_recall.runtime import backup_rotation as rotation
    from scope_recall.runtime.backup_primitives import BackupError

    _, _, binding = _create_synthetic_store(tmp_path)
    for day in (1, 2):
        create_backup_bundle(
            binding=binding,
            output=tmp_path / f"scope-recall-backup-2026010{day}T000000Z.zip",
            now=datetime(2026, 1, day, tzinfo=UTC),
        )
    original = rotation.verify_backup_bundle
    checked = []

    def stop_second(path):
        checked.append(path.name)
        if len(checked) == 2:
            raise BackupError("backup deadline exceeded")
        return original(path)

    progress = binding.data_directory / "backup-retention.json"
    monkeypatch.setattr(rotation, "verify_backup_bundle", stop_second)
    with pytest.raises(BackupError, match="deadline"):
        rotation.rotate_backup_bundles(
            tmp_path,
            installation_id=binding.installation_id,
            copy_id=installation_copy_id(binding.data_directory, binding.installation_id),
            progress_path=progress,
        )
    first = checked[0]
    checked.clear()
    monkeypatch.setattr(rotation, "verify_backup_bundle", lambda path: (checked.append(path.name), original(path))[1])
    rotation.rotate_backup_bundles(
        tmp_path,
        installation_id=binding.installation_id,
        copy_id=installation_copy_id(binding.data_directory, binding.installation_id),
        progress_path=progress,
    )
    assert first not in checked


def test_backup_job_timeout_is_visible_and_stops_blind_retries(tmp_path, monkeypatch):
    from scope_recall.runtime import backup_upkeep as upkeep
    from scope_recall.runtime.backup_primitives import BackupError

    core, _, binding = _create_synthetic_store(tmp_path)
    create_backup_bundle(binding=binding, output=tmp_path / "manual.zip")
    attempts = []

    def timeout(**kwargs):
        attempts.append(1)
        raise BackupError("backup deadline exceeded")

    monkeypatch.setattr(upkeep, "create_backup_bundle", timeout)
    first = datetime(2026, 1, 1, tzinfo=UTC)
    upkeep.backup_if_due(core.storage, None, available_seconds=upkeep.BACKUP_JOB_SECONDS, now=first)
    upkeep.backup_if_due(core.storage, None, available_seconds=upkeep.BACKUP_JOB_SECONDS, now=first + timedelta(days=1))
    assert len(attempts) == 1
    report = DoctorReport(host="codex", status="ok")
    check_deletion_mirror(report, binding.data_directory)
    assert "scheduled_backup_incomplete" in report.capability_gaps


def test_receipt_persistence_failure_keeps_recovery_fenced(tmp_path, monkeypatch):
    from scope_recall.core.restore import restore_pending
    from scope_recall.maintenance import disaster_restore as recovery

    _, _, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)

    def unwritable(*args, **kwargs):
        raise OSError("synthetic receipt disk failure")

    monkeypatch.setattr(recovery, "atomic_json", unwritable)
    destination = tmp_path / "restored"
    with pytest.raises(OSError, match="receipt disk failure"):
        apply_disaster_restore(bundle, destination)
    assert restore_pending(destination / "data")


@pytest.mark.parametrize("kind", ["operation", "source_group", "absence"])
def test_malformed_complete_mirror_is_rejected_before_extraction(tmp_path, kind):
    _, _, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    mirror = tmp_path / DELETION_MIRROR_FILENAME
    record = {
        "format": "scope-recall-deletion-mirror/1",
        "installation_id": binding.installation_id,
        "record_id": "record1",
        "kind": kind,
    }
    with mirror.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="mirror"):
        apply_disaster_restore(bundle, destination)
    assert not destination.exists()


def test_crash_after_extraction_keeps_recovery_fenced(tmp_path, monkeypatch):
    from scope_recall.maintenance import disaster_restore as recovery

    core, ctx, binding = _create_synthetic_store(tmp_path)
    source = _say(core, ctx, "NameOne deleted note")
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    _forget(core, ctx, source)
    destination = tmp_path / "restored"

    def interrupted(*args):
        raise RuntimeError("synthetic interruption")

    # The binding is read after the actual database relocation and before replay.
    monkeypatch.setattr(recovery, "_restored_binding", interrupted)
    with pytest.raises(RuntimeError, match="interruption"):
        apply_disaster_restore(bundle, destination)
    data = destination / "data"
    restored = replace(binding, data_directory=data)
    with pytest.raises(ContractError, match="RESTORE_UNVERIFIED"):
        with SQLiteStorage(restored).read(replace(ctx, binding=restored)):
            pass


def test_extraction_begins_only_after_recovery_is_fenced(tmp_path, monkeypatch):
    from scope_recall.core.restore import restore_pending
    from scope_recall.maintenance import disaster_restore as recovery

    _, _, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    destination = tmp_path / "restored"

    def inspect_boundary(*args):
        assert restore_pending(destination / "data")
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(recovery, "write_verified_members", inspect_boundary)
    with pytest.raises(RuntimeError, match="interruption"):
        apply_disaster_restore(bundle, destination)


def test_disabling_snapshots_preserves_mirroring(tmp_path):
    from scope_recall.runtime.backup_upkeep import backup_if_due

    core, ctx, binding = _create_synthetic_store(tmp_path)
    source = _say(core, ctx, "NameOne forgotten note")
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    config_path = binding.data_directory / "backup-config.json"
    config = json.loads(config_path.read_text())
    config["snapshots_enabled"] = False
    config_path.write_text(json.dumps(config))
    _forget(core, ctx, source)
    assert len(load_mirror_operations(tmp_path / DELETION_MIRROR_FILENAME)) == 1
    result = backup_if_due(core.storage, None, available_seconds=30)
    assert result["outcome"] == "snapshots_disabled"
    restored = apply_disaster_restore(bundle, tmp_path / "restored")
    target_binding = replace(binding, data_directory=Path(restored["data_directory"]))
    assert MemoryCore(CoreConfig(target_binding)).source(replace(ctx, binding=target_binding), source.ref, 1) is None


def test_a_store_that_was_never_backed_up_keeps_a_clean_doctor(tmp_path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    _forget(core, ctx, _say(core, ctx, "NameOne note"))
    report = DoctorReport(host="hermes", status="ok")
    check_deletion_mirror(report, binding.data_directory)
    assert report.capability_gaps == []
    assert report.checks[-1]["result"] == "not_configured"
    assert not (binding.data_directory / "deletion-mirror-lag.json").exists()


def test_a_removed_backup_directory_leaves_deletions_visibly_unprotected(tmp_path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    (tmp_path / "backups").mkdir()
    create_backup_bundle(binding=binding, output=tmp_path / "backups" / "first.zip")
    (binding.data_directory / "backup-config.json").write_text(json.dumps({"backup_dir": None}))
    _forget(core, ctx, _say(core, ctx, "NameOne note"))
    report = DoctorReport(host="hermes", status="ok")
    check_deletion_mirror(report, binding.data_directory)
    assert "deletion_mirror_lag" in report.capability_gaps
    assert report.checks[-1]["detail"] == "backup directory removed; later deletions are not mirrored"


def test_a_broken_backup_configuration_never_stops_queue_work(tmp_path):
    from scope_recall.runtime.backup_upkeep import backup_if_due, mirror_if_due, next_backup_wake

    core, _ctx, binding = _create_synthetic_store(tmp_path)
    config_path = binding.data_directory / "backup-config.json"
    config_path.write_text('{"backup_dir": "unfinished",')
    assert mirror_if_due(core.storage, None, available_seconds=30) == {
        "outcome": "failed",
        "error": "backup configuration unreadable: JSONDecodeError",
    }
    config_path.write_text(json.dumps({"backup_dir": str(tmp_path / "backups"), "snapshots_enabled": "no"}))
    assert next_backup_wake(binding.data_directory, datetime.now(UTC))[1] == "daily_backup"
    result = backup_if_due(core.storage, None, available_seconds=30)
    assert result["outcome"] == "failed"
    assert result["error"] == "snapshots_enabled must be a boolean"


def test_recovery_preserves_a_runtime_only_mirror_destination(tmp_path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    source = _say(core, ctx, "NameOne note")
    mirror_dir = tmp_path / "backups"
    (binding.data_directory / "runtime-config.json").write_text(json.dumps({"backup_dir": str(mirror_dir)}))
    bundle = mirror_dir / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    assert not (binding.data_directory / "backup-config.json").exists()
    result = apply_disaster_restore(bundle, tmp_path / "restored")
    data = Path(result["data_directory"])
    config = json.loads((data / "backup-config.json").read_text())
    assert Path(config["backup_dir"]) == mirror_dir
    assert config["snapshots_enabled"] is False
    restored = replace(binding, data_directory=data)
    _forget(MemoryCore(CoreConfig(restored)), replace(ctx, binding=restored), source)
    assert len(load_mirror_operations(mirror_dir / DELETION_MIRROR_FILENAME)) == 1


def test_bundle_publication_never_overwrites_a_raced_output(tmp_path, monkeypatch):
    from scope_recall.runtime import backup_bundle as bundles

    _, _, binding = _create_synthetic_store(tmp_path)
    output = tmp_path / "backup.zip"
    actual_check = bundles.check_backup_deadline

    def race():
        actual_check()
        if not output.exists():
            output.write_bytes(b"NameOne owns this output")

    monkeypatch.setattr(bundles, "check_backup_deadline", race)
    with pytest.raises(BackupBundleError, match="overwrite"):
        create_backup_bundle(binding=binding, output=output)
    assert output.read_bytes() == b"NameOne owns this output"


def test_predictable_publication_temporary_is_untouched(tmp_path):
    _, _, binding = _create_synthetic_store(tmp_path)
    output = tmp_path / "backup.zip"
    unowned = tmp_path / ".backup.zip.tmp"
    unowned.write_bytes(b"NameOne existing file")
    create_backup_bundle(binding=binding, output=output)
    assert unowned.read_bytes() == b"NameOne existing file"


def test_publication_does_not_follow_a_preexisting_temporary_link(tmp_path):
    _, _, binding = _create_synthetic_store(tmp_path)
    output = tmp_path / "backup.zip"
    unowned = tmp_path / ".backup.zip.tmp"
    victim = tmp_path / "unrelated"
    victim.mkdir()
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(victim), str(unowned))
    else:
        unowned.symlink_to(victim, target_is_directory=True)
    create_backup_bundle(binding=binding, output=output)
    assert output.is_file()
    assert list(victim.iterdir()) == []
    assert unowned.exists()


def test_mirror_deadline_resumes_after_the_last_validated_line(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scope_recall.core import deletion_mirror
    from scope_recall.core import deletion_mirror_checkpoint as checkpoint
    from scope_recall.runtime import backup_primitives as io

    core, ctx, binding = _create_synthetic_store(tmp_path)
    _forget(core, ctx, _say(core, ctx, "NameOne note"))
    mirror_dir = tmp_path / "backups"
    deletion_mirror.sync_deletion_mirror(binding.data_directory, mirror_dir)
    mirror = mirror_dir / DELETION_MIRROR_FILENAME
    mirror.write_bytes(mirror.read_bytes())
    actual_decode = checkpoint.decode_mirror_record
    seen = []
    clock = [0.0]
    monkeypatch.setattr(io, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def slow_decode(line):
        item = actual_decode(line)
        seen.append(item["record_id"])
        clock[0] = 20.0
        return item

    monkeypatch.setattr(checkpoint, "decode_mirror_record", slow_decode)
    with io.backup_deadline(10.0), pytest.raises(io.BackupError, match="deadline"):
        deletion_mirror.sync_deletion_mirror(binding.data_directory, mirror_dir, check=io.check_backup_deadline)
    assert len(seen) == 1
    with sqlite3.connect(binding.data_directory / "deletion-mirror-checkpoint.sqlite3") as conn:
        offset = conn.execute("SELECT offset FROM checkpoint WHERE singleton=1").fetchone()[0]
    assert 0 < offset < mirror.stat().st_size
    deletion_mirror.sync_deletion_mirror(binding.data_directory, mirror_dir)
    assert seen.count(binding.installation_id) == 1
    assert len(deletion_mirror.load_mirror_operations(mirror)) == 1


def test_recovery_remains_fenced_when_final_verification_fails(tmp_path, monkeypatch):
    from scope_recall.core.restore import restore_pending
    from scope_recall.maintenance import disaster_restore as recovery

    _, _, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic verification failure")

    monkeypatch.setattr(recovery, "finish_disaster_recovery", unavailable)
    destination = tmp_path / "restored"
    with pytest.raises(RuntimeError, match="verification failure"):
        apply_disaster_restore(bundle, destination)
    assert restore_pending(destination / "data")


@pytest.mark.parametrize(
    "url",
    [
        "https://user1:ordinary-pass@host.test/api",
        "https://host.test/api?api_key=ordinary-pass",
        "http://host.test/?x-access-token=ordinary-pass",
    ],
)
def test_configuration_urls_cannot_archive_credentials(tmp_path, url):
    _, _, binding = _create_synthetic_store(tmp_path)
    (binding.data_directory / "runtime-config.json").write_text(json.dumps({"endpoint": url, "routes": [url]}))
    output = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=output)
    with zipfile.ZipFile(output) as archive:
        assert b"ordinary-pass" not in archive.read("configs/runtime-config.json")


def test_recovery_preserves_daily_queue_budget(tmp_path, monkeypatch):
    from scope_recall.runtime import worker_entry

    _, _, binding = _create_synthetic_store(tmp_path)
    state = {"installation_id": binding.installation_id, "day": "2026-01-01", "used": 17}
    (binding.data_directory / "runtime-worker-day.json").write_text(json.dumps(state))
    output = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=output)
    result = apply_disaster_restore(output, tmp_path / "restored")
    data = Path(result["data_directory"])
    restored = replace(binding, data_directory=data)
    config = RuntimeInstanceConfig(
        binding=restored, session_id="session1", allowed_scope_ids=restored.scope_ids, daily_work_limit=20, max_items=10
    )
    monkeypatch.setattr(worker_entry, "utc_now", lambda: "2026-01-01T12:00:00Z")
    assert worker_entry._reserve_daily_work(config)[2] == 3
    assert json.loads((data / "runtime-worker-day.json").read_text())["used"] == 20


@pytest.mark.parametrize(
    "state",
    [
        {"installation_id": "foreign", "day": "2026-01-01", "used": 2},
        {"day": "2026-01-01", "used": -1},
        {"day": "invalid", "used": 2},
    ],
)
def test_invalid_daily_budget_refuses_backup(tmp_path, state):
    _, _, binding = _create_synthetic_store(tmp_path)
    state.setdefault("installation_id", binding.installation_id)
    (binding.data_directory / "runtime-worker-day.json").write_text(json.dumps(state))
    with pytest.raises(BackupBundleError, match="budget"):
        create_backup_bundle(binding=binding, output=tmp_path / "backup.zip")


@pytest.mark.parametrize("tree", ["retained", "kept-captures"])
def test_a_payload_named_like_worker_metadata_stays_opaque(tmp_path, tree):
    _, _, binding = _create_synthetic_store(tmp_path)
    directory = binding.data_directory / tree
    directory.mkdir()
    (directory / "runtime-worker-day.json").write_bytes(b"NameOne attachment")
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    assert verify_backup_bundle(bundle)["status"] == "verified"


@pytest.mark.parametrize("limit", ["manifest", "members", "expanded"])
def test_archive_limits_precede_decompression(tmp_path, monkeypatch, limit):
    from scope_recall.runtime import backup_bundle as bundles

    _, _, binding = _create_synthetic_store(tmp_path)
    output = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=output)
    name = {"manifest": "MAX_MANIFEST_BYTES", "members": "MAX_ARCHIVE_MEMBERS", "expanded": "MAX_EXPANDED_BYTES"}[limit]
    monkeypatch.setattr(bundles, name, 1, raising=False)
    actual_open = zipfile.ZipFile.open

    def refuse_read(archive, *args, **kwargs):
        pytest.fail("member decompressed before archive limits were checked")

    monkeypatch.setattr(zipfile.ZipFile, "open", refuse_read)
    with pytest.raises(BackupBundleError, match="limit"):
        verify_backup_bundle(output)
    monkeypatch.setattr(zipfile.ZipFile, "open", actual_open)


def _create_synthetic_store(root: Path) -> tuple[MemoryCore, TrustedContext, InstanceBinding]:
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    config, core = install_codex_scope_recall(root / "install", project_root=project, agent_id="agent1")
    scope = config.audience_scopes["owner_private"]
    binding = config.to_binding()
    ctx = TrustedContext(binding, "sess_001", frozenset({scope}), "human_direct")
    return core, ctx, binding


def _say(core: MemoryCore, ctx: TrustedContext, text: str, key: str = "msg1"):
    scope = next(iter(ctx.allowed_scope_ids))
    saved = core.record_event(
        ctx,
        source_event(source_event_key=f"evt-{key}", content=text),
        scope_id=scope,
        remaining_seconds=10,
    )
    return core.source(ctx, saved.event_refs[0].ref, 1)


def _forget(core: MemoryCore, ctx: TrustedContext, source):
    _say(core, ctx, f"删除 {source.ref}", key=f"cmd-del-{source.ref}")
    req = {
        "protocol_version": "1.1",
        "target_refs": [source.ref],
        "mode": "delete",
        "expected_revisions": {source.ref: source.revision},
    }
    return core.forget(ctx, req, remaining_seconds=10)


def test_bundle_completeness_and_secret_redaction(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory

    runtime_conf = {
        "binding": {
            "installation_id": binding.installation_id,
            "agent_id": binding.agent_id,
            "data_directory": str(data_dir),
            "scope_ids": sorted(binding.scope_ids),
        },
        "api_key": "sk-secret-token-value-12345",
        "credential_env": "TEST_MODEL_KEY",
        "nested": {
            "token": "bearer-token-abcde",
            "safe_param": "value_one",
        },
    }
    (data_dir / "runtime-config.json").write_text(json.dumps(runtime_conf), encoding="utf-8")

    retained_dir = data_dir / "retained"
    retained_dir.mkdir(parents=True, exist_ok=True)
    (retained_dir / "attachment_001.bin").write_bytes(b"synthetic-binary-payload")

    kept_dir = data_dir / "kept-captures"
    kept_dir.mkdir(parents=True, exist_ok=True)
    (kept_dir / "capture_one.json").write_text('{"item": "pending_one"}', encoding="utf-8")

    inbox_dir = data_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / "queue_item.json").write_text('{"item": "pending_one"}', encoding="utf-8")

    bundle_dest = tmp_path / "backups" / "scope-recall-backup-20261019T140000Z.zip"
    receipt = create_backup_bundle(binding=binding, output=bundle_dest, with_vectors=False)

    assert receipt["status"] == "created"
    assert bundle_dest.is_file()
    assert receipt["secrets_redacted"] >= 2

    with zipfile.ZipFile(bundle_dest, "r") as zf:
        namelist = set(zf.namelist())
        assert "manifest.json" in namelist
        assert "database.sqlite3" in namelist
        assert "configs/runtime-config.json" in namelist
        assert "retained/attachment_001.bin" in namelist
        assert "kept-captures/capture_one.json" in namelist
        _assert_kept_capture_resource(zf, namelist)

        archived_conf = json.loads(zf.read("configs/runtime-config.json").decode("utf-8"))
        assert archived_conf["api_key"] == "[REDACTED_SECRET]"
        assert archived_conf["credential_env"] == "TEST_MODEL_KEY"
        assert archived_conf["nested"]["token"] == "[REDACTED_SECRET]"
        assert archived_conf["nested"]["safe_param"] == "value_one"
        assert archived_conf["binding"]["agent_id"] == "agent1"
        contents = b"".join(zf.read(name) for name in zf.namelist())
        assert b"sk-secret-token-value-12345" not in contents
        assert b"bearer-token-abcde" not in contents


def _assert_kept_capture_resource(archive: zipfile.ZipFile, namelist: set[str]) -> None:
    assert "inbox/queue_item.json" not in namelist
    assert not any(name.startswith(("inbox/", "outbox/", "retry/", "budget/")) for name in namelist)
    resources = json.loads(archive.read("manifest.json"))["resources"]
    kept = [item for item in resources if item["role"] == "kept_captures"]
    assert kept == [
        {
            "role": "kept_captures",
            "archive_path": "kept-captures/capture_one.json",
            "target_relative": "kept-captures/capture_one.json",
            "config_file": None,
            "field": None,
        }
    ]


_WAL_ROW = "wal-row-one"
_LEDGER_INSERT = (
    "INSERT INTO requests (batch, model, body_sha256, request_bytes, reserved_input, "
    "reserved_output, actual_input, actual_output, charge_micro_usd, status, started_ns, "
    "cached_input, unreported_output) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _open_uncheckpointed_ledger(ledger: Path) -> sqlite3.Connection:
    """A budget ledger whose last committed row is still only in the WAL."""
    initialize_auxiliary_budget_ledger(ledger, default_budget_policy())
    conn = sqlite3.connect(ledger)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute(
        _LEDGER_INSERT,
        ("auxiliary", "model1", "abc", 4, 1, 1, 0, 0, 0, _WAL_ROW, 7, 0, 0),
    )
    conn.commit()
    return conn


def _assert_main_file_has_no_row(ledger: Path, copy: Path) -> None:
    copy.write_bytes(ledger.read_bytes())
    with sqlite3.connect(copy) as bare:
        assert bare.execute("SELECT COUNT(*) FROM requests").fetchone() == (0,)


def _write_budget_runtime_config(data_dir: Path, binding: InstanceBinding, external_dir: Path, ledger: Path) -> None:
    payload = {
        "binding": {
            "installation_id": binding.installation_id,
            "agent_id": binding.agent_id,
            "data_directory": str(data_dir),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": binding.test_mode,
            "installation_kind": binding.installation_kind,
        },
        "session_id": "session_001",
        "allowed_scope_ids": sorted(binding.scope_ids),
        "auxiliary": {
            "external_embedding": False,
            "external_consolidation": False,
            "installation_dir": str(external_dir),
            "ledger_path": str(ledger),
        },
    }
    (data_dir / "runtime-config.json").write_text(json.dumps(payload), encoding="utf-8")


def _budget_config_fields(bundle: Path) -> set[str]:
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        resources = json.loads(archive.read("manifest.json"))["resources"]
    assert "budgets/budget1.sqlite3" in names
    assert not any(name.endswith("auxiliary-budget.sqlite3") for name in names)
    return {item["field"] for item in resources if item["config_file"] == "runtime-config.json"}


def _assert_restored_budget(fresh: Path, external_dir: Path, source: Path) -> None:
    loaded = RuntimeInstanceConfig.from_mapping(json.loads((fresh / "runtime-config.json").read_text(encoding="utf-8")))
    restored_ledger = loaded.auxiliary.ledger_path
    restored_dir = loaded.auxiliary.installation_dir
    assert restored_ledger is not None
    assert restored_dir is not None
    assert restored_ledger.resolve() == (fresh / "budgets" / "budget1.sqlite3").resolve()
    assert restored_dir.resolve() == (fresh / "budgets").resolve()
    assert restored_ledger.resolve() != source.resolve()
    assert not restored_ledger.resolve().is_relative_to(external_dir.resolve())
    assert not restored_dir.resolve().is_relative_to(external_dir.resolve())
    with sqlite3.connect(restored_ledger) as restored:
        last = restored.execute("SELECT status FROM requests ORDER BY id DESC LIMIT 1").fetchone()
    assert last == (_WAL_ROW,)


def test_external_budget_ledger_restores_wal_row_and_keeps_source(tmp_path: Path):
    core, _ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory
    external_dir = tmp_path / "external-budget"
    ledger = external_dir / "auxiliary-budget.sqlite3"
    assert core.storage.path.is_file()
    assert not ledger.resolve().is_relative_to(data_dir.resolve())
    kept = data_dir / "kept-captures"
    kept.mkdir()
    (kept / "capture_one.json").write_text('{"item": "pending_one"}', encoding="utf-8")
    conn = _open_uncheckpointed_ledger(ledger)
    try:
        bare = tmp_path / "bare-ledger.sqlite3"
        _assert_main_file_has_no_row(ledger, bare)
        before_main = ledger.read_bytes()
        before_wal = Path(f"{ledger}-wal").read_bytes()
        _write_budget_runtime_config(data_dir, binding, external_dir, ledger)
        bundle = tmp_path / "budget-backup.zip"
        create_backup_bundle(binding=binding, output=bundle, with_vectors=False)
        assert _budget_config_fields(bundle) == {"auxiliary.ledger_path", "auxiliary.installation_dir"}
        assert ledger.read_bytes() == before_main
        assert Path(f"{ledger}-wal").read_bytes() == before_wal
        assert conn.execute("SELECT status FROM requests").fetchone() == (_WAL_ROW,)
        _assert_main_file_has_no_row(ledger, bare)
        fresh = tmp_path / "restored-budget"
        result = apply_disaster_restore(bundle, fresh)
        fresh = Path(result["data_directory"])
        _assert_restored_budget(fresh, external_dir, ledger)
        kept_text = (fresh / "kept-captures" / "capture_one.json").read_text(encoding="utf-8")
        assert kept_text == '{"item": "pending_one"}'
        assert conn.execute("SELECT status FROM requests").fetchone() == (_WAL_ROW,)
        assert ledger.is_file()
    finally:
        conn.close()


def test_tamper_and_missing_member_detection(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    bundle_dest = tmp_path / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle_dest)

    verify_receipt = verify_backup_bundle(bundle_dest)
    assert verify_receipt["status"] == "verified"

    tampered_dest = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle_dest, "r") as src_zf, zipfile.ZipFile(tampered_dest, "w") as dst_zf:
        for item in src_zf.infolist():
            data = src_zf.read(item.filename)
            if item.filename == "database.sqlite3":
                data = data + b"\x00corrupt"
            dst_zf.writestr(item, data)

    with pytest.raises(BackupBundleError) as exc_info:
        verify_backup_bundle(tampered_dest)
    assert "checksum mismatch" in str(exc_info.value) or "size mismatch" in str(exc_info.value)

    missing_dest = tmp_path / "missing.zip"
    with zipfile.ZipFile(bundle_dest, "r") as src_zf, zipfile.ZipFile(missing_dest, "w") as dst_zf:
        for item in src_zf.infolist():
            if item.filename != "database.sqlite3":
                dst_zf.writestr(item, src_zf.read(item.filename))

    with pytest.raises(BackupBundleError) as exc_info:
        verify_backup_bundle(missing_dest)
    assert "missing" in str(exc_info.value)


def test_disaster_restore_recall_equivalence_and_path_rewriting(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory

    source = _say(core, ctx, "NameOne preferences: color blue", key="pref1")
    request = recall_request(query="NameOne preferences color blue", mode="current")
    original_recall = core.recall(ctx, request, deadline_seconds=10)
    assert original_recall.items

    runtime_conf = {
        "binding": {
            "installation_id": binding.installation_id,
            "agent_id": binding.agent_id,
            "data_directory": str(data_dir),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": binding.test_mode,
            "installation_kind": binding.installation_kind,
        },
        "session_id": "session_001",
        "allowed_scope_ids": sorted(binding.scope_ids),
        "vector": {
            "backend": "lancedb",
            "storage_dir": str(data_dir / "vectors" / "default_space"),
            "table_name": "t1",
            "dimensions": 384,
            "test_injection_override": True,
        },
    }
    (data_dir / "runtime-config.json").write_text(json.dumps(runtime_conf), encoding="utf-8")

    bundle_dest = tmp_path / "disaster_backup.zip"
    create_backup_bundle(binding=binding, output=bundle_dest, with_vectors=False)

    fresh_target = tmp_path / "restored_store"
    plan = plan_disaster_restore(bundle_dest, fresh_target)
    assert plan["status"] == "planned"
    assert not fresh_target.exists()

    restore_receipt = apply_disaster_restore(bundle_dest, fresh_target)
    fresh_target = Path(restore_receipt["data_directory"])
    assert restore_receipt["status"] == "restored"
    assert fresh_target.is_dir()
    assert (fresh_target / "memory.sqlite3").is_file()

    with sqlite3.connect(fresh_target / "memory.sqlite3") as conn:
        meta_dir = conn.execute("SELECT data_directory FROM instance_meta WHERE singleton=1").fetchone()[0]
        assert Path(meta_dir).resolve() == fresh_target.resolve()

    restored_conf = json.loads((fresh_target / "runtime-config.json").read_text(encoding="utf-8"))
    restored_obj = RuntimeInstanceConfig.from_mapping(restored_conf)
    assert restored_obj.binding.data_directory.resolve() == fresh_target.resolve()
    assert (
        restored_obj.vector.storage_dir.resolve()
        == (fresh_target / "vectors" / restored_obj.embedding_space_id()).resolve()
    )

    restored_binding = InstanceBinding(
        agent_id=binding.agent_id,
        installation_id=binding.installation_id,
        data_directory=fresh_target,
        scope_ids=binding.scope_ids,
        test_mode=binding.test_mode,
        installation_kind=binding.installation_kind,
    )
    restored_core = MemoryCore(CoreConfig(restored_binding))
    restored_ctx = TrustedContext(
        restored_binding,
        "restored_session",
        ctx.allowed_scope_ids,
        "human_direct",
    )
    found = restored_core.source(restored_ctx, source.ref, 1)
    assert found is not None
    assert "NameOne preferences: color blue" in found.event["content"]

    results = restored_core.search_sources(restored_ctx, "NameOne preferences")
    assert len(results) >= 1
    assert any("NameOne preferences: color blue" in s.event["content"] for s in results)
    restored_recall = restored_core.recall(restored_ctx, request, deadline_seconds=10)
    assert restored_recall.items == original_recall.items

    assert "unrecoverable_later_interval" in restore_receipt
    assert restore_receipt["unrecoverable_later_interval"]["start_utc"] == restore_receipt["recovery_point_utc"]


def test_post_backup_deletion_mirror_replay(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}), encoding="utf-8")

    source = _say(core, ctx, "NameOne secret note", key="secret1")

    bundle_dest = backup_dir / "scope-recall-backup-snapshot.zip"
    create_backup_bundle(binding=binding, output=bundle_dest)

    _forget(core, ctx, source)

    catchup_deletion_mirror(core.storage, backup_dir)
    assert (backup_dir / DELETION_MIRROR_FILENAME).is_file()

    fresh_target = tmp_path / "restored_with_mirror"
    restore_receipt = apply_disaster_restore(
        bundle=bundle_dest,
        to=fresh_target,
        mirror=backup_dir / DELETION_MIRROR_FILENAME,
    )
    fresh_target = Path(restore_receipt["data_directory"])

    assert restore_receipt["status"] == "restored"
    assert restore_receipt["mirror_replayed_operations"] >= 1

    restored_binding = InstanceBinding(
        agent_id=binding.agent_id,
        installation_id=binding.installation_id,
        data_directory=fresh_target,
        scope_ids=binding.scope_ids,
        test_mode=binding.test_mode,
        installation_kind=binding.installation_kind,
    )
    restored_core = MemoryCore(CoreConfig(restored_binding))
    restored_ctx = TrustedContext(
        restored_binding,
        "restored_session",
        ctx.allowed_scope_ids,
        "human_direct",
    )
    assert restored_core.source(restored_ctx, source.ref, 1) is None
    results = restored_core.search_sources(restored_ctx, "NameOne secret note")
    assert not any(s.ref == source.ref for s in results)


@pytest.mark.parametrize("state", ["pending", "done"])
def test_no_vectors_queues_rebuild(tmp_path: Path, state):
    core, ctx, binding = _create_synthetic_store(tmp_path)

    source = _say(core, ctx, "NameOne text requiring embedding rebuild", key="embed1")
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE work_items SET state=? WHERE work_type='embed'", (state,))

    bundle_dest = tmp_path / "bundle_no_vectors.zip"
    create_backup_bundle(binding=binding, output=bundle_dest, with_vectors=False)

    fresh_target = tmp_path / "restored_rebuild"
    restore_receipt = apply_disaster_restore(bundle_dest, fresh_target)
    fresh_target = Path(restore_receipt["data_directory"])
    assert restore_receipt["with_vectors"] is False
    assert restore_receipt["vector_rebuild_queued"] >= 1

    restored_binding = InstanceBinding(
        agent_id=binding.agent_id,
        installation_id=binding.installation_id,
        data_directory=fresh_target,
        scope_ids=binding.scope_ids,
        test_mode=binding.test_mode,
        installation_kind=binding.installation_kind,
    )
    restored_core = MemoryCore(CoreConfig(restored_binding))
    restored_ctx = TrustedContext(
        restored_binding,
        "restored_session",
        ctx.allowed_scope_ids,
        "human_direct",
    )
    found = restored_core.source(restored_ctx, source.ref, 1)
    assert found is not None
    assert "NameOne text requiring embedding rebuild" in found.event["content"]
    with restored_core.storage.read(restored_ctx) as tx:
        assert tx._check().execute("SELECT state FROM work_items WHERE work_type='embed'").fetchone()[0] == "pending"

    with sqlite3.connect(fresh_target / "memory.sqlite3") as conn:
        queued = conn.execute(
            "SELECT count(*) FROM work_items WHERE work_type='embed' AND subject_ref=?", (source.ref,)
        ).fetchone()[0]
        assert queued == 1


def test_backup_rotation_counts(tmp_path: Path):
    _, _, binding = _create_synthetic_store(tmp_path)
    backup_dir = tmp_path / "rotation_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    base_time = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)

    created_files: list[str] = []
    for day_offset in range(35):
        dt = base_time - timedelta(days=day_offset)
        ts_slug = dt.strftime("%Y%m%dT%H%M%SZ")
        filename = f"scope-recall-backup-{ts_slug}.zip"
        file_path = backup_dir / filename
        create_backup_bundle(binding=binding, output=file_path, now=dt)
        created_files.append(filename)

    assert len(list(backup_dir.glob("scope-recall-backup-*.zip"))) == 35

    # Corrupt and foreign newer packages cannot evict valid recovery points.
    corrupt = backup_dir / "scope-recall-backup-20261020T120000Z.zip"
    corrupt.write_bytes(b"dummy-zip-data")
    _, _, foreign_binding = _create_synthetic_store(tmp_path / "foreign")
    foreign = backup_dir / "scope-recall-backup-20261021T120000Z.zip"
    create_backup_bundle(binding=foreign_binding, output=foreign, now=base_time + timedelta(days=2))
    result = rotate_backup_bundles(
        backup_dir,
        installation_id=binding.installation_id,
        copy_id=installation_copy_id(binding.data_directory, binding.installation_id),
        now=base_time,
    )

    assert result.kept_daily == created_files[:DAILY_LIMIT]
    weeks = {}
    for offset, name in enumerate(created_files):
        weeks.setdefault((base_time - timedelta(days=offset)).isocalendar()[:2], name)
    assert result.kept_weekly == list(weeks.values())[:WEEKLY_LIMIT]
    assert result.retained_total <= DAILY_LIMIT + WEEKLY_LIMIT
    assert len(result.removed) > 0

    remaining = [p.name for p in backup_dir.glob("scope-recall-backup-*.zip")]
    assert len(remaining) == result.retained_total + 2
    assert corrupt.is_file() and foreign.is_file()
    for removed_file in result.removed:
        assert not (backup_dir / removed_file).exists()


def test_deletion_resilience_when_mirror_fails(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory

    barrier_file = tmp_path / "barrier_file"
    barrier_file.write_text("file_not_dir", encoding="utf-8")
    invalid_backup_dir = barrier_file / "invalid_sub"
    (data_dir / "backup-config.json").write_text(json.dumps({"backup_dir": str(invalid_backup_dir)}), encoding="utf-8")

    source = _say(core, ctx, "NameOne persistent entry", key="persist1")

    result = _forget(core, ctx, source)
    assert result["read_blocked"] == 1 or result["read_blocked"] is True
    assert core.source(ctx, source.ref, 1) is None

    lag_file = data_dir / "deletion-mirror-lag.json"
    assert lag_file.is_file()
    lag_data = json.loads(lag_file.read_text(encoding="utf-8"))
    assert lag_data.get("failed") is True or lag_data.get("status") == "lagging"

    report = DoctorReport(host="hermes", status="ok")
    check_deletion_mirror(report, data_dir)
    assert "deletion_mirror_lag" in report.capability_gaps
    backup_dir = tmp_path / "recovered-backups"
    (data_dir / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}))
    assert catchup_deletion_mirror(core.storage, backup_dir)["status"] == "caught_up"
    assert len(load_mirror_operations(backup_dir / DELETION_MIRROR_FILENAME)) == 1
    report = DoctorReport(host="hermes", status="ok")
    check_deletion_mirror(report, data_dir)
    assert "deletion_mirror_lag" not in report.capability_gaps


def test_sub_transaction_deletion_immediately_mirrored_on_commit(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}), encoding="utf-8")

    source = _say(core, ctx, "NameOne item deleted through sub transaction", key="subdel1")
    mirror_file = backup_dir / DELETION_MIRROR_FILENAME
    assert not mirror_file.is_file()

    with core.storage.write(ctx) as outer_tx:
        sub_ctx = TrustedContext(binding, "sub_sess_001", ctx.allowed_scope_ids, "human_direct")
        sub_tx = Transaction(outer_tx._check(write=True), sub_ctx, writable=True)
        req = {
            "protocol_version": "1.1",
            "target_refs": [source.ref],
            "mode": "delete",
            "expected_revisions": {source.ref: source.revision},
        }
        del_target = sub_tx.deletions.target(source.ref)
        closure = sub_tx.deletions.closure((del_target,), delete=True)
        sub_tx.deletions.block(req, closure, now=core.clock.utc_now())

    assert mirror_file.is_file()
    lines = load_mirror_operations(mirror_file)
    assert len(lines) == 1
    assert lines[0]["operation"]["mode"] == "delete"
    assert source.ref in lines[0]["operation"]["requested_refs_json"]


def test_mirror_tracks_operations_sharing_an_import_epoch(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    for key in ("first", "second"):
        _forget(core, ctx, _say(core, ctx, f"NameOne {key} note", key=key))
    # Shared imports can assign the same epoch to distinct source operations.
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE deletion_operations SET memory_epoch=1")
    backup_dir = tmp_path / "backups"
    catchup_deletion_mirror(core.storage, backup_dir)
    path = backup_dir / DELETION_MIRROR_FILENAME
    first_records = path.read_bytes().splitlines(keepends=True)[:2]
    path.write_bytes(b"".join(first_records))
    catchup_deletion_mirror(core.storage, backup_dir)
    assert len(load_mirror_operations(path)) == 2


def test_mirror_catchup_repairs_only_an_incomplete_tail(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    _forget(core, ctx, _say(core, ctx, "NameOne private note"))
    backup_dir = tmp_path / "backups"
    catchup_deletion_mirror(core.storage, backup_dir)
    path = backup_dir / DELETION_MIRROR_FILENAME
    complete = path.read_bytes()
    path.write_bytes(complete + b'{"format":')
    catchup_deletion_mirror(core.storage, backup_dir)
    assert len(load_mirror_operations(path)) == 1
    assert path.read_bytes() == complete


def test_mirror_catchup_does_not_append_while_another_writer_owns_the_lock(tmp_path: Path):
    core, ctx, _binding = _create_synthetic_store(tmp_path)
    _forget(core, ctx, _say(core, ctx, "NameOne private note"))
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    mirror = backup_dir / DELETION_MIRROR_FILENAME
    results = []
    with advisory_file_lock(mirror.with_suffix(".lock")):
        writer = threading.Thread(target=lambda: results.append(catchup_deletion_mirror(core.storage, backup_dir)))
        writer.start()
        writer.join(timeout=10)
        assert not writer.is_alive()
        assert results[0]["status"] == "error"
        assert not mirror.exists()
    assert catchup_deletion_mirror(core.storage, backup_dir)["status"] == "caught_up"
    assert len(load_mirror_operations(mirror)) == 1


def test_disaster_replay_preserves_unrelated_pending_capture(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    backup_dir = tmp_path / "backups"
    (binding.data_directory / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}))
    source = _say(core, ctx, "NameOne deleted note")
    capture_inbox.enqueue(
        core.storage,
        core.clock,
        ctx,
        source_event(source_event_key=source.event["source_event_key"], content=source.event["content"]),
        scope_id=next(iter(ctx.allowed_scope_ids)),
        host_scope="synthetic-host",
    )
    event = source_event(source_event_key="pending-new", content="NameOne separate pending note")
    token, _ = capture_inbox.enqueue(
        core.storage,
        core.clock,
        ctx,
        event,
        scope_id=next(iter(ctx.allowed_scope_ids)),
        host_scope="synthetic-host",
    )
    bundle = backup_dir / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle)
    _forget(core, ctx, source)
    dest = tmp_path / "restored"
    dest = Path(apply_disaster_restore(bundle, dest)["data_directory"])
    with sqlite3.connect(dest / "memory.sqlite3") as conn:
        assert conn.execute("SELECT token FROM capture_inbox").fetchall() == [(token,)]


def test_mirror_preserves_groups_created_after_snapshot(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    backup_dir = tmp_path / "backups"
    (binding.data_directory / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}))
    bundle = backup_dir / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle)
    event = source_event(
        source_event_key="segment-0",
        content="NameOne source part one",
        segment={"group_key": "message-one", "index": 0, "total": 2, "truncated": False},
    )
    receipt = core.record_event(ctx, event, scope_id=next(iter(ctx.allowed_scope_ids)), remaining_seconds=10)
    source = core.source(ctx, receipt.event_refs[0].ref, 1)
    _forget(core, ctx, source)
    dest = tmp_path / "restored"
    dest = Path(apply_disaster_restore(bundle, dest)["data_directory"])
    with sqlite3.connect(dest / "memory.sqlite3") as conn:
        digest = group_digest(binding, next(iter(ctx.allowed_scope_ids)), None, None, "message-one")
        assert conn.execute(
            "SELECT read_blocked FROM source_group_blocks WHERE group_sha256=?", (digest,)
        ).fetchone() == (1,)
    restored = InstanceBinding(
        binding.agent_id, binding.installation_id, dest, binding.scope_ids, binding.test_mode, binding.installation_kind
    )
    context = TrustedContext(restored, ctx.session_id, ctx.allowed_scope_ids, "human_direct")
    event["source_event_key"] = "segment-1"
    event["segment"]["index"] = 1
    event["content"] = "NameOne later source part"
    with pytest.raises(ContractError):
        MemoryCore(CoreConfig(restored)).record_event(
            context,
            event,
            scope_id=next(iter(ctx.allowed_scope_ids)),
            remaining_seconds=10,
        )


def test_manual_backup_establishes_deletion_protection(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    source = _say(core, ctx, "NameOne manual backup note")
    bundle = tmp_path / "backups" / "bundle.zip"
    snapshot_time = datetime(2026, 10, 20, tzinfo=UTC)
    receipt = create_backup_bundle(binding=binding, output=bundle, now=snapshot_time)
    assert Path(receipt["deletion_mirror"]).is_file()
    _forget(core, ctx, source)
    dest = tmp_path / "restored"
    result = apply_disaster_restore(bundle, dest, now=snapshot_time + timedelta(days=3))
    dest = Path(result["data_directory"])
    assert result["mirror_replayed_operations"] == 1
    assert result["unrecoverable_later_interval"]["start_utc"] == receipt["created_at_utc"]
    assert result["unrecoverable_later_interval"]["duration_seconds"] == 3 * 86400
    assert result["deletion_protection"]["status"] == "replayed"
    assert result["deletion_protection"]["mirror_path"] == receipt["deletion_mirror"]
    with sqlite3.connect(dest / "memory.sqlite3") as conn:
        assert conn.execute("SELECT read_blocked FROM source_events WHERE event_id=?", (source.ref,)).fetchone() == (1,)
    recovered_config = json.loads((dest / "backup-config.json").read_text())
    assert Path(recovered_config["backup_dir"]) == Path(receipt["deletion_mirror"]).parent
    assert recovered_config["snapshots_enabled"] is False


def test_missing_or_foreign_mirror_refuses_before_extraction(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "original" / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle)
    moved = tmp_path / "moved" / "bundle.zip"
    moved.parent.mkdir()
    moved.write_bytes(bundle.read_bytes())
    dest = tmp_path / "restored"
    with pytest.raises(DisasterRestoreError, match="mirror is required"):
        apply_disaster_restore(moved, dest)
    assert not dest.exists()
    other_core, _, _ = _create_synthetic_store(tmp_path / "other")
    catchup_deletion_mirror(other_core.storage, moved.parent)
    with pytest.raises(ValueError, match="does not identify"):
        apply_disaster_restore(moved, dest)
    assert not dest.exists()


def test_corrupt_complete_mirror_record_is_not_silently_repaired(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    backup_dir = tmp_path / "backups"
    (binding.data_directory / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}))
    catchup_deletion_mirror(core.storage, backup_dir)
    path = backup_dir / DELETION_MIRROR_FILENAME
    damaged = path.read_bytes() + b'{"broken":\n'
    path.write_bytes(damaged)
    assert catchup_deletion_mirror(core.storage, backup_dir)["status"] == "error"
    assert path.read_bytes() == damaged
    report = DoctorReport(host="hermes", status="ok")
    check_deletion_mirror(report, binding.data_directory)
    assert "deletion_mirror_lag" in report.capability_gaps


def test_idle_runtime_wakes_for_backup_without_model_work(tmp_path: Path, monkeypatch):
    _, _, binding = _create_synthetic_store(tmp_path)
    backup_dir = tmp_path / "backups"
    (binding.data_directory / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}))
    config = RuntimeInstanceConfig.from_mapping(
        {
            "binding": {
                "agent_id": binding.agent_id,
                "installation_id": binding.installation_id,
                "data_directory": str(binding.data_directory),
                "scope_ids": sorted(binding.scope_ids),
                "test_mode": binding.test_mode,
                "installation_kind": binding.installation_kind,
            },
            "session_id": "backup-worker",
            "allowed_scope_ids": sorted(binding.scope_ids),
            "auxiliary": {"external_embedding": False, "external_consolidation": False},
        }
    )
    moment = datetime.now(UTC)
    plan = next_wake(config, now=moment)
    assert plan.due_at is not None
    assert plan.reason in {"daily_backup", "deletion_mirror"}
    instance = build_runtime_instance(config)
    try:
        instance.drain(purge_only=True)
        assert instance.backup_upkeep["status"] in {"caught_up", "up_to_date"}
        assert not list(backup_dir.glob("*.zip"))
    finally:
        instance.close()
    monkeypatch.setattr(worker_entry, "load_config", lambda _: config)
    output = StringIO()
    assert worker_entry.run_backup_worker(binding.data_directory / "runtime-config.json", output=output) == 0
    assert json.loads(output.getvalue())["backup_outcome"] == "backed_up"
    state = json.loads((binding.data_directory / "backup-state.json").read_text())
    assert verify_backup_bundle(Path(state["last_bundle"]))["status"] == "verified"
    later = next_wake(config, now=datetime.now(UTC))
    assert datetime.fromisoformat(later.due_at.replace("Z", "+00:00")) > moment


def _listed(path: str, data: bytes) -> dict[str, object]:
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def _repack(
    target: Path,
    manifest: dict[str, object],
    payload: dict[str, bytes],
    *,
    duplicate: str | None = None,
) -> None:
    """Write deliberately unsafe names unchanged, then verify the fixture itself."""
    entries = [("manifest.json", json.dumps(manifest).encode("utf-8"))]
    for name, data in payload.items():
        entries.append((name, data))
        if name == duplicate:
            entries.append((name, data))
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    with zipfile.ZipFile(target, "r") as archive:
        names = archive.namelist()
    if duplicate is not None and names.count(duplicate) != 2:
        raise AssertionError(f"{duplicate} was not stored twice")
    for name, _data in entries:
        if name not in names:
            raise AssertionError(f"{name} was not stored as an archive member")


def _untouched(sentinel: Path, original: bytes, dest: Path) -> None:
    assert sentinel.read_bytes() == original
    assert not dest.exists()


@pytest.mark.parametrize("kind", ["parent", "absolute", "extra", "collision", "duplicate", "manifest"])
def test_unsafe_archive_members_do_not_escape(tmp_path: Path, kind: str):
    core, _ctx, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle)
    with zipfile.ZipFile(bundle, "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        payload = {
            info.filename: archive.read(info.filename)
            for info in archive.infolist()
            if info.filename != "manifest.json"
        }
    sentinel = tmp_path / "victim"
    sentinel.write_bytes(b"sentinel")
    original = sentinel.read_bytes()
    dest = tmp_path / "restore-target"
    duplicate = None
    if kind == "parent":
        payload["../victim"] = b"escaped"
        expected = ".."
    elif kind == "absolute":
        payload[sentinel.resolve().as_posix()] = b"escaped"
        expected = "drive" if os.name == "nt" else "absolute"
    elif kind == "extra":
        payload["unlisted.txt"] = b"unverified"
        expected = "not listed in the manifest"
    elif kind == "collision":
        blob = b"not-the-database"
        payload["configs/memory.sqlite3"] = blob
        manifest["files"] = [*manifest["files"], _listed("configs/memory.sqlite3", blob)]
        expected = "same destination"
    elif kind == "duplicate":
        duplicate = "database.sqlite3"
        expected = "duplicate"
    else:
        manifest["files"] = [*manifest["files"], _listed("manifest.json", b"{}")]
        expected = "manifest.json is listed as a payload member"
    bad = tmp_path / f"bad-{kind}.zip"
    _repack(bad, manifest, payload, duplicate=duplicate)

    with pytest.raises(BackupBundleError) as verify_error:
        verify_backup_bundle(bad)
    assert expected in str(verify_error.value)
    _untouched(sentinel, original, dest)

    with pytest.raises(BackupBundleError) as restore_error:
        apply_disaster_restore(bad, dest)
    assert expected in str(restore_error.value)
    _untouched(sentinel, original, dest)
    assert core.storage.path.is_file()


def test_normal_bundle_restore_stays_inside_destination(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    source = _say(core, ctx, "NameOne stays inside the restore root", key="inside")
    bundle = tmp_path / "good.zip"
    create_backup_bundle(binding=binding, output=bundle)
    sentinel = tmp_path / "victim"
    sentinel.write_bytes(b"sentinel")
    dest = tmp_path / "restored-good"
    verify_backup_bundle(bundle)
    receipt = apply_disaster_restore(bundle, dest)
    dest = Path(receipt["data_directory"])

    assert receipt["status"] == "restored"
    assert sentinel.read_bytes() == b"sentinel"
    assert (dest / "memory.sqlite3").is_file()
    assert not (tmp_path / "memory.sqlite3").exists()
    restored = InstanceBinding(
        binding.agent_id,
        binding.installation_id,
        dest,
        binding.scope_ids,
        binding.test_mode,
        binding.installation_kind,
    )
    restored_core = MemoryCore(CoreConfig(restored))
    restored_ctx = TrustedContext(restored, "restored_session", ctx.allowed_scope_ids, "human_direct")
    found = restored_core.source(restored_ctx, source.ref, 1)
    assert found is not None
    assert "NameOne stays inside the restore root" in found.event["content"]


def test_verify_rehearses_disaster_restore_with_the_deletion_mirror(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    _say(core, ctx, "NameOne keeps a blue notebook", key="kept")
    removed = _say(core, ctx, "NameOne removes a green notebook", key="removed")
    bundle = tmp_path / "backups" / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle)
    _forget(core, ctx, removed)
    result = verify_backup_bundle(bundle, temp_restore_check=True)
    rehearsal = result["restore_check"]
    assert rehearsal["status"] == "restored"
    assert rehearsal["doctor"]["binding_verified"] is True
    assert rehearsal["mirror_replayed_operations"] == 1
    assert rehearsal["vector_rebuild_queued"] >= 1
    assert rehearsal["deletion_protection"]["status"] == "replayed"
    assert core.source(ctx, removed.ref, 1) is None


def test_restore_keeps_management_audit_suppression_search_and_export(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    muted = _say(core, ctx, "NameOne keeps a blue notebook", key="muted")
    _say(core, ctx, "NameOne keeps a green notebook", key="visible")
    owner = MemoryManagement(core, InstallationMaintenance(replace(ctx, allowed_scope_ids=binding.scope_ids)))
    owner.apply(owner.preview([muted.ref], mode="suppress"))
    filters = MemoryFilter(kind="event", query="NameOne keeps")
    before = owner.list(filters)
    with owner.export_reader(filters) as reader:
        exported = list(reader.iter_sources())
    with core.storage.read(ctx) as tx:
        audit = [tuple(row) for row in tx._check().execute("SELECT * FROM management_actions ORDER BY action_id")]
    bundle = tmp_path / "backups" / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle)
    result = apply_disaster_restore(bundle, tmp_path / "restored")
    restored = replace(binding, data_directory=Path(result["data_directory"]))
    restored_ctx = replace(ctx, binding=restored)
    restored_core = MemoryCore(CoreConfig(restored))
    restored_owner = MemoryManagement(
        restored_core, InstallationMaintenance(replace(restored_ctx, allowed_scope_ids=restored.scope_ids))
    )
    assert restored_owner.list(filters)["items"] == before["items"]
    assert restored_core.source(restored_ctx, muted.ref, 1).suppressed
    assert muted.ref not in {row.ref for row in restored_core.search_sources(restored_ctx, "blue", automatic=True)}
    with restored_owner.export_reader(filters) as reader:
        assert list(reader.iter_sources()) == exported
    with restored_core.storage.read(restored_ctx) as tx:
        recovered = [tuple(row) for row in tx._check().execute("SELECT * FROM management_actions ORDER BY action_id")]
    assert all(row in recovered for row in audit)
    assert any(row[2] == "restore" for row in recovered)
    restored_owner.apply(restored_owner.preview([muted.ref], mode="unmute"))
    assert muted.ref in {row.ref for row in restored_core.search_sources(restored_ctx, "blue", automatic=True)}


def test_manifest_entry_paths_are_rejected_before_extraction(tmp_path: Path):
    _core, _ctx, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle)
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        payload = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
    manifest["entries"] = [{"entry_id": "../../../victim", "display_name": "agent1", "host": "codex"}]
    document = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    bad = tmp_path / "unsafe-entry.zip"
    _repack(bad, manifest, payload)
    victim = tmp_path / "victim" / "runtime-config.json"
    victim.parent.mkdir()
    victim.write_text('{"binding":{"data_directory":"sentinel"}}')
    dest = tmp_path / "restored"
    with pytest.raises(BackupBundleError, match="invalid backup manifest"):
        apply_disaster_restore(bad, dest)
    assert not dest.exists()
    assert json.loads(victim.read_text())["binding"]["data_directory"] == "sentinel"


def test_backup_entry_uses_its_own_window_without_reserving_model_work(tmp_path, monkeypatch):
    config = SimpleNamespace(
        binding=SimpleNamespace(data_directory=tmp_path, installation_id="synthetic-backup"),
        owner_id="synthetic-owner",
        drain_seconds=0.01,
    )
    budgets = []
    monkeypatch.setattr(worker_entry, "load_config", lambda path: config)
    monkeypatch.setattr(worker_entry, "SQLiteStorage", lambda binding: SimpleNamespace(binding=binding))
    monkeypatch.setattr(worker_entry, "build_runtime_instance", lambda cfg: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(worker_entry, "_reserve_daily_work", lambda cfg: (_ for _ in ()).throw(AssertionError()))

    def backup(storage, cfg, *, available_seconds):
        budgets.append(available_seconds)
        return {"outcome": "backed_up"}

    monkeypatch.setattr(worker_entry, "backup_if_due", backup)
    output = StringIO()
    assert worker_entry.run_backup_worker(tmp_path / "runtime-config.json", output=output) == 0
    assert 1700 < budgets[0] <= worker_entry.BACKUP_JOB_SECONDS + 0.001
    assert '"backup_outcome":"backed_up"' in output.getvalue()
    assert not (tmp_path / "runtime-worker-day.json").exists()
    assert not (tmp_path / "runtime-worker-status.json").exists()


def test_due_backup_has_priority_over_old_queue_timestamps(monkeypatch):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    config = SimpleNamespace(binding=SimpleNamespace(data_directory="synthetic"))
    monkeypatch.setattr(scheduling, "next_backup_wake", lambda directory, moment: (now, "daily_backup"))
    plan = scheduling._wake_plan(config, now, [(now - timedelta(days=1), "work_available")], 1, 0, 0)
    assert plan.reason == "daily_backup"


def test_backup_job_does_not_consume_a_supervisor_drain(tmp_path, monkeypatch):
    now = datetime(2026, 1, 2, tzinfo=UTC)
    elapsed = [0.0]
    calls = []
    done = []
    config = SimpleNamespace(
        supervisor_seconds=3600,
        supervisor_max_drains=1,
        worker_min_interval_seconds=0,
        drain_seconds=0.01,
        auto_retry_cooldown_seconds=60,
        auxiliary=None,
    )

    class Control:
        def __init__(self, cfg):
            self.value = {"wake_revision": 1}

        def request(self):
            pass

        def read(self):
            return dict(self.value)

        def update(self, **fields):
            self.value.update(fields)

        def close_if_unchanged(self, revision, **fields):
            self.update(**fields)
            return True

    monkeypatch.setattr(scheduling, "load_config", lambda path: config)
    monkeypatch.setattr(scheduling, "SupervisorControl", Control)
    monkeypatch.setattr(scheduling, "_acquire_ownership", lambda control: SimpleNamespace(__exit__=lambda *args: None))
    monkeypatch.setattr(scheduling, "read_control", lambda cfg: None)

    def planner(cfg, *, now, unavailable_until):
        reason = "work_available" if done else "daily_backup"
        return scheduling.WakePlan(now.isoformat(), reason)

    def backup(remaining):
        calls.append(("backup", remaining))
        elapsed[0] += 120
        done.append(True)
        return 0, {"backup_outcome": "backed_up"}

    def drain(remaining):
        calls.append(("drain", remaining))
        return 0, {"completed": 0}

    assert (
        scheduling.supervise(
            tmp_path / "config.json",
            drain,
            backup_once=backup,
            planner=planner,
            clock=lambda: elapsed[0],
            sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
            utc_now=lambda: now + timedelta(seconds=elapsed[0]),
        )
        == 0
    )
    assert calls == [("backup", scheduling.BACKUP_JOB_SECONDS), ("drain", config.drain_seconds)]


def test_backup_watchdog_honors_cancel_before_its_deadline(monkeypatch):
    waits = []

    def wait(child, deadline, *, cancelled=None):
        waits.append((deadline, cancelled))
        return cancelled is None

    monkeypatch.setattr(worker_watchdog, "_wait_for_exit", wait)
    monkeypatch.setattr(worker_watchdog.time, "monotonic", lambda: 10)
    assert worker_watchdog._owned_job_failure(object(), 1800, cancelled=lambda: True) == "backup_cancelled"
    assert waits[0][0] == 1800 + worker_watchdog.KILL_GRACE_SECONDS
    assert waits[1] == (10 + worker_watchdog.KILL_GRACE_SECONDS, None)


def test_backup_timeout_records_its_state_without_overwriting_model_status(tmp_path, monkeypatch):
    failures = []
    config = SimpleNamespace(binding=SimpleNamespace(data_directory=tmp_path))
    monkeypatch.setattr(worker_watchdog, "record_backup_job_failure", lambda *args: failures.append(args))
    monkeypatch.setattr(
        worker_watchdog, "persist_worker_status", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    )
    worker_watchdog._persist_job_status(
        config,
        {"capability_gaps": ["backup_deadline_exceeded"]},
        job_kind="backup",
        started_at="synthetic",
        exit_code=124,
        worker_pid=1,
    )
    assert failures == [(tmp_path, "backup deadline exceeded")]


def test_backup_pause_check_observes_the_switch_within_one_second(monkeypatch):
    elapsed = [0.0]
    enabled = [True]
    monkeypatch.setattr(worker_entry.time, "monotonic", lambda: elapsed[0])
    check = worker_entry.backup_pause_check(object(), lambda config: not enabled[0])
    assert not check()
    enabled[0] = False
    elapsed[0] = 0.5
    assert not check()
    elapsed[0] = 1.0
    assert check()
