"""Synthetic public tests for the P18 score-report aggregator."""
from __future__ import annotations

import hashlib
import json
import math
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p18_score_report as report

TEST_DIR = Path(__file__).resolve().parent / "TEST-p18-score-report"
IDENTITY_MAP = report.build_public_identity_map()
QUERY_PAIR_INDICES = sorted(int(pair_id.rsplit("-", 1)[1]) for pair_id in IDENTITY_MAP["query_pair_ids"])


def _metadata(**overrides) -> dict:
    base = {
        "schema": report.BUNDLE_SCHEMA,
        "grading_module": {"id": "test-grader", "version": "0.1.0"},
        "annotation_input_sha256": "a" * 64,
        "run_id": "TEST-run-001",
        "protocol_sha256": report.PROTOCOL_SHA256,
        "allocation_adjudication_sha256": report.ALLOCATION_ADJUDICATION_SHA256,
        "identity_map_sha256": report.IDENTITY_MAP_SHA256,
        "method_ids": {
            "hermes_a2a": report.HERMES_METHOD_ID,
            "codex_windows_desktop": report.CODEX_METHOD_ID,
        },
        "method_artifact_sha256": {
            report.HERMES_METHOD_ID: report.HERMES_METHOD_ARTIFACT_SHA256,
            report.CODEX_METHOD_ID: report.CODEX_METHOD_ARTIFACT_SHA256,
        },
    }
    base.update(overrides)
    return base


def _annotation_bytes(records: list[dict]) -> bytes:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records).encode("utf-8")


def _metadata_with_annotation_hash(records: list[dict]) -> dict:
    return _metadata(annotation_input_sha256=hashlib.sha256(_annotation_bytes(records)).hexdigest())


def _evidence(path: str = "stub.json") -> list[dict[str, str]]:
    return [{"path": path, "sha256": "b" * 64}]


def _pair_id(index: int) -> str:
    return f"TEST-pair-{index:03d}"


PAIR_LOOKUP = {item["pair_id"]: item for item in IDENTITY_MAP["pairs"]}


def _group_for_pair(index: int) -> str:
    return PAIR_LOOKUP[_pair_id(index)]["group_id"]


def _core_class_for_pair(index: int) -> str:
    return PAIR_LOOKUP[_pair_id(index)]["core_class"]


def _graded_core(
    *,
    arm: str = "C",
    pair_index: int = 1,
    condition_id: int = 1,
    attempt: int = 1,
    unit_suffix: str | None = None,
    **overrides,
) -> dict:
    pair_id = _pair_id(pair_index)
    unit_id = unit_suffix or f"core-{arm}-{pair_id}-c{condition_id}-a{attempt}"
    record = {
        "run_id": "TEST-run-001",
        "attempt": attempt,
        "kind": "core",
        "arm_id": arm,
        "host_id": None,
        "unit_id": unit_id,
        "status": "graded",
        "reason": None,
        "evidence": _evidence(f"core/{unit_id}.json"),
        "pair_id": pair_id,
        "condition_id": condition_id,
        "group_id": _group_for_pair(pair_index),
        "l1_pass": True,
        "l2_pass": True,
        "l3_pass": True,
        "l4_pass": None,
        "l3_answerable": True,
        "required_slots": 2,
        "covered_slots": 2,
        "safety_violations": [],
    }
    record.update(overrides)
    return record


def _graded_query(
    *,
    host: str = "hermes_a2a",
    arm: str = "C",
    pair_index: int = 1,
    condition_id: int = 1,
    attempt: int = 1,
    **overrides,
) -> dict:
    pair_id = _pair_id(pair_index)
    unit_id = f"query-{host}-{arm}-{pair_id}-c{condition_id}-a{attempt}"
    record = {
        "run_id": "TEST-run-001",
        "attempt": attempt,
        "kind": "query",
        "arm_id": arm,
        "host_id": host,
        "unit_id": unit_id,
        "status": "graded",
        "reason": None,
        "evidence": _evidence(f"query/{unit_id}.json"),
        "pair_id": pair_id,
        "condition_id": condition_id,
        "group_id": _group_for_pair(pair_index),
        "l1_pass": True,
        "l2_pass": True,
        "l3_pass": True,
        "l4_pass": True,
        "l3_answerable": True,
        "required_slots": 1,
        "covered_slots": 1,
        "safety_violations": [],
    }
    record.update(overrides)
    return record


def _graded_journey(
    *,
    host: str = "hermes_a2a",
    arm: str = "C",
    journey_id: str = "J01",
    attempt: int = 1,
    **overrides,
) -> dict:
    unit_id = f"journey-{host}-{arm}-{journey_id}-a{attempt}"
    record = {
        "run_id": "TEST-run-001",
        "attempt": attempt,
        "kind": "journey",
        "arm_id": arm,
        "host_id": host,
        "unit_id": unit_id,
        "status": "graded",
        "reason": None,
        "evidence": _evidence(f"journey/{unit_id}.json"),
        "journey_id": journey_id,
        "l1_pass": True,
        "l2_pass": True,
        "l3_pass": True,
        "l4_pass": True,
        "safety_violations": [],
        "all_required_steps_pass": True,
        "primary_rounds": 6,
    }
    record.update(overrides)
    return record


def _unsupported_core(arm: str = "B", pair_index: int = 1, condition_id: int = 1, attempt: int = 1) -> dict:
    pair_id = _pair_id(pair_index)
    unit_id = f"core-{arm}-{pair_id}-c{condition_id}-a{attempt}"
    return {
        "run_id": "TEST-run-001",
        "attempt": attempt,
        "kind": "core",
        "arm_id": arm,
        "host_id": None,
        "unit_id": unit_id,
        "status": "unsupported",
        "reason": "baseline_578b_unrunnable",
        "evidence": _evidence(f"core/{unit_id}.json"),
        "pair_id": pair_id,
        "condition_id": condition_id,
        "group_id": _group_for_pair(pair_index),
        "capability_proof": {"schema": "scope-recall.p18-capability-proof.v1", "run_id": "TEST-run-001", "attempt": attempt, "arm_id": arm, "host_id": None, "unit_id": unit_id, "capability_supported": False},
    }


def _unsupported_query(host: str, arm: str, pair_index: int, condition_id: int, attempt: int = 1) -> dict:
    pair_id = _pair_id(pair_index)
    unit_id = f"query-{host}-{arm}-{pair_id}-c{condition_id}-a{attempt}"
    return {
        "run_id": "TEST-run-001",
        "attempt": attempt,
        "kind": "query",
        "arm_id": arm,
        "host_id": host,
        "unit_id": unit_id,
        "status": "unsupported",
        "reason": "baseline_578b_unrunnable",
        "evidence": _evidence(f"query/{unit_id}.json"),
        "pair_id": pair_id,
        "condition_id": condition_id,
        "group_id": _group_for_pair(pair_index),
        "capability_proof": {"schema": "scope-recall.p18-capability-proof.v1", "run_id": "TEST-run-001", "attempt": attempt, "arm_id": arm, "host_id": host, "unit_id": unit_id, "capability_supported": False},
    }


def _unsupported_journey(host: str, arm: str, journey_id: str, attempt: int = 1) -> dict:
    unit_id = f"journey-{host}-{arm}-{journey_id}-a{attempt}"
    return {
        "run_id": "TEST-run-001",
        "attempt": attempt,
        "kind": "journey",
        "arm_id": arm,
        "host_id": host,
        "unit_id": unit_id,
        "status": "unsupported",
        "reason": "baseline_578b_unrunnable",
        "evidence": _evidence(f"journey/{unit_id}.json"),
        "journey_id": journey_id,
        "capability_proof": {"schema": "scope-recall.p18-capability-proof.v1", "run_id": "TEST-run-001", "attempt": attempt, "arm_id": arm, "host_id": host, "unit_id": unit_id, "capability_supported": False},
    }


def _build_complete_inventory(*, attempt: int = 1, arm_c_pass: bool = True) -> list[dict]:
    records: list[dict] = []
    for arm in report.ARMS:
        for pair_index in range(1, report.CORE_PAIRS + 1):
            for condition_id in report.CONDITIONS:
                if arm == "B":
                    records.append(_unsupported_core(arm=arm, pair_index=pair_index, condition_id=condition_id, attempt=attempt))
                else:
                    l3_pass = arm_c_pass or arm != "C"
                    records.append(
                        _graded_core(
                            arm=arm,
                            pair_index=pair_index,
                            condition_id=condition_id,
                            attempt=attempt,
                            l3_pass=l3_pass,
                            covered_slots=2 if l3_pass else 0,
                        )
                    )
        for host in report.HOSTS:
            for pair_index in QUERY_PAIR_INDICES:
                for condition_id in report.CONDITIONS:
                    if arm == "B":
                        records.append(_unsupported_query(host, arm, pair_index, condition_id, attempt))
                    else:
                        l4_pass = arm_c_pass or arm != "C"
                        records.append(
                            _graded_query(
                                host=host,
                                arm=arm,
                                pair_index=pair_index,
                                condition_id=condition_id,
                                attempt=attempt,
                                l4_pass=l4_pass,
                            )
                        )
            for journey_index in range(1, report.JOURNEYS_PER_HOST_ARM + 1):
                journey_id = f"J{journey_index:02d}"
                if arm == "B":
                    records.append(_unsupported_journey(host, arm, journey_id, attempt))
                else:
                    records.append(_graded_journey(host=host, arm=arm, journey_id=journey_id, attempt=attempt))
    return records


def _validate_records(records: list[dict]) -> list[dict]:
    return [report.validate_record(item, line_no=index, expected_run_id="TEST-run-001") for index, item in enumerate(records, start=1)]


def _build_report(records: list[dict], **metadata_overrides) -> dict:
    metadata = _metadata_with_annotation_hash(records)
    metadata.update(metadata_overrides)
    return report.build_report(metadata, _validate_records(records))


def test_bool_coercion_rejected() -> None:
    bad = _graded_core(l1_pass=1)
    with pytest.raises(report.AnnotationSchemaError, match="l1_pass_must_be_strict_boolean"):
        report.validate_record(bad, line_no=1, expected_run_id="TEST-run-001")


def test_missing_condition_blocks_inventory() -> None:
    records = _validate_records([_graded_core(pair_index=1, condition_id=1)])
    errors = report._inventory_errors_for_attempt(records, 1, IDENTITY_MAP)
    assert any("missing_expected_unit" in item for item in errors)
    result = _build_report([_graded_core(pair_index=1, condition_id=1)])
    assert result["aggregation_complete"] is False
    assert result["attempts"]["1"]["inventory"]["complete"] is False


def test_duplicate_first_run_rejected_in_inventory() -> None:
    dup = _graded_core(pair_index=1, condition_id=1)
    records = _validate_records([dup, dict(dup)])
    errors = report._inventory_errors_for_attempt(records, 1, IDENTITY_MAP)
    assert any("duplicate_unit" in item for item in errors)


def test_rerun_does_not_hide_first_failure() -> None:
    first = _graded_core(pair_index=1, condition_id=1, attempt=1, l3_pass=False, covered_slots=0)
    second_condition = _graded_core(pair_index=1, condition_id=2, attempt=1)
    rerun = _graded_core(pair_index=1, condition_id=1, attempt=2, unit_suffix="core-C-TEST-pair-001-c1-a2")
    records = [first, second_condition, rerun]
    result = _build_report(records)
    assert "1" in result["attempts"]
    assert "2" in result["attempts"]
    assert result["attempts"]["1"]["arms"]["C"]["core"]["l3"]["overall"]["independent_pair_level"]["numerator"] == 0
    assert result["attempts"]["2"]["arms"]["C"]["core"]["l3"]["overall"]["independent_pair_level"]["numerator"] == 0
    assert result["first_run_c_threshold"]["blocked"] is True
    assert result["attempts"]["1"]["c_threshold"]["blocked"] is True


def test_unsupported_baseline_has_no_invented_score() -> None:
    records = _validate_records([_unsupported_core()])
    summary = report.build_attempt_report(records, 1, IDENTITY_MAP)
    core = summary["arms"]["B"]["core"]
    assert core["baseline_unsupported"] is True
    assert core["invented_success_score"] is False
    assert "layers" not in core
    assert summary["c_threshold"]["baseline_b_failures_block_c_threshold"] is False


def test_zero_answerable_denominator_blocks_c_threshold() -> None:
    records = _validate_records(
        [
            _graded_core(pair_index=1, condition_id=1, l3_answerable=False),
            _graded_core(pair_index=1, condition_id=2, l3_answerable=False),
        ]
    )
    attempt = report.build_attempt_report(records, 1, IDENTITY_MAP)
    assert attempt["c_threshold"]["l3_overall"]["denominator"] == 1
    assert attempt["c_threshold"]["l3_overall"]["zero_denominator_blocks"] is False
    assert "l3_slot_coverage_zero_answerable_denominator" in attempt["c_threshold"]["blockers"]
    assert attempt["c_threshold"]["blocked"] is True


def test_safety_violation_blocks_c_threshold() -> None:
    records = _validate_records(
        [
            _graded_core(pair_index=1, condition_id=1, safety_violations=["deletion_assertion_failed"]),
            _graded_core(pair_index=1, condition_id=2),
        ]
    )
    attempt = report.build_attempt_report(records, 1, IDENTITY_MAP)
    assert attempt["safety"]["c_arm_blocked"] is True
    assert attempt["c_threshold"]["safety_blocks"] is True


def test_wilson_known_boundaries() -> None:
    assert report.wilson_interval(0, 0) is None
    interval = report.wilson_interval(5, 10)
    assert interval is not None
    low, high = interval
    assert 0.0 <= low < 0.5 < high <= 1.0
    assert math.isclose(low, 0.236, abs_tol=0.02)
    assert math.isclose(high, 0.764, abs_tol=0.02)


def test_condition_and_pair_denominators_differ() -> None:
    records = _validate_records(
        [
            _graded_core(pair_index=1, condition_id=1, l3_pass=True),
            _graded_core(pair_index=1, condition_id=2, l3_pass=False),
            _graded_core(pair_index=2, condition_id=1, l3_pass=True),
            _graded_core(pair_index=2, condition_id=2, l3_pass=True),
        ]
    )
    metrics = report._aggregate_condition_and_pair_metrics(records, layer="l3_pass", pair_filter=report._answerable_pair_filter)
    assert metrics["condition_level"]["denominator"] == 4
    assert metrics["condition_level"]["numerator"] == 3
    assert metrics["independent_pair_level"]["denominator"] == 2
    assert metrics["independent_pair_level"]["numerator"] == 1


def test_tampered_evidence_hash_rejected(tmp_path: Path) -> None:
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    record = _graded_core()
    rel = record["evidence"][0]["path"]
    blob = evidence_root / rel
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text(json.dumps({"run_id": "TEST-run-001", "attempt": 1, "operation_id": "public-op", "unit_id": record["unit_id"], "host_id": None, "arm_id": "C"}), encoding="utf-8")
    actual = hashlib.sha256(blob.read_bytes()).hexdigest()
    record["evidence"] = [{"path": rel, "sha256": "c" * 64}]
    records = _validate_records([record])
    with pytest.raises(report.EvidenceValidationError, match="evidence_hash_mismatch"):
        report.build_report(_metadata_with_annotation_hash(records), records, evidence_root=evidence_root, validate_evidence=True)
    record["evidence"] = [{"path": rel, "sha256": actual}]
    records = _validate_records([record])
    report.build_report(_metadata_with_annotation_hash(records), records, evidence_root=evidence_root, validate_evidence=True)


def test_complete_inventory_passing_c_threshold() -> None:
    records = _build_complete_inventory(attempt=1, arm_c_pass=True)
    result = _build_report(records)
    assert result["attempts"]["1"]["inventory"]["complete"] is True
    assert result["aggregation_complete"] is True
    assert result["declares_formal_evaluation_complete"] is False
    assert result["attempts"]["1"]["c_threshold"]["blocked"] is False
    l3 = result["attempts"]["1"]["arms"]["C"]["core"]["l3"]["overall"]["independent_pair_level"]
    assert l3["denominator"] == report.CORE_PAIRS
    assert l3["numerator"] == report.CORE_PAIRS
    for host in report.HOSTS:
        gate = result["attempts"]["1"]["arms"]["C"]["hosts"][host]["query"]["l4_pair_gate"]
        assert gate["numerator"] == report.L4_QUERY_PAIR_DENOMINATOR
        assert gate["passes_threshold"] is True


def test_subset_input_not_marked_complete() -> None:
    result = _build_report(_build_complete_inventory()[:100])
    assert result["aggregation_complete"] is False


def test_wrong_protocol_or_method_rejected(tmp_path: Path) -> None:
    records = _build_complete_inventory()[:4]
    metadata = _metadata_with_annotation_hash(records)
    metadata["protocol_sha256"] = "1" * 64
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(report.AnnotationSchemaError, match="protocol_sha256_mismatch"):
        report.load_bundle_metadata(metadata_path)


def test_identity_map_and_frozen_query_subset_required() -> None:
    records = _build_complete_inventory()
    for record in records:
        if record["kind"] == "query" and record["pair_id"] == _pair_id(3):
            record["pair_id"] = _pair_id(3)
    bad_query = deepcopy(records)
    for record in bad_query:
        if record["kind"] == "query" and record["arm_id"] == "C" and record["host_id"] == "hermes_a2a":
            new_pair = "outside-core-TEST-pair-999"
            record["pair_id"] = new_pair
            record["unit_id"] = f"query-hermes_a2a-C-{new_pair}-c{record['condition_id']}-a1"
            record["evidence"] = _evidence(f"query/{record['unit_id']}.json")
    result = _build_report(bad_query)
    assert result["aggregation_complete"] is False
    assert any("query_pair_not_in_frozen_subset" in err or "unknown_pair_id" in err for err in result["attempts"]["1"]["inventory"]["errors"])


def test_cli_validate_only_emits_aggregate_summary(tmp_path: Path) -> None:
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    metadata_path = tmp_path / "metadata.json"
    annotations_path = tmp_path / "annotations.jsonl"
    records = _build_complete_inventory()[:4]
    annotations_path.write_bytes(_annotation_bytes(records))
    metadata = _metadata_with_annotation_hash(records)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    exit_code = report.main(["--metadata", str(metadata_path), "--annotations", str(annotations_path)])
    assert exit_code == 0


def test_report_written_with_utf8_hash(tmp_path: Path) -> None:
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    records_raw = _build_complete_inventory()
    for item in records_raw:
        rel = item["evidence"][0]["path"]
        target = evidence_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"run_id": item["run_id"], "attempt": item["attempt"], "operation_id": item["unit_id"] + "-op", "unit_id": item["unit_id"], "host_id": item["host_id"], "arm_id": item["arm_id"], "capability_supported": False if item["status"] == "unsupported" else True}, sort_keys=True).encode("utf-8")
        target.write_bytes(payload)
        item["evidence"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
    metadata = _metadata_with_annotation_hash(records_raw)
    metadata_path = tmp_path / "metadata.json"
    annotations_path = tmp_path / "annotations.jsonl"
    output_path = tmp_path / "report.json"
    annotations_path.write_bytes(_annotation_bytes(records_raw))
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    exit_code = report.main(
        [
            "--metadata",
            str(metadata_path),
            "--annotations",
            str(annotations_path),
            "--evidence-root",
            str(evidence_root),
            "--output",
            str(output_path),
        ]
    )
    assert exit_code == 0
    written = output_path.read_bytes()
    assert output_path.read_text(encoding="utf-8") == written.decode("utf-8")
    assert hashlib.sha256(written).hexdigest()


# --- Nine public audit reproduction regressions ---


def _audit_case(mutate) -> dict:
    records = _build_complete_inventory()
    mutate(records)
    try:
        _validate_records(records)
        result = _build_report(records)
        first = result["attempts"]["1"]
        return {
            "aggregation_complete": result["aggregation_complete"],
            "inventory_complete": first["inventory"]["complete"],
            "c_threshold_blocked": first["c_threshold"]["blocked"],
            "blockers": first["c_threshold"]["blockers"],
            "inventory_errors": first["inventory"]["errors"],
            "lower_layer_gap_count": len(first["l1_l2_evidence_gaps"]),
        }
    except report.AnnotationSchemaError as exc:
        return {"validation_error": str(exc)}


def test_regression_c_all_l1_l2_false_query_l3_unknown() -> None:
    def mutate(records):
        for record in records:
            if record["arm_id"] == "C":
                record["l1_pass"] = False
                record["l2_pass"] = False
                if record["kind"] == "query":
                    record["l3_pass"] = None

    outcome = _audit_case(mutate)
    assert outcome["aggregation_complete"] is False or outcome["c_threshold_blocked"] is True
    assert outcome["lower_layer_gap_count"] > 0


def test_regression_c_all_l1_l2_unknown() -> None:
    def mutate(records):
        for record in records:
            if record["arm_id"] == "C":
                record["l1_pass"] = None
                record["l2_pass"] = None

    outcome = _audit_case(mutate)
    assert outcome["c_threshold_blocked"] is True


def test_regression_supported_a_d_entirely_not_run() -> None:
    def mutate(records):
        for record in records:
            if record["arm_id"] in {"A", "D"}:
                record["status"] = "not_run"
                record["reason"] = "public synthetic fixture: supported baseline never executed"

    outcome = _audit_case(mutate)
    assert outcome["aggregation_complete"] is False
    assert outcome["inventory_complete"] is False
    assert any("not_run_incomplete" in err or "supported_baseline_not_run" in err for err in outcome["inventory_errors"])


def test_regression_c_covered_slots_zero() -> None:
    def mutate(records):
        for record in records:
            if record["arm_id"] == "C" and record["kind"] == "core":
                record["covered_slots"] = 0

    outcome = _audit_case(mutate)
    assert outcome["c_threshold_blocked"] is True
    assert any("slot_coverage" in blocker for blocker in outcome["blockers"])


def test_regression_core_groups_lopsided() -> None:
    def mutate(records):
        for record in records:
            if record["status"] != "graded" or record["kind"] != "core":
                continue
            idx = int(record["pair_id"].rsplit("-", 1)[1])
            if idx > 40:
                record["group_id"] = "B05" if idx <= 113 else f"B{idx - 108:02d}"

    outcome = _audit_case(mutate)
    assert outcome["aggregation_complete"] is False
    assert any("group_class_count" in err or "group_id_mismatch" in err for err in outcome["inventory_errors"])


def test_regression_arm_host_pairs_differ() -> None:
    def mutate(records):
        for record in records:
            if record["status"] != "graded":
                continue
            if record["arm_id"] == "D" and record["kind"] == "core":
                record["pair_id"] = "different-dataset-" + record["pair_id"]
            if record["kind"] == "query":
                record["pair_id"] = f"outside-core-{record['host_id']}-{record['arm_id']}-" + record["pair_id"]

    outcome = _audit_case(mutate)
    assert outcome["aggregation_complete"] is False


def test_regression_c_108_unanswerable_pairs() -> None:
    def mutate(records):
        for record in records:
            if record["arm_id"] == "C" and record["kind"] == "core":
                idx = int(record["pair_id"].rsplit("-", 1)[1])
                if idx % 10 != 1:
                    record["l3_answerable"] = False
                    record["l3_pass"] = False

    outcome = _audit_case(mutate)
    assert outcome["c_threshold_blocked"] is True


def test_regression_default_fixture_uses_frozen_query_subset() -> None:
    records = _build_complete_inventory()
    query_pairs = {record["pair_id"] for record in records if record["kind"] == "query"}
    assert query_pairs == set(IDENTITY_MAP["query_pair_ids"])
    outcome = _audit_case(lambda _: None)
    assert outcome["aggregation_complete"] is True


def test_regression_cli_wrong_hashes_rejected(tmp_path: Path) -> None:
    evidence = tmp_path / "public-evidence.json"
    evidence.write_text('{"fixture":"public synthetic evidence"}\n', encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    records = _build_complete_inventory()
    for record in records:
        record["evidence"] = [{"path": evidence.name, "sha256": digest}]
    annotations_path = tmp_path / "annotations.jsonl"
    annotations_path.write_bytes(_annotation_bytes(records))
    metadata = _metadata(
        annotation_input_sha256="0" * 64,
        protocol_sha256="1" * 64,
        method_ids={"hermes_a2a": "unapproved-entry", "codex_windows_desktop": "123"},
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    exit_code = report.main(
        [
            "--metadata",
            str(metadata_path),
            "--annotations",
            str(annotations_path),
            "--evidence-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "report.json"),
        ]
    )
    assert exit_code == 2


def test_p1_unknown_supported_query_and_journey_are_incomplete() -> None:
    records = _build_complete_inventory()
    for record in records:
        if record["arm_id"] in {"A", "D"} and record["kind"] == "query":
            record["l4_pass"] = None
        if record["arm_id"] in {"A", "D"} and record["kind"] == "journey":
            record["l1_pass"] = record["l2_pass"] = record["l3_pass"] = record["l4_pass"] = None
    result = _build_report(records)
    assert result["aggregation_complete"] is False
    assert any("unknown_layer" in item for item in result["attempts"]["1"]["inventory"]["errors"])


def test_p1_negative_refusal_is_scored_and_kept() -> None:
    records = _build_complete_inventory()
    for record in records:
        if record["arm_id"] == "C" and record["kind"] == "core" and PAIR_LOOKUP[record["pair_id"]]["core_class"] == "negative":
            record["l3_answerable"] = False
            record["l3_pass"] = True
            record["required_slots"] = record["covered_slots"] = 0
    result = _build_report(records)
    first = result["attempts"]["1"]
    assert not any("abstention_assertion_failed" in item for item in first["inventory"]["errors"])
    assert first["arms"]["C"]["core"]["l3"]["overall"]["independent_pair_level"]["denominator"] == report.CORE_PAIRS


def test_p1_safety_results_are_retained_for_every_arm() -> None:
    records = _build_complete_inventory()
    for record in records:
        if record["arm_id"] == "D":
            record["safety_violations"] = ["authorization_violation"]
    result = _build_report(records)
    safety = result["attempts"]["1"]["safety"]
    assert any("authorization_violation" in item for item in safety["violations_by_arm"]["D"])
    assert result["attempts"]["1"]["c_threshold"]["blocked"] is False


def test_p1_unsupported_supported_baseline_requires_bound_proof() -> None:
    records = _build_complete_inventory()
    for record in records:
        if record["arm_id"] == "A":
            record["status"] = "unsupported"
            record["reason"] = "unverified claim"
            record.pop("capability_proof", None)
    result = _build_report(records)
    assert result["aggregation_complete"] is False
    assert any("unsupported_capability_proof_unverified" in item for item in result["attempts"]["1"]["inventory"]["errors"])


def test_p1_opaque_sidecar_preserves_real_ids_and_journey_groups() -> None:
    sidecar = Path(r"F:\SCOPERECALL更新项目\worktrees\scope-recall-runtime-integration\.execution\TEST-P18-SEALED-RUN-PLAN-v7\opaque-allocation-identity.json")
    identity = report.load_identity_map({"identity_map_sha256": report.OPAQUE_IDENTITY_SHA256}, sidecar, expected_sha256=report.OPAQUE_IDENTITY_SHA256)
    assert len(identity["expected_units"]) == 1600
    assert len(identity["journey_operation_groups"]) == 64
    key = ("core", "A", None, "P001", 1)
    assert identity["expected_units"][key]["unit_id"] == "core-A-001"
    assert len(identity["journey_operation_groups"][("hermes_a2a", "C", "J01")]) > 1
    record = _graded_core(unit_suffix="arbitrary-unit")
    record.update({"pair_id": "P001", "group_id": "B01"})
    normalized = _validate_records([record])
    errors = report._inventory_errors_for_attempt(normalized, 1, identity)
    assert any("unit_id_mismatch" in item for item in errors)


def test_p1_wrong_runtime_association_rejected(tmp_path: Path) -> None:
    record = _graded_core()
    target = tmp_path / record["evidence"][0]["path"]
    target.parent.mkdir(parents=True)
    payload = {"run_id": "OTHER-RUN", "attempt": 999, "operation_id": "OTHER-OP", "unit_id": "OTHER-UNIT", "host_id": "OTHER-HOST", "arm_id": "OTHER-ARM", "capability_supported": True}
    target.write_text(json.dumps(payload), encoding="utf-8")
    record["evidence"][0]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    records = _validate_records([record])
    with pytest.raises(report.EvidenceValidationError, match="evidence_association_mismatch"):
        report.build_report(_metadata_with_annotation_hash(records), records, evidence_root=tmp_path, validate_evidence=True)


def test_v3_failed_conditions_remain_in_group_denominator() -> None:
    records = _build_complete_inventory()
    for record in records:
        if record["arm_id"] == "C" and record["kind"] == "core" and record["pair_id"] in {"TEST-pair-005", "TEST-pair-006", "TEST-pair-007"}:
            record["status"] = "failed"
            record["reason"] = "public synthetic observed execution failure"
    result = _build_report(records)
    group = result["attempts"]["1"]["c_threshold"]["l3_by_group"]["B01"]
    assert group["denominator"] == 10
    assert group["numerator"] == 7
    assert group["passes_threshold"] is False
    assert result["attempts"]["1"]["c_threshold"]["blocked"] is True


def test_v3_runtime_binding_stays_pending_until_composer_receipt() -> None:
    records = _build_complete_inventory()
    result = _build_report(records)
    assert result["binding_verified"] is False
    assert result["binding_status"] == "pending_formal_composer_binding"


def test_v3_sidecar_hash_must_match_metadata_declaration() -> None:
    sidecar = Path(r"F:\SCOPERECALL更新项目\worktrees\scope-recall-runtime-integration\.execution\TEST-P18-SEALED-RUN-PLAN-v7\opaque-allocation-identity.json")
    with pytest.raises(report.AnnotationSchemaError, match="identity_sidecar_metadata_sha256_mismatch"):
        report.load_identity_map(
            {"identity_map_sha256": "0" * 64},
            sidecar,
            expected_sha256=report.OPAQUE_IDENTITY_SHA256,
        )
