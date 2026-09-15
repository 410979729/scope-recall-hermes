"""Focused P15 archival-bridge tests against real installer, migrator, and Core APIs."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from maintenance.legacy_fixture import build_official_578b_fixture
from maintenance.migrate_v2 import MigrationError, build_legacy_catalog, main, migrate_legacy
from scope_recall.adapters.hermes import bind_hermes_identity
from scope_recall.adapters.hermes.installation import (
    HermesIdentityError,
    build_archive_scope_id,
    build_installation_manifest,
    install_hermes_archive_migration,
    is_archive_scope,
    load_installation_manifest,
    write_installation_manifest,
)
from scope_recall.contracts import TrustedContext
from scope_recall.core import CoreConfig, MemoryCore


def _insert_row(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    columns = ",".join(row)
    placeholders = ",".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table}({columns}) VALUES ({placeholders})", tuple(row.values()))


def _archival_source(tmp_path: Path) -> Path:
    legacy = build_official_578b_fixture(tmp_path / "legacy.sqlite3", repo_root=Path.cwd())
    sha1 = "0123456789abcdef0123456789abcdef01234567"
    with closing(sqlite3.connect(legacy)) as conn:
        conn.row_factory = sqlite3.Row
        memory = dict(conn.execute("SELECT * FROM memories WHERE id='mem-1'").fetchone())
        memory.update(
            id="mem-2",
            session_id="s-2",
            content="第二持久化记忆",
            summary="第二持久化记忆",
            created_at="2026-09-01T00:04:00Z",
            updated_at="2026-09-01T00:04:00Z",
        )
        _insert_row(conn, "memories", memory)
        conn.execute(
            "INSERT INTO memory_journal_sources(memory_id,journal_entry_id,run_id,created_at) VALUES ('mem-2',3,'run-2','2026-09-01T00:04:00Z')"
        )
        conn.execute(
            "INSERT INTO governance_audit_events(id,event_type,action,scope_id,created_at) VALUES (?,?,?,?,?)",
            ("g-empty", "p15-synthetic-audit", "p15-sentinel-empty", "", "2026-09-01T00:06:00Z"),
        )
        conn.execute(
            "INSERT INTO governance_audit_events(id,event_type,action,scope_id,created_at) VALUES (?,?,?,?,?)",
            ("g-star", "p15-synthetic-audit", "p15-sentinel-star", "*", "2026-09-01T00:06:00Z"),
        )
        conn.execute(
            "INSERT INTO governance_audit_events(id,event_type,action,scope_id,created_at) VALUES (?,?,?,?,?)",
            ("g-audit", "p15-synthetic-audit", "p15-sentinel-audit-only", "audit-only-1", "2026-09-01T00:06:00Z"),
        )
        conn.execute("INSERT INTO relation_scope_statistics(scope_id) VALUES (NULL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_digest_sources ("
            "memory_id TEXT, run_id TEXT, session_id TEXT, message_ids TEXT, source_hash TEXT, created_at TEXT)"
        )
        for memory_id in ("mem-1", "mem-2", "mem-missing"):
            conn.execute(
                "INSERT INTO memory_digest_sources(memory_id,run_id,session_id,message_ids,source_hash,created_at) VALUES (?,?,?,?,?,?)",
                (memory_id, "r1", "digest-s1", '["ext-msg-1"]', sha1, "2026-09-01T00:05:00Z"),
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS nightly_digest_quarantine ("
            "id TEXT, run_id TEXT, session_id TEXT, candidate_hash TEXT, reason_codes TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO nightly_digest_quarantine(id,run_id,session_id,candidate_hash,reason_codes,created_at) VALUES (?,?,?,?,?,?)",
            ("q1", "r1", "digest-s1", sha1, '["low_confidence"]', "2026-09-01T00:05:00Z"),
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS nightly_digest_runs ("
            "id TEXT, digest_date TEXT, source_db TEXT, started_at TEXT, extractor TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO nightly_digest_runs(id,digest_date,source_db,started_at,extractor,status) VALUES (?,?,?,?,?,?)",
            ("r1", "2026-09-01", "legacy", "2026-09-01T00:05:00Z", "ext1", "completed"),
        )
        conn.commit()
    return legacy


def _file_digests(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def _source_map_keys(catalog: dict) -> set[str]:
    return set(catalog["content_scopes"]) | set(catalog["shared_only_scopes"]) | set(catalog["audit_only_scopes"])


def _archive_cli(source: Path, home: Path, catalog: dict, *, report: Path | None = None, batch: str = "p15-fixed-batch-001") -> list[str]:
    args = [
        "--archive-install-test",
        "--source",
        str(source),
        "--target",
        str(home),
        "--source-hash",
        catalog["source_sha256"],
        "--catalog-hash",
        catalog["catalog_sha256"],
        "--batch-key",
        batch,
    ]
    if report is not None:
        args.extend(["--report", str(report)])
    return args


def _legacy_rows(db: Path, table: str) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return list(
            conn.execute(
                "SELECT * FROM source_events WHERE json_extract(extra_json,'$.legacy_table')=?",
                (table,),
            )
        )


def _install_archive(tmp_path: Path, *, name: str = "TEST-archive"):
    source = _archival_source(tmp_path)
    source_bytes = source.read_bytes()
    catalog = build_legacy_catalog(source)
    home = tmp_path / name
    binding, manifest, catalog = install_hermes_archive_migration(
        home,
        source_database=source,
        test_mode=True,
        expected_source_hash=catalog["source_sha256"],
        expected_catalog_hash=catalog["catalog_sha256"],
    )
    assert source.read_bytes() == source_bytes
    return source, source_bytes, catalog, home, binding, manifest


def test_catalog_keeps_sentinels_and_exact_source_map(tmp_path: Path) -> None:
    source = _archival_source(tmp_path)
    catalog = build_legacy_catalog(source)
    assert catalog["is_supported"]
    assert "scope-a" in catalog["content_scopes"]
    assert "shared-a" in catalog["shared_only_scopes"]
    assert "audit-only-1" in catalog["audit_only_scopes"]
    assert catalog["total_nonempty_raw_values"] == sum(1 for key in catalog["scope_occurrences"] if key != "")
    assert "" in catalog["scope_occurrences"]
    assert catalog["audit_sentinels"][""]["total"] >= 1
    assert catalog["audit_sentinels"]["*"]["total"] >= 1
    assert catalog["audit_sentinels"]["<null>"]["total"] >= 1
    assert catalog["audit_sentinels"][""]["occurrences"]["governance_audit_events.scope_id"] >= 1
    assert catalog["audit_sentinels"]["*"]["occurrences"]["governance_audit_events.scope_id"] >= 1
    assert catalog["audit_sentinels"]["<null>"]["occurrences"]["relation_scope_statistics.scope_id"] >= 1
    assert catalog["audit_sentinels"][""]["occurrences"] == {
        "governance_audit_events.scope_id": 1,
        "procedural_playbooks.shared_scope_id": 1,
    }
    assert catalog["audit_sentinels"][""]["total"] == 2
    assert catalog["audit_sentinels"]["*"]["occurrences"] == {
        "governance_audit_events.scope_id": 1,
    }
    assert catalog["audit_sentinels"]["*"]["total"] == 1
    assert catalog["audit_sentinels"]["<null>"]["occurrences"] == {
        "relation_scope_statistics.scope_id": 1,
    }
    assert catalog["audit_sentinels"]["<null>"]["total"] == 1
    assert "scope-a" in catalog["scope_occurrences"]
    assert "shared-a" in catalog["scope_occurrences"]
    assert "audit-only-1" in catalog["scope_occurrences"]


def test_two_digest_bridges_keep_distinct_parent_segments(tmp_path: Path) -> None:
    source = _archival_source(tmp_path)
    sha1 = "0123456789abcdef0123456789abcdef01234567"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute(
            "INSERT INTO memory_digest_sources(memory_id,run_id,session_id,message_ids,source_hash,created_at) VALUES (?,?,?,?,?,?)",
            ("mem-1", "r2", "digest-s1", '["ext-msg-2"]', sha1, "2026-09-01T00:05:01Z"),
        )
        conn.commit()
    source_bytes = source.read_bytes()
    catalog = build_legacy_catalog(source)
    home = tmp_path / "TEST-archive"
    _binding, _manifest, catalog = install_hermes_archive_migration(
        home,
        source_database=source,
        test_mode=True,
        expected_source_hash=catalog["source_sha256"],
        expected_catalog_hash=catalog["catalog_sha256"],
    )
    report = migrate_legacy(
        source,
        installation_manifest=home,
        batch_key="p15-fixed-batch-001",
        host="hermes",
    )
    assert report["completion_status"] == "complete"
    assert source.read_bytes() == source_bytes
    db = home / "scope-recall" / "memory.sqlite3"
    memories = {json.loads(row["extra_json"])["legacy_id"]: row for row in _legacy_rows(db, "memories")}
    parent = memories["mem-1"]
    parent_auth = json.loads(parent["extra_json"])["scope_authorization"]
    attached = [
        row
        for row in _legacy_rows(db, "memory_digest_sources")
        if json.loads(row["extra_json"]).get("legacy_parent_event_id") == parent["event_id"]
    ]
    assert len(attached) == 2
    assert {row["source_event_key"] for row in attached} == {
        "legacy:memory_digest_sources:5:mem-1-2:r1-9:digest-s1",
        "legacy:memory_digest_sources:5:mem-1-2:r2-9:digest-s1",
    }
    segments = {int(row["segment_index"]) for row in attached}
    assert len(segments) == 2
    assert all(segment > 0 for segment in segments)
    assert int(parent["segment_index"]) not in segments
    for row in attached:
        extra = json.loads(row["extra_json"])
        assert row["source_group_key"] == parent["source_group_key"]
        assert row["source_revision"] == parent["source_revision"]
        assert row["scope_id"] == parent["scope_id"]
        assert row["project_id"] == parent["project_id"]
        assert row["branch_id"] == parent["branch_id"]
        assert extra["scope_authorization"] == parent_auth
        assert row["read_blocked"] == 1
        assert extra["read_blocked"] is True
        assert extra["message_ids_namespace"] == "hermes_external_session_messages"


def test_install_cli_reuse_default_report_and_runtime_identity(tmp_path: Path) -> None:
    source, source_bytes, catalog, home, binding, manifest = _install_archive(tmp_path)
    loaded = load_installation_manifest(home)
    assert loaded.installation_id == manifest.installation_id == binding.installation_id
    assert set(loaded.archive_source_map) == _source_map_keys(catalog)
    identity = bind_hermes_identity(
        "p15-runtime",
        hermes_home=str(home),
        platform="cli",
        user_id="local",
        chat_type="cli",
        chat_id="local",
        thread_id="main",
        agent_identity=manifest.agent_id,
        agent_workspace="default",
    )
    assert not any(is_archive_scope(scope) for scope in identity.runtime_audience.allowed_scope_ids)
    assert not any(is_archive_scope(scope) for scope in identity.writable_scope_ids)
    assert not any(is_archive_scope(scope) for scope in loaded.audience_scopes.values())

    argv = _archive_cli(source, home, catalog)
    assert main(argv) == 0
    default_report = home / "scope-recall" / "p15-archive-migration-report.json"
    default_receipt = default_report.with_suffix(".receipt")
    assert default_report.is_file()
    assert default_receipt.is_file()
    assert source.read_bytes() == source_bytes
    first_state = _file_digests(home)
    assert main(argv) == 0
    assert _file_digests(home) == first_state
    assert source.read_bytes() == source_bytes

    db = home / "scope-recall" / "memory.sqlite3"
    memories = _legacy_rows(db, "memories")
    bridges = _legacy_rows(db, "memory_digest_sources")
    quarantines = _legacy_rows(db, "nightly_digest_quarantine")
    runs = _legacy_rows(db, "nightly_digest_runs")
    memory_ids = {json.loads(row["extra_json"])["legacy_id"] for row in memories}
    assert memory_ids == {"mem-1", "mem-2"}
    assert "mem-missing" not in memory_ids
    attached = []
    orphans = []
    for bridge in bridges:
        extra = json.loads(bridge["extra_json"])
        assert extra["message_ids_namespace"] == "hermes_external_session_messages"
        assert extra["source_hash_meaning"] == "sha1_of_candidate_content"
        assert extra["read_blocked"] is True
        assert bridge["read_blocked"] == 1
        if extra.get("attachment_resolution") == "attached":
            parent = next(row for row in memories if row["event_id"] == extra["legacy_parent_event_id"])
            parent_extra = json.loads(parent["extra_json"])
            assert bridge["source_group_key"] == parent["source_group_key"]
            assert bridge["scope_id"] == parent["scope_id"]
            assert bridge["project_id"] == parent["project_id"]
            assert bridge["branch_id"] == parent["branch_id"]
            assert extra["scope_authorization"] == parent_extra["scope_authorization"]
            attached.append(bridge)
        else:
            assert extra.get("attachment_resolution") == "absent_memory"
            orphans.append(bridge)
    assert len(attached) == 2
    assert len(orphans) == 1
    assert len(quarantines) == 1
    assert len(runs) == 1
    for row in (*quarantines, *runs):
        extra = json.loads(row["extra_json"])
        assert row["read_blocked"] == 1
        assert extra["read_blocked"] is True
        assert row["suppressed"] == 0


def test_cli_reuse_refuses_held_open_nonempty_wal(tmp_path: Path) -> None:
    source = _archival_source(tmp_path)
    catalog = build_legacy_catalog(source)
    home = tmp_path / "TEST-archive"
    report = tmp_path / "migration-report.json"
    argv = _archive_cli(source, home, catalog, report=report)
    assert main(argv) == 0

    db = home / "scope-recall" / "memory.sqlite3"
    wal = Path(str(db) + "-wal")
    receipt = report.with_suffix(".receipt")
    held = sqlite3.connect(db)
    try:
        held.execute("PRAGMA journal_mode=WAL")
        held.execute("PRAGMA wal_autocheckpoint=0")
        held.commit()
        main_digest = hashlib.sha256(db.read_bytes()).hexdigest()
        held.execute("CREATE TABLE p15_wal_probe(id INTEGER)")
        held.execute("INSERT INTO p15_wal_probe(id) VALUES (1)")
        held.commit()
        assert hashlib.sha256(db.read_bytes()).hexdigest() == main_digest
        assert wal.is_file() and wal.stat().st_size > 0
        before_home = _file_digests(home)
        before_report = report.read_bytes()
        before_receipt = receipt.read_bytes()
        with pytest.raises(MigrationError, match="nonempty WAL"):
            main(argv)
        assert _file_digests(home) == before_home
        assert report.read_bytes() == before_report
        assert receipt.read_bytes() == before_receipt
        assert hashlib.sha256(db.read_bytes()).hexdigest() == main_digest
    finally:
        held.close()


def test_forget_and_suppress_close_one_attached_bridge(tmp_path: Path) -> None:
    source, _source_bytes, catalog, home, binding, _manifest = _install_archive(tmp_path)
    report = migrate_legacy(
        source,
        installation_manifest=home,
        batch_key="p15-fixed-batch-001",
        host="hermes",
    )
    assert report["completion_status"] == "complete"
    db = home / "scope-recall" / "memory.sqlite3"
    memories = {json.loads(row["extra_json"])["legacy_id"]: row for row in _legacy_rows(db, "memories")}
    bridges = _legacy_rows(db, "memory_digest_sources")
    mem1 = memories["mem-1"]
    mem2 = memories["mem-2"]
    attached = {
        json.loads(row["extra_json"])["legacy_parent_event_id"]: row
        for row in bridges
        if json.loads(row["extra_json"]).get("attachment_resolution") == "attached"
    }
    bridge1 = attached[mem1["event_id"]]
    bridge2 = attached[mem2["event_id"]]
    assert json.loads(bridge1["extra_json"])["read_blocked"] is True
    assert bridge1["suppressed"] == 0

    core = MemoryCore(CoreConfig(binding))
    core.initialize()
    ctx = TrustedContext(binding, "p15-maintenance-forget", binding.scope_ids, "human_direct")

    def _authorize(text: str, key: str) -> None:
        event = {
            "protocol_version": "1.1",
            "source_event_key": key,
            "source_revision": 1,
            "origin": "human_direct",
            "role": "user",
            "content": text,
            "occurred_at": "2026-09-06T12:00:00Z",
            "recorded_at": "2026-09-06T12:00:00Z",
            "time_precision": "instant",
            "capture_state": "complete",
            "evidence_refs": [],
        }
        saved = core.record_event(ctx, event, scope_id=mem1["scope_id"], remaining_seconds=10)
        assert saved.durability == "persisted"

    def _forget(mode: str) -> dict:
        request = {
            "protocol_version": "1.1",
            "target_refs": [mem1["event_id"]],
            "mode": mode,
            "expected_revisions": {mem1["event_id"]: mem1["source_revision"]},
        }
        return core.forget(ctx, request, remaining_seconds=10)

    _authorize(f"不要主动提 {mem1['event_id']}", "p15-archive-forget-auth/suppress")
    suppressed = _forget("suppress")
    assert suppressed["suppressed"]

    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        after_bridge1 = conn.execute(
            "SELECT suppressed, read_blocked FROM source_events WHERE event_id=?",
            (bridge1["event_id"],),
        ).fetchone()
        after_mem2 = conn.execute(
            "SELECT suppressed FROM source_events WHERE event_id=?",
            (mem2["event_id"],),
        ).fetchone()
        after_bridge2 = conn.execute(
            "SELECT suppressed FROM source_events WHERE event_id=?",
            (bridge2["event_id"],),
        ).fetchone()
        assert after_bridge1["suppressed"] == 1
        assert after_mem2["suppressed"] == 0
        assert after_bridge2["suppressed"] == 0

    _authorize(f"删除 {mem1['event_id']}", "p15-archive-forget-auth/delete")
    deleted = _forget("delete")
    assert deleted["read_blocked"] and deleted["suppressed"]

    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        members = {
            row[0]
            for row in conn.execute("SELECT object_ref FROM deletion_members WHERE operation_id=?", (deleted["operation_id"],))
        }
        assert mem1["event_id"] in members
        assert bridge1["event_id"] in members
        assert mem2["event_id"] not in members
        assert bridge2["event_id"] not in members
        for table in ("nightly_digest_quarantine", "nightly_digest_runs"):
            row = conn.execute(
                "SELECT event_id, read_blocked, suppressed FROM source_events WHERE json_extract(extra_json,'$.legacy_table')=?",
                (table,),
            ).fetchone()
            assert row["read_blocked"] == 1
            assert row["event_id"] not in members
        assert conn.execute(
            "SELECT suppressed FROM source_events WHERE event_id=?",
            (bridge2["event_id"],),
        ).fetchone()[0] == 0


def test_ordinary_digest_retention_block_matches_table_units(tmp_path: Path) -> None:
    source = _archival_source(tmp_path)
    target = tmp_path / "ordinary-target"
    report = migrate_legacy(source, target)
    assert report["completion_status"] == "blocked"
    assert "digest_table_requires_distinct_registered_archive_retention_scopes" in report["cutover_block"]["reasons"]
    assert report["counts"]["unmapped"] == len(report["unmapped"])
    assert {item["table"] for item in report["unmapped"]} == {
        "memory_digest_sources",
        "nightly_digest_quarantine",
        "nightly_digest_runs",
    }
    assert not (target / "memory.sqlite3").exists()


@pytest.mark.parametrize(
    "kind",
    (
        "malformed_scope",
        "empty_selection",
        "subset_selection",
        "duplicate_selection",
        "invalid_selection_type",
        "duplicate_allowed_scopes",
        "non_mapping_retention",
        "explicit_null_archive_hash",
    ),
)
def test_prewrite_refusals_leave_targets_untouched(tmp_path: Path, kind: str) -> None:
    if kind == "duplicate_allowed_scopes":
        with pytest.raises(HermesIdentityError, match="duplicate"):
            build_installation_manifest(
                tmp_path / "TEST-archive-dup",
                agent_id="p15-dup",
                test_mode=True,
                audiences=[
                    {
                        "platform": "cli",
                        "user_id": "local",
                        "gateway_session_key": "",
                        "chat_type": "cli",
                        "chat_id": "c1",
                        "thread_id": "main",
                        "agent_workspace": "w1",
                        "allowed_scope_ids": ["scope-x", "scope-x"],
                        "writable_scope_ids": ["scope-x"],
                        "capture_scope_id": "scope-x",
                    }
                ],
            )
        return

    if kind == "explicit_null_archive_hash":
        home = tmp_path / "TEST-archive-null"
        manifest = build_installation_manifest(home, agent_id="p15-null", test_mode=True)
        write_installation_manifest(manifest)
        load_installation_manifest(home)
        path = home / "scope-recall" / "installation.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["archive_snapshot_hash"] = None
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with pytest.raises(HermesIdentityError, match="explicit null"):
            load_installation_manifest(home)
        return

    source = _archival_source(tmp_path)
    catalog = build_legacy_catalog(source)
    if kind == "malformed_scope":
        with closing(sqlite3.connect(source)) as conn:
            conn.execute(
                "UPDATE journal_entries SET scope_id=? WHERE id=1",
                (sqlite3.Binary(b"\x01\x02"),),
            )
            conn.commit()
            assert conn.execute("SELECT typeof(scope_id) FROM journal_entries WHERE id=1").fetchone()[0] == "blob"
        target = tmp_path / "ordinary-malformed"
        report = migrate_legacy(source, target)
        assert report["completion_status"] == "blocked"
        assert "malformed_non_string_identity" in report["cutover_block"]["reasons"]
        assert report["counts"]["unmapped"] == len(report["unmapped"])
        assert not (target / "memory.sqlite3").exists()
        return

    if kind == "non_mapping_retention":
        with pytest.raises(HermesIdentityError, match="mapping"):
            build_installation_manifest(
                tmp_path / "TEST-archive-retention",
                agent_id="p15-retention",
                test_mode=True,
                archive_retention_scopes=["not-a-mapping"],
                archive_snapshot_hash=catalog["source_sha256"],
                archive_catalog_hash=catalog["catalog_sha256"],
            )
        return

    home = tmp_path / "TEST-archive-select"
    source_bytes = source.read_bytes()
    install_hermes_archive_migration(
        home,
        source_database=source,
        test_mode=True,
        expected_source_hash=catalog["source_sha256"],
        expected_catalog_hash=catalog["catalog_sha256"],
    )
    before = _file_digests(home)
    complete = list(_source_map_keys(catalog))
    if kind == "empty_selection":
        with pytest.raises(MigrationError, match="ENTIRE real catalog set"):
            migrate_legacy(source, installation_manifest=home, scope_ids=[], host="hermes")
    elif kind == "subset_selection":
        with pytest.raises(MigrationError, match="ENTIRE real catalog set"):
            migrate_legacy(source, installation_manifest=home, scope_ids=["scope-a"], host="hermes")
    elif kind == "duplicate_selection":
        with pytest.raises(MigrationError, match="duplicates"):
            migrate_legacy(source, installation_manifest=home, scope_ids=[*complete, complete[0]], host="hermes")
    else:
        with pytest.raises(MigrationError, match="must be strings"):
            migrate_legacy(source, installation_manifest=home, scope_ids=[1], host="hermes")
    assert _file_digests(home) == before
    assert source.read_bytes() == source_bytes


@pytest.mark.parametrize(
    "kind",
    (
        "report_only",
        "changed_batch",
        "changed_report",
        "changed_manifest",
        "changed_database",
        "changed_source",
    ),
)
def test_replay_refusals_do_not_mutate_evidence(tmp_path: Path, kind: str) -> None:
    source = _archival_source(tmp_path)
    catalog = build_legacy_catalog(source)
    home = tmp_path / "TEST-archive"
    report = tmp_path / "migration-report.json"
    if kind == "report_only":
        report.write_text("{}\n", encoding="utf-8")
        planted = report.read_bytes()
        with pytest.raises(MigrationError, match="without matching receipt"):
            main(_archive_cli(source, home, catalog, report=report))
        assert report.read_bytes() == planted
        assert not (home / "scope-recall" / "memory.sqlite3").exists()
        return

    assert main(_archive_cli(source, home, catalog, report=report)) == 0
    if kind == "changed_batch":
        before = _file_digests(home)
        with pytest.raises(MigrationError, match="identical run"):
            main(_archive_cli(source, home, catalog, report=report, batch="p15-other-batch"))
        assert _file_digests(home) == before
        return

    if kind == "changed_report":
        report.write_bytes(report.read_bytes() + b" ")
        before = _file_digests(home)
        with pytest.raises(MigrationError, match="identical run"):
            main(_archive_cli(source, home, catalog, report=report))
        assert _file_digests(home) == before
        return

    if kind == "changed_manifest":
        manifest_path = home / "scope-recall" / "installation.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        before = _file_digests(home)
        with pytest.raises(MigrationError, match="identical run"):
            main(_archive_cli(source, home, catalog, report=report))
        assert _file_digests(home) == before
        return

    if kind == "changed_database":
        db = home / "scope-recall" / "memory.sqlite3"
        db.write_bytes(db.read_bytes() + b"\x00")
        before = _file_digests(home)
        with pytest.raises(MigrationError, match="identical run"):
            main(_archive_cli(source, home, catalog, report=report))
        assert _file_digests(home) == before
        return

    with closing(sqlite3.connect(source)) as conn:
        conn.execute("UPDATE memories SET content=content||'changed' WHERE id='mem-1'")
        conn.commit()
    before = _file_digests(home)
    with pytest.raises(MigrationError, match="does not match the current files"):
        main(_archive_cli(source, home, catalog, report=report))
    assert _file_digests(home) == before


def test_long_archive_scopes_fit_binding_without_runtime_grants(tmp_path: Path) -> None:
    source = _archival_source(tmp_path)
    long_ascii = "A" * 120
    long_multibyte = "记" * 80
    lead = " pad-scope"
    trail = "pad-scope "
    exact = "pad-scope"
    long_lead = " " + "B" * 120
    long_trail = "B" * 120 + " "
    synthetic = {
        "g-long-ascii": long_ascii,
        "g-long-multi": long_multibyte,
        "g-lead": lead,
        "g-trail": trail,
        "g-exact": exact,
        "g-long-lead": long_lead,
        "g-long-trail": long_trail,
    }
    with closing(sqlite3.connect(source)) as conn:
        for event_id, scope in synthetic.items():
            conn.execute(
                "INSERT INTO governance_audit_events(id,event_type,action,scope_id,created_at) VALUES (?,?,?,?,?)",
                (event_id, "p15-synthetic-audit", "p15-long-scope", scope, "2026-09-01T00:07:00Z"),
            )
        conn.commit()
    source_bytes = source.read_bytes()
    catalog = build_legacy_catalog(source)
    home = tmp_path / "TEST-archive-long"
    binding, manifest, catalog = install_hermes_archive_migration(
        home,
        source_database=source,
        test_mode=True,
        expected_source_hash=catalog["source_sha256"],
        expected_catalog_hash=catalog["catalog_sha256"],
    )
    assert source.read_bytes() == source_bytes
    loaded = load_installation_manifest(home)
    rebound = loaded.to_binding()
    assert rebound.scope_ids == binding.scope_ids == manifest.scope_ids
    assert all(len(scope) <= 240 for scope in rebound.scope_ids)
    catalog_keys = _source_map_keys(catalog)
    for original in synthetic.values():
        assert original in catalog_keys
        assert original in loaded.archive_source_map
    assert set(loaded.archive_source_map) == catalog_keys
    src_map = loaded.archive_source_map
    assert src_map[lead] != src_map[trail] != src_map[exact]
    assert src_map[lead] != src_map[exact]
    assert src_map[long_lead] != src_map[long_trail]
    assert src_map[long_ascii] != src_map[long_multibyte]
    assert len(set(src_map.values())) == len(src_map)
    assert all(len(value) <= 240 and is_archive_scope(value) for value in src_map.values())
    assert src_map[exact] == build_archive_scope_id(exact)
    assert src_map[exact].startswith("archive|source:")
    assert src_map[long_ascii] == build_archive_scope_id(long_ascii)
    assert src_map[long_ascii].startswith("archive|sha256:")
    assert src_map[long_multibyte].startswith("archive|sha256:")
    assert src_map["scope-a"].startswith("archive|source:")
    identity = bind_hermes_identity(
        "p15-runtime",
        hermes_home=str(home),
        platform="cli",
        user_id="local",
        chat_type="cli",
        chat_id="local",
        thread_id="main",
        agent_identity=manifest.agent_id,
        agent_workspace="default",
    )
    assert not any(is_archive_scope(scope) for scope in identity.runtime_audience.allowed_scope_ids)
    assert not any(is_archive_scope(scope) for scope in identity.writable_scope_ids)
    assert not any(is_archive_scope(scope) for scope in loaded.audience_scopes.values())
    for original in synthetic.values():
        assert original not in identity.runtime_audience.allowed_scope_ids
        assert original not in identity.writable_scope_ids
        assert src_map[original] not in identity.runtime_audience.allowed_scope_ids
        assert src_map[original] not in identity.writable_scope_ids
