from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from maintenance.legacy_fixture import build_official_578b_fixture
from maintenance.migrate_v2 import _procedure_claim_id, _stable, migrate_legacy
from scope_recall.core.claims import claim_slot


def _insert_row(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    columns = ",".join(row)
    placeholders = ",".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table}({columns}) VALUES ({placeholders})", tuple(row.values()))


def _history_fixture(tmp_path: Path) -> Path:
    legacy = build_official_578b_fixture(tmp_path / "legacy.sqlite3", repo_root=Path.cwd())
    with sqlite3.connect(legacy) as conn:
        conn.row_factory = sqlite3.Row
        journal = dict(conn.execute("SELECT * FROM journal_entries WHERE id=1").fetchone())
        journal.update(id=4, turn_number=3, content="用户现已改为不保留。", created_at="2026-09-02T00:00:00Z")
        journal["content_hash"] = hashlib.sha256(str(journal["content"]).encode()).hexdigest()
        _insert_row(conn, "journal_entries", journal)
        fact = dict(conn.execute("SELECT * FROM fact_claims WHERE claim_id='fact-1'").fetchone())
        fact.update(claim_id="fact-2", value="现已改为不保留", normalized_value="现已改为不保留", value_fingerprint="fp-2", source_ref="4", recorded_at="2026-09-02T00:00:00Z", status="current")
        conn.execute("UPDATE fact_claims SET status='superseded', superseded_by_claim_id='fact-2', retired_at='2026-09-02T00:00:00Z' WHERE claim_id='fact-1'")
        _insert_row(conn, "fact_claims", fact)
        evidence = dict(conn.execute("SELECT * FROM fact_claim_evidence WHERE evidence_id='ev-1'").fetchone())
        evidence.update(evidence_id="ev-2", claim_id="fact-2", source_ref="4", evidence_hash="eh-2", excerpt=journal["content"], recorded_at="2026-09-02T00:00:00Z")
        _insert_row(conn, "fact_claim_evidence", evidence)
    return legacy


def test_fact_history_preserves_head_natural_slot_and_delete_rerun(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path)
    target = tmp_path / "target"
    first = migrate_legacy(legacy, target)
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        fact_claim = conn.execute("SELECT claim_id,current_revision,slot_key FROM claims WHERE kind='fact'").fetchone()
        versions = conn.execute("SELECT revision,state,payload_json FROM claim_versions WHERE claim_id=? ORDER BY revision", (fact_claim[0],)).fetchall()
        assert first["completion_status"] == "complete"
        assert first["counts"]["fact_claims_mapped"] == 2
        assert fact_claim[1] == 2 and [row[0] for row in versions] == [1, 2]
        assert [row[1] for row in versions] == ["superseded", "active"]
        payloads = [json.loads(row[2]) for row in versions]
        assert [payload["value_text"] for payload in payloads] == ["已确认保留", "现已改为不保留"]
        assert all(payload["conditions"] == [] for payload in payloads)
        assert all("_legacy" not in payload for payload in payloads)
        archive_ref = conn.execute("SELECT event_id FROM source_events WHERE json_extract(extra_json,'$.legacy_table')='fact_claims' AND json_extract(extra_json,'$.legacy_id')='fact-1'").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='claim' AND object_ref=? AND source_ref=? AND relation='derived_from'", (fact_claim[0], archive_ref)).fetchone()[0] == 1
        assert fact_claim[2] == claim_slot("scope-a", None, None, payloads[1])
        procedure = conn.execute("SELECT payload_json FROM claim_versions WHERE claim_id=?", (_procedure_claim_id("pb-1"),)).fetchone()
        procedure_payload = json.loads(procedure[0])
        assert procedure_payload["conditions"] == procedure_payload["procedure"]["conditions"]
        assert all(not condition.startswith("legacy_") for condition in procedure_payload["conditions"])

        # Model the durable SQLite scrub performed by Core deletion.  The
        # source remains immutable, while the target payload and supporting
        # quote are irreversibly cleared.
        conn.execute("UPDATE claims SET subject='',predicate='' WHERE claim_id=?", (fact_claim[0],))
        conn.execute("UPDATE claim_versions SET payload_json='{}' WHERE claim_id=?", (fact_claim[0],))
        conn.execute("UPDATE source_events SET content='',source_event_key='removed-'||event_id WHERE event_id IN (SELECT source_ref FROM evidence_links WHERE object_kind='claim' AND object_ref=?)", (fact_claim[0],))
    second = migrate_legacy(legacy, target)
    assert second["counts"] == first["counts"]
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM claim_versions WHERE claim_id=?", (fact_claim[0],)).fetchone()[0] == 2
        assert conn.execute("SELECT payload_json FROM claim_versions WHERE claim_id=? ORDER BY revision", (fact_claim[0],)).fetchall() == [("{}",), ("{}",)]


def test_different_legacy_fact_keys_block_without_duplicate_authority(tmp_path: Path) -> None:
    legacy = build_official_578b_fixture(tmp_path / "conflict.sqlite3", repo_root=Path.cwd())
    with sqlite3.connect(legacy) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE fact_claims SET fact_key='retention-a' WHERE claim_id='fact-1'")
        fact = dict(conn.execute("SELECT * FROM fact_claims WHERE claim_id='fact-1'").fetchone())
        fact.update(claim_id="fact-2", fact_key="retention-b", value="另一个值", normalized_value="另一个值", value_fingerprint="fp-2", recorded_at="2026-09-02T00:00:00Z", status="current", superseded_by_claim_id=None)
        _insert_row(conn, "fact_claims", fact)
    report = migrate_legacy(legacy, tmp_path / "target")
    assert report["completion_status"] == "blocked"
    assert "fact_slot_conflict_different_legacy_fact_key" in report["cutover_block"]["reasons"]
    assert report["counts"]["fact_claims_mapped"] == 0
    with sqlite3.connect(tmp_path / "target" / "memory.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM claims WHERE kind='fact'").fetchone()[0] == 0


def test_procedure_slot_change_is_archived_and_blocked(tmp_path: Path) -> None:
    legacy = build_official_578b_fixture(tmp_path / "procedure.sqlite3", repo_root=Path.cwd())
    with sqlite3.connect(legacy) as conn:
        snapshot = json.loads(conn.execute("SELECT snapshot FROM playbook_versions WHERE id='pbv-1'").fetchone()[0])
        snapshot["title"] = "迁移后方法"
        snapshot["preconditions"] = [{"check": "需要二次确认"}]
        conn.execute("INSERT INTO playbook_versions(id,playbook_id,version,change_type,snapshot,created_at) VALUES (?,?,?,?,?,?)", ("pbv-2", "pb-1", 2, "update", json.dumps(snapshot, ensure_ascii=False), "2026-09-02T00:00:00Z"))
    report = migrate_legacy(legacy, tmp_path / "target")
    assert report["completion_status"] == "blocked"
    assert "procedure_version_slot_changed_blocks_cutover" in report["cutover_block"]["reasons"]
    with sqlite3.connect(tmp_path / "target" / "memory.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM claims WHERE claim_id=?", (_procedure_claim_id("pb-1"),)).fetchone()[0] == 0
