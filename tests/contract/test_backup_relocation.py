"""Disaster recovery remains usable through the real installation loaders."""

import hashlib
import json
import shlex
import sqlite3
import sys
import threading
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from scope_recall import contracts as contract_module
from scope_recall.adapters.clients.config import CodexConfigError, install_codex_scope_recall, load_codex_config
from scope_recall.adapters.hermes.installation import (
    HermesIdentityError,
    build_installation_manifest,
    install_hermes_scope_recall,
    load_installation_manifest,
)
from scope_recall.adapters.hermes.shared_entries import (
    attach_shared_record,
    read_shared_payload,
    shared_entry_manifest,
    shared_entry_record,
)
from scope_recall.contracts import TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.maintenance.backup_bundle import BackupBundleError, create_backup_bundle, verify_backup_bundle
from scope_recall.maintenance.cli import main
from scope_recall.maintenance.disaster_restore import apply_disaster_restore
from scope_recall.maintenance.doctor import load_binding
from scope_recall.maintenance.shared import init_shared, store_binding
from v11_support import source_event


@pytest.fixture(autouse=True)
def _immediate_mirror_attempt_fits_a_slow_runner(monkeypatch):
    """These tests check what reaches the mirror; the runner's disk speed must not decide it."""
    from scope_recall.core import deletion_mirror

    monkeypatch.setattr(deletion_mirror, "IMMEDIATE_MIRROR_SECONDS", 60.0)


@pytest.mark.parametrize("tree", ["spool", "retained", "kept-captures", "vectors", "database", "codex-config"])
@pytest.mark.parametrize("alias", [False, True])
def test_credential_overlap_with_any_copied_tree_refuses_backup(tmp_path, tree, alias):
    binding, _ = local_store(tmp_path, "codex")
    state = tmp_path / "remote-state"
    (state / "spool").mkdir(parents=True)
    if tree in {"database", "codex-config"}:
        credential = (
            binding.data_directory / "memory.sqlite3"
            if tree == "database"
            else binding.data_directory.parent / "codex-installation.json"
        )
    else:
        root = state / "spool" if tree == "spool" else binding.data_directory / tree
        root.mkdir(exist_ok=True)
        credential = root / "credential.ref"
        credential.write_text("ordinary-pass")
    reference = tmp_path / "credential.ref" if alias else credential
    if alias:
        reference.hardlink_to(credential)
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"state_dir": str(state), "token_file": str(reference)}))
    output = tmp_path / "backup.zip"
    with pytest.raises(BackupBundleError, match="credential"):
        create_backup_bundle(binding=binding, output=output, client_configs=(config,), with_vectors=True)
    assert not output.exists()


@pytest.mark.parametrize("host", ["codex", "hermes", "shared"])
def test_restored_copy_owns_retention_and_task_identity(tmp_path, monkeypatch, host):
    from scope_recall.maintenance import autostart
    from scope_recall.runtime.backup_rotation import rotate_backup_bundles
    from scope_recall.runtime.instance_config import RuntimeInstanceConfig

    monkeypatch.setattr(autostart, "wake_command", lambda python, config: [str(python), "--config", str(config)])

    binding, _ = local_store(tmp_path, host)
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    recovered = apply_disaster_restore(bundle, tmp_path / "restored")
    if host == "shared":
        data = Path(recovered["data_directory"])
        payload = read_shared_payload(data)
        restored = store_binding(payload, data, frozenset(payload["scope_ids"]))
    else:
        restored, data = load_binding(host, Path(recovered["destination"]))

    def configuration(path):
        held = binding if Path(path).parent == binding.data_directory else restored
        return RuntimeInstanceConfig(binding=held, session_id="session1", allowed_scope_ids=held.scope_ids)

    monkeypatch.setattr(autostart, "load_config", configuration)
    original_task = autostart.plan(
        binding.data_directory / "runtime-config.json", Path(sys.executable), user_id="user1"
    )
    restored_task = autostart.plan(data / "runtime-config.json", Path(sys.executable), user_id="user1")
    assert restored_task["task_name"] != original_task["task_name"]
    from scope_recall.runtime.scheduling import control_path, read_control
    from scope_recall.runtime.worker_entry import write_worker_metadata

    restored_config = configuration(data / "runtime-config.json")
    write_worker_metadata(control_path(restored_config), restored_task)
    assert read_control(restored_config)["task_name"] == restored_task["task_name"]
    write_worker_metadata(control_path(restored_config), original_task)
    with pytest.raises(ValueError, match="autostart_copy_identity_invalid"):
        read_control(restored_config)
    write_worker_metadata(control_path(restored_config), restored_task)
    output = tmp_path / "scope-recall-backup-20260101T000000Z.zip"
    from datetime import UTC, datetime

    create_backup_bundle(binding=restored, output=output, now=datetime(2026, 1, 1, tzinfo=UTC))
    with zipfile.ZipFile(output) as archive:
        copied = json.loads(archive.read("manifest.json"))
    with zipfile.ZipFile(bundle) as archive:
        original = json.loads(archive.read("manifest.json"))
    assert copied["copy_id"] != original["copy_id"]
    rotated = rotate_backup_bundles(tmp_path, installation_id=binding.installation_id, copy_id=original["copy_id"])
    assert rotated.retained_total == 0
    assert output.exists()


def test_existing_task_registration_keeps_its_name_when_adopting_copy_identity(tmp_path, monkeypatch):
    from scope_recall.maintenance import autostart
    from scope_recall.runtime.instance_config import RuntimeInstanceConfig
    from scope_recall.runtime.scheduling import control_path, read_control
    from scope_recall.runtime.worker_entry import write_worker_metadata

    monkeypatch.setattr(autostart, "wake_command", lambda python, config: [str(python), "--config", str(config)])

    binding, _ = local_store(tmp_path, "codex")
    config = RuntimeInstanceConfig(binding=binding, session_id="session1", allowed_scope_ids=binding.scope_ids)
    legacy_name = "ScopeRecall-" + hashlib.sha256(binding.installation_id.encode()).hexdigest()[:20]
    write_worker_metadata(
        control_path(config), {"installation_id": binding.installation_id, "enabled": True, "task_name": legacy_name}
    )
    monkeypatch.setattr(autostart, "load_config", lambda _: config)
    prepared = autostart.plan(binding.data_directory / "runtime-config.json", Path(sys.executable), user_id="user1")
    assert prepared["task_name"] == legacy_name
    assert prepared["copy_id"]
    write_worker_metadata(control_path(config), prepared)
    assert read_control(config)["copy_id"] == prepared["copy_id"]


@pytest.mark.parametrize("foreign_path", ["/foreign/store/vectors/wrong-space", "C:\\foreign\\store\\vectors\\space1"])
def test_foreign_vector_path_uses_embedding_identity(tmp_path, monkeypatch, foreign_path):
    from pathlib import PurePosixPath

    from scope_recall.core.recall_policy import SPACE_ID
    from scope_recall.maintenance import disaster_restore
    from scope_recall.maintenance.disaster_restore import _rewrite_extracted_configs

    config = {
        "vector": {"storage_dir": foreign_path},
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }
    (tmp_path / "runtime-config.json").write_text(json.dumps(config))
    monkeypatch.setattr(disaster_restore, "Path", PurePosixPath)
    _rewrite_extracted_configs(tmp_path, {"entries": [], "resources": []})
    restored = json.loads((tmp_path / "runtime-config.json").read_text())
    assert Path(restored["vector"]["storage_dir"]) == tmp_path / "vectors" / SPACE_ID


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE_MEMBERS", "MAX_EXPANDED_BYTES", "MAX_MANIFEST_BYTES"])
def test_creation_cannot_publish_a_bundle_above_restore_limits(tmp_path, monkeypatch, limit):
    from scope_recall.runtime import backup_bundle as bundles

    binding, _ = local_store(tmp_path, "codex")
    monkeypatch.setattr(bundles, limit, 1)
    output = tmp_path / "backup.zip"
    with pytest.raises(BackupBundleError, match="limit"):
        create_backup_bundle(binding=binding, output=output)
    assert not output.exists()


def test_backup_blocks_shared_commands_through_assembly(tmp_path, monkeypatch):
    from scope_recall.core.file_lock import advisory_file_lock
    from scope_recall.runtime import backup_bundle as bundles

    root = tmp_path / "shared"
    init_shared(root=root, agent_id="agent1", test_mode=True)
    local = build_installation_manifest(tmp_path / "agent1", agent_id="agent1", test_mode=True)
    attach_shared_record(
        root,
        shared_entry_record(local, entry_id="agent1", display_name="agent1", attached_at="2026-01-01T00:00:00Z"),
        now="2026-01-01T00:00:00Z",
    )
    payload = read_shared_payload(root)
    binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
    actual_stage = bundles._stage_database

    def stage(*args):
        results = []

        def attempt():
            try:
                with advisory_file_lock(root / "commands.lock", timeout_seconds=0):
                    results.append("unlocked")
            except TimeoutError:
                results.append("locked")

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(5)
        assert results == ["locked"]
        actual_stage(*args)

    monkeypatch.setattr(bundles, "_stage_database", stage)
    create_backup_bundle(binding=binding, output=tmp_path / "backup.zip")


def test_shared_staged_manifest_matches_entry_inventory(tmp_path, monkeypatch):
    from scope_recall.runtime import backup_bundle as bundles

    root = tmp_path / "shared"
    init_shared(root=root, agent_id="agent1", test_mode=True)
    local = build_installation_manifest(tmp_path / "agent1", agent_id="agent1", test_mode=True)
    attach_shared_record(
        root,
        shared_entry_record(local, entry_id="agent1", display_name="agent1", attached_at="2026-01-01T00:00:00Z"),
        now="2026-01-01T00:00:00Z",
    )
    payload = read_shared_payload(root)
    binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
    actual_stage = bundles._stage_named_configs

    def stage(data, target, files):
        payload = json.loads((data / "installation.json").read_text())
        payload["entries"].append({"entry_id": "agent2", "host": "hermes", "display_name": "agent2"})
        (data / "installation.json").write_text(json.dumps(payload))
        return actual_stage(data, target, files)

    monkeypatch.setattr(bundles, "_stage_named_configs", stage)
    with pytest.raises(BackupBundleError, match="entries"):
        create_backup_bundle(binding=binding, output=tmp_path / "backup.zip")


def test_unused_backup_cli_handlers_are_absent():
    from scope_recall.maintenance import cli

    assert not hasattr(cli, "_add_backup_arguments")
    assert not hasattr(cli, "_backup")


def test_deletions_in_scopes_added_after_snapshot_survive_without_new_grants(tmp_path):
    root = tmp_path / "shared"
    init_shared(root=root, agent_id="agent1", test_mode=True)
    stamp = "2026-01-01T00:00:00Z"
    first = build_installation_manifest(tmp_path / "agent1", agent_id="agent1", test_mode=True)
    record = shared_entry_record(first, entry_id="agent1", display_name="agent1", attached_at=stamp)
    attach_shared_record(root, record, now=stamp)
    before = read_shared_payload(root)
    binding = store_binding(before, root, frozenset(before["scope_ids"]))
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    second = build_installation_manifest(tmp_path / "agent2", agent_id="agent2", test_mode=True)
    added = shared_entry_record(second, entry_id="agent2", display_name="agent2", attached_at=stamp)
    attach_shared_record(root, added, now=stamp)
    after = read_shared_payload(root)
    expanded = store_binding(after, root, frozenset(after["scope_ids"]))
    scope = next(iter(set(added["scope_ids"]) - set(before["scope_ids"])))
    core = MemoryCore(CoreConfig(expanded))
    ctx = TrustedContext(expanded, "session1", frozenset({scope}), "human_direct", entry_id="agent2")
    saved = core.record_event(ctx, source_event(content="NameOne later note"), scope_id=scope, remaining_seconds=10)
    ref = saved.event_refs[0].ref
    core.record_event(
        ctx, source_event(source_event_key="delete1", content=f"删除 {ref}"), scope_id=scope, remaining_seconds=10
    )
    core.forget(
        ctx,
        {"protocol_version": "1.1", "mode": "delete", "target_refs": [ref], "expected_revisions": {ref: 1}},
        remaining_seconds=10,
    )
    result = apply_disaster_restore(bundle, tmp_path / "recovered")
    recovered = Path(result["data_directory"])
    manifest = read_shared_payload(recovered)
    assert manifest["scope_ids"] == before["scope_ids"]
    assert [item["entry_id"] for item in manifest["entries"]] == ["agent1"]
    assert manifest["entries"][0]["audiences"] == before["entries"][0]["audiences"]
    with sqlite3.connect(recovered / "memory.sqlite3") as conn:
        assert conn.execute(
            "SELECT read_blocked FROM object_blocks WHERE object_ref=? AND scope_id=?", (ref, scope)
        ).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM source_events WHERE scope_id=?", (scope,)).fetchone() == (0,)


def test_deadline_during_archive_io_does_not_publish_or_touch_unowned_file(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scope_recall.runtime import backup_bundle as bundles
    from scope_recall.runtime import backup_primitives as io

    binding, _ = local_store(tmp_path, "codex")
    output = tmp_path / "backup.zip"
    unowned = tmp_path / ".backup.zip.tmp"
    unowned.write_bytes(b"NameOne file")
    clock = [0.0]
    monkeypatch.setattr(io, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    actual_copy = bundles.copy_stream
    writes = []

    def slow_copy(source, target):
        # Zip payload streams are the publication I/O, after staging finished.
        if isinstance(target, zipfile._ZipWriteFile):
            assert list(output.parent.glob(".backup.zip.*.tmp")), "publication must write through its owned descriptor"
            target.write(source.read(16))
            writes.append(1)
            clock[0] = 100.0
        actual_copy(source, target)

    monkeypatch.setattr(bundles, "copy_stream", slow_copy)
    with pytest.raises(BackupBundleError, match="deadline"):
        create_backup_bundle(binding=binding, output=output, deadline=10.0)
    assert writes
    assert not output.exists()
    assert unowned.read_bytes() == b"NameOne file"
    assert [path.name for path in tmp_path.glob(".backup.zip.*.tmp")] == []


def test_deadline_during_mirror_io_checkpoints_and_saves_retry_state(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scope_recall.core import deletion_mirror
    from scope_recall.runtime import backup_primitives as io
    from scope_recall.runtime import backup_upkeep as upkeep

    binding, core = local_store(tmp_path, "codex")
    scope = sorted(binding.scope_ids)[0]
    ctx = TrustedContext(binding, "session1", frozenset({scope}), "human_direct")
    for index in range(3):
        saved = core.record_event(
            ctx,
            source_event(source_event_key=f"note{index}", content="NameOne note"),
            scope_id=scope,
            remaining_seconds=10,
        )
        ref = saved.event_refs[0].ref
        core.record_event(
            ctx,
            source_event(source_event_key=f"delete{index}", content=f"删除 {ref}"),
            scope_id=scope,
            remaining_seconds=10,
        )
        core.forget(
            ctx,
            {"protocol_version": "1.1", "mode": "delete", "target_refs": [ref], "expected_revisions": {ref: 1}},
            remaining_seconds=10,
        )
    directory = binding.data_directory
    backup_dir = tmp_path / "backups"
    (directory / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}))
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(io, "time", fake_time)
    monkeypatch.setattr(upkeep, "time", fake_time)
    actual_records = deletion_mirror._truth_records

    def slow_records(*args, **kwargs):
        for item in actual_records(*args, **kwargs):
            yield item
            clock[0] = 100.0

    monkeypatch.setattr(deletion_mirror, "_truth_records", slow_records)
    from scope_recall.core.storage import SQLiteStorage

    result = upkeep.backup_if_due(SQLiteStorage(binding), None, available_seconds=10)
    assert result["outcome"] == "failed"
    assert "deadline" in result["error"]
    assert json.loads((directory / "backup-state.json").read_text())["last_error"]
    mirror = backup_dir / "deletion-mirror.jsonl"
    assert mirror.read_bytes().endswith(b"\n")
    assert len(mirror.read_bytes().splitlines()) == 1, "catch-up must stop between actual journal writes"
    monkeypatch.setattr(deletion_mirror, "_truth_records", actual_records)
    clock[0] = 0.0
    assert upkeep.backup_if_due(SQLiteStorage(binding), None, available_seconds=10)["outcome"] == "skipped_not_due"


def local_store(tmp_path, host):
    home = tmp_path / "original"
    if host == "shared":
        init_shared(root=home, agent_id="agent1", test_mode=True)
        stamp = "2026-01-01T00:00:00Z"
        local = build_installation_manifest(tmp_path / "agent1", agent_id="agent1", test_mode=True)
        attach_shared_record(
            home, shared_entry_record(local, entry_id="agent1", display_name="agent1", attached_at=stamp), now=stamp
        )
        payload = read_shared_payload(home)
        binding = store_binding(payload, home, frozenset(payload["scope_ids"]))
        return binding, MemoryCore(CoreConfig(binding))
    if host == "hermes":
        return install_hermes_scope_recall(home, agent_id="agent1", test_mode=True)
    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(home, project_root=project, agent_id="agent1")
    return config.to_binding(), core


@pytest.mark.parametrize("host", ["hermes", "codex"])
def test_restore_binds_through_the_real_host_and_keeps_truth_identity(tmp_path, host):
    binding, core = local_store(tmp_path, host)
    scope = sorted(binding.scope_ids)[0]
    ctx = TrustedContext(binding, "TEST-session", frozenset({scope}), "human_direct")
    saved = core.record_event(
        ctx, source_event(content="NameOne keeps a blue notebook"), scope_id=scope, remaining_seconds=10
    )
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    home = tmp_path / "relocated"
    result = apply_disaster_restore(bundle, home)
    restored, data = load_binding(host, home)
    assert restored.installation_id == binding.installation_id
    assert data.is_relative_to(home)
    restored_core = MemoryCore(CoreConfig(restored))
    found = restored_core.source(replace(ctx, binding=restored), saved.event_refs[0].ref, 1)
    assert found is not None and found.event["content"] == "NameOne keeps a blue notebook"
    assert result["doctor"]["binding_verified"] is True
    assert result["reregistration_commands"]
    copied_home = tmp_path / "copied"
    if host == "hermes":
        copied = copied_home / "scope-recall" / "installation.json"
        source = data / "installation.json"
    else:
        copied = copied_home / "codex-installation.json"
        source = home / "codex-installation.json"
    copied.parent.mkdir(parents=True)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["data_directory"] = str(copied_home / ("scope-recall" if host == "hermes" else "data"))
    copied.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((HermesIdentityError, CodexConfigError), match="installation_id mismatch"):
        load_installation_manifest(copied_home) if host == "hermes" else load_codex_config(copied)


@pytest.mark.parametrize("host", ["hermes", "codex"])
def test_recovery_install_commands_reach_the_normal_planner(tmp_path, host, capsys):
    binding, _core = local_store(tmp_path, host)
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    result = apply_disaster_restore(bundle, tmp_path / "relocated")
    arguments = result["reregistration_argv"][0][1:]
    assert arguments[0] == "plan-install"
    replacements = {
        "<host-python>": sys.executable,
        "<plugin-directory>": str(tmp_path / "plugin"),
        "<project-directory>": str(tmp_path / "project"),
    }
    arguments = [replacements.get(value, value) for value in arguments]
    assert main(arguments) == 0
    assert not json.loads(capsys.readouterr().out)["conflicts"]


def test_shared_restore_keeps_each_entry_grants_and_local_resources(tmp_path, capsys):
    root = tmp_path / "shared"
    init_shared(root=root, agent_id="agent1", test_mode=True)
    now = "2026-01-01T00:00:00Z"
    for entry_id in ("agent1", "agent2"):
        home = tmp_path / entry_id
        local = build_installation_manifest(home, agent_id="agent1", agent_workspace=entry_id, test_mode=True)
        record = shared_entry_record(local, entry_id=entry_id, display_name=entry_id, attached_at=now)
        attach_shared_record(root, record, now=now)
    payload = read_shared_payload(root)
    binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
    # This entry's spend file is independent of the shared worker's ledger.
    entry_dir = tmp_path / "agent1" / "scope-recall"
    ledger = entry_dir / "auxiliary-budget.sqlite3"
    with sqlite3.connect(ledger) as connection:
        connection.execute("CREATE TABLE preserved_spend(amount INTEGER)")
        connection.execute("INSERT INTO preserved_spend VALUES(23)")
    entry_config = {
        "binding": {
            "agent_id": binding.agent_id,
            "installation_id": binding.installation_id,
            "data_directory": str(root),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": True,
            "installation_kind": "shared",
        },
        "session_id": "session1",
        "allowed_scope_ids": sorted(binding.scope_ids),
        "auxiliary": {"external_embedding": False, "external_consolidation": False, "ledger_path": str(ledger)},
    }
    (entry_dir / "runtime-config.json").write_text(json.dumps(entry_config), encoding="utf-8")
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    result = apply_disaster_restore(bundle, tmp_path / "restored")
    restored_root = Path(result["data_directory"])
    with zipfile.ZipFile(bundle) as archive:
        assert "entries/agent1/runtime-config.json" in archive.namelist()
    commands = result["reregistration_argv"]
    assert len(commands) == 2
    for command in commands:
        entry = command[command.index("--entry") + 1]
        home = tmp_path / f"new-{entry}"
        arguments = [str(home) if value == f"<new-home-for-{entry}>" else value for value in command[1:]]
        assert main(arguments) == 0
        capsys.readouterr()
        current = shared_entry_manifest(restored_root, entry, hermes_home=home)
        original = shared_entry_manifest(root, entry)
        assert current.audiences == original.audiences
    config = json.loads((tmp_path / "new-agent1" / "scope-recall" / "runtime-config.json").read_text())
    restored_ledger = Path(config["auxiliary"]["ledger_path"])
    assert restored_ledger.is_relative_to(restored_root)
    with sqlite3.connect(restored_ledger) as connection:
        assert connection.execute("SELECT amount FROM preserved_spend").fetchone() == (23,)


def test_remote_spool_is_explicitly_packed_without_its_credential(tmp_path):
    binding, _ = local_store(tmp_path, "codex")
    state = tmp_path / "remote-state"
    (state / "spool").mkdir(parents=True)
    (state / "spool" / "capture.json").write_text('{"event":"NameOne pending capture"}')
    credential = tmp_path / "credential.ref"
    credential.write_text("synthetic-credential-body")
    config = tmp_path / "client.json"
    config.write_text(
        json.dumps(
            {"host": "codex", "url": "https://example.test", "state_dir": str(state), "token_file": str(credential)}
        )
    )
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle, client_configs=(config,))
    with zipfile.ZipFile(bundle) as archive:
        assert b"synthetic-credential-body" not in b"".join(archive.read(name) for name in archive.namelist())
    result = apply_disaster_restore(bundle, tmp_path / "restored")
    client = Path(result["data_directory"]) / "clients" / "client1" / "client.json"
    restored = json.loads(client.read_text())
    spool = Path(restored["state_dir"]) / "spool" / "capture.json"
    assert spool.read_text() == '{"event":"NameOne pending capture"}'
    assert Path(restored["token_file"]).is_relative_to(client.parent)
    assert not Path(restored["token_file"]).exists()
    backup_config = binding.data_directory / "backup-config.json"
    configured = json.loads(backup_config.read_text())
    configured["client_configs"] = [str(config)]
    backup_config.write_text(json.dumps(configured))
    scheduled = tmp_path / "scheduled.zip"
    create_backup_bundle(binding=binding, output=scheduled)
    with zipfile.ZipFile(scheduled) as archive:
        assert "clients/client1/spool/capture.json" in archive.namelist()


def test_bundle_manifest_matches_its_published_schema(tmp_path):
    binding, _ = local_store(tmp_path, "codex")
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    schema = json.loads(
        (Path(contract_module.__file__).parent / "contracts" / "backup_manifest.schema.json").read_text()
    )
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    Draft202012Validator(schema).validate(manifest)


@pytest.mark.parametrize("field", ["installation_id", "manifest_sha256"])
def test_verify_checks_manifest_digest_and_database_identity(tmp_path, field):
    binding, _ = local_store(tmp_path, "codex")
    bundle = tmp_path / "backup.zip"
    create_backup_bundle(binding=binding, output=bundle)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bundle) as original, zipfile.ZipFile(bad, "w") as archive:
        manifest = json.loads(original.read("manifest.json"))
        manifest[field] = "codex-install:" + "0" * 30 if field == "installation_id" else "0" * 64
        if field != "manifest_sha256":
            document = {name: value for name, value in manifest.items() if name != "manifest_sha256"}
            manifest["manifest_sha256"] = hashlib.sha256(
                json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        for name in original.namelist():
            archive.writestr(name, json.dumps(manifest).encode() if name == "manifest.json" else original.read(name))
    with pytest.raises(BackupBundleError):
        verify_backup_bundle(bad)


def test_background_backup_cannot_exceed_an_expired_pass(tmp_path):
    binding, _ = local_store(tmp_path, "codex")
    target = tmp_path / "expired.zip"
    with pytest.raises(BackupBundleError, match="deadline"):
        create_backup_bundle(binding=binding, output=target, deadline=time.monotonic() - 1)
    assert not target.exists()


@pytest.mark.parametrize("host", ["hermes", "shared"])
def test_documented_commands_back_up_verify_rehearse_and_restore(tmp_path, host, capsys):
    documentation = Path(__file__).resolve().parents[2] / "docs" / "backup-restore.md"
    commands = [
        line for line in documentation.read_text(encoding="utf-8").splitlines() if line.startswith("scope-recall ")
    ]
    home = tmp_path / "original"
    if host == "shared":
        init_shared(root=home, agent_id="agent1", test_mode=True)
        local = build_installation_manifest(tmp_path / "agent1", agent_id="agent1", test_mode=True)
        moment = "2026-10-23T00:00:00Z"
        record = shared_entry_record(local, entry_id="agent1", display_name="agent1", attached_at=moment)
        attach_shared_record(home, record, now=moment)
        payload = read_shared_payload(home)
        binding = store_binding(payload, home, frozenset(payload["scope_ids"]))
    else:
        binding, _core = install_hermes_scope_recall(home, agent_id="agent1", test_mode=True)
    selected = [commands[0 if host == "shared" else 1], *commands[2:]]
    replacements = {
        "/path/to/shared-store": str(home),
        "/path/to/agent1": str(home),
        "/path/to/backups/memory.zip": str(tmp_path / "backups" / "memory.zip"),
        "/path/to/recovered": str(tmp_path / "recovered"),
    }
    receipts = []
    for command in selected:
        arguments = [replacements.get(value, value) for value in shlex.split(command)[1:]]
        assert main(arguments) == 0
        receipts.append(json.loads(capsys.readouterr().out))
        if receipts[-1]["status"] == "planned":
            assert not (tmp_path / "recovered").exists()
    assert [item["status"] for item in receipts] == ["created", "verified", "verified", "planned", "restored"]
    assert receipts[2]["restore_check"]["doctor"]["binding_verified"]
    recovered = receipts[-1]
    assert recovered["doctor"]["binding_verified"]
    assert recovered["deletion_protection"]["installation_id"] == binding.installation_id
    assert recovered["unrecoverable_later_interval"]["start_utc"] == receipts[0]["created_at_utc"]
