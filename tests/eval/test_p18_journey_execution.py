"""Offline public tests for real local journey controls; no host/model evidence."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from p18_journey_execution import CoreJourneyControls, JourneyExecutionError, execute_journey, load_artifact, load_journey
from scope_recall.core import CoreConfig, MemoryCore
from v11_support import context


@pytest.fixture
def controls(tmp_path):
    root = tmp_path / "TEST-journey"
    ctx = replace(context(root / "data"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding))
    core.initialize()
    runtime = SimpleNamespace(core=core)
    control = CoreJourneyControls(runtime=runtime, operator_context=ctx, scope_id="TEST-scope",
                                  arm_root=root, workspace=root / "workspace", evidence_root=root / "evidence", host=Mock())
    yield control
    control.close()


def test_copy_checks_real_asset_bytes_and_rejects_escape(controls, tmp_path):
    source = tmp_path / "asset.txt"
    source.write_text("PUBLIC TEST material", encoding="utf-8")
    ref = {"path": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    result = controls.run({"kind": "copy_assets", "parameters": {"assets": [{"asset": ref, "destination": "v1/asset.txt"}]}}, tmp_path)
    assert Path(result["copied"][0]["path"]).read_bytes() == source.read_bytes()
    with pytest.raises(JourneyExecutionError, match="outside"):
        load_artifact(tmp_path, {**ref, "path": "../asset.txt"})
    source.write_text("changed", encoding="utf-8")
    with pytest.raises(JourneyExecutionError, match="hash_mismatch"):
        load_artifact(tmp_path, ref)


def test_collection_uses_real_pages_not_expected_objects(controls):
    for i in range(5):
        controls._capture(f"PUBLIC TEST object {i}", f"TEST-public/{i}", origin="human_direct")
    result = controls._enumerate({"page_size": 2, "object_kind": "event"})
    assert [len(page["items"]) for page in result["pages"]] == [2, 2, 1]
    assert result["exhausted"]
    assert all(page["coverage"] != "complete" for page in result["pages"][:-1])


def test_delete_restore_replays_real_latest_ledger_before_open(controls, tmp_path):
    refs = controls._capture("PUBLIC TEST source for deletion", "TEST-public/delete", origin="human_direct")
    controls.observations["public-source"] = {"source_refs": refs}
    controls.run({"kind": "backup_sqlite", "parameters": {"slot": "old"}}, tmp_path)
    controls.run({"kind": "authorized_forget", "operation_id": "public-delete", "parameters": {
        "target_source_operation": "public-source", "purge_attachments": False,
    }}, tmp_path)
    assert controls._source(refs[0]) is None
    controls.run({"kind": "restore_snapshot", "parameters": {"slot": "old"}}, tmp_path)
    assert (controls.context.binding.data_directory / "restore-required.json").exists()
    controls.run({"kind": "replay_deletion_ledger", "parameters": {}}, tmp_path)
    assert not (controls.context.binding.data_directory / "restore-required.json").exists()
    assert controls._source(refs[0]) is None


def test_natural_input_cannot_contain_assertion_fields(tmp_path):
    natural = tmp_path / "input.json"
    natural.write_text(json.dumps({"query": "PUBLIC TEST query", "attachments": [], "expected": "bad"}), encoding="utf-8")
    input_ref = {"path": natural.name, "sha256": hashlib.sha256(natural.read_bytes()).hexdigest()}
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"schema": "scope-recall.p18-private-journey-execution.v1", "journeys": [{
        "journey_id": "J01", "actions": [{"operation_id": "public-turn", "kind": "host_turn",
        "source_step_orders": [1, 2, 3, 4, 5, 6], "primary_round_ordinal": 1, "parameters": {"input": input_ref}}],
    }]}), encoding="utf-8")
    with pytest.raises(JourneyExecutionError, match="natural_input_only"):
        load_journey(bundle, "J01")


def test_scale_fixture_accepts_bucket_size_template_without_index(controls):
    params = {
        "counts": [4],
        "series_id": "PUBLIC-similar",
        "generator": {"template": "PUBLIC similar project bucket {bucket} size {size}"},
    }
    result = controls._seed_scale({"operation_id": "scale-similar"}, params)
    assert result["path"] == "test_only_bulk_fixture"
    assert result["checkpoints"] == [{"requested": 4, "actual_source_objects": 4}]


def test_scale_extension_does_not_duplicate_first_stage(controls):
    params = {"counts": [3], "series_id": "PUBLIC-scale", "generator": {"template": "PUBLIC TEST noise {index}"}}
    first = controls._seed_scale({"operation_id": "scale-one"}, params)
    second = controls._seed_scale({"operation_id": "scale-two"}, {**params, "start_index": 4, "counts": [7]})
    assert first["checkpoints"] == [{"requested": 3, "actual_source_objects": 3}]
    assert second["checkpoints"] == [{"requested": 7, "actual_source_objects": 7}]
    assert first["path"] == second["path"] == "test_only_bulk_fixture"
    assert first["claims_preseeded"] is False
    assert first["external_model_calls"] == first["real_embeddings"] == 0
    assert controls.host.quiesce_workers.call_count == 2
    with controls.core.storage.read(controls.context) as tx:
        work = tx._check().execute("SELECT count(*) FROM work_items").fetchone()[0]
        claims = tx._check().execute("SELECT count(*) FROM claims").fetchone()[0]
        lexical = tx._check().execute("SELECT count(*) FROM lexical_projection").fetchone()[0]
    assert work == 0 and claims == 0 and lexical > 0


def test_scale_production_capture_path_is_throughput_only(controls):
    params = {"counts": [2], "series_id": "PUBLIC-throughput", "generator": {"template": "PUBLIC TEST noise {index}"},
              "write_path": "production_capture_throughput"}
    result = controls._seed_scale({"operation_id": "scale-throughput"}, params)
    assert result["path"] == "production_capture_throughput"
    assert result["throughput_only"] is True
    with controls.core.storage.read(controls.context) as tx:
        work = tx._check().execute("SELECT count(*) FROM work_items").fetchone()[0]
    assert work >= 2


def test_semantic_fixture_still_uses_real_capture(controls):
    refs = controls._capture("TEST key event: P-418 chose silver-gray.", "TEST-M45/S1/1", origin="human_direct")
    assert refs
    source = controls._source(refs[0])
    assert source.event["origin"] == "human_direct"
    assert source.event["content"].startswith("TEST key event")
    with controls.core.storage.read(controls.context) as tx:
        work = tx._check().execute("SELECT count(*) FROM work_items").fetchone()[0]
    assert work >= 1


def test_pause_fault_only_leases_named_source_and_cannot_reclaim(controls):
    from scope_recall.core.worker import _episode_batch
    noise = controls._capture("PUBLIC TEST earlier noise", "noise", origin="external_document")
    target = controls._capture("PUBLIC TEST later target", "target", origin="external_document", session_id="TEST-isolated-pause")
    leased = controls._lease_fault_target(target[0])
    with controls.core.storage.read(controls.context) as tx:
        rows = tx._check().execute("SELECT subject_ref,state,lease_token FROM work_items WHERE work_type='consolidate'").fetchall()
        _, batch, pending = _episode_batch(tx, tx.source(leased.subject_ref, leased.subject_revision), leased, now=controls.core.clock.utc_now())
    assert pending == () and [s.ref for s in batch] == [leased.subject_ref]
    state = {row["subject_ref"]: row["state"] for row in rows}
    assert state[noise[0].rsplit("@", 1)[0]] == "pending"
    assert state[target[0].rsplit("@", 1)[0]] == "leased"
    assert leased.lease_token == 1 and leased.attempt == 1
    with pytest.raises(JourneyExecutionError, match="not_pending"):
        controls._lease_fault_target(target[0])


def test_hold_cannot_upgrade_unattested_A2A_source_to_document(controls, monkeypatch):
    refs = controls._capture("PUBLIC remote message", "PUBLIC-a2a", origin="origin_unknown")
    controls.observations["remote-turn"] = {"source_refs": refs}
    model = Mock()
    monkeypatch.setattr("scope_recall.core.worker.build_consolidation_model", lambda _: model)
    with pytest.raises(JourneyExecutionError, match="source_not_eligible"):
        controls._hold({"operation_id": "PUBLIC-hold"}, {"source_operation": "remote-turn"})
    model.propose.assert_not_called()
    controls.host.quiesce_workers.assert_not_called()
    assert controls._source(refs[0]).event["origin"] == "origin_unknown"
    with controls.core.storage.read(controls.context) as tx:
        assert tx._check().execute("SELECT count(*) FROM source_events").fetchone()[0] == 1


@pytest.mark.parametrize("pending_session", (False, True))
def test_baseline_unsupported_control_preserves_following_real_turn(controls, tmp_path, pending_session):
    controls.core = None
    natural = tmp_path / "public-input.json"
    natural.write_text(json.dumps({"query": "PUBLIC TEST follow-up", "attachments": []}), encoding="utf-8")
    ref = {"path": natural.name, "sha256": hashlib.sha256(natural.read_bytes()).hexdigest()}
    formal = tmp_path / "public-formal.json"
    formal.write_text(json.dumps({"operation_id": "turn", "status": "COMPLETED", "ids": {"session_id": "actual-public-session"}}), encoding="utf-8")
    host = Mock()
    host.new_session.return_value = {"session_id": None, "pending_context_id": "PUBLIC-actual-audience"}
    host.execute_turn.return_value = {"session_id": "actual-public-session", "formal_evidence_path": str(formal), "source_refs": []}
    bundle = tmp_path / "public-bundle.json"
    actions = [
            {"operation_id": "control", "kind": "bounded_enumeration", "session_alias": "s1", "source_step_orders": [1], "parameters": {"page_size": 2}},
            {"operation_id": "turn", "kind": "host_turn", "session_alias": "s1", "source_step_orders": [2,3,4,5,6], "primary_round_ordinal": 1, "parameters": {"input": ref}},
        ]
    if pending_session:
        actions.insert(0, {"operation_id": "open", "kind": "new_session", "session_alias": "s1", "source_step_orders": [1], "parameters": {}})
    bundle.write_text(json.dumps({"schema": "scope-recall.p18-private-journey-execution.v1", "journeys": [{
        "journey_id": "J01", "actions": actions}]}), encoding="utf-8")
    result = execute_journey(bundle, "J01", host=host, controls=controls)
    assert result["status"] == "EXECUTED_WITH_UNSUPPORTED_CONTROLS"
    assert result["unsupported_operations"] == ["control"]
    assert host.execute_turn.call_count == 1
    assert host.execute_turn.call_args.kwargs["session_id"] is None
    if pending_session:
        assert json.loads((controls.evidence_root / "open.json").read_text(encoding="utf-8"))["result"]["session_id"] is None
    assert result["semantic_pass"] is False
