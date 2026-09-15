"""Agent routing, durable migration and existing-queue indexing boundaries."""

from contextlib import closing
import json
import sqlite3
import shutil

import pytest

from scope_recall.adapters.hermes import install_hermes_scope_recall
from scope_recall.maintenance.onboarding import inspect_installation
from scope_recall.maintenance.upgrade import (
    MigrationError,
    prepare_upgrade,
    run_upgrade,
    verify_upgrade,
    queue_upgrade_index,
    upgrade_status,
)
from scope_recall.maintenance.upgrade_cli import main
from test_v6_fact_history_slots import _history_fixture


@pytest.fixture
def migration(tmp_path):
    source = _history_fixture(tmp_path / "old")
    target = tmp_path / "new"
    install_hermes_scope_recall(
        target,
        agent_id="TEST-agent-upgrade",
        test_mode=True,
        retained_scope_ids=["shared-a"],
    )
    job = tmp_path / "job"
    return source, target, job


def prepare(migration, **kwargs):
    source, target, job = migration
    return prepare_upgrade(
        source,
        job,
        installation_manifest=target,
        scope_map=kwargs.pop("scope_map", {"scope-a": "owner_private"}),
        **kwargs,
    )


def test_routing_is_read_only_and_does_not_migrate_new_users(
    tmp_path, migration, capsys
):
    fresh = tmp_path / "first-install"
    assert inspect_installation(fresh)["route"] == "fresh_install"
    assert not fresh.exists()
    assert main(["setup", "--home", str(fresh)]) == 0
    assert json.loads(capsys.readouterr().out)["operator"] == "agent"
    source, target, _ = migration
    assert inspect_installation(target)["route"] == "current_upgrade"
    home = tmp_path / "old-home"
    (home / "lancepro").mkdir(parents=True)
    shutil.copyfile(source, home / "lancepro" / "memory.sqlite3")
    assert inspect_installation(home)["route"] == "legacy_migration"
    (home / "scope-recall").mkdir()
    (home / "scope-recall" / "memory.sqlite3").write_bytes(b"not sqlite")
    assert inspect_installation(home)["route"] == "needs_agent_inspection"
    (home / "lancepro" / "memory.sqlite3").unlink()
    assert inspect_installation(home)["route"] == "repair_required"


def test_custom_database_and_missing_explicit_path_are_not_fresh(
    tmp_path, migration, capsys
):
    source, _, _ = migration
    route = inspect_installation(tmp_path / "custom-home", database=source)
    assert route["route"] == "legacy_migration"
    assert (
        main(
            [
                "setup",
                "--home",
                str(tmp_path / "custom-home"),
                "--database",
                str(tmp_path / "missing.sqlite3"),
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


def test_conversion_preserves_history_resumes_and_only_schedules_embeddings(migration):
    source, target, job = migration
    original = source.read_bytes()
    planned = prepare(migration)
    assert planned["state"] == "prepared", planned
    with pytest.raises(MigrationError, match="quiesce"):
        run_upgrade(job)
    result = run_upgrade(job, source_quiesced=True)
    assert result["state"] == "verified", result
    assert result["host_validation"] == "agent_required"
    assert result["history_reextraction"] is False
    assert source.read_bytes() == original
    assert run_upgrade(job, source_quiesced=True)["attempts"] == 1
    assert verify_upgrade(job)["integrity"] == "ok"
    for _ in range(100):
        queued = queue_upgrade_index(job, limit=2)
        if queued["index_queue_complete"]:
            break
    assert queued["index_queue_complete"]
    assert queue_upgrade_index(job) == queued
    with closing(sqlite3.connect(target / "scope-recall" / "memory.sqlite3")) as conn:
        work = conn.execute(
            "SELECT work_type,subject_ref,subject_revision FROM work_items"
        ).fetchall()
        assert work and {r[0] for r in work} == {"embed"}
        assert len(set(work)) == len(work)
        heads = conn.execute(
            "SELECT state FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id AND c.current_revision=v.revision WHERE c.kind='fact'"
        ).fetchall()
        assert heads == [("active",)]
        assert (
            conn.execute(
                "SELECT count(*) FROM claim_versions WHERE state='superseded'"
            ).fetchone()[0]
            >= 1
        )


def test_unknown_or_unmapped_scopes_do_not_write_target(migration):
    source, target, job = migration
    db = target / "scope-recall" / "memory.sqlite3"
    before = db.read_bytes()
    planned = prepare(migration, scope_map={})
    assert planned["state"] == "blocked"
    assert planned["missing_scope_ids"] == ["scope-a"]
    assert run_upgrade(job, source_quiesced=True)["state"] == "blocked"
    assert db.read_bytes() == before


def test_import_ledger_is_preserved_without_replaying_or_granting_access(migration):
    from scope_recall.maintenance.legacy_tianshu_compat import (
        LegacyCompatibilityError, verify_import_ledger_archive,
    )

    source, target, job = migration
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("""CREATE TABLE import_ledger (
            import_fingerprint TEXT PRIMARY KEY, source_kind TEXT NOT NULL,
            source_scope TEXT NOT NULL, source_path TEXT NOT NULL,
            memory_id TEXT NOT NULL, imported_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO import_ledger VALUES (?,?,?,?,?,?)", (
            "TEST-fingerprint", "openclaw", "TEST-unmapped-old-import-scope",
            "sha256:TEST-path", "TEST-deleted-memory", "2026-01-01T00:00:00Z",
        ))
        conn.commit()
    assert prepare(migration)["state"] == "prepared"
    result = run_upgrade(job, source_quiesced=True)
    assert result["state"] == "verified"
    archive = json.loads((job / "import-ledger.json").read_text())
    assert archive["rows"][0]["memory_id"] == "TEST-deleted-memory"
    assert archive["row_count"] == 1 and archive["replay"] is False
    report = json.loads((job / "conversion-1.json").read_text())
    assert report["import_ledger_audit"]["sha256"] == archive["sha256"]
    with closing(sqlite3.connect(target / "scope-recall" / "memory.sqlite3")) as conn:
        assert conn.execute("SELECT count(*) FROM source_events WHERE content LIKE '%TEST-fingerprint%'").fetchone()[0] == 0
    archive["rows"][0]["memory_id"] = "TEST-rewritten"
    with closing(sqlite3.connect(source)) as conn:
        with pytest.raises(LegacyCompatibilityError, match="not_lossless"):
            verify_import_ledger_archive(conn, archive)


def test_production_digest_audit_retention_has_no_runtime_grants(tmp_path):
    from scope_recall.adapters.hermes.installation import load_installation_manifest
    source = _history_fixture(tmp_path / "old")
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("""CREATE TABLE nightly_digest_runs (
            id TEXT, digest_date TEXT, source_db TEXT, started_at TEXT,
            extractor TEXT, status TEXT)""")
        conn.execute("INSERT INTO nightly_digest_runs VALUES (?,?,?,?,?,?)", (
            "TEST-digest", "2026-01-01", "TEST-source", "2026-01-01T00:00:00Z", "llm", "completed",
        ))
        conn.commit()
    target = tmp_path / "new"
    install_hermes_scope_recall(target, agent_id="TEST-upgrade", test_mode=False,
                               retained_scope_ids=["shared-a"], legacy_audit_retention=True)
    manifest = load_installation_manifest(target)
    assert manifest.test_mode is False and not manifest.archive_source_map
    assert manifest.archive_scopes == {"archive|reserved:orphan_bridge", "archive|reserved:digest_audit"}
    assert not any(set(row['allowed_scope_ids']) & manifest.archive_scopes for row in manifest.audiences)
    job = tmp_path / "job"
    assert prepare_upgrade(source, job, installation_manifest=target,
                           scope_map={"scope-a": "owner_private"})['state'] == 'prepared'
    assert run_upgrade(job, source_quiesced=True)['state'] == 'verified'
    with closing(sqlite3.connect(target / 'scope-recall/memory.sqlite3')) as conn:
        assert conn.execute("SELECT scope_id,read_blocked FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='nightly_digest_runs'").fetchall() == [('archive|reserved:digest_audit',1)]


@pytest.mark.parametrize("tamper", ["snapshot", "source", "manifest"])
def test_changed_inputs_prevent_cutover(migration, tamper):
    source, target, job = migration
    planned = prepare(migration)
    if tamper == "snapshot":
        with (job / "source.sqlite3").open("ab") as stream:
            stream.write(b"changed")
    elif tamper == "source":
        with closing(sqlite3.connect(source)) as conn:
            conn.execute(
                "UPDATE journal_entries SET content='new user message' WHERE id=1"
            )
            conn.commit()
    else:
        manifest = target / "scope-recall" / "installation.json"
        manifest.write_text(manifest.read_text() + "\n")
    with pytest.raises(MigrationError, match="changed"):
        run_upgrade(job, source_quiesced=True)
    assert planned["attempts"] == 0


def test_committed_wal_change_is_not_missed(migration):
    source, _, job = migration
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        planned = prepare(migration)
        assert planned["state"] == "prepared", planned["blockers"]
        writer.execute(
            "UPDATE journal_entries SET content='committed in WAL' WHERE id=1"
        )
        writer.commit()
        with pytest.raises(MigrationError, match="source changed"):
            run_upgrade(job, source_quiesced=True)


def test_quiesced_wal_source_migrates_from_standalone_snapshot(migration):
    source, _, job = migration
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE memories SET summary=summary || ' TEST-WAL'")
        writer.commit()
        planned = prepare(migration)
        assert planned["state"] == "prepared", planned["blockers"]
        assert not (job / "source.sqlite3-wal").exists()
        assert run_upgrade(job, source_quiesced=True)["state"] == "verified"


def test_interrupted_conversion_reuses_engine_and_has_bounded_retry(
    migration, monkeypatch
):
    from scope_recall.maintenance import upgrade

    _, _, job = migration
    prepare(migration)
    converter = upgrade.migrate_legacy

    def interrupted(*args, **kwargs):
        converter(*args, **kwargs)
        raise OSError("TEST-interruption after converter commit")

    monkeypatch.setattr(upgrade, "migrate_legacy", interrupted)
    with pytest.raises(OSError):
        run_upgrade(job, source_quiesced=True)
    assert upgrade_status(job)["state"] == "retryable"
    monkeypatch.setattr(upgrade, "migrate_legacy", converter)
    result = run_upgrade(job, source_quiesced=True)
    assert result["state"] == "verified" and result["attempts"] == 2


def test_status_does_not_turn_post_activation_writes_into_verified_migration(migration):
    from scope_recall.adapters.hermes.installation import load_installation_manifest
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.contracts import TrustedContext
    from tests.v11_support import source_event

    _, target, job = migration
    prepare(migration)
    run_upgrade(job, source_quiesced=True)
    manifest = load_installation_manifest(target)
    binding = manifest.to_binding()
    context = TrustedContext(
        binding, "TEST-after-upgrade", binding.scope_ids, "human_direct"
    )
    core = MemoryCore(CoreConfig(binding))
    core.record_event(
        context,
        source_event(content="TEST-new message after upgrade"),
        scope_id=manifest.audience_scopes["owner_private"],
    )
    with pytest.raises(MigrationError, match="count reconciliation"):
        verify_upgrade(job)
