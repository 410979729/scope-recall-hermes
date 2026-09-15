from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import p18_core_execution as execution
from p18_core_execution import CoreExecutionError, _record_manifest, run_core_unit
from p18_history_loader import history_event_dtos


_REAL_FORMAL_VALIDATOR = execution._validate_formal_config


@pytest.fixture(autouse=True)
def _mock_formal_admission_for_offline_core_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_validator(path: Path, unit_file: Path, rows: list[dict], runtime_config_path: Path | None = None) -> dict:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        freeze_core = loaded.get("core") if isinstance(loaded, dict) else {}
        return {
            "config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "readiness": {"status": "READY", "test_mock": True},
            "arm_id": "C",
            "conditions_per_arm": freeze_core.get("conditions_per_arm"),
            "unit_path": str(unit_file.resolve()),
            "unit_sha256": hashlib.sha256(unit_file.read_bytes()).hexdigest(),
            "plan_path": None,
            "plan_sha256": None,
        }

    monkeypatch.setattr(execution, "_validate_formal_config", fake_validator)


def _write_fixture(root: Path, *, provenance: bool = True, unit: dict | None = None) -> tuple[Path, Path]:
    unit_path = root / "core-C.jsonl"
    default_unit = {
        "unit_id": "core-C-001",
        "kind": "core_condition",
        "ordinal": 1,
        "arm_id": "C",
        "source_sequence": "source_capture_then_actual_arm_extraction",
        "source_records": [{
            "event_id": "TEST-event-001", "sequence": 1, "source_type": "human_direct",
            "speaker_role": "user", "text": "TEST public source", "occurred_at": "2026-09-06T00:00:00Z",
        }],
        "query_record": {"clean_session": True, "query_id": "TEST-query-001", "session_id": "TEST-session-001", "text": "TEST query"},
        "model_input": {"history": [{"source_type": "human_direct", "speaker_role": "user", "text": "TEST public source", "occurred_at": "2026-09-06T00:00:00Z"}], "query": {"text": "TEST query"}},
    }
    unit_path.write_text(json.dumps(unit or default_unit) + "\n", encoding="utf-8")
    config_path = root / "runtime.json"
    payload = {
        "binding": {"agent_id": "TEST-agent", "installation_id": "TEST-installation", "data_directory": str(root / "data"), "scope_ids": ["TEST-scope"], "test_mode": True},
        "session_id": "TEST-session", "allowed_scope_ids": ["TEST-scope"],
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }
    if provenance:
        payload["source_provenance"] = {"commit": "TEST-source-commit", "source_root": "TEST-source-root"}
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return unit_path, config_path


def _freeze_receipt(tmp_path: Path) -> Path:
    freeze = tmp_path / "freeze.json"
    freeze.write_text(json.dumps({"status": "READY_FOR_FORMAL_FREEZE", "freeze_ready": True, "core": {"conditions_per_arm": 240}}), encoding="utf-8")
    return freeze


def _contradictory_units() -> list[dict]:
    shared_query = "What was recorded?"
    return [
        {
            "unit_id": "core-C-001",
            "kind": "core_condition",
            "ordinal": 1,
            "arm_id": "C",
            "source_sequence": "source_capture_then_actual_arm_extraction",
            "source_records": [{
                "event_id": "TEST-event-alpha", "sequence": 1, "source_type": "human_direct",
                "speaker_role": "user", "text": "ALPHA contradictory source truth", "occurred_at": "2026-09-06T00:00:00Z",
            }],
            "query_record": {"clean_session": True, "query_id": "TEST-query-alpha", "session_id": "TEST-session-alpha", "text": shared_query},
            "model_input": {
                "history": [{"source_type": "human_direct", "speaker_role": "user", "text": "ALPHA contradictory source truth", "occurred_at": "2026-09-06T00:00:00Z"}],
                "query": {"text": shared_query},
            },
        },
        {
            "unit_id": "core-C-002",
            "kind": "core_condition",
            "ordinal": 2,
            "arm_id": "C",
            "source_sequence": "source_capture_then_actual_arm_extraction",
            "source_records": [{
                "event_id": "TEST-event-beta", "sequence": 1, "source_type": "human_direct",
                "speaker_role": "user", "text": "BETA contradictory source truth", "occurred_at": "2026-09-06T00:00:00Z",
            }],
            "query_record": {"clean_session": True, "query_id": "TEST-query-beta", "session_id": "TEST-session-beta", "text": shared_query},
            "model_input": {
                "history": [{"source_type": "human_direct", "speaker_role": "user", "text": "BETA contradictory source truth", "occurred_at": "2026-09-06T00:00:00Z"}],
                "query": {"text": shared_query},
            },
        },
    ]


def _write_multi_unit_fixture(root: Path, units: list[dict]) -> tuple[Path, Path]:
    unit_path = root / "core-C.jsonl"
    unit_path.write_text("\n".join(json.dumps(unit) for unit in units) + "\n", encoding="utf-8")
    config_path = root / "runtime.json"
    payload = {
        "binding": {"agent_id": "TEST-agent", "installation_id": "TEST-installation", "data_directory": str(root / "data"), "scope_ids": ["TEST-scope"], "test_mode": True},
        "session_id": "TEST-session", "allowed_scope_ids": ["TEST-scope"],
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
        "source_provenance": {"commit": "TEST-source-commit", "source_root": "TEST-source-root"},
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return unit_path, config_path


def test_default_is_preflight_and_does_not_initialize_core(tmp_path: Path) -> None:
    unit_path, config_path = _write_fixture(tmp_path)
    result = run_core_unit(unit_path, config_path, tmp_path / "TEST-core-preflight")
    assert result["status"] == "PREFLIGHT_ONLY"
    assert result["network_calls"] == 0
    assert result["model_calls"] == 0
    assert not (tmp_path / "data").exists()


def test_preflight_requires_installed_source_provenance(tmp_path: Path) -> None:
    unit_path, config_path = _write_fixture(tmp_path, provenance=False)
    with pytest.raises(CoreExecutionError, match="source_provenance"):
        run_core_unit(unit_path, config_path, tmp_path / "TEST-core-preflight")


def test_private_source_projection_preserves_origin_and_time(tmp_path: Path) -> None:
    unit_path, _ = _write_fixture(tmp_path)
    unit = json.loads(unit_path.read_text(encoding="utf-8"))
    manifest = _record_manifest(unit)
    events = history_event_dtos(manifest)
    assert events[0].event["source_original_origin"] == "human_direct"
    assert events[0].event["occurred_at"] == "2026-09-06T00:00:00Z"
    assert events[0].event["recorded_at"] == "2026-09-06T00:00:00Z"


def test_run_requires_frozen_core_denominator(tmp_path: Path) -> None:
    unit_path, config_path = _write_fixture(tmp_path)
    freeze = tmp_path / "freeze.json"
    freeze.write_text(json.dumps({"status": "READY_FOR_FORMAL_FREEZE", "freeze_ready": True, "core": {"conditions_per_arm": 1}}), encoding="utf-8")
    with pytest.raises(CoreExecutionError, match="denominator"):
        run_core_unit(unit_path, config_path, tmp_path / "TEST-core-run", run=True, freeze_receipt_path=freeze)


def test_old_boolean_freeze_label_is_rejected_before_runtime_or_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unit_path, config_path = _write_fixture(tmp_path)
    monkeypatch.setattr(execution, "_validate_formal_config", _REAL_FORMAL_VALIDATOR)
    with pytest.raises(CoreExecutionError, match="formal_config_not_ready"):
        run_core_unit(unit_path, config_path, tmp_path / "TEST-old-label", run=True, freeze_receipt_path=_freeze_receipt(tmp_path))
    assert not (tmp_path / "TEST-old-label").exists()
    assert not (tmp_path / "data").exists()


def test_formal_core_binding_checks_unit_and_plan_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unit_path, _ = _write_fixture(tmp_path)
    first = json.loads(unit_path.read_text(encoding="utf-8"))
    rows = [dict(first, unit_id=f"core-C-{index:03d}", ordinal=index) for index in range(1, 241)]
    unit_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    dataset_id = "TEST-EVAL-SEALED-120-v6"
    raw_sha256 = "a" * 64
    plan_path = tmp_path / "plan-summary.json"
    plan_path.write_text(json.dumps({"status": "READY_FOR_FORMAL_FREEZE", "freeze_ready": True, "dataset_id": dataset_id, "raw_sha256": raw_sha256, "core": {"conditions_per_arm": 240, "arms": 4, "core_paths": ["core-C.jsonl"]}}), encoding="utf-8")
    formal_path = tmp_path / "formal.json"
    formal_path.write_text(json.dumps({"core_inputs": {"arm_id": "C", "conditions_per_arm": 240, "dataset_id": dataset_id, "raw_sha256": raw_sha256, "unit": {"path": unit_path.name, "sha256": hashlib.sha256(unit_path.read_bytes()).hexdigest(), "record_count": 240}, "plan": {"path": plan_path.name, "sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest()}}}), encoding="utf-8")
    import p18_formal_evidence as evidence

    monkeypatch.setattr(evidence, "verify_formal_run_config", lambda path: SimpleNamespace(formal_execution_allowed=True, reasons=(), details={}))
    details = _REAL_FORMAL_VALIDATOR(formal_path, unit_path, execution._load_units(unit_path))
    assert details["arm_id"] == "C"
    assert details["conditions_per_arm"] == 240
    unit_path.write_text(unit_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(CoreExecutionError, match="unit_hash"):
        _REAL_FORMAL_VALIDATOR(formal_path, unit_path, execution._load_units(unit_path))


def test_public_fixture_runs_through_core_capture_drain_and_fresh_recall(tmp_path: Path) -> None:
    unit_path, config_path = _write_fixture(tmp_path)
    result = run_core_unit(unit_path, config_path, tmp_path / "TEST-core-run", run=True, freeze_receipt_path=_freeze_receipt(tmp_path))
    assert result["status"] == "EXECUTED_WITHOUT_SEMANTIC_SCORING"
    assert result["units_succeeded"] == 1
    assert result["units_failed"] == 0
    assert result["semantic_score"] is None
    assert not (tmp_path / "data").exists()
    assert result["template_data_directory_touched"] is False
    artifact = json.loads((tmp_path / "TEST-core-run" / "units" / "core-C-001" / "unit-artifact.json").read_text(encoding="utf-8"))
    assert artifact["recall_request"]["mode"] == "auto"
    assert isinstance(artifact["recall_packet"], dict)
    assert artifact["recall_packet"]["request_id"] == "core-C-001"


def test_contradictory_same_query_units_do_not_contaminate(tmp_path: Path) -> None:
    unit_path, config_path = _write_multi_unit_fixture(tmp_path, _contradictory_units())
    result = run_core_unit(unit_path, config_path, tmp_path / "TEST-core-isolation", run=True, freeze_receipt_path=_freeze_receipt(tmp_path))
    assert result["status"] == "EXECUTED_WITHOUT_SEMANTIC_SCORING"
    assert result["units_succeeded"] == 2
    alpha = json.loads((tmp_path / "TEST-core-isolation" / "units" / "core-C-001" / "unit-artifact.json").read_text(encoding="utf-8"))
    beta = json.loads((tmp_path / "TEST-core-isolation" / "units" / "core-C-002" / "unit-artifact.json").read_text(encoding="utf-8"))
    assert alpha["source_load"]["source_refs"] != beta["source_load"]["source_refs"]
    assert alpha["query_status"]["sources"] == 1
    assert beta["query_status"]["sources"] == 1
    assert alpha["source_load"]["source_refs"][0] not in beta["source_load"]["source_refs"]
    assert isinstance(alpha["recall_packet"], dict)
    assert isinstance(beta["recall_packet"], dict)
    assert alpha["data_directory"] != beta["data_directory"]
    assert alpha["installation_id"] != beta["installation_id"]


def test_each_unit_has_distinct_data_and_session_ids(tmp_path: Path) -> None:
    unit_path, config_path = _write_multi_unit_fixture(tmp_path, _contradictory_units())
    result = run_core_unit(unit_path, config_path, tmp_path / "TEST-core-identities", run=True, freeze_receipt_path=_freeze_receipt(tmp_path))
    summaries = result["unit_summaries"]
    assert len({item["installation_id"] for item in summaries}) == 2
    assert len({item["data_directory"] for item in summaries}) == 2
    assert len({item["source_session_id"] for item in summaries}) == 2
    assert len({item["query_session_id"] for item in summaries}) == 2


def test_parallel_public_units_overlap_without_cross_contamination(tmp_path, monkeypatch):
    from threading import Barrier
    unit_path, config_path = _write_multi_unit_fixture(tmp_path, _contradictory_units())
    freeze = _freeze_receipt(tmp_path)
    frozen = json.loads(freeze.read_text(encoding='utf-8'))
    frozen['core_concurrency'] = 2
    freeze.write_text(json.dumps(frozen), encoding='utf-8')
    original = execution._execute_unit
    barrier = Barrier(2, timeout=5)
    def concurrent_unit(*args):
        barrier.wait()
        return original(*args)
    monkeypatch.setattr(execution, '_execute_unit', concurrent_unit)
    result = run_core_unit(unit_path, config_path, tmp_path / 'TEST-parallel-core',
                           run=True, freeze_receipt_path=freeze)
    assert result['max_parallel_units'] == result['units_succeeded'] == 2
    assert result['units_failed'] == 0
    assert len({r['data_directory'] for r in result['unit_summaries']}) == 2
    assert len({r['query_session_id'] for r in result['unit_summaries']}) == 2


def test_template_runtime_db_is_never_initialized(tmp_path: Path) -> None:
    unit_path, config_path = _write_fixture(tmp_path)
    run_core_unit(unit_path, config_path, tmp_path / "TEST-core-preflight")
    assert not (tmp_path / "data").exists()
    run_core_unit(unit_path, config_path, tmp_path / "TEST-core-run", run=True, freeze_receipt_path=_freeze_receipt(tmp_path))
    assert not (tmp_path / "data").exists()


def test_packet_body_and_source_refs_are_preserved_for_scorer(tmp_path: Path) -> None:
    unit_path, config_path = _write_fixture(tmp_path)
    run_core_unit(unit_path, config_path, tmp_path / "TEST-core-packet", run=True, freeze_receipt_path=_freeze_receipt(tmp_path))
    artifact = json.loads((tmp_path / "TEST-core-packet" / "units" / "core-C-001" / "unit-artifact.json").read_text(encoding="utf-8"))
    packet = artifact["recall_packet"]
    assert set(packet) >= {"protocol_version", "request_id", "status", "items"}
    assert artifact["source_load"]["source_refs"]
    assert artifact["source_ref_count"] >= 0
    assert artifact["packet_sha256"]
    receipt = json.loads((tmp_path / "TEST-core-packet" / "receipt.json").read_text(encoding="utf-8"))
    assert "recall_packet" not in json.dumps(receipt)
    artifact_path = tmp_path / "TEST-core-packet" / "units" / "core-C-001" / "unit-artifact.json"
    assert receipt["unit_summaries"][0]["artifact_sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()


def test_query_defaults_to_auto_not_history(tmp_path: Path) -> None:
    unit_path, config_path = _write_fixture(tmp_path)
    run_core_unit(unit_path, config_path, tmp_path / "TEST-core-mode", run=True, freeze_receipt_path=_freeze_receipt(tmp_path))
    artifact = json.loads((tmp_path / "TEST-core-mode" / "units" / "core-C-001" / "unit-artifact.json").read_text(encoding="utf-8"))
    assert artifact["recall_request"]["mode"] == "auto"
    assert artifact["recall_request"]["mode"] != "history"


def test_failed_unit_writes_immutable_evidence_and_batch_continues(tmp_path: Path) -> None:
    good_unit = {
        "unit_id": "core-C-002",
        "kind": "core_condition",
        "ordinal": 2,
        "arm_id": "C",
        "source_sequence": "source_capture_then_actual_arm_extraction",
        "source_records": [{
            "event_id": "TEST-event-good", "sequence": 1, "source_type": "human_direct",
            "speaker_role": "user", "text": "TEST good source", "occurred_at": "2026-09-06T00:00:00Z",
        }],
        "query_record": {"clean_session": True, "query_id": "TEST-query-good", "session_id": "TEST-session-good", "text": "TEST good query"},
        "model_input": {
            "history": [{"source_type": "human_direct", "speaker_role": "user", "text": "TEST good source", "occurred_at": "2026-09-06T00:00:00Z"}],
            "query": {"text": "TEST good query"},
        },
    }
    bad_unit = {
        "unit_id": "core-C-001",
        "kind": "core_condition",
        "ordinal": 1,
        "arm_id": "C",
        "source_sequence": "source_capture_then_actual_arm_extraction",
        "source_records": [{
            "event_id": "", "sequence": 1, "source_type": "human_direct",
            "speaker_role": "user", "text": "TEST bad source", "occurred_at": "2026-09-06T00:00:00Z",
        }],
        "query_record": {"clean_session": True, "query_id": "TEST-query-bad", "session_id": "TEST-session-bad", "text": "TEST bad query"},
        "model_input": {
            "history": [{"source_type": "human_direct", "speaker_role": "user", "text": "TEST bad source", "occurred_at": "2026-09-06T00:00:00Z"}],
            "query": {"text": "TEST bad query"},
        },
    }
    unit_path, config_path = _write_multi_unit_fixture(tmp_path, [bad_unit, good_unit])
    result = run_core_unit(unit_path, config_path, tmp_path / "TEST-core-failure", run=True, freeze_receipt_path=_freeze_receipt(tmp_path))
    assert result["status"] == "EXECUTED_WITH_UNIT_FAILURES"
    assert result["units_failed"] == 1
    assert result["units_succeeded"] == 1
    assert result["semantic_score"] is None
    failure = json.loads((tmp_path / "TEST-core-failure" / "units" / "core-C-001" / "unit-failure.json").read_text(encoding="utf-8"))
    assert failure["status"] == "FAILED"
    assert failure["unit_id"] == "core-C-001"
    assert (tmp_path / "TEST-core-failure" / "units" / "core-C-002" / "unit-artifact.json").is_file()
    failure_path = tmp_path / "TEST-core-failure" / "units" / "core-C-001" / "unit-failure.json"
    assert result["unit_summaries"][0]["failure_sha256"] == hashlib.sha256(failure_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("unit_id", ["../outside", r"..\outside", "/absolute", "C:drive", "x" * 96 + "y"])
def test_unit_id_cannot_escape_output_root(tmp_path: Path, unit_id: str) -> None:
    units, config = _write_fixture(tmp_path)
    payload = json.loads(units.read_text(encoding="utf-8"))
    payload["unit_id"] = unit_id
    units.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CoreExecutionError, match="identity"):
        run_core_unit(units, config, tmp_path / "TEST-invalid")
    assert not (tmp_path / "TEST-invalid").exists()


@pytest.mark.parametrize(
    ("mutation", "error"),
    [("arm", "identity|binding"), ("extra", "schema"), ("control", "control")],
)
def test_core_unit_shape_and_controls_fail_closed_before_output(tmp_path: Path, mutation: str, error: str) -> None:
    unit_path, config_path = _write_fixture(tmp_path)
    payload = json.loads(unit_path.read_text(encoding="utf-8"))
    if mutation == "arm":
        payload["arm_id"] = "B"
    elif mutation == "extra":
        payload["unexpected"] = "TEST"
    else:
        payload["model_input"]["query"]["expected"] = "must not enter model input"
    unit_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CoreExecutionError, match=error):
        run_core_unit(unit_path, config_path, tmp_path / "TEST-shape-rejected")
    assert not (tmp_path / "TEST-shape-rejected").exists()


def test_formal_runtime_bytes_candidate_and_vector_override_are_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unit_path, runtime_path = _write_fixture(tmp_path)
    first = json.loads(unit_path.read_text(encoding="utf-8"))
    rows = [dict(first, unit_id=f"core-C-{index:03d}", ordinal=index) for index in range(1, 241)]
    unit_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    runtime_raw = json.loads(runtime_path.read_text(encoding="utf-8"))
    ledger_path = tmp_path / "shared-ledger.sqlite3"
    runtime_raw["auxiliary"]["ledger_path"] = str(ledger_path)
    runtime_raw["source_provenance"] = {"commit": "c" * 40, "package_sha256": "d" * 64, "source_root": "TEST"}
    runtime_path.write_text(json.dumps(runtime_raw), encoding="utf-8")
    dataset_id, raw_sha256 = "TEST-EVAL-SEALED-120-v6", "a" * 64
    plan_path = tmp_path / "plan-summary.json"
    plan_path.write_text(json.dumps({"status": "READY_FOR_FORMAL_FREEZE", "freeze_ready": True, "dataset_id": dataset_id, "raw_sha256": raw_sha256, "core": {"conditions_per_arm": 240, "arms": 4, "core_paths": ["core-C.jsonl"]}}), encoding="utf-8")
    formal_path = tmp_path / "formal.json"
    import p18_formal_evidence as evidence
    monkeypatch.setattr(evidence, "verify_formal_run_config", lambda path: SimpleNamespace(
        formal_execution_allowed=True, reasons=(), details={"candidate_source_commit": "c" * 40, "wheel_sha256": "d" * 64, "ledger_path": str(ledger_path)},
    ))
    frozen_sha = execution._runtime_frozen_sha256(runtime_raw)
    formal_path.write_text(json.dumps({"core_inputs": {
        "arm_id": "C", "conditions_per_arm": 240, "dataset_id": dataset_id, "raw_sha256": raw_sha256,
        "unit": {"path": unit_path.name, "sha256": hashlib.sha256(unit_path.read_bytes()).hexdigest(), "record_count": 240},
        "plan": {"path": plan_path.name, "sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest()},
        "runtime": {"path": runtime_path.name, "sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(), "source_commit": "c" * 40, "wheel_sha256": "d" * 64, "frozen_fields_sha256": frozen_sha},
    }}), encoding="utf-8")
    details = _REAL_FORMAL_VALIDATOR(formal_path, unit_path, execution._load_units(unit_path), runtime_path)
    config, provenance = execution._load_runtime_config(runtime_path)
    execution._validate_formal_runtime_binding(runtime_path, config, provenance, details)
    runtime_raw["request_seconds"] = 44.0
    runtime_path.write_text(json.dumps(runtime_raw), encoding="utf-8")
    with pytest.raises(CoreExecutionError, match="runtime_hash"):
        _REAL_FORMAL_VALIDATOR(formal_path, unit_path, execution._load_units(unit_path), runtime_path)
    with pytest.raises(CoreExecutionError, match="vector_test_injection"):
        execution._validate_formal_runtime_binding(
            runtime_path, SimpleNamespace(vector=SimpleNamespace(test_injection_override=True), auxiliary=SimpleNamespace(ledger_path=ledger_path)), provenance, details,
        )


@dataclass
class _FakeRuntimeConfig:
    drain_seconds: float
    binding: SimpleNamespace


class _FakeDrain:
    def __init__(self, *, processed=0, completed=0, failed=0, retried=0, idle=True, items=()):
        self.processed = processed
        self.completed = completed
        self.failed = failed
        self.retried = retried
        self.idle = idle
        self.items = list(items)


class _FakeStatus:
    def __init__(self, pending_work=0, sources=0, memory_epoch=0):
        self.pending_work = pending_work
        self.sources = sources
        self.memory_epoch = memory_epoch


class _FakeInstance:
    def __init__(self, drains, statuses, *, data_directory):
        self._drains = list(drains)
        self._statuses = list(statuses)
        self.config = _FakeRuntimeConfig(5.0, SimpleNamespace(data_directory=data_directory))
        self.drain_calls = 0

    def drain(self):
        self.drain_calls += 1
        if not self._drains:
            raise AssertionError("unexpected extra drain")
        return self._drains.pop(0)

    def status(self):
        if not self._statuses:
            return _FakeStatus()
        if len(self._statuses) == 1:
            return self._statuses[0]
        return self._statuses.pop(0)


def _install_execution_clock(monkeypatch, now, sleeps=None):
    def fake_sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(execution, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=fake_sleep))


def test_bounded_drain_waits_scheduled_backoff_instead_of_idle_exit(tmp_path, monkeypatch):
    sleeps: list[float] = []
    now = [0.0]
    _install_execution_clock(monkeypatch, now, sleeps)
    monkeypatch.setattr(execution, "_pending_work_schedule", lambda instance: (1, 0, 0.2) if instance.drain_calls == 1 else (0, 0, None))
    instance = _FakeInstance(
        [
            _FakeDrain(processed=0, idle=True, retried=0),
            _FakeDrain(processed=1, completed=1, idle=False, retried=1),
        ],
        [_FakeStatus(pending_work=1), _FakeStatus(pending_work=0), _FakeStatus(pending_work=0)],
        data_directory=tmp_path,
    )
    result = execution._bounded_drain(instance, timeout_seconds=2.0)
    assert instance.drain_calls == 2
    assert sleeps == [0.2]
    assert result["timed_out"] is False
    assert result["pending_work_after"] == 0
    assert result["aggregate"]["retried"] == 1


def test_bounded_drain_does_not_stop_on_retried_when_work_remains(tmp_path, monkeypatch):
    monkeypatch.setattr(
        execution,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda seconds: (_ for _ in ()).throw(AssertionError("no sleep expected"))),
    )
    instance = _FakeInstance(
        [
            _FakeDrain(processed=1, retried=1, idle=False),
            _FakeDrain(processed=1, completed=1, idle=False),
        ],
        [_FakeStatus(pending_work=1), _FakeStatus(pending_work=0), _FakeStatus(pending_work=0)],
        data_directory=tmp_path,
    )
    result = execution._bounded_drain(instance, timeout_seconds=2.0)
    assert instance.drain_calls == 2
    assert result["timed_out"] is False
    assert result["aggregate"]["retried"] == 1


def test_bounded_drain_records_timeout_when_backoff_exceeds_deadline(tmp_path, monkeypatch):
    sleeps: list[float] = []
    now = [0.0]
    _install_execution_clock(monkeypatch, now, sleeps)
    monkeypatch.setattr(execution, "_pending_work_schedule", lambda instance: (1, 0, 5.0))
    instance = _FakeInstance(
        [_FakeDrain(processed=0, idle=True)],
        [_FakeStatus(pending_work=1), _FakeStatus(pending_work=1)],
        data_directory=tmp_path,
    )
    result = execution._bounded_drain(instance, timeout_seconds=0.5)
    assert instance.drain_calls == 1
    assert sleeps == [0.5]
    assert result["timed_out"] is True
    assert result["pending_work_after"] == 1
    assert min(sleeps) >= 0.001


def test_bounded_drain_uses_remaining_budget_after_drain_and_status(tmp_path, monkeypatch):
    now = [0.0]
    _install_execution_clock(monkeypatch, now)
    monkeypatch.setattr(execution, "_pending_work_schedule", lambda _: (1, 0, 5.0))
    instance = _FakeInstance(
        [_FakeDrain(processed=0, idle=True)],
        [_FakeStatus(pending_work=1), _FakeStatus(pending_work=1)],
        data_directory=tmp_path,
    )
    original_drain = instance.drain

    def delayed_drain():
        now[0] += 0.4
        return original_drain()

    instance.drain = delayed_drain
    result = execution._bounded_drain(instance, timeout_seconds=0.5)
    assert now[0] == 0.5
    assert result["timed_out"] is True
    assert result["pending_work_after"] == 1


def test_build_recall_request_uses_automatic_packet_budget_default():
    from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS

    unit = {
        "unit_id": "core-C-budget-default",
        "query_record": {"text": "TEST query"},
        "model_input": {"query": {"text": "TEST query"}},
    }
    request = execution._build_recall_request(unit, SimpleNamespace(max_items=6))
    assert request["budget_tokens"] == AUTOMATIC_PACKET_BUDGET_UNITS == 4096
    assert request["query"] == "TEST query"


def test_build_recall_request_keeps_explicit_smaller_budget():
    unit = {
        "unit_id": "core-C-budget-1200",
        "query_record": {"text": "TEST query"},
        "model_input": {"query": {"text": "TEST query", "budget_tokens": 1200}},
    }
    request = execution._build_recall_request(unit, SimpleNamespace(max_items=6))
    assert request["budget_tokens"] == 1200


def test_build_recall_request_rejects_illegal_budget_before_model_call():
    config = SimpleNamespace(max_items=6)
    for illegal in (True, False, "1200", 0, -1, 63, 8001, 12.5):
        unit = {
            "unit_id": "core-C-budget-illegal",
            "query_record": {"text": "TEST query"},
            "model_input": {"query": {"text": "TEST query", "budget_tokens": illegal}},
        }
        with pytest.raises(CoreExecutionError, match="core_query_budget_tokens_invalid"):
            execution._build_recall_request(unit, config)


def test_pending_work_schedule_closes_sqlite_connection(tmp_path, monkeypatch):
    import sqlite3

    database = tmp_path / "memory.sqlite3"
    setup = sqlite3.connect(database)
    setup.execute("CREATE TABLE work_items (state TEXT, available_at TEXT, lease_until TEXT)")
    setup.commit()
    setup.close()
    connections = []
    original_connect = execution.sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(execution, "sqlite3", SimpleNamespace(connect=tracking_connect, Error=sqlite3.Error))
    observed = execution._pending_work_schedule(
        SimpleNamespace(config=SimpleNamespace(binding=SimpleNamespace(data_directory=tmp_path)))
    )
    assert observed == (0, 0, None)
    assert connections
    with pytest.raises(sqlite3.ProgrammingError):
        connections[-1].execute("SELECT 1")
