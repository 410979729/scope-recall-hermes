from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from maintenance.migrate_v2 import MigrationError, main, migrate_legacy
from scope_recall.adapters.codex.config import install_codex_scope_recall
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, bind_hermes_identity, install_hermes_scope_recall
from scope_recall.adapters.hermes.installation import load_installation_manifest

from test_v6_fact_history_slots import _history_fixture


def test_manifest_aware_migration_reuses_installed_identity_and_adapter(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    home = tmp_path / "installed-hermes"
    binding, core = install_hermes_scope_recall(home, agent_id="P15-HANDOFF-AGENT", test_mode=False)
    manifest = load_installation_manifest(home)

    report = migrate_legacy(
        legacy,
        installation_manifest=home,
        source_scope_map={"scope-a": "owner_private"},
        report_path=tmp_path / "migration-report.json",
    )
    assert report["completion_status"] == "complete"
    assert report["installation_handoff"]["installation_id"] == manifest.installation_id == binding.installation_id
    assert report["installation_handoff"]["resolved_scope_mapping"]["scope-a"] == manifest.audience_scopes["owner_private"]

    with sqlite3.connect(home / "scope-recall" / "memory.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM source_events WHERE scope_id=?", (manifest.audience_scopes["owner_private"],)).fetchone()[0] > 0

    # This is the normal host handoff path: the adapter binds the same
    # installer-owned identity and can see the migrated database.
    initialize_kwargs = {
        "hermes_home": str(home),
        "platform": "cli",
        "user_id": "local",
        "chat_type": "cli",
        "chat_id": "local",
        "thread_id": "main",
        "agent_identity": "P15-HANDOFF-AGENT",
        "agent_workspace": "default",
    }
    identity = bind_hermes_identity("P15-handoff-session", **initialize_kwargs)
    assert identity.binding.test_mode is False
    adapter = ScopeRecallHermesAdapter(core=core)
    adapter.initialize("P15-handoff-session", **initialize_kwargs)
    try:
        assert adapter.is_available()
        assert adapter.installation_token == manifest.installation_id
        rendered = adapter.prefetch("现已改为不保留", session_id="P15-handoff-session")
        assert "现已改为不保留" in rendered
        with sqlite3.connect(home / "scope-recall" / "memory.sqlite3") as conn:
            fact_ref = conn.execute("SELECT claim_id FROM claims WHERE kind='fact'").fetchone()[0]
        inspected = core.inspect_object(
            identity.trusted_context(session_id="P15-handoff-session"),
            fact_ref,
            revision=1,
        )
        assert inspected.kind == "claim"
        assert inspected.revision == 1
        assert inspected.value.payload["value_text"] == "已确认保留"
    finally:
        adapter.shutdown()

    before = (home / "scope-recall" / "memory.sqlite3").read_bytes()
    rerun = migrate_legacy(legacy, installation_manifest=home, source_scope_map={"scope-a": "owner_private"})
    assert rerun["counts"] == report["counts"]
    assert (home / "scope-recall" / "memory.sqlite3").read_bytes() == before


def test_manifest_handoff_blocks_unmapped_scope_without_writing(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    home = tmp_path / "installed-hermes"
    install_hermes_scope_recall(home, agent_id="P15-HANDOFF-AGENT", test_mode=True)
    db = home / "scope-recall" / "memory.sqlite3"
    before = db.read_bytes()

    report = migrate_legacy(legacy, installation_manifest=home, source_scope_map={})
    assert report["completion_status"] == "blocked"
    assert "source_scope_not_mapped" in report["cutover_block"]["reasons"]
    assert db.read_bytes() == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0


def test_manifest_aware_cli_derives_target_and_identity(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    home = tmp_path / "installed-hermes"
    install_hermes_scope_recall(home, agent_id="P15-HANDOFF-CLI", test_mode=True)
    scope_map = tmp_path / "scope-map.json"
    scope_map.write_text(json.dumps({"scope-a": "owner_private"}), encoding="utf-8")
    report_path = tmp_path / "migration-report.json"

    assert main([
        "--source", str(legacy),
        "--installation-manifest", str(home),
        "--scope-map", str(scope_map),
        "--report", str(report_path),
    ]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["completion_status"] == "complete"
    assert report["installation_handoff"]["agent_id"] == "P15-HANDOFF-CLI"


def test_manifest_cli_single_scope_mapping_is_explicit(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    home = tmp_path / "installed-hermes"
    install_hermes_scope_recall(home, agent_id="P15-HANDOFF-SINGLE", test_mode=False)
    report_path = tmp_path / "migration-report.json"
    assert main([
        "--source", str(legacy),
        "--installation-manifest", str(home),
        "--single-scope-to", "owner_private",
        "--report", str(report_path),
    ]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["completion_status"] == "complete"
    assert report["installation_handoff"]["source_scope_mapping"] == {"scope-a": "owner_private"}


def test_manifest_cli_single_scope_mapping_blocks_multiple_scopes(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    with sqlite3.connect(legacy) as conn:
        conn.execute("UPDATE journal_entries SET scope_id='scope-b' WHERE id=4")
    home = tmp_path / "installed-hermes"
    install_hermes_scope_recall(home, agent_id="P15-HANDOFF-MULTI", test_mode=False)
    report = migrate_legacy(
        legacy,
        installation_manifest=home,
        single_scope_to="owner_private",
    )
    assert report["completion_status"] == "blocked"
    assert "single_scope_to_requires_single_legacy_scope" in report["cutover_block"]["reasons"]
    assert {item["key"] for item in report["unmapped"]} >= {"scope-a", "scope-b"}


def test_manifest_handoff_rejects_cross_instance_target(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    home_a = tmp_path / "installed-a"
    home_b = tmp_path / "installed-b"
    install_hermes_scope_recall(home_a, agent_id="P15-HANDOFF-A", test_mode=True)
    install_hermes_scope_recall(home_b, agent_id="P15-HANDOFF-B", test_mode=True)

    with pytest.raises(MigrationError, match="target directory differs"):
        migrate_legacy(
            legacy,
            target_directory=home_b / "scope-recall",
            installation_manifest=home_a,
            source_scope_map={"scope-a": "owner_private"},
        )


def test_manifest_handoff_reads_codex_binding_without_new_identity(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    root = tmp_path / "installed-codex"
    config, _core = install_codex_scope_recall(root, project_root=tmp_path / "project", agent_id="P15-CODEX", test_mode=True)
    report = migrate_legacy(
        legacy,
        installation_manifest=config.config_path,
        host="codex",
        source_scope_map={"scope-a": "owner_private"},
    )
    assert report["completion_status"] == "complete"
    assert report["installation_handoff"]["host"] == "codex"
    assert report["installation_handoff"]["installation_id"] == config.installation_id


def test_manifest_handoff_scope_collision_is_explicitly_blocked(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    with sqlite3.connect(legacy) as conn:
        conn.execute("UPDATE journal_entries SET scope_id='scope-b' WHERE id=4")
    home = tmp_path / "installed-hermes"
    install_hermes_scope_recall(home, agent_id="P15-HANDOFF-AGENT", test_mode=True)
    report = migrate_legacy(
        legacy,
        installation_manifest=home,
        source_scope_map={"scope-a": "owner_private", "scope-b": "owner_private"},
    )
    assert report["completion_status"] == "blocked"
    assert "source_scope_mapping_collision" in report["cutover_block"]["reasons"]


def test_fact_history_archive_does_not_depend_on_memories(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path)
    with sqlite3.connect(legacy) as conn:
        conn.execute("DELETE FROM memory_journal_sources")
        conn.execute("DELETE FROM memories")
    report = migrate_legacy(legacy, tmp_path / "target")
    assert report["completion_status"] == "complete"
    assert report["counts"]["fact_claims_mapped"] == 2
    with sqlite3.connect(tmp_path / "target" / "memory.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM claims WHERE kind='fact'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='fact_claims'").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='fact_claim_evidence'").fetchone()[0] == 2


def test_manifest_handoff_unknown_direct_scope_is_blocked_before_mapping(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    with sqlite3.connect(legacy) as conn:
        conn.execute("UPDATE procedural_playbooks SET scope_id='scope-unknown'")
    home = tmp_path / "installed-hermes"
    install_hermes_scope_recall(home, agent_id="P15-HANDOFF-AGENT", test_mode=False)
    report = migrate_legacy(
        legacy,
        installation_manifest=home,
        source_scope_map={"scope-a": "owner_private"},
    )
    assert report["completion_status"] == "blocked"
    assert "source_scope_not_mapped" in report["cutover_block"]["reasons"]
    assert any(item["key"] == "scope-unknown" for item in report["unmapped"])


def test_manifest_handoff_blocks_unscoped_base_rows_instead_of_leaking_legacy_scope(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    with sqlite3.connect(legacy) as conn:
        conn.execute("UPDATE journal_entries SET scope_id='' WHERE id=4")
    home = tmp_path / "installed-hermes"
    install_hermes_scope_recall(home, agent_id="P15-HANDOFF-BASE-SCOPE", test_mode=False)
    db = home / "scope-recall" / "memory.sqlite3"
    before = db.read_bytes()
    report = migrate_legacy(
        legacy,
        installation_manifest=home,
        source_scope_map={"scope-a": "owner_private"},
    )
    assert report["completion_status"] == "blocked"
    assert "source_scope_not_mapped" in report["cutover_block"]["reasons"]
    assert any(item["key"] == "legacy-scope" for item in report["unmapped"])
    assert db.read_bytes() == before


def test_manifest_handoff_scope_mapping_change_fails_closed_without_partial_write(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    conversation_scope = "scope-conversation"
    home = tmp_path / "installed-hermes"
    install_hermes_scope_recall(
        home,
        agent_id="P15-HANDOFF-SCOPE-CHANGE",
        audiences=[{
            "platform": "cli",
            "chat_type": "group",
            "chat_id": "group-1",
            "thread_id": "main",
            "agent_workspace": "default",
            "allowed_scope_ids": [conversation_scope],
            "capture_scope_id": conversation_scope,
            "kind": "conversation",
        }],
        test_mode=False,
    )
    first = migrate_legacy(
        legacy,
        installation_manifest=home,
        source_scope_map={"scope-a": "owner_private"},
    )
    assert first["completion_status"] == "complete"
    db = home / "scope-recall" / "memory.sqlite3"
    before = db.read_bytes()
    with pytest.raises(MigrationError, match="idempotence conflict: source scope"):
        migrate_legacy(
            legacy,
            installation_manifest=home,
            source_scope_map={"scope-a": "conversation"},
        )
    assert db.read_bytes() == before


def test_cli_rejects_scope_mapping_without_manifest_and_conflicting_duplicate_map_scope(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "legacy")
    with pytest.raises(SystemExit):
        main(["--source", str(legacy), "--target", str(tmp_path / "target"), "--map-scope", "scope-a=owner_private"])
    home = tmp_path / "installed-hermes"
    install_hermes_scope_recall(home, agent_id="P15-HANDOFF-DUPLICATE", test_mode=False)
    with pytest.raises(SystemExit):
        main([
            "--source", str(legacy),
            "--installation-manifest", str(home),
            "--map-scope", "scope-a=owner_private",
            "--map-scope", "scope-a=conversation",
        ])
