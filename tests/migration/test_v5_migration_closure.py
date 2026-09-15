"""Implementation regressions for the three remaining v4 migration gaps."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from maintenance.legacy_fixture import build_official_578b_fixture
from maintenance.migrate_v2 import _procedure_claim_id, _stable, migrate_legacy
from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core.storage import SQLiteStorage


def _store(target: Path, *, project: str | None = None, branch: str | None = None):
    binding = InstanceBinding("p15-synthetic-agent", "p15-synthetic-installation", target, frozenset({"scope-a"}), True)
    return SQLiteStorage(binding), TrustedContext(binding, "v5-test", binding.scope_ids, "host_generated", project_id=project, branch_id=branch)


def _insert_copy(conn: sqlite3.Connection, table: str, row: dict) -> None:
    conn.execute(f"INSERT INTO {table}({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))


def test_unknown_acl_blocks_before_any_target_content_and_preserves_source(tmp_path: Path) -> None:
    legacy = build_official_578b_fixture(tmp_path / "acl.sqlite3", repo_root=Path.cwd())
    with sqlite3.connect(legacy) as conn:
        conn.execute("ALTER TABLE journal_entries ADD COLUMN owner_only_id TEXT")
        conn.execute("UPDATE journal_entries SET owner_only_id='owner-a' WHERE id=1")
    original_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()
    target = tmp_path / "target"
    first = migrate_legacy(legacy, target, report_path=tmp_path / "first.json")
    second = migrate_legacy(legacy, target, report_path=tmp_path / "second.json")
    assert first == second
    assert first["completion_status"] == "blocked"
    assert first["cutover_block"]["reasons"] == ["unknown_legacy_columns_blocks_cutover"]
    assert first["unmapped"][0]["columns"] == ["owner_only_id"]
    assert first["target_content_written"] is False
    assert not (target / "memory.sqlite3").exists()
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == original_hash


def test_version_archive_inherits_parent_project_and_branch_at_final_write(tmp_path: Path) -> None:
    legacy = build_official_578b_fixture(tmp_path / "private.sqlite3", repo_root=Path.cwd())
    with sqlite3.connect(legacy) as conn:
        conn.executescript("ALTER TABLE procedural_playbooks ADD COLUMN project_id TEXT; ALTER TABLE procedural_playbooks ADD COLUMN branch_id TEXT;")
        conn.execute("UPDATE procedural_playbooks SET project_id='private-project',branch_id='private-branch'")
    target = tmp_path / "target"
    first = migrate_legacy(legacy, target)
    second = migrate_legacy(legacy, target)
    assert first["completion_status"] == second["completion_status"] == "complete"
    assert first["counts"] == second["counts"]
    ref = _stable("event", "playbook_versions:pbv-1")
    storage, no_project = _store(target)
    with storage.read(no_project) as tx:
        assert tx.source(ref, 1) is None
    _, wrong_branch = _store(target, project="private-project", branch="other-branch")
    with storage.read(wrong_branch) as tx:
        assert tx.source(ref, 1) is None
    _, owner = _store(target, project="private-project", branch="private-branch")
    with storage.read(owner) as tx:
        source = tx.source(ref, 1)
        assert source is not None and source.event["content"]
        assert source.project_id == "private-project" and source.branch_id == "private-branch"


def test_deleted_historical_procedure_source_scrubs_every_archive_and_rerun(tmp_path: Path) -> None:
    legacy = build_official_578b_fixture(tmp_path / "delete.sqlite3", repo_root=Path.cwd())
    with sqlite3.connect(legacy) as conn:
        conn.row_factory = sqlite3.Row
        safe_quote = conn.execute("SELECT content FROM journal_entries WHERE id=2").fetchone()[0]
        safe_anchors = [{"source_ref": "2", "excerpt": safe_quote}]
        # The current parent and latest version have live evidence. Only the
        # older snapshot cites the deleted source: deleting by latest only fails.
        conn.execute("UPDATE procedural_playbooks SET evidence_anchors=?,status='promoted'", (json.dumps(safe_anchors),))
        latest = json.loads(conn.execute("SELECT snapshot FROM playbook_versions WHERE id='pbv-1'").fetchone()[0])
        latest.update(status="promoted", evidence_anchors=safe_anchors)
        conn.execute("INSERT INTO playbook_versions(id,playbook_id,version,change_type,snapshot,created_at) VALUES ('pbv-2','pb-1',2,'update',?,'2026-09-02T00:00:00Z')", (json.dumps(latest),))
        safe = dict(conn.execute("SELECT * FROM procedural_playbooks WHERE id='pb-1'").fetchone())
        safe.update(id="pb-safe", title="Unrelated live procedure")
        _insert_copy(conn, "procedural_playbooks", safe)
        latest["title"] = "Unrelated live procedure"
        conn.execute("INSERT INTO playbook_versions(id,playbook_id,version,change_type,snapshot,created_at) VALUES ('safe-v1','pb-safe',1,'create',?,'2026-09-02T00:00:00Z')", (json.dumps(latest),))
        conn.execute("INSERT INTO privacy_purge_operations VALUES ('purge-v5','fp','sh',1,1,0,'completed','2026-09-03T00:00:00Z','2026-09-03T00:00:00Z','2026-09-03T00:00:00Z','2026-09-03T00:00:00Z')")
        conn.execute("INSERT INTO privacy_purge_source_tombstones VALUES ('purge-v5',1,'hash','2026-09-03T00:00:00Z')")
    original_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()
    target = tmp_path / "target"
    first = migrate_legacy(legacy, target)
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        before = conn.execute("SELECT claim_id,current_revision FROM claims ORDER BY claim_id").fetchall()
    second = migrate_legacy(legacy, target)
    assert first["completion_status"] == second["completion_status"] == "complete"
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        assert conn.execute("SELECT claim_id,current_revision FROM claims ORDER BY claim_id").fetchall() == before
        deleted_claim = _procedure_claim_id("pb-1")
        assert conn.execute("SELECT count(*) FROM claim_versions WHERE claim_id=? AND payload_json!='{}'", (deleted_claim,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_ref=? AND quote!=''", (deleted_claim,)).fetchone()[0] == 0
        for key in ("procedural_playbooks:pb-1", "playbook_versions:pbv-1", "playbook_versions:pbv-2"):
            assert conn.execute("SELECT content,read_blocked FROM source_events WHERE event_id=?", (_stable("event", key),)).fetchone() == ("", 1)
    storage, context = _store(target)
    with storage.read(context) as tx:
        for key in ("procedural_playbooks:pb-1", "playbook_versions:pbv-1", "playbook_versions:pbv-2"):
            assert tx.source(_stable("event", key), 1) is None
        assert tx.source(_stable("event", "playbook_versions:safe-v1"), 1) is not None
        safe_versions = tx.claims.versions(_procedure_claim_id("pb-safe"))
        assert safe_versions[-1].state == "active"
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == original_hash


def test_normal_private_active_fact_and_procedure_survive_rerun(tmp_path: Path) -> None:
    legacy = build_official_578b_fixture(tmp_path / "normal.sqlite3", repo_root=Path.cwd())
    with sqlite3.connect(legacy) as conn:
        conn.execute("UPDATE procedural_playbooks SET status='promoted'")
        snapshot = json.loads(conn.execute("SELECT snapshot FROM playbook_versions WHERE id='pbv-1'").fetchone()[0])
        snapshot["status"] = "promoted"
        conn.execute("UPDATE playbook_versions SET snapshot=? WHERE id='pbv-1'", (json.dumps(snapshot),))
    target = tmp_path / "target"
    first = migrate_legacy(legacy, target)
    second = migrate_legacy(legacy, target)
    assert first["completion_status"] == second["completion_status"] == "complete"
    assert first["counts"] == second["counts"]
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        assert conn.execute("SELECT kind,state,current_revision FROM claims JOIN claim_versions USING(claim_id) ORDER BY kind").fetchall() == [("fact", "active", 1), ("procedure", "active", 1)]
