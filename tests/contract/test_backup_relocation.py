"""Disaster recovery remains usable through the real installation loaders."""

import hashlib
import json
import sqlite3
import sys
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


def local_store(tmp_path, host):
    home = tmp_path / "original"
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
