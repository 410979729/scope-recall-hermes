from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from maintenance.migrate_v2 import MigrationError, migrate_legacy
from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core.capture import record_event
from scope_recall.core.deletion import forget, purge_sqlite
from scope_recall.core.mutate import accept_claim_proposals, current_claim, revise
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.visibility import ObjectRef, release_objects

from test_v6_fact_history_slots import _history_fixture


class _Clock:
    def __init__(self) -> None:
        self._seconds = 0

    def utc_now(self) -> str:
        self._seconds += 1
        return f"2026-09-06T00:00:{self._seconds:02d}Z"

    def monotonic(self) -> float:
        return 0.0


def _ascii_history_fixture(tmp_path: Path) -> Path:
    legacy = _history_fixture(tmp_path)
    with sqlite3.connect(legacy) as conn:
        for row_id, content in ((1, "project keep"), (4, "project final")):
            conn.execute("UPDATE journal_entries SET content=?,content_hash=? WHERE id=?", (content, hashlib.sha256(content.encode()).hexdigest(), row_id))
        conn.execute("UPDATE fact_claims SET subject_key='project',predicate_key='preference',value='keep',normalized_value='keep' WHERE claim_id='fact-1'")
        conn.execute("UPDATE fact_claims SET subject_key='project',predicate_key='preference',value='final',normalized_value='final' WHERE claim_id='fact-2'")
        conn.execute("UPDATE fact_claim_evidence SET excerpt='project keep' WHERE claim_id='fact-1'")
        conn.execute("UPDATE fact_claim_evidence SET excerpt='project final' WHERE claim_id='fact-2'")
    return legacy


def _runtime(target: Path) -> tuple[SQLiteStorage, TrustedContext, _Clock]:
    binding = InstanceBinding("p15-synthetic-agent", "p15-synthetic-installation", target, frozenset({"scope-a"}), True)
    storage = SQLiteStorage(binding)
    storage.initialize()
    return storage, TrustedContext(binding, "v7-human-session", binding.scope_ids, "human_direct"), _Clock()


def _capture(storage: SQLiteStorage, context: TrustedContext, clock: _Clock, key: str, content: str):
    event = dict(protocol_version="1.1", source_event_key=key, source_revision=1, origin="human_direct", role="user", content=content, occurred_at=clock.utc_now(), recorded_at=clock.utc_now(), time_precision="instant", capture_state="complete", evidence_refs=[])
    return record_event(storage, clock, context, event, scope_id="scope-a", remaining_seconds=10)


def test_ordered_migration_is_revisable_releasable_and_forget_rerun_safe(tmp_path: Path) -> None:
    legacy = _ascii_history_fixture(tmp_path)
    target = tmp_path / "target"
    report = migrate_legacy(legacy, target)
    assert report["completion_status"] == "complete"
    storage, context, clock = _runtime(target)
    with storage.read(context) as tx:
        fact_ref = next(ref for ref in tx.claims.list_refs() if tx.claims.versions(ref)[-1].payload["kind"] == "fact")
        head = tx.claims.versions(fact_ref)[-1]
        assert "_legacy" not in head.payload
        assert all(set(json.loads(row[0])) <= {"kind", "subject", "predicate", "value_text", "conditions", "statement_kind", "valid_from", "valid_to", "evidence_spans", "procedure", "intention", "alias"} for row in tx._check().execute("SELECT payload_json FROM claim_versions WHERE claim_id=?", (fact_ref,)))
    correction = _capture(storage, context, clock, "v7-correction", "project final change to updated_value.")
    current = current_claim(storage, clock, context, fact_ref)
    revised = revise(storage, clock, context, {"protocol_version": "1.1", "target_ref": fact_ref, "expected_revision": current.revision, "new_value": "updated_value", "conditions": [], "source_evidence_refs": [f"{correction.event_refs[0].ref}@1"], "valid_from": None}, remaining_seconds=10)
    assert revised.items[0].disposition == "revised"
    current = current_claim(storage, clock, context, fact_ref)
    with storage.read(context) as tx:
        epoch = tx.status().memory_epoch
    released = release_objects(storage, clock, context, (ObjectRef("claim", fact_ref, current.revision),), expected_epoch=epoch)
    assert released[0].payload["value_text"] == "updated_value"

    deletion_source = _capture(storage, context, clock, "v7-delete", f"delete {fact_ref}")
    current = current_claim(storage, clock, context, fact_ref)
    deletion = forget(storage, clock, context, {"protocol_version": "1.1", "target_refs": [fact_ref], "mode": "delete", "expected_revisions": {fact_ref: current.revision}, "reason": "delete"}, remaining_seconds=10)
    purge_sqlite(storage, context, deletion["operation_id"], remaining_seconds=10)
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        assert conn.execute("SELECT payload_json FROM claim_versions WHERE claim_id=? AND revision=?", (fact_ref, current.revision)).fetchone()[0] == "{}"
        assert conn.execute("SELECT content FROM source_events WHERE event_id=?", (deletion_source.event_refs[0].ref,)).fetchone()[0] == ""
        versions_after_forget = conn.execute("SELECT count(*) FROM claim_versions").fetchone()[0]
    rerun = migrate_legacy(legacy, target)
    assert rerun["counts"]["claim_versions"] == versions_after_forget
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM claim_versions WHERE claim_id=?", (fact_ref,)).fetchone()[0] == current.revision
        assert conn.execute("SELECT payload_json FROM claim_versions WHERE claim_id=? AND revision=?", (fact_ref, current.revision)).fetchone()[0] == "{}"


def test_current_earlier_than_history_blocks_entire_fact_slot(tmp_path: Path) -> None:
    legacy = _history_fixture(tmp_path)
    with sqlite3.connect(legacy) as conn:
        conn.execute("UPDATE fact_claims SET recorded_at='2026-09-03T00:00:00Z',retired_at='2026-09-04T00:00:00Z' WHERE claim_id='fact-1'")
    report = migrate_legacy(legacy, tmp_path / "target")
    assert report["completion_status"] == "blocked"
    assert "fact_current_not_latest_recorded_blocks_cutover" in report["cutover_block"]["reasons"]
    with sqlite3.connect(tmp_path / "target" / "memory.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM claims WHERE kind='fact'").fetchone()[0] == 0


def test_preexisting_natural_core_slot_conflict_rolls_back_migration(tmp_path: Path) -> None:
    legacy = _ascii_history_fixture(tmp_path)
    target = tmp_path / "target"
    storage, context, clock = _runtime(target)
    source = _capture(storage, context, clock, "v7-existing", "project keep")
    proposal = {"kind": "fact", "subject": "project", "predicate": "preference", "value_text": "existing", "conditions": [], "statement_kind": "assertion", "valid_from": None, "valid_to": None, "evidence_spans": [{"source_ref": source.event_refs[0].ref, "source_revision": 1, "quote": "project keep"}]}
    accepted = accept_claim_proposals(storage, clock, context, {"protocol_version": "1.1", "source_refs": [f"{source.event_refs[0].ref}@1"], "claim_proposals": [proposal], "resume_proposals": [], "reference_proposals": []}, scope_id="scope-a", remaining_seconds=10)
    existing_ref = accepted.items[0].ref
    before = sqlite3.connect(target / "memory.sqlite3")
    before_counts = tuple(before.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("source_events", "claims", "claim_versions"))
    before.close()
    with pytest.raises(MigrationError, match="claim slot collision"):
        migrate_legacy(legacy, target)
    with sqlite3.connect(target / "memory.sqlite3") as conn:
        assert tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("source_events", "claims", "claim_versions")) == before_counts
        assert conn.execute("SELECT claim_id FROM claims WHERE claim_id=?", (existing_ref,)).fetchone()[0] == existing_ref
