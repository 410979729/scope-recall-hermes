"""Small synthetic P15 migration, idempotence, deletion, and restore drills."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from legacy_fixture import build_official_578b_fixture
from release_fixture import LATEST_RELEASE, LATEST_SCHEMA, PREVIOUS_SCHEMA, build_previous_release_store
from scope_recall.adapters.clients.config import install_codex_scope_recall
from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.capture import record_event
from scope_recall.core.restore import (
    InstallationMaintenance,
    begin_restore,
    export_deletion_ledger,
    ledger_digest,
    replay_deletion_ledger,
)
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.writer_lease import TruthWriterBusyError, truth_writer_process_snapshot
from scope_recall.maintenance import cli
from scope_recall.maintenance.restore import apply_restore
from scope_recall.runtime import backup_primitives
from test_denial_values import NEGATED_STATES
from v11_support import downgrade_store

from maintenance.backup import backup_sqlite
from maintenance.migrate_v2 import migrate_legacy
from maintenance.migration_records import stable_legacy_id


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
        (
            "m-1",
            "scope-a",
            "s-1",
            "legacy",
            "memory",
            "持久化摘要",
            "摘要",
            "2026-09-01T00:02:00Z",
            "2026-09-01T00:02:00Z",
            '{"journal_entry_ids":[1]}',
        ),
    )
    conn.executemany(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "m-2",
                "scope-a",
                "s-1",
                "legacy",
                "memory",
                "删除关联摘要",
                "删除关联摘要",
                "2026-09-01T00:02:00Z",
                "2026-09-01T00:02:00Z",
                "{}",
            ),
            (
                "m-3",
                "scope-a",
                "s-2",
                "legacy",
                "memory",
                "另一条摘要",
                "另一条摘要",
                "2026-09-01T00:02:00Z",
                "2026-09-01T00:02:00Z",
                "{}",
            ),
        ],
    )
    conn.execute("INSERT INTO memory_journal_sources VALUES ('m-1',1,'run-1','2026-09-01T00:02:00Z')")
    conn.execute("INSERT INTO memory_journal_sources VALUES ('m-2',2,'run-1','2026-09-01T00:02:00Z')")
    conn.execute("INSERT INTO memory_journal_sources VALUES ('m-3',3,'run-1','2026-09-01T00:02:00Z')")
    conn.execute(
        "INSERT INTO task_episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ep-1",
            "scope-a",
            "s-1",
            "完成保留验证",
            "open",
            "2026-09-01T00:00:00Z",
            None,
            "[]",
            "[1,2]",
            "[]",
            "[]",
            "[]",
            "{}",
            "{}",
        ),
    )
    conn.execute(
        "INSERT INTO task_episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ep-2",
            "scope-a",
            "s-2",
            "完成第二条验证",
            "completed",
            "2026-09-01T00:04:00Z",
            "2026-09-01T00:05:00Z",
            "[]",
            "[3]",
            "[]",
            "[]",
            "[]",
            "{}",
            "{}",
        ),
    )
    conn.execute(
        "INSERT INTO privacy_purge_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "purge-1",
            "fp",
            "scope",
            1,
            1,
            0,
            "completed",
            "2026-09-01T00:03:00Z",
            "2026-09-01T00:03:00Z",
            "",
            "2026-09-01T00:03:00Z",
        ),
    )
    conn.execute("INSERT INTO privacy_purge_source_tombstones VALUES ('purge-1',2,'hash-2','2026-09-01T00:03:00Z')")
    conn.execute(
        "INSERT INTO privacy_purge_source_tombstones VALUES ('purge-1',99,'hash-missing','2026-09-01T00:03:00Z')"
    )
    conn.execute(
        "INSERT INTO procedural_playbooks VALUES ('pb-1','scope-a','candidate','候选流程','不要自动晋升','{}')"
    )
    conn.commit()
    conn.close()


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("source_events", "deletion_operations", "episodes", "claims", "lexical_postings")
        }
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
        deleted = conn.execute(
            "SELECT read_blocked FROM source_events WHERE event_id=?", (stable_legacy_id("event", "journal_entries:2"),)
        ).fetchone()[0]
        linked_deleted = conn.execute(
            "SELECT read_blocked FROM source_events WHERE event_id=?", (stable_legacy_id("event", "memories:m-2"),)
        ).fetchone()[0]
        active = conn.execute(
            "SELECT read_blocked FROM source_events WHERE source_event_key='legacy:journal_entries:1'"
        ).fetchone()[0]
        assert deleted == 1 and linked_deleted == 1 and active == 0
        assert conn.execute("SELECT count(*) FROM deletion_members").fetchone()[0] == 3
        assert (
            conn.execute("SELECT count(*) FROM source_events WHERE read_blocked=1 AND length(content)>0").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT count(*) FROM source_events WHERE source_revision=1").fetchone()[0] == 7
        assert (
            conn.execute("SELECT role FROM source_events WHERE source_event_key='legacy:journal_entries:3'").fetchone()[
                0
            ]
            == "assistant"
        )
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
        procedure_ref, procedure_state, procedure_payload = conn.execute(
            "SELECT c.claim_id,v.state,v.payload_json FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.kind='procedure'"
        ).fetchone()
        assert procedure_state == "proposed"
        assert "_legacy" not in json.loads(procedure_payload)
        archive_ref = conn.execute(
            "SELECT event_id FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='procedural_playbooks' AND json_extract(extra_json,'$.legacy_id')='pb-1'"
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT count(*) FROM evidence_links WHERE object_kind='claim' AND object_ref=? AND source_ref=? AND relation='derived_from'",
                (procedure_ref, archive_ref),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_p15_unknown_authority_permissions_and_content_redaction_block_cutover(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy-sensitive.sqlite3"
    target = tmp_path / "isolated-target"
    _legacy(legacy)
    conn = sqlite3.connect(legacy)
    conn.executescript(
        "ALTER TABLE journal_entries ADD COLUMN project_id TEXT; ALTER TABLE journal_entries ADD COLUMN branch_id TEXT; ALTER TABLE journal_entries ADD COLUMN shared_scope_id TEXT; CREATE TABLE audit_unknown_authority(id TEXT, state TEXT, content TEXT); CREATE TABLE fact_versions(id TEXT, state TEXT, payload TEXT); CREATE TABLE aliases(id TEXT, state TEXT, target_ref TEXT); CREATE TABLE claims(id TEXT, state TEXT, payload TEXT);"
    )
    conn.execute(
        "UPDATE journal_entries SET project_id='private-project',branch_id='private-branch',content='请保留进度 password=supersecret' WHERE id=1"
    )
    conn.execute(
        "INSERT INTO audit_unknown_authority VALUES ('alias-1','active','token=ghp_123456789012345678901234567890')"
    )
    conn.execute("INSERT INTO fact_versions VALUES ('fact-1','active','{kind:fact}')")
    conn.execute("INSERT INTO aliases VALUES ('alias-2','active','fact-1')")
    conn.execute("INSERT INTO claims VALUES ('intent-1','active','{kind:intention,state:pending}')")
    conn.commit()
    conn.close()
    report = migrate_legacy(legacy, target)
    assert report["cutover_block"]["blocked"] is True
    assert "audit_unknown_authority" in report["schema_inventory"]["unknown_tables"]
    assert report["redaction"]["redacted_records"] >= 1
    assert any(
        item.get("table") == "audit_unknown_authority" and item.get("redacted_rows") for item in report["unmapped"]
    )
    assert any(item.get("reason") == "fact_version_authority_not_losslessly_mapped" for item in report["unmapped"])
    assert any(
        item.get("reason") == "alias_or_reference_authority_not_losslessly_mapped" for item in report["unmapped"]
    )
    assert all("supersecret" not in json.dumps(item, ensure_ascii=False) for item in report["unmapped"])
    conn = sqlite3.connect(target / "memory.sqlite3")
    try:
        row = conn.execute(
            "SELECT content,project_id,branch_id,read_blocked FROM source_events WHERE source_event_key='legacy:journal_entries:1'"
        ).fetchone()
        assert (
            "[REDACTED_SECRET]" in row[0]
            and "supersecret" not in row[0]
            and row[1:] == ("private-project", "private-branch", 0)
        )
    finally:
        conn.close()
    binding = InstanceBinding("p15-synthetic-agent", "p15-synthetic-installation", target, frozenset({"scope-a"}), True)
    storage = SQLiteStorage(binding)
    with storage.read(TrustedContext(binding, "other", binding.scope_ids, "host_generated")) as tx:
        assert tx.sources.search_sources("[REDACTED_SECRET]") == ()


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
    ledger = export_deletion_ledger(storage, authority)
    begin_restore(storage, authority, expected_ledger_sha256=ledger_digest(ledger))

    class Clock:
        def utc_now(self) -> str:
            return "2026-09-06T00:00:00Z"

        def monotonic(self) -> float:
            return 0.0

    event = {
        "protocol_version": "1.1",
        "source_event_key": "after-replay",
        "source_revision": 1,
        "origin": "host_generated",
        "role": "system",
        "content": "must not write",
        "occurred_at": "2026-09-06T00:00:00Z",
        "recorded_at": "2026-09-06T00:00:00Z",
        "time_precision": "instant",
        "capture_state": "complete",
        "evidence_refs": [],
    }
    before = _counts(storage.path)["source_events"]
    with pytest.raises(ContractError, match="RESTORE_UNVERIFIED"):
        record_event(
            storage,
            Clock(),
            TrustedContext(binding, "p15-audit", binding.scope_ids, "host_generated"),
            event,
            scope_id="scope-a",
        )
    after = _counts(storage.path)["source_events"]
    assert before == after and (target / "restore-required.json").is_file()


def test_p15_restoring_a_backup_of_the_migrated_store_keeps_its_migrated_deletions(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    target = tmp_path / "target"
    _legacy(legacy)
    migrate_legacy(legacy, target)
    legacy_before = legacy.read_bytes()
    current = target / "memory.sqlite3"
    backup = tmp_path / "migrated-snapshot.sqlite3"
    backup_receipt = backup_sqlite(current, backup, manifest=tmp_path / "migrated-snapshot.manifest.json")
    binding = InstanceBinding("p15-synthetic-agent", "p15-synthetic-installation", target, frozenset({"scope-a"}), True)
    moment = datetime(2026, 9, 6, tzinfo=UTC)
    result = apply_restore(binding, backup, backup_dir=tmp_path / "kept", now=moment)

    assert backup_receipt["quick_check"] == "ok"
    assert (result["status"], result["ledger_operations"]) == ("restored", 1)
    assert not (target / "restore-required.json").exists() and Path(result["backup"]["path"]).is_file()
    assert _counts(current)["source_events"] == 7 and legacy.read_bytes() == legacy_before
    with closing(sqlite3.connect(current)) as conn:
        assert conn.execute(
            "SELECT read_blocked FROM source_events WHERE event_id=?", (stable_legacy_id("event", "journal_entries:2"),)
        ).fetchone() == (1,)
    storage = SQLiteStorage(binding)
    with storage.read(TrustedContext(binding, "p15-audit", binding.scope_ids, "host_generated")) as tx:
        assert tx.sources.search_sources("不能复活") == ()


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
        procedure_ref, procedure_state, procedure_payload = conn.execute(
            "SELECT c.claim_id,v.state,v.payload_json FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.kind='procedure'"
        ).fetchone()
        assert procedure_state == "proposed"
        assert "_legacy" not in json.loads(procedure_payload)
        archive_ref = conn.execute(
            "SELECT event_id FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='playbook_versions' AND json_extract(extra_json,'$.legacy_id')='pbv-1'"
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT count(*) FROM evidence_links WHERE object_kind='claim' AND object_ref=? AND source_ref=? AND relation='derived_from'",
                (procedure_ref, archive_ref),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM source_events WHERE json_extract(extra_json,'$.source_kind')='legacy_history'"
            ).fetchone()[0]
            == 3
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM evidence_links WHERE object_kind='claim' AND source_ref IN (SELECT event_id FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='fact_action_receipts')"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_p15_official_multivalue_is_archived_without_fake_conditions(tmp_path: Path) -> None:
    legacy = build_official_578b_fixture(
        tmp_path / "official-578b-multi.sqlite3", repo_root=Path.cwd(), include_multivalue=True
    )
    report = migrate_legacy(legacy, tmp_path / "multi-target")
    assert report["completion_status"] == "blocked"
    assert "multi_value_fact_not_losslessly_mapped_to_core_slot" in report["cutover_block"]["reasons"]
    assert report["counts"]["fact_claims_mapped"] == 1
    assert (
        sum(item["reason"] == "multi_value_fact_not_losslessly_mapped_to_core_slot" for item in report["unmapped"]) == 2
    )
    conn = sqlite3.connect(tmp_path / "multi-target" / "memory.sqlite3")
    try:
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT count(*) FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='fact_claims'"
            ).fetchone()[0]
            == 3
        )
        payloads = [
            row[0]
            for row in conn.execute(
                "SELECT extra_json FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='fact_claims'"
            )
        ]
        assert all("value_fingerprint" in item for item in payloads)
        # The representable single fact uses the same semantic slot as a new
        # Core proposal. Legacy bookkeeping keys remain in archived metadata.
        fact = conn.execute(
            "SELECT v.payload_json,c.slot_key FROM claims c JOIN claim_versions v USING(claim_id) WHERE c.kind='fact'"
        ).fetchone()
        proposal = json.loads(fact[0])
        from scope_recall.core.claims import claim_slot

        assert proposal["conditions"] == []
        assert fact[1] == claim_slot("scope-a", None, None, proposal)
        assert all("legacy_value_fingerprint" not in item for item in payloads)
    finally:
        conn.close()


def test_migration_public_facade_has_real_responsibility_owners():
    import ast

    from scope_recall.maintenance import (
        legacy_catalog,
        legacy_claims,
        legacy_conversion,
        legacy_deletions,
        legacy_plan,
        legacy_sources,
        migrate_v2,
        migration_activation,
        migration_index,
        migration_records,
    )

    assert migrate_v2.MigrationError is migration_records.MigrationError
    assert migrate_v2.build_legacy_catalog is legacy_catalog.build_legacy_catalog
    assert migrate_v2.migrate_legacy is legacy_conversion.migrate_legacy
    assert migrate_v2.queue_index_page is migration_index.queue_index_page
    from scope_recall.maintenance import upgrade

    source = Path(upgrade.__file__).read_text(encoding="utf-8")
    assert "from .migration_index import queue_index_page" in source
    assert callable(migration_index.queue_index_page)
    # Lower responsibilities never import the orchestration facade; this would
    # create cycles or make facade monkeypatches a hidden execution dependency.
    for module in (
        legacy_conversion,
        legacy_catalog,
        legacy_claims,
        legacy_deletions,
        legacy_plan,
        legacy_sources,
        migration_activation,
        migration_index,
        migration_records,
    ):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        assert not any(isinstance(n, ast.ImportFrom) and n.module == "migrate_v2" for n in ast.walk(tree))


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
    assert status.schema_version == SCHEMA_VERSION and status.sources == 12
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal", (
            "the previous release wrote a rollback-journal store"
        )
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='episode'").fetchone()[0] == 12
        assert conn.execute("SELECT count(*) FROM work_items").fetchone()[0] == work
        assert conn.execute("SELECT count(*) FROM lexical_postings").fetchone()[0] == projection > 0
        assert conn.execute("SELECT count(*) FROM source_events WHERE source_id IS NULL").fetchone()[0] == 0
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"expired_vectors", "authorization_payloads", "source_authorizations"} <= tables
    (episode,) = core.episodes(replace(context, task_anchor="TEST-previous-release"))
    assert len(episode.evidence_refs) == 12
    assert core.initialize().schema_version == SCHEMA_VERSION


def test_a_store_the_3_1_2_release_wrote_opens_current_with_every_source_its_own(tmp_path):
    """The step 3.2 users meet: 3.1.2's own code writes the store, this tree opens it once."""
    path = build_previous_release_store(tmp_path / "latest", repo_root=Path.cwd(), sources=12, release=LATEST_RELEASE)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA
    binding = InstanceBinding("TEST-agent", "TEST-installation", tmp_path / "latest", frozenset({"TEST-scope"}), True)
    context = TrustedContext(binding, "TEST-session", frozenset({"TEST-scope"}), "human_direct")
    status = MemoryCore(CoreConfig(binding)).status(context)
    assert status.schema_version == SCHEMA_VERSION and status.sources == 12
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT installation_kind FROM instance_meta").fetchone()[0] == "local"
        assert conn.execute("SELECT entry_id, count(*) FROM source_events GROUP BY entry_id").fetchall() == [
            ("local", 12)
        ]
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


# --- A migration's destination is written whole before its store ----------------------------------------------------

_OLD_CLI = "platform:3:cli|workspace:6:hermes|agent:7:default|user:5:local"
_OLD_DESKTOP = "platform:7:desktop|workspace:6:hermes|agent:7:default|user:11:srdesk_TEST"
_OLD_FEISHU = "platform:6:feishu|workspace:6:hermes|agent:7:default|user:7:ou_TEST"
_OLD_FEISHU_DM = f"{_OLD_FEISHU}|session:28:agent:main:feishu:dm:oc_TEST"
_OLD_SCOPES = (_OLD_CLI, _OLD_DESKTOP, _OLD_FEISHU, _OLD_FEISHU_DM)
_OWNERS = [
    {"platform": "cli", "user_id": "local"},
    {"platform": "desktop", "user_id": "local"},
    {"platform": "feishu", "user_id": "ou_TEST"},
]


def _four_scope_legacy(tmp_path: Path) -> Path:
    """A legacy store whose rows lie in a CLI scope, a minted Desktop principal's, a Feishu user's and a Feishu DM's."""
    path = tmp_path / "old" / "memory.sqlite3"
    path.parent.mkdir()
    _legacy(path)
    conn = sqlite3.connect(path)
    # The columns a real store of that release has, without which the catalog refuses the format.
    conn.executescript(
        "ALTER TABLE journal_entries ADD COLUMN shared_scope_id TEXT;"
        "ALTER TABLE procedural_playbooks ADD COLUMN created_at TEXT NOT NULL DEFAULT '2026-09-01T00:00:00Z';"
        "ALTER TABLE procedural_playbooks ADD COLUMN updated_at TEXT NOT NULL DEFAULT '2026-09-01T00:00:00Z';"
        "ALTER TABLE procedural_playbooks ADD COLUMN task_class TEXT NOT NULL DEFAULT 'general';"
    )
    for table in ("journal_entries", "memories", "task_episodes", "procedural_playbooks"):
        conn.execute(f"UPDATE {table} SET scope_id=? WHERE scope_id='scope-a'", (_OLD_CLI,))
    conn.executemany(
        """INSERT INTO journal_entries(id,scope_id,session_id,turn_number,role,content,created_at,metadata)
           VALUES (?,?,?,?,?,?,?,?)""",
        [
            (10 + index, scope, f"s-{10 + index}", 1, "user", "TEST 旧会话里的一句话。", "2026-09-02T00:00:00Z", "{}")
            for index, scope in enumerate(_OLD_SCOPES[1:])
        ],
    )
    conn.commit()
    conn.close()
    return path


def _owner_rows() -> list[dict[str, object]]:
    """The owner reads every old scope at the CLI, on the approved Desktop panel and in their Feishu DM, and each
    surface writes where its old scope was."""

    def row(platform: str, user_id: str, chat_type: str, chat_id: str, thread_id: str, scope: str) -> dict:
        return {
            "platform": platform,
            "user_id": user_id,
            "chat_type": chat_type,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "gateway_session_key": "",
            "agent_workspace": "hermes",
            "allowed_scope_ids": list(_OLD_SCOPES),
            "writable_scope_ids": [scope],
            "capture_scope_id": scope,
            "kind": "owner_private",
        }

    return [
        row("cli", "local", "cli", "local", "main", _OLD_CLI),
        # The route ``--local-platform desktop`` writes, so a later ``apply-install`` leaves this row as it is.
        row("desktop", "local", "private", "local", "main", _OLD_DESKTOP),
        row("feishu", "ou_TEST", "dm", "oc_TEST", "", _OLD_FEISHU_DM),
    ]


def test_a_destination_written_whole_takes_every_old_scope_as_itself(tmp_path: Path) -> None:
    """A migration done the supported way: the destination's manifest names the gateway owner, its DM and every old
    scope before its store exists, so each old scope maps to itself and the owner reads all of them on each surface,
    while another Feishu user reads nothing."""
    from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
    from scope_recall.adapters.hermes.installation import approve_local_platforms, load_installation_manifest
    from scope_recall.maintenance.upgrade import prepare_upgrade

    legacy = _four_scope_legacy(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _binding, core = install_hermes_scope_recall(
        home, agent_id="default", agent_workspace="hermes", owner_principals=_OWNERS, audiences=_owner_rows()
    )
    manifest = load_installation_manifest(home)
    assert approve_local_platforms(manifest, ["desktop"], agent_workspace="hermes") == manifest, (
        "a later apply-install --local-platform desktop finds the surface approved and changes nothing"
    )
    report = prepare_upgrade(legacy, tmp_path / "job", installation_manifest=home)
    assert (report["state"], report["blockers"]) == ("prepared", []), report
    assert report["scope_map"] == {scope: scope for scope in _OLD_SCOPES}
    assert report["missing_scope_ids"] == report["invalid_scope_ids"] == report["colliding_scope_ids"] == []

    def readable(**route: str) -> frozenset[str]:
        provider = ScopeRecallHermesAdapter(core=core)
        base = dict(hermes_home=str(home), agent_context="primary", agent_identity="default", agent_workspace="hermes")
        provider.initialize("TEST-231-session", **base, parent_session_id="", **route)
        try:
            return provider._identity.runtime_audience.allowed_scope_ids
        finally:
            provider.shutdown()

    def feishu_dm(user: str, chat: str) -> dict[str, str]:
        key = f"agent:main:feishu:dm:{chat}"
        return dict(
            platform="feishu", user_id=user, chat_type="dm", chat_id=chat, thread_id="", gateway_session_key=key
        )

    assert readable(platform="cli") == frozenset(_OLD_SCOPES)
    assert readable(platform="desktop") == frozenset(_OLD_SCOPES)
    assert readable(**feishu_dm("ou_TEST", "oc_TEST")) == frozenset(_OLD_SCOPES)
    assert readable(**feishu_dm("ou_TEST_OTHER", "oc_TEST_OTHER")) == frozenset()


def test_a_manifest_rewritten_after_its_store_was_made_is_named(tmp_path: Path, capsys) -> None:
    """A destination installed first, its store made with the owner's scope alone, and then given a manifest naming
    more scopes no longer opens that store.  ``migrate prepare`` says what happened and what to do, not only
    ``invalid_or_unavailable_input``, and writes no job."""
    from scope_recall.adapters.hermes import install_hermes_scope_recall
    from scope_recall.adapters.hermes.installation import build_installation_manifest, write_installation_manifest
    from scope_recall.maintenance import upgrade_cli
    from scope_recall.maintenance.upgrade import MigrationError, prepare_upgrade

    legacy = _four_scope_legacy(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    install_hermes_scope_recall(home, agent_id="default", agent_workspace="hermes", local_platforms=["desktop"])
    write_installation_manifest(
        build_installation_manifest(
            home, agent_id="default", agent_workspace="hermes", owner_principals=_OWNERS, audiences=_owner_rows()
        )
    )
    with pytest.raises(MigrationError, match="before anything creates the store"):
        prepare_upgrade(legacy, tmp_path / "job", installation_manifest=home)
    assert not (tmp_path / "job").exists()
    argv = ["migrate", "prepare", "--source", str(legacy), "--job", str(tmp_path / "job")]
    assert upgrade_cli.main([*argv, "--installation-manifest", str(home)]) == 3
    told = json.loads(capsys.readouterr().out)
    assert (told["error_type"], told["status"]) == ("MigrationError", "blocked")
    assert told["reason"].startswith("destination store holds other scopes than its manifest names")


def test_old_scopes_mapped_onto_one_target_are_each_named(tmp_path: Path) -> None:
    """Two old scopes onto ``owner_private``, as the workflow once told a Desktop migration to do beside the CLI's own,
    is refused; the refusal named neither, and dropped the target the destination does not register, as a key the
    old store does not have also did."""
    from scope_recall.adapters.hermes import install_hermes_scope_recall
    from scope_recall.maintenance.upgrade import prepare_upgrade

    legacy = _four_scope_legacy(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    install_hermes_scope_recall(home, agent_id="default", agent_workspace="hermes", local_platforms=["desktop"])
    mapping = {
        _OLD_CLI: "owner_private",
        _OLD_DESKTOP: "owner_private",
        _OLD_FEISHU: "TEST-unregistered",
        "TEST-not-in-the-old-store": "TEST-unregistered-too",
    }
    report = prepare_upgrade(legacy, tmp_path / "job", installation_manifest=home, scope_map=mapping)
    assert report["state"] == "blocked" and report["blockers"] == ["audience_binding_required"]
    assert report["colliding_scope_ids"] == sorted([_OLD_CLI, _OLD_DESKTOP])
    assert report["invalid_scope_ids"] == ["TEST-not-in-the-old-store", _OLD_FEISHU, "non_injective_scope_map"]
    assert report["missing_scope_ids"] == [_OLD_FEISHU_DM]


def test_a_manifest_the_installer_would_refuse_is_told_not_thrown(tmp_path: Path, capsys) -> None:
    """A hand-edited manifest ended ``migrate prepare`` in a traceback; it is a blocked answer naming what is
    missing, and a contract refusal names its code and field."""
    from scope_recall.adapters.hermes import install_hermes_scope_recall
    from scope_recall.maintenance import upgrade_cli

    legacy = _four_scope_legacy(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    install_hermes_scope_recall(home, agent_id="default", agent_workspace="hermes")
    path = home / "scope-recall" / "installation.json"
    written = path.read_text(encoding="utf-8")
    edited = json.loads(written)
    del edited["audiences"][0]["capture_scope_id"]
    path.write_text(json.dumps(edited), encoding="utf-8")
    argv = ["migrate", "prepare", "--source", str(legacy), "--job", str(tmp_path / "job")]
    assert upgrade_cli.main([*argv, "--installation-manifest", str(home)]) == 3
    told = json.loads(capsys.readouterr().out)
    assert told["error_type"] == "HermesIdentityError" and "capture_scope_id" in told["reason"]

    path.write_text(written, encoding="utf-8")
    with closing(sqlite3.connect(home / "scope-recall" / "memory.sqlite3")) as conn:
        conn.execute("PRAGMA application_id=0")  # a store that is not one of ours
    assert upgrade_cli.main([*argv, "--installation-manifest", str(home)]) == 3
    told = json.loads(capsys.readouterr().out)
    assert (told["error_type"], told["reason"]) == ("ContractError", "SCHEMA_UNSUPPORTED: invalid payload")
    assert not (tmp_path / "job").exists()


def test_the_cli_runs_from_a_source_checkout():
    """``maintenance`` is a top-level package in a source checkout as well (these drills import it so): the operator
    CLI answers ``python -m maintenance.cli --help`` there, so no module-level import of it climbs above the package."""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", "maintenance.cli", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.startswith("usage:")


# --- 2.0 stores the migration job ended on ---------------------------------------------------------------------------

_SHAPED_SCHEMA = """
CREATE TABLE journal_entries (id INTEGER PRIMARY KEY, scope_id TEXT NOT NULL, shared_scope_id TEXT,
    session_id TEXT NOT NULL, turn_number INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    created_at TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}');
CREATE TABLE memories (id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, session_id TEXT, source TEXT NOT NULL,
    target TEXT NOT NULL, content TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}');
CREATE TABLE memory_journal_sources (memory_id TEXT NOT NULL, journal_entry_id INTEGER NOT NULL,
    run_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(memory_id, journal_entry_id));
CREATE TABLE task_episodes (id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, session_id TEXT NOT NULL,
    task_goal TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
    message_ids TEXT NOT NULL DEFAULT '[]', journal_entry_ids TEXT NOT NULL DEFAULT '[]',
    tool_names TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '[]',
    verification TEXT NOT NULL DEFAULT '[]', environment TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}');
CREATE TABLE privacy_purge_operations (operation_id TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL,
    scope_set_hash TEXT NOT NULL, target_count INTEGER NOT NULL, source_count INTEGER NOT NULL,
    vector_intent_count INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    denied_at TEXT NOT NULL, erased_at TEXT NOT NULL DEFAULT '');
CREATE TABLE privacy_purge_source_tombstones (operation_id TEXT NOT NULL, journal_entry_id INTEGER NOT NULL,
    source_hash TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(operation_id, journal_entry_id));
CREATE TABLE fact_claims (claim_id TEXT PRIMARY KEY, memory_id TEXT, scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL, predicate_key TEXT NOT NULL, fact_key TEXT NOT NULL, value TEXT NOT NULL,
    cardinality TEXT NOT NULL, assertion_kind TEXT NOT NULL, recorded_at TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE fact_claim_evidence (evidence_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL, evidence_hash TEXT NOT NULL, excerpt TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE memory_digest_sources (memory_id TEXT NOT NULL, run_id TEXT NOT NULL, session_id TEXT NOT NULL,
    message_ids TEXT NOT NULL, source_hash TEXT NOT NULL, created_at TEXT NOT NULL);
"""
_AT = "2026-07-01T00:00:00Z"


def _shaped_store(path: Path, *, journals, memories=(), episodes=(), purged=(), facts=(), digests=(), links=()) -> Path:
    """A 2.0 store of these rows: journals (id, scope, text), memories (id, scope, text, metadata), episodes (id,
    scope, journal ids), the journal ids one completed purge took, current facts (id, scope, each evidence's
    (source type, source id, excerpt)), a memory's digest rows (memory id, run id, message ids) and the entries a
    memory was made from (memory id, journal id)."""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(_SHAPED_SCHEMA)
        conn.executemany(
            "INSERT INTO journal_entries(id,scope_id,session_id,turn_number,role,content,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            [(entry, scope, "s-1", entry, "user", text, _AT) for entry, scope, text in journals],
        )
        conn.executemany(
            "INSERT INTO memories(id,scope_id,session_id,source,target,content,summary,created_at,updated_at,metadata)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (name, scope, "s-1", "legacy", "memory", text, text[:40], _AT, _AT, meta)
                for name, scope, text, meta in memories
            ],
        )
        conn.executemany(
            "INSERT INTO task_episodes(id,scope_id,session_id,task_goal,status,started_at,journal_entry_ids)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (name, scope, "s-1", f"TEST 任务 {name}", "completed", _AT, json.dumps(ids))
                for name, scope, ids in episodes
            ],
        )
        if purged:
            conn.execute(
                "INSERT INTO privacy_purge_operations VALUES ('purge-1','fp','scope',?,?,0,'completed',?,?,'',?)",
                (len(purged), len(purged), _AT, _AT, _AT),
            )
            conn.executemany(
                "INSERT INTO privacy_purge_source_tombstones VALUES ('purge-1',?,?,?)",
                [(entry, f"hash-{entry}", _AT) for entry in purged],
            )
        for name, scope, evidence in facts:
            conn.execute(
                "INSERT INTO fact_claims VALUES (?,NULL,?,'用户','偏好',?,'TEST',?,?,?,?)",
                (name, scope, f"key-{name}", "single", "direct", _AT, "current"),
            )
            conn.executemany(
                "INSERT INTO fact_claim_evidence VALUES (?,?,?,?,?,?,?)",
                [
                    (f"{name}-{index}", name, kind, str(ref), f"eh-{index}", excerpt, _AT)
                    for index, (kind, ref, excerpt) in enumerate(evidence)
                ],
            )
        conn.executemany(
            "INSERT INTO memory_digest_sources VALUES (?,?,'s-1',?,?,?)",
            [(memory, run, ids, f"sh-{run}", _AT) for memory, run, ids in digests],
        )
        conn.executemany(
            "INSERT INTO memory_journal_sources VALUES (?,?,'run-1',?)",
            [(memory, entry, _AT) for memory, entry in links],
        )
        conn.commit()
    return path


def _read(target: Path, scopes: set[str], allowed: set[str]):
    """A read of what ``migrate_legacy(source, target)`` wrote, as a reader of ``allowed``."""
    binding = InstanceBinding("p15-synthetic-agent", "p15-synthetic-installation", target, frozenset(scopes), True)
    return SQLiteStorage(binding).read(TrustedContext(binding, "TEST-reader", frozenset(allowed), "human_direct"))


def test_a_record_longer_than_a_source_is_stored_in_parts_and_read_whole(tmp_path: Path) -> None:
    """A 2.0 memory merged again and again reached 77,000 characters, and the migration ended on
    ``CHECK(length(content)<=65536)``.  A record that long is stored as a long message is captured: in parts of at most
    65,536 characters, every character once and in order, one source group under the record's key and id."""
    from scope_recall.core import lexical_index
    from scope_recall.core.events import MAX_SEGMENT_CHARS

    from maintenance.migration_records import sanitized_text

    merged = "".join(f"第{i}次合并：部署检查记录，服务正常。" for i in range(1, 9000))[: 2 * MAX_SEGMENT_CHARS + 1000]
    merged += " TESTTAILWORD"
    said = "很长的一次对话。" * 9000
    assert sanitized_text(merged)[0] == merged and sanitized_text(said)[0] == said
    legacy = _shaped_store(
        tmp_path / "old.sqlite3",
        journals=[(1, "scope-a", said), (2, "scope-a", "一句短话。")],
        memories=[("m-long", "scope-a", merged, "{}"), ("m-1", "scope-a", "短记忆。", "{}")],
    )
    target = tmp_path / "target"
    report = migrate_legacy(legacy, target)
    assert report["completion_status"] == "complete", report["cutover_block"]
    assert report["segmented_records"] == [
        {"record": "legacy:journal_entries:1", "parts": 2},
        {"record": "legacy:memories:m-long", "parts": 3},
    ]
    with closing(sqlite3.connect(target / "memory.sqlite3")) as conn:

        def parts(group: str) -> list[tuple]:
            return conn.execute(
                "SELECT event_id,source_event_key,segment_index,segment_total,content FROM source_events"
                " WHERE source_group_key=? ORDER BY segment_index",
                (group,),
            ).fetchall()

        memory, journal = parts("legacy:memories:m-long"), parts("legacy:journal_entries:1")
        assert [row[2:4] for row in memory] == [(0, 3), (1, 3), (2, 3)]
        assert "".join(row[4] for row in memory) == merged and "".join(row[4] for row in journal) == said
        assert max(len(row[4]) for row in memory + journal) == MAX_SEGMENT_CHARS
        # Whatever cites the record finds its first part under the record's own id and key.
        assert memory[0][:2] == (stable_legacy_id("event", "memories:m-long"), "legacy:memories:m-long")
        assert "testtailword" in lexical_index.terms_of(conn, lexical_index.source_id(conn, memory[2][0], 1))
        assert (
            conn.execute("SELECT count(*) FROM source_events").fetchone()[0] == report["counts"]["source_events"] == 7
        )
    with _read(target, {"scope-a"}, {"scope-a"}) as tx:
        for event_id, _key, _index, _total, content in memory + journal:
            source = tx.source(event_id, 1)
            assert source is not None and source.event["content"] == content
            assert "source_segments_incomplete" not in source.capture_gaps
    assert migrate_legacy(legacy, target)["counts"] == report["counts"]


def test_journal_entries_two_episodes_share_have_one_owner_and_both_links(tmp_path: Path) -> None:
    """2.0's automatic episodes overlap, and the migration job ended on ``UNIQUE(episode_events.source_ref,
    source_revision)``: it never used the converter's membership projection.  Each entry now has one processing
    owner, both episodes keep their evidence links, and the one that lost entries is out of automatic recall."""
    legacy = _shaped_store(
        tmp_path / "old.sqlite3",
        journals=[(entry, "scope-a", f"第{entry}句。") for entry in range(1, 7)],
        episodes=[("episode_auto_a", "scope-a", [1, 2, 3, 4]), ("episode_auto_b", "scope-a", [3, 4, 5, 6])],
    )
    report = migrate_legacy(legacy, tmp_path / "target")
    assert report["completion_status"] == "complete", report["cutover_block"]
    audit = report["legacy_episode_memberships"]
    assert (audit["verified"], audit["primary_memberships"], audit["evidence_only_edges"]) == (True, 6, 2)
    owner, other = sorted(stable_legacy_id("episode", name) for name in ("episode_auto_a", "episode_auto_b"))
    with closing(sqlite3.connect(tmp_path / "target" / "memory.sqlite3")) as conn:
        owners = dict(conn.execute("SELECT source_ref,episode_id FROM episode_events").fetchall())
        assert len(owners) == 6
        assert {owners[stable_legacy_id("event", f"journal_entries:{entry}")] for entry in (3, 4)} == {owner}
        links = conn.execute(
            "SELECT object_ref,count(*) FROM evidence_links WHERE object_kind='episode' GROUP BY object_ref"
        ).fetchall()
        assert dict(links) == {owner: 4, other: 4}
        assert dict(conn.execute("SELECT episode_id,suppressed FROM episodes").fetchall()) == {owner: 0, other: 1}


def test_an_episode_entry_under_another_scope_is_reported_and_the_episode_stays_readable(tmp_path: Path) -> None:
    """A 2.0 store whose scope ids changed while it ran has episodes naming entries of their own session under the old
    id.  Linked, the episode cited a source its readers cannot open, which hides the whole episode from them; the
    entry now stays its own source, unlinked, and the report names it."""
    legacy = _shaped_store(
        tmp_path / "old.sqlite3",
        journals=[(1, "scope-a", "第一句。"), (2, "scope-a", "第二句。"), (3, "scope-b", "旧身份下的第三句。")],
        episodes=[("ep-1", "scope-a", [1, 2, 3])],
    )
    target = tmp_path / "target"
    report = migrate_legacy(legacy, target)
    assert report["completion_status"] == "complete", report["cutover_block"]
    (row,) = [item for item in report["unmapped"] if item["reason"] == "episode_source_outside_episode_scope"]
    assert (row["key"], row["journal_entry_ids"]) == ("ep-1", ["3"])
    with _read(target, {"scope-a", "scope-b"}, {"scope-a"}) as tx:
        assert tx.episodes.get(stable_legacy_id("episode", "ep-1")) is not None
    third = stable_legacy_id("event", "journal_entries:3")
    with closing(sqlite3.connect(target / "memory.sqlite3")) as conn:
        assert conn.execute("SELECT count(*) FROM episode_events").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE source_ref=?", (third,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT scope_id,read_blocked FROM source_events WHERE event_id=?", (third,)
        ).fetchone() == (
            "scope-b",
            0,
        )


def test_more_hidden_memories_than_one_suppression_request_holds(tmp_path: Path) -> None:
    """Core refuses a suppression whose closure passes 2,000 objects, and a large 2.0 store hides more memories than
    that (archived, obsolete, ...): the converter asks in parts, each within the budget.  A fact two of them support,
    one in each part, is reached twice and counted once."""
    archived = json.dumps({"lifecycle": "archived"})
    legacy = _shaped_store(
        tmp_path / "old.sqlite3",
        journals=[(1, "scope-a", "一句话。")],
        memories=[(f"m-{index}", "scope-a", f"第{index}条旧记忆。", archived) for index in range(2050)],
        facts=[("fact-1", "scope-a", [("memory", "m-0", "第0条旧记忆"), ("memory", "m-2049", "第2049条旧记忆")])],
    )
    report = migrate_legacy(legacy, tmp_path / "target")
    assert report["completion_status"] == "complete", report["cutover_block"]
    lifecycle = report["legacy_lifecycle_suppression"]
    assert (lifecycle["target_count"], len(lifecycle["receipts"])) == (2050, 2)
    assert lifecycle["affected_count"] == 2051, "the 2,050 memories and the fact, once"
    with closing(sqlite3.connect(tmp_path / "target" / "memory.sqlite3")) as conn:
        hidden = conn.execute("SELECT object_kind,count(*) FROM object_blocks WHERE suppressed=1 GROUP BY object_kind")
        assert dict(hidden.fetchall()) == {"event": 2050, "claim": 1}


def test_a_purged_entry_stored_in_parts_is_purged_whole(tmp_path: Path) -> None:
    """A purge 2.0 completed is replayed as a Core deletion of its members, and the purge scrubs the members it is
    given: a deleted entry stored in parts must give it every part, or its tail stays readable."""
    from maintenance.migration_records import canonical_json

    legacy = _shaped_store(
        tmp_path / "old.sqlite3",
        journals=[(1, "scope-a", "被删掉的长对话。" * 9000), (2, "scope-a", "留下的一句。")],
        purged=[1],
    )
    target = tmp_path / "target"
    report = migrate_legacy(legacy, target)
    assert report["completion_status"] == "complete", report["cutover_block"]
    parts = [
        stable_legacy_id("event", "journal_entries:1"),
        stable_legacy_id("event", canonical_json(["journal_entries", "1", 1])),
    ]
    with closing(sqlite3.connect(target / "memory.sqlite3")) as conn:
        rows = conn.execute("SELECT read_blocked,content FROM source_events WHERE event_id IN (?,?)", parts).fetchall()
    assert rows == [(1, ""), (1, "")]
    with _read(target, {"scope-a"}, {"scope-a"}) as tx:
        assert [tx.source(ref, 1) for ref in parts] == [None, None]
        assert tx.source(stable_legacy_id("event", "journal_entries:2"), 1) is not None
    assert migrate_legacy(legacy, target)["counts"] == report["counts"]


def test_a_quote_running_across_a_part_boundary_keeps_its_evidence(tmp_path: Path) -> None:
    """A fact's excerpt can sit where a long entry is cut in two.  Each part is linked with its piece of the quote, so
    the fact keeps live, exact evidence and stays current."""
    from scope_recall.core.events import MAX_SEGMENT_CHARS

    from maintenance.migration_records import canonical_json

    said = "甲" * (MAX_SEGMENT_CHARS - 3) + "用户确认保留" + "乙" * 10
    legacy = _shaped_store(
        tmp_path / "old.sqlite3",
        journals=[(1, "scope-a", said)],
        facts=[("fact-1", "scope-a", [("journal", 1, "用户确认保留")])],
    )
    report = migrate_legacy(legacy, tmp_path / "target")
    assert report["completion_status"] == "complete", report["cutover_block"]
    assert report["fact_authority"]["active"] == 1
    first = stable_legacy_id("event", "journal_entries:1")
    second = stable_legacy_id("event", canonical_json(["journal_entries", "1", 1]))
    with closing(sqlite3.connect(tmp_path / "target" / "memory.sqlite3")) as conn:
        links = conn.execute(
            "SELECT source_ref,quote FROM evidence_links WHERE object_kind='claim' AND relation='supports'"
        ).fetchall()
    assert sorted(links) == sorted([(first, "用户确"), (second, "认保留")])


def test_digest_rows_of_a_memory_stored_in_parts_join_its_group_unreadable(tmp_path: Path, capsys) -> None:
    """A memory's digest rows are kept as unreadable audit archives in the memory's source group, after its parts.  A
    digest row long enough to be stored in parts itself is so in every part: none is readable, none left outside."""
    from scope_recall.adapters.hermes import install_hermes_scope_recall
    from scope_recall.maintenance import upgrade_cli

    (tmp_path / "old").mkdir()
    long_ids = json.dumps([f"msg-{index:06d}" for index in range(7000)])
    legacy = _shaped_store(
        tmp_path / "old" / "memory.sqlite3",
        journals=[(1, _OLD_CLI, "一句。")],
        memories=[("m-long", _OLD_CLI, "合并后的长记忆。" * 9000, "{}")],
        digests=[("m-long", "run-1", "[]"), ("m-long", "run-2", long_ids)],
    )
    home = tmp_path / "home"
    home.mkdir()
    cli = {**_owner_rows()[0], "allowed_scope_ids": [_OLD_CLI]}
    install_hermes_scope_recall(
        home,
        agent_id="default",
        agent_workspace="hermes",
        owner_principals=_OWNERS[:1],
        audiences=[cli],
        legacy_audit_retention=True,
    )
    job = str(tmp_path / "job")
    prepare = ["migrate", "prepare", "--source", str(legacy), "--job", job, "--installation-manifest", str(home)]
    assert upgrade_cli.main(prepare) == 0
    capsys.readouterr()
    assert upgrade_cli.main(["migrate", "run", "--job", job, "--source-quiesced"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "verified"
    with closing(sqlite3.connect(home / "scope-recall" / "memory.sqlite3")) as conn:
        group = conn.execute(
            "SELECT segment_index,segment_total,read_blocked,json_extract(extra_json,'$.legacy_table')"
            " FROM source_events WHERE source_group_key='legacy:memories:m-long' ORDER BY segment_index"
        ).fetchall()
        readable = conn.execute("SELECT count(*) FROM source_events WHERE read_blocked=0").fetchone()[0]
    assert group[:2] == [(0, 2, 0, "memories"), (1, 2, 0, "memories")]
    assert [row[0] for row in group[2:]] == [2, 3, 4]
    assert all(row[1:] == (None, 1, "memory_digest_sources") for row in group[2:])
    assert readable == 3, "the entry and the memory's two parts"
    # The report names each record by its own key, not by the group a digest row joined.
    (report,) = (tmp_path / "job").glob("conversion-*.json")
    assert json.loads(report.read_text(encoding="utf-8"))["segmented_records"] == [
        {"record": "legacy:memories:m-long", "parts": 2},
        {"record": "legacy:memory_digest_sources:6:m-long-5:run-2-3:s-1", "parts": 2},
    ]


def test_the_migration_job_takes_a_store_with_a_long_memory_and_shared_entries(tmp_path: Path, capsys) -> None:
    """The user's run: ``migrate prepare`` and ``migrate run --source-quiesced``, which verifies what it wrote, on a
    2.0 store holding a memory longer than a source and two automatic episodes sharing entries."""
    from scope_recall.adapters.hermes import install_hermes_scope_recall
    from scope_recall.maintenance import upgrade_cli

    (tmp_path / "old").mkdir()
    legacy = _shaped_store(
        tmp_path / "old" / "memory.sqlite3",
        journals=[(entry, _OLD_CLI, f"第{entry}句。") for entry in range(1, 9)],
        memories=[("m-long", _OLD_CLI, "合并后的长记忆。" * 9000, "{}")],
        episodes=[("episode_auto_a", _OLD_CLI, [1, 2, 3, 4, 5]), ("episode_auto_b", _OLD_CLI, [4, 5, 6, 7, 8])],
    )
    home = tmp_path / "home"
    home.mkdir()
    cli = {**_owner_rows()[0], "allowed_scope_ids": [_OLD_CLI]}
    install_hermes_scope_recall(
        home, agent_id="default", agent_workspace="hermes", owner_principals=_OWNERS[:1], audiences=[cli]
    )
    job = str(tmp_path / "job")
    assert (
        upgrade_cli.main(
            ["migrate", "prepare", "--source", str(legacy), "--job", job, "--installation-manifest", str(home)]
        )
        == 0
    )
    capsys.readouterr()
    assert upgrade_cli.main(["migrate", "run", "--job", job, "--source-quiesced"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert (done["state"], done["integrity"]) == ("verified", "ok")
    assert done["target_counts"]["source_events"] == done["counts"]["source_events"] == 8 + 2


def test_a_purge_takes_the_digest_rows_of_a_memory_it_deletes(tmp_path: Path, capsys) -> None:
    """A purge 2.0 completed deletes the memories made from the entries it took, each with its whole source group, as
    Core's own deletion takes siblings: the memory's parts and its digest rows, each scrubbed."""
    from scope_recall.adapters.hermes import install_hermes_scope_recall
    from scope_recall.maintenance import upgrade_cli

    (tmp_path / "old").mkdir()
    kept = "留下的一句。"
    legacy = _shaped_store(
        tmp_path / "old" / "memory.sqlite3",
        journals=[(1, _OLD_CLI, "要删掉的一句。"), (2, _OLD_CLI, kept)],
        memories=[("m-long", _OLD_CLI, "合并后的长记忆。" * 9000, "{}")],
        links=[("m-long", 1)],
        digests=[("m-long", "run-1", "[]"), ("m-long", "run-2", json.dumps([f"msg-{i:06d}" for i in range(7000)]))],
        purged=[1],
    )
    home = tmp_path / "home"
    home.mkdir()
    cli = {**_owner_rows()[0], "allowed_scope_ids": [_OLD_CLI]}
    install_hermes_scope_recall(
        home,
        agent_id="default",
        agent_workspace="hermes",
        owner_principals=_OWNERS[:1],
        audiences=[cli],
        legacy_audit_retention=True,
    )
    job = str(tmp_path / "job")
    prepare = ["migrate", "prepare", "--source", str(legacy), "--job", job, "--installation-manifest", str(home)]
    assert upgrade_cli.main(prepare) == 0
    capsys.readouterr()
    assert upgrade_cli.main(["migrate", "run", "--job", job, "--source-quiesced"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "verified"
    with closing(sqlite3.connect(home / "scope-recall" / "memory.sqlite3")) as conn:
        rows = conn.execute(
            "SELECT read_blocked,count(*),max(length(content)) FROM source_events GROUP BY read_blocked"
        ).fetchall()
    # Unreadable and empty: the entry, the memory's two parts, one digest row and the two parts of the other.
    assert rows == [(0, 1, len(kept)), (1, 6, 0)]


def test_a_record_sqlite_refuses_is_named_by_its_table_and_event_id(tmp_path: Path) -> None:
    """A record SQLite refuses is named by its table and event id, never by its legacy id, which is legacy text and
    can hold a path or a token.  The record here, a memory whose summary is more than a source's metadata holds, is
    one 2.0 never writes: it keeps a summary to 220 characters."""
    from maintenance.migration_records import MigrationError

    name = "C:/private/token=TEST-secret"
    legacy = _shaped_store(
        tmp_path / "old.sqlite3", journals=[(1, "scope-a", "一句。")], memories=[(name, "scope-a", "短", "{}")]
    )
    with closing(sqlite3.connect(legacy)) as conn:
        conn.execute("UPDATE memories SET content='', summary=?", ("长" * 140_000,))
        conn.commit()
    with pytest.raises(MigrationError) as refused:
        migrate_legacy(legacy, tmp_path / "target")
    assert str(refused.value) == (
        f"legacy memories record {stable_legacy_id('event', 'memories:' + name)} cannot be stored: "
        "CHECK constraint failed: length(extra_json)<=131072 AND json_valid(extra_json)"
    )


@pytest.mark.parametrize(
    ("kind", "told_as_is"),
    [("constraint", True), ("unique", True), ("converter", True), ("decode", False), ("unknown_form", False)],
)
def test_a_refusal_is_told_only_when_its_text_cannot_carry_a_row(
    tmp_path: Path, capsys, monkeypatch, kind, told_as_is
) -> None:
    """A CHECK failure that SQLite has named is told, not masked as ``invalid_or_unavailable_input``.  A constraint
    failure in the forms SQLite gives one names a constraint, table or column of the schema, and the converter's
    refusals are fixed phrases, so they are told; any other text stays masked, whatever its class, for an SQLite error
    can quote a cell that is not UTF-8.  And the workflow names the reader contract the converter accepts."""
    from scope_recall.maintenance import upgrade_cli
    from scope_recall.maintenance.legacy_v2_compat import MEMORY_READER_CONTRACT, LegacyCompatibilityError

    refusal = {
        "constraint": sqlite3.IntegrityError("CHECK constraint failed: length(content)<=65536"),
        "unique": sqlite3.IntegrityError(
            "UNIQUE constraint failed: episode_events.source_ref, episode_events.source_revision"
        ),
        "converter": LegacyCompatibilityError("memory_storage_reader_contract_not_verified"),
        "decode": sqlite3.OperationalError("Could not decode to UTF-8 column 'content' with text 'TEST-secret=abc'"),
        "unknown_form": sqlite3.IntegrityError("TEST-secret=abc"),
    }[kind]

    def run(*_args, **_kwargs):
        raise refusal

    monkeypatch.setattr(upgrade_cli, "run_upgrade", run)
    assert upgrade_cli.main(["migrate", "run", "--job", str(tmp_path / "job"), "--source-quiesced"]) == 3
    told = json.loads(capsys.readouterr().out)
    assert told["error_type"] == type(refusal).__name__
    assert told["reason"] == (str(refusal) if told_as_is else "invalid_or_unavailable_input")
    workflow = Path(__file__).resolve().parents[2] / "maintenance" / "AGENT_WORKFLOW.md"
    assert f"--legacy-reader-contract {MEMORY_READER_CONTRACT}" in workflow.read_text(encoding="utf-8")


def _legacy_facts(path: Path, facts) -> None:
    """Facts of a shaped store in scope-a, each (id, subject, predicate, value, status, recorded at, journal id,
    excerpt), one fact key to a slot as 2.0 kept a slot's history."""
    with closing(sqlite3.connect(path)) as conn:
        for name, subject, predicate, value, status, recorded, entry, excerpt in facts:
            conn.execute(
                "INSERT INTO fact_claims VALUES (?,NULL,'scope-a',?,?,?,?,'single','direct',?,?)",
                (name, subject, predicate, f"key-{subject}-{predicate}", value, recorded, status),
            )
            conn.execute(
                "INSERT INTO fact_claim_evidence VALUES (?,?,'journal',?,?,?,?)",
                (f"{name}-ev", name, str(entry), f"eh-{name}", excerpt, recorded),
            )
        conn.commit()


def test_a_converted_store_holds_no_denial_as_a_value(tmp_path: Path) -> None:
    """2.0 took a denial for its slot's value as readily as any other value.  The conversion settles each as
    restore-displaced-values does, in its own transaction: "不是 CircleCI", all TEST-project's CI service held, leaves
    it no value, "不再是 vault-1" ends the vault-1 it denies, and the person's own 不吃辣 stays their value.  Every
    version stays, and a second run changes nothing."""
    legacy = _shaped_store(
        tmp_path / "old.sqlite3",
        journals=[
            (1, "scope-a", "TEST-project 的 CI service 不是 CircleCI。"),
            (2, "scope-a", "TEST-project 的 backup bucket 是 vault-1。"),
            (3, "scope-a", "TEST-project 的 backup bucket 不再是 vault-1。"),
            (4, "scope-a", "我不吃辣。"),
        ],
    )
    later = "2026-07-02T00:00:00Z"
    _legacy_facts(
        legacy,
        [
            ("fact-ci", "TEST-project", "CI service", "不是 CircleCI", "current", _AT, 1, "不是 CircleCI"),
            ("fact-bucket-1", "TEST-project", "backup bucket", "vault-1", "superseded", _AT, 2, "vault-1"),
            ("fact-bucket-2", "TEST-project", "backup bucket", "不再是 vault-1", "current", later, 3, "不再是 vault-1"),
            ("fact-diet", "用户", "饮食", "不吃辣", "current", _AT, 4, "不吃辣"),
        ],
    )
    target = tmp_path / "target"
    report = migrate_legacy(legacy, target)
    assert report["completion_status"] == "complete", report["cutover_block"]
    assert report["fact_authority"]["denials_settled"] == 2

    def facts() -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
        """Each fact's head state, and its versions' states and values, by predicate."""
        with closing(sqlite3.connect(target / "memory.sqlite3")) as conn:
            heads = dict(
                conn.execute(
                    "SELECT c.predicate, v.state FROM claims c JOIN claim_versions v"
                    " ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.kind='fact'"
                ).fetchall()
            )
            versions: dict[str, list[tuple[str, str]]] = {}
            for predicate, state, value in conn.execute(
                "SELECT c.predicate, v.state, json_extract(v.payload_json, '$.value_text') FROM claims c"
                " JOIN claim_versions v USING(claim_id) WHERE c.kind='fact' ORDER BY v.revision"
            ):
                versions.setdefault(predicate, []).append((state, value))
        return heads, versions

    heads, versions = facts()
    assert heads == {"CI service": "retracted", "backup bucket": "retracted", "饮食": "active"}
    assert versions == {
        "CI service": [("active", "不是 CircleCI"), ("retracted", "不是 CircleCI")],
        "backup bucket": [("superseded", "vault-1"), ("active", "不再是 vault-1"), ("retracted", "不再是 vault-1")],
        "饮食": [("active", "不吃辣")],
    }
    assert migrate_legacy(legacy, target)["fact_authority"]["denials_settled"] == 0
    assert facts() == (heads, versions)


def test_conversion_preserves_every_tabulated_description_and_settles_only_denials(tmp_path):
    """Conversion preserves descriptions as active project facts, keeps their history and retries without changes."""
    states = (*NEGATED_STATES["zh"], *NEGATED_STATES["en"])
    values = (*states, "不是 CircleCI", "not Jenkins")
    legacy = _shaped_store(
        tmp_path / "old.sqlite3",
        journals=[(i, "scope-a", f"TEST-project state {i} 是 {value}。") for i, value in enumerate(values, 1)],
    )
    _legacy_facts(
        legacy,
        [
            (f"state-{i}", "TEST-project", f"state {i}", value, "current", _AT, i, value)
            for i, value in enumerate(values, 1)
        ],
    )
    target = tmp_path / "target"
    report = migrate_legacy(legacy, target)
    assert report["completion_status"] == "complete", report["cutover_block"]
    assert report["fact_authority"]["denials_settled"] == 2
    query = (
        "SELECT c.predicate,v.state,json_extract(v.payload_json,'$.value_text') FROM claims c"
        " JOIN claim_versions v USING(claim_id) ORDER BY c.predicate,v.revision"
    )
    with closing(sqlite3.connect(target / "memory.sqlite3")) as conn:
        versions = conn.execute(query).fetchall()
    for i, value in enumerate(states, 1):
        assert [row for row in versions if row[0] == f"state {i}"] == [(f"state {i}", "active", value)]
    for i, value in enumerate(values[len(states) :], len(states) + 1):
        assert [row for row in versions if row[0] == f"state {i}"] == [
            (f"state {i}", "active", value),
            (f"state {i}", "retracted", value),
        ]
    assert migrate_legacy(legacy, target)["fact_authority"]["denials_settled"] == 0
    with closing(sqlite3.connect(target / "memory.sqlite3")) as conn:
        assert conn.execute(query).fetchall() == versions


def test_upgrade_snapshot_holds_off_other_processes_migration(tmp_path, capsys, monkeypatch):
    project = tmp_path / "TEST-project"
    project.mkdir()
    instance = tmp_path / "TEST-install"
    _config, core = install_codex_scope_recall(instance, project_root=project)
    downgrade_store(core.storage.path, 1111)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute("CREATE TABLE synthetic_padding(body BLOB)")
        conn.executemany("INSERT INTO synthetic_padding VALUES (zeroblob(4096))", [()] * 350)
        conn.commit()
    probe = """
import sys
from pathlib import Path
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.writer_lease import TruthWriterBusyError
from scope_recall.maintenance.doctor import load_binding
binding, _ = load_binding("codex", Path(sys.argv[1]))
try:
    print(SQLiteStorage(binding, timeout_seconds=0.05).initialize().schema_version)
except TruthWriterBusyError:
    print("busy")
"""

    def attempt_migration():
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe, str(instance)], cwd=tmp_path, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    attempts = []

    def check_ownership():
        if not attempts:
            attempts.append(attempt_migration())

    monkeypatch.setattr(backup_primitives, "check_backup_deadline", check_ownership)
    code = cli.main(
        [
            "upgrade-store",
            "--host",
            "codex",
            "--instance-root",
            str(instance),
            "--backup-dir",
            str(tmp_path / "TEST-backups"),
        ]
    )
    receipt = json.loads(capsys.readouterr().out)
    assert code == 0 and attempts == ["busy"], receipt
    assert receipt["backup_schema"] == receipt["backup_recorded_schema"] == 1111
    with closing(sqlite3.connect(receipt["backup"])) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1111
        assert conn.execute("SELECT schema_version FROM instance_meta").fetchone()[0] == 1111
    assert attempt_migration() == str(SCHEMA_VERSION), "write ownership ends with the command"


@pytest.mark.parametrize("close_number", [1, 3])
def test_session_index_open_keeps_its_budget_when_another_process_takes_the_lease(tmp_path, monkeypatch, close_number):
    project = tmp_path / "TEST-project"
    project.mkdir()
    _config, core = install_codex_scope_recall(tmp_path / "TEST-install", project_root=project)
    storage = core.storage
    ctx = TrustedContext(storage.binding, "TEST-session", storage.binding.scope_ids, "host_generated")
    with closing(sqlite3.connect(storage.path)) as conn:
        conn.execute("DROP INDEX source_session")
    probe = """
import sys
import time
from pathlib import Path
from scope_recall.core.writer_lease import TruthWriterLease
lease = TruthWriterLease(Path(sys.argv[1]), role="provider")
print("ready", flush=True)
sys.stdin.readline()
assert lease.acquire()
print("held", flush=True)
time.sleep(0.2)
lease.release()
"""
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", probe, str(storage.path.parent)],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    actual_close, closed = storage._close, []

    def close_and_contend(conn, original):
        actual_close(conn, original)
        closed.append(conn)
        # The first close precedes initialization; the third precedes reopening.
        if len(closed) == close_number:
            child.stdin.write("acquire\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "held"

    monkeypatch.setattr(storage, "_close", close_and_contend)
    try:
        assert child.stdout.readline().strip() == "ready"
        started = time.monotonic()
        with pytest.raises((TruthWriterBusyError, ContractError)) as refused:
            with storage.write(ctx, remaining_seconds=0.05):
                pytest.fail("the index open waited for the other writer's full lease")
        elapsed = time.monotonic() - started
        if isinstance(refused.value, ContractError):
            assert refused.value.code == "DEADLINE_EXCEEDED"
        # Allow scheduler overhead, while remaining below the holder's 200 ms.
        assert elapsed < 0.1
        assert truth_writer_process_snapshot(storage.path.parent)["connection_pin_count"] == 0
    finally:
        _out, err = child.communicate(timeout=5)
        assert child.returncode == 0, err
