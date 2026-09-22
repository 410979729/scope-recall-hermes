"""Small synthetic P15 migration, idempotence, deletion, and rollback drills."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from maintenance.backup import backup_sqlite
from maintenance.migrate_v2 import _stable, migrate_legacy
from maintenance.rollback import rollback_to_verified_snapshot
from legacy_fixture import build_official_578b_fixture
from release_fixture import PREVIOUS_SCHEMA, build_previous_release_store
from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext
from scope_recall.core.restore import InstallationMaintenance, begin_restore, export_deletion_ledger, ledger_digest, replay_deletion_ledger
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.capture import record_event


def _legacy(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA user_version=10815;
        CREATE TABLE journal_entries (
            id INTEGER PRIMARY KEY, scope_id TEXT NOT NULL, session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, session_id TEXT,
            source TEXT NOT NULL, target TEXT NOT NULL, content TEXT NOT NULL,
            summary TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE memory_journal_sources (
            memory_id TEXT NOT NULL, journal_entry_id INTEGER NOT NULL,
            run_id TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(memory_id, journal_entry_id)
        );
        CREATE TABLE task_episodes (
            id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, session_id TEXT NOT NULL,
            task_goal TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
            ended_at TEXT, message_ids TEXT NOT NULL DEFAULT '[]',
            journal_entry_ids TEXT NOT NULL DEFAULT '[]', tool_names TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL DEFAULT '[]', verification TEXT NOT NULL DEFAULT '[]',
            environment TEXT NOT NULL DEFAULT '{}', metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE privacy_purge_operations (
            operation_id TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL,
            scope_set_hash TEXT NOT NULL, target_count INTEGER NOT NULL,
            source_count INTEGER NOT NULL, vector_intent_count INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            denied_at TEXT NOT NULL, erased_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE privacy_purge_source_tombstones (
            operation_id TEXT NOT NULL, journal_entry_id INTEGER NOT NULL,
            source_hash TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(operation_id, journal_entry_id)
        );
        CREATE TABLE procedural_playbooks (
            id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, status TEXT NOT NULL,
            title TEXT NOT NULL, goal TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    conn.executemany(
        "INSERT INTO journal_entries VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, "scope-a", "s-1", 1, "user", "请保留这条有证据的进度。", "2026-09-01T00:00:00Z", '{"safe":"yes"}'),
            (2, "scope-a", "s-1", 2, "tool", "这条已删除，不能复活。", "2026-09-01T00:01:00Z", "{}"),
            (3, "scope-a", "s-2", 1, "assistant", "观察到一次可追溯的结果。", "not-a-time", "{}"),
        ],
    )
    conn.execute(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("m-1", "scope-a", "s-1", "legacy", "memory", "持久化摘要", "摘要", "2026-09-01T00:02:00Z", "2026-09-01T00:02:00Z", '{"journal_entry_ids":[1]}'),
    )
    conn.executemany(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("m-2", "scope-a", "s-1", "legacy", "memory", "删除关联摘要", "删除关联摘要", "2026-09-01T00:02:00Z", "2026-09-01T00:02:00Z", "{}"),
            ("m-3", "scope-a", "s-2", "legacy", "memory", "另一条摘要", "另一条摘要", "2026-09-01T00:02:00Z", "2026-09-01T00:02:00Z", "{}"),
        ],
    )
    conn.execute("INSERT INTO memory_journal_sources VALUES ('m-1',1,'run-1','2026-09-01T00:02:00Z')")
    conn.execute("INSERT INTO memory_journal_sources VALUES ('m-2',2,'run-1','2026-09-01T00:02:00Z')")
    conn.execute("INSERT INTO memory_journal_sources VALUES ('m-3',3,'run-1','2026-09-01T00:02:00Z')")
    conn.execute(
        "INSERT INTO task_episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ep-1", "scope-a", "s-1", "完成保留验证", "open", "2026-09-01T00:00:00Z", None, "[]", "[1,2]", "[]", "[]", "[]", "{}", "{}"),
    )
    conn.execute("INSERT INTO task_episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("ep-2", "scope-a", "s-2", "完成第二条验证", "completed", "2026-09-01T00:04:00Z", "2026-09-01T00:05:00Z", "[]", "[3]", "[]", "[]", "[]", "{}", "{}"))
    conn.execute("INSERT INTO privacy_purge_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("purge-1", "fp", "scope", 1, 1, 0, "completed", "2026-09-01T00:03:00Z", "2026-09-01T00:03:00Z", "", "2026-09-01T00:03:00Z"))
    conn.execute("INSERT INTO privacy_purge_source_tombstones VALUES ('purge-1',2,'hash-2','2026-09-01T00:03:00Z')")
    conn.execute("INSERT INTO privacy_purge_source_tombstones VALUES ('purge-1',99,'hash-missing','2026-09-01T00:03:00Z')")
    conn.execute("INSERT INTO procedural_playbooks VALUES ('pb-1','scope-a','candidate','候选流程','不要自动晋升','{}')")
    conn.commit()
    conn.close()


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("source_events", "deletion_operations", "episodes", "claims", "lexical_postings")}
    finally:
        conn.close()


def test_p15_positive_negative_idempotent_and_candidate_safe(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy-578b.sqlite3"
    target = tmp_path / "isolated-target"
    _legacy(legacy)
    first = migrate_legacy(legacy, target, report_path=tmp_path / "first-report.json")
    before = _counts(target / "memory.sqlite3")
    second = migrate_legacy(legacy, target, report_path=tmp_path / "second-report.json")
    after = _counts(target / "memory.sqlite3")

    assert before == after
    assert before["source_events"] == 7
    assert before["deletion_operations"] == 1 and before["episodes"] == 2
    assert before["claims"] == 1
    assert first["candidate_auto_promoted"] is False
    assert any(item["reason"] == "source_missing_unknown_blocks_cutover" for item in first["unmapped"])
    assert second["counts"] == first["counts"]

    conn = sqlite3.connect(target / "memory.sqlite3")
    try:
        deleted = conn.execute("SELECT read_blocked FROM source_events WHERE event_id=?", (_stable("event", "journal_entries:2"),)).fetchone()[0]
        linked_deleted = conn.execute("SELECT read_blocked FROM source_events WHERE event_id=?", (_stable("event", "memories:m-2"),)).fetchone()[0]
        active = conn.execute("SELECT read_blocked FROM source_events WHERE source_event_key='legacy:journal_entries:1'").fetchone()[0]
        assert deleted == 1 and linked_deleted == 1 and active == 0
        assert conn.execute("SELECT count(*) FROM deletion_members").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM source_events WHERE read_blocked=1 AND length(content)>0").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM source_events WHERE source_revision=1").fetchone()[0] == 7
        assert conn.execute("SELECT role FROM source_events WHERE source_event_key='legacy:journal_entries:3'").fetchone()[0] == "assistant"
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
        procedure_ref, procedure_state, procedure_payload = conn.execute("SELECT c.claim_id,v.state,v.payload_json FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.kind='procedure'").fetchone()
        assert procedure_state == "proposed"
        assert "_legacy" not in json.loads(procedure_payload)
        archive_ref = conn.execute("SELECT event_id FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='procedural_playbooks' AND json_extract(extra_json,'$.legacy_id')='pb-1'").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='claim' AND object_ref=? AND source_ref=? AND relation='derived_from'", (procedure_ref, archive_ref)).fetchone()[0] == 1
    finally:
        conn.close()


def test_p15_unknown_authority_permissions_and_content_redaction_block_cutover(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy-sensitive.sqlite3"
    target = tmp_path / "isolated-target"
    _legacy(legacy)
    conn = sqlite3.connect(legacy)
    conn.executescript("ALTER TABLE journal_entries ADD COLUMN project_id TEXT; ALTER TABLE journal_entries ADD COLUMN branch_id TEXT; ALTER TABLE journal_entries ADD COLUMN shared_scope_id TEXT; CREATE TABLE audit_unknown_authority(id TEXT, state TEXT, content TEXT); CREATE TABLE fact_versions(id TEXT, state TEXT, payload TEXT); CREATE TABLE aliases(id TEXT, state TEXT, target_ref TEXT); CREATE TABLE claims(id TEXT, state TEXT, payload TEXT);")
    conn.execute("UPDATE journal_entries SET project_id='private-project',branch_id='private-branch',content='请保留进度 password=supersecret' WHERE id=1")
    conn.execute("INSERT INTO audit_unknown_authority VALUES ('alias-1','active','token=ghp_123456789012345678901234567890')")
    conn.execute("INSERT INTO fact_versions VALUES ('fact-1','active','{""kind"":""fact""}')")
    conn.execute("INSERT INTO aliases VALUES ('alias-2','active','fact-1')")
    conn.execute("INSERT INTO claims VALUES ('intent-1','active','{""kind"":""intention"",""state"":""pending""}')")
    conn.commit(); conn.close()
    report = migrate_legacy(legacy, target)
    assert report["cutover_block"]["blocked"] is True
    assert "audit_unknown_authority" in report["schema_inventory"]["unknown_tables"]
    assert report["redaction"]["redacted_records"] >= 1
    assert any(item.get("table") == "audit_unknown_authority" and item.get("redacted_rows") for item in report["unmapped"])
    assert any(item.get("reason") == "fact_version_authority_not_losslessly_mapped" for item in report["unmapped"])
    assert any(item.get("reason") == "alias_or_reference_authority_not_losslessly_mapped" for item in report["unmapped"])
    assert all("supersecret" not in json.dumps(item, ensure_ascii=False) for item in report["unmapped"])
    conn = sqlite3.connect(target / "memory.sqlite3")
    try:
        row = conn.execute("SELECT content,project_id,branch_id,read_blocked FROM source_events WHERE source_event_key='legacy:journal_entries:1'").fetchone()
        assert "[REDACTED_SECRET]" in row[0] and "supersecret" not in row[0] and row[1:] == ("private-project", "private-branch", 0)
    finally:
        conn.close()
    binding = InstanceBinding("p15-synthetic-agent", "p15-synthetic-installation", target, frozenset({"scope-a"}), True)
    storage = SQLiteStorage(binding)
    with storage.read(TrustedContext(binding, "other", binding.scope_ids, "host_generated")) as tx:
        assert tx.search_sources("[REDACTED_SECRET]") == ()


def test_p15_exported_deletion_ledger_replays_and_core_fence_denies_normal_capture(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy-replay.sqlite3"
    target = tmp_path / "target-replay"
    _legacy(legacy)
    migrate_legacy(legacy, target)
    binding = InstanceBinding("p15-synthetic-agent", "p15-synthetic-installation", target, frozenset({"scope-a"}), True)
    storage = SQLiteStorage(binding)
    authority_context = TrustedContext(binding, "p15-audit", binding.scope_ids, "host_generated")
    authority = InstallationMaintenance(authority_context)
    ledger = export_deletion_ledger(storage, authority)
    assert ledger["memory_epoch"] >= max(item["operation"]["memory_epoch"] for item in ledger["operations"])
    begin_restore(storage, authority, expected_ledger_sha256=ledger_digest(ledger))
    assert replay_deletion_ledger(storage, authority, ledger)["status"] == "deletion_ledger_replayed"
    rollback = rollback_to_verified_snapshot(storage.path, legacy)
    assert rollback["status"] == "stop_write_protection_required"
    class Clock:
        def utc_now(self) -> str: return "2026-09-06T00:00:00Z"
        def monotonic(self) -> float: return 0.0
    event = {"protocol_version": "1.1", "source_event_key": "after-replay", "source_revision": 1, "origin": "host_generated", "role": "system", "content": "must not write", "occurred_at": "2026-09-06T00:00:00Z", "recorded_at": "2026-09-06T00:00:00Z", "time_precision": "instant", "capture_state": "complete", "evidence_refs": []}
    before = _counts(storage.path)["source_events"]
    with pytest.raises(ContractError, match="RESTORE_UNVERIFIED"):
        record_event(storage, Clock(), TrustedContext(binding, "p15-audit", binding.scope_ids, "host_generated"), event, scope_id="scope-a")
    after = _counts(storage.path)["source_events"]
    assert before == after and (target / "restore-required.json").is_file()


def test_p15_consistent_backup_and_incompatible_rollback_protects_new_events(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    target = tmp_path / "target"
    _legacy(legacy)
    migrate_legacy(legacy, target)
    current = target / "memory.sqlite3"
    backup = tmp_path / "old-snapshot.sqlite3"
    manifest = tmp_path / "old-snapshot.manifest.json"
    backup_receipt = backup_sqlite(current, backup, manifest=manifest)
    result = rollback_to_verified_snapshot(current, backup)

    assert backup_receipt["quick_check"] == "ok"
    assert result["status"] == "stop_write_protection_required"
    marker = Path(result["stop_write_marker"])
    assert marker.is_file()
    assert current.is_file() and backup.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["old_snapshot_preserved"] is True
    assert _counts(current)["source_events"] == 7


def test_p15_official_578b_private_fixture_maps_facts_history_and_multivalue(tmp_path: Path) -> None:
    legacy = build_official_578b_fixture(tmp_path / "official-578b.sqlite3", repo_root=Path.cwd())
    target = tmp_path / "official-target"
    report = migrate_legacy(legacy, target)

    assert report["completion_status"] == "complete"
    assert report["cutover_block"]["blocked"] is False
    assert report["permission_classification"]["rows_with_explicit_scope_gap"] == 0
    assert report["counts"]["fact_claims_mapped"] == 1
    assert report["counts"]["procedure_claims_mapped"] == 1
    assert report["fact_authority"]["active"] == 1
    assert report["candidate_auto_promoted"] is False
    second = migrate_legacy(legacy, target)
    assert second["counts"] == report["counts"]

    conn = sqlite3.connect(target / "memory.sqlite3")
    try:
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM claim_versions").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM source_events WHERE read_blocked=0").fetchone()[0] == 9
        assert conn.execute("SELECT count(*) FROM claim_versions WHERE state='active'").fetchone()[0] == 1
        procedure_ref, procedure_state, procedure_payload = conn.execute("SELECT c.claim_id,v.state,v.payload_json FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.kind='procedure'").fetchone()
        assert procedure_state == "proposed"
        assert "_legacy" not in json.loads(procedure_payload)
        archive_ref = conn.execute("SELECT event_id FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='playbook_versions' AND json_extract(extra_json,'$.legacy_id')='pbv-1'").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='claim' AND object_ref=? AND source_ref=? AND relation='derived_from'", (procedure_ref, archive_ref)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM source_events WHERE json_extract(extra_json,'$.source_kind')='legacy_history'").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='claim' AND source_ref IN (SELECT event_id FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='fact_action_receipts')").fetchone()[0] == 1
    finally:
        conn.close()


def test_p15_official_multivalue_is_archived_without_fake_conditions(tmp_path: Path) -> None:
    legacy = build_official_578b_fixture(tmp_path / "official-578b-multi.sqlite3", repo_root=Path.cwd(), include_multivalue=True)
    report = migrate_legacy(legacy, tmp_path / "multi-target")
    assert report["completion_status"] == "blocked"
    assert "multi_value_fact_not_losslessly_mapped_to_core_slot" in report["cutover_block"]["reasons"]
    assert report["counts"]["fact_claims_mapped"] == 1
    assert sum(item["reason"] == "multi_value_fact_not_losslessly_mapped_to_core_slot" for item in report["unmapped"]) == 2
    conn = sqlite3.connect(tmp_path / "multi-target" / "memory.sqlite3")
    try:
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='fact_claims'").fetchone()[0] == 3
        payloads = [row[0] for row in conn.execute("SELECT extra_json FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='fact_claims'")]
        assert all("value_fingerprint" in item for item in payloads)
        # The representable single fact uses the same semantic slot as a new
        # Core proposal. Legacy bookkeeping keys remain in archived metadata.
        fact = conn.execute("SELECT v.payload_json,c.slot_key FROM claims c JOIN claim_versions v USING(claim_id) WHERE c.kind='fact'").fetchone()
        proposal = json.loads(fact[0])
        from scope_recall.core.claims import claim_slot
        assert proposal["conditions"] == []
        assert fact[1] == claim_slot("scope-a", None, None, proposal)
        assert all("legacy_value_fingerprint" not in item for item in payloads)
    finally:
        conn.close()


def test_migration_public_facade_has_real_responsibility_owners():
    import ast
    from scope_recall.maintenance import (migrate_v2, legacy_conversion, legacy_catalog, legacy_claims,
                                          legacy_deletions, legacy_plan, legacy_sources,
                                          migration_activation, migration_index, migration_records)
    assert migrate_v2.MigrationError is migration_records.MigrationError
    assert migrate_v2.build_legacy_catalog is legacy_catalog.build_legacy_catalog
    assert migrate_v2.migrate_legacy is legacy_conversion.migrate_legacy
    assert migrate_v2.queue_index_page is migration_index.queue_index_page
    from scope_recall.maintenance import upgrade
    source = Path(upgrade.__file__).read_text(encoding='utf-8')
    assert 'from .migration_index import queue_index_page' in source
    assert callable(migration_index.queue_index_page)
    # Lower responsibilities never import the orchestration facade; this would
    # create cycles or make facade monkeypatches a hidden execution dependency.
    for module in (legacy_conversion, legacy_catalog, legacy_claims, legacy_deletions, legacy_plan, legacy_sources,
                   migration_activation, migration_index, migration_records):
        tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
        assert not any(isinstance(n, ast.ImportFrom) and n.module == 'migrate_v2' for n in ast.walk(tree))


def test_a_store_written_by_the_previous_release_upgrades_on_first_open(tmp_path: Path) -> None:
    """Built by v3.1.0's own code, not by downgrading a fresh store.  Opening it
    with this release brings it forward in one transaction; nothing is run by
    hand, and what that release wrote is still there afterwards."""
    path = build_previous_release_store(tmp_path / "previous", repo_root=Path.cwd(), sources=12)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == PREVIOUS_SCHEMA
        copies = conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='episode'").fetchone()[0]
        work = conn.execute("SELECT count(*) FROM work_items").fetchone()[0]
        projection = conn.execute("SELECT count(*) FROM lexical_projection").fetchone()[0]
    assert copies == 12 * 13 // 2, "one copied lineage row per revision: the growth 1109 removes"
    binding = InstanceBinding("TEST-agent", "TEST-installation", tmp_path / "previous", frozenset({"TEST-scope"}), True)
    context = TrustedContext(binding, "TEST-session", frozenset({"TEST-scope"}), "human_direct")
    core = MemoryCore(CoreConfig(binding))
    status = core.status(context)
    assert status.schema_version == SCHEMA_VERSION == 1110 and status.sources == 12
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1110
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "the previous release wrote a rollback-journal store"
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='episode'").fetchone()[0] == 12
        assert conn.execute("SELECT count(*) FROM work_items").fetchone()[0] == work
        assert conn.execute("SELECT count(*) FROM lexical_postings").fetchone()[0] == projection > 0
        assert conn.execute("SELECT count(*) FROM source_events WHERE source_id IS NULL").fetchone()[0] == 0
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"expired_vectors", "authorization_payloads", "source_authorizations"} <= tables
    episode, = core.episodes(replace(context, task_anchor="TEST-previous-release"))
    assert len(episode.evidence_refs) == 12
    assert core.initialize().schema_version == 1110
