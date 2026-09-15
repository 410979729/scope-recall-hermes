from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from maintenance.migrate_v2 import _procedure_claim_id, migrate_legacy
from scope_recall.core.deletion import forget, purge_sqlite
from scope_recall.core.mutate import current_claim, revise

from test_v6_fact_history_slots import _history_fixture
from test_v7_migration_acceptance import _capture, _runtime


def test_chinese_shared_source_forget_purge_and_rerun_does_not_resurrect(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path / "chinese")
    target = tmp_path / "target"
    report = migrate_legacy(legacy, target)
    assert report["completion_status"] == "complete"
    storage, context, clock = _runtime(target)
    with storage.read(context) as tx:
        fact_ref = next(ref for ref in tx.claims.list_refs() if tx.claims.versions(ref)[-1].payload["kind"] == "fact")
        procedure_ref = next(ref for ref in tx.claims.list_refs() if tx.claims.versions(ref)[-1].payload["kind"] == "procedure")
        assert procedure_ref.startswith("claim-")
        assert tx.deletions.target(procedure_ref).ref == procedure_ref
    proof = _capture(storage, context, clock, "v8-delete", f"删除 {fact_ref}")
    current = current_claim(storage, clock, context, fact_ref)
    deletion = forget(storage, clock, context, {"protocol_version": "1.1", "target_refs": [fact_ref], "mode": "delete", "expected_revisions": {fact_ref: current.revision}, "reason": "delete"}, remaining_seconds=10)
    purge_sqlite(storage, context, deletion["operation_id"], remaining_seconds=10)
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        version_count = conn.execute("SELECT count(*) FROM claim_versions").fetchone()[0]
        assert conn.execute("SELECT payload_json FROM claim_versions WHERE claim_id=?", (fact_ref,)).fetchall()
        assert all(row[0] == "{}" for row in conn.execute("SELECT payload_json FROM claim_versions WHERE claim_id IN (?,?)", (fact_ref, procedure_ref)))
        assert conn.execute("SELECT content FROM source_events WHERE event_id=?", (proof.event_refs[0].ref,)).fetchone()[0] == ""
    rerun = migrate_legacy(legacy, target)
    assert rerun["counts"]["claim_versions"] == version_count
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        assert all(row[0] == "{}" for row in conn.execute("SELECT payload_json FROM claim_versions WHERE claim_id IN (?,?)", (fact_ref, procedure_ref)))
        assert conn.execute("SELECT content FROM source_events WHERE event_id=?", (proof.event_refs[0].ref,)).fetchone()[0] == ""


@pytest.mark.parametrize("historical_id", ["fact-1", "z-history-1"])
def test_equal_recorded_at_current_is_last_and_core_can_read_release_and_revise(tmp_path: Path, historical_id: str) -> None:
    legacy = _history_fixture(tmp_path / historical_id.replace("-", "_"))
    with sqlite3.connect(legacy) as conn:
        conn.execute("UPDATE fact_claims SET recorded_at='2026-09-02T00:00:00Z',retired_at='2026-09-03T00:00:00Z' WHERE claim_id='fact-1'")
        if historical_id != "fact-1":
            conn.execute("UPDATE fact_claims SET claim_id=? WHERE claim_id='fact-1'", (historical_id,))
            conn.execute("UPDATE fact_claim_evidence SET claim_id=? WHERE claim_id='fact-1'", (historical_id,))
    target = tmp_path / f"target-{historical_id.replace('-', '_')}"
    report = migrate_legacy(legacy, target)
    assert report["completion_status"] == "complete"
    storage, context, clock = _runtime(target)
    with storage.read(context) as tx:
        fact_ref = next(ref for ref in tx.claims.list_refs() if tx.claims.versions(ref)[-1].payload["kind"] == "fact")
    current = current_claim(storage, clock, context, fact_ref)
    assert current.payload["value_text"] == "现已改为不保留"
    with storage.read(context) as tx:
        epoch = tx.status().memory_epoch
    released = __import__("scope_recall.core.visibility", fromlist=["release_objects"]).release_objects(storage, clock, context, (__import__("scope_recall.core.visibility", fromlist=["ObjectRef"]).ObjectRef("claim", fact_ref, current.revision),), expected_epoch=epoch)
    assert released[0].payload["value_text"] == current.payload["value_text"]
    correction = _capture(storage, context, clock, "v8-correction", "用户 现已改为不保留，改为 更新后的值。")
    current = current_claim(storage, clock, context, fact_ref)
    revised = revise(storage, clock, context, {"protocol_version": "1.1", "target_ref": fact_ref, "expected_revision": current.revision, "new_value": "更新后的值", "conditions": [], "source_evidence_refs": [f"{correction.event_refs[0].ref}@1"], "valid_from": None}, remaining_seconds=10)
    assert revised.items[0].disposition in {"revised", "duplicate"}
    assert current_claim(storage, clock, context, fact_ref).payload["value_text"] == "更新后的值"
