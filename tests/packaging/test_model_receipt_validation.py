from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check  # noqa: E402
from model_receipt_evidence import CAPS, ledger_totals  # noqa: E402


def test_go_authorization_caps_are_hash_bound_and_leave_codex_unchanged(tmp_path, monkeypatch):
    import model_receipt_evidence as evidence
    auth = tmp_path / "authorization.json"
    auth.write_text(json.dumps({"purchase_authorized": False, "metering_required": True}))
    policy = tmp_path / "budget.json"
    policy.write_text(json.dumps({"batch": "P18_EVALUATION", "cap_micro_usd": 10**12,
                                  "total_call_cap": 100000, "total_input_cap": 10**12,
                                  "total_output_cap": 10**12}))
    budget = {"authorization_override": {"authorization": _reference(auth), "frozen_budget": _reference(policy)}}
    with pytest.raises(ValueError, match="unapproved"):
        evidence.effective_budget_caps(budget, tmp_path)
    monkeypatch.setattr(evidence, "GO_AUTHORIZATION_SHA256", _reference(auth)["sha256"])
    result = evidence.effective_budget_caps(budget, tmp_path)
    assert result["go_calls"] == 100000
    assert {k: v for k, v in result.items() if k.startswith("codex_")} == {k: v for k, v in CAPS.items() if k.startswith("codex_")}
    policy.write_text("{}")
    with pytest.raises(ValueError):
        evidence.effective_budget_caps(budget, tmp_path)
    assert evidence.effective_budget_caps({}, tmp_path) == CAPS


def test_formal_budget_authorization_is_consistent_across_both_validators(tmp_path, monkeypatch):
    import model_receipt_evidence as evidence
    payload = _formal_receipt(tmp_path)
    auth = tmp_path / "authorization.json"
    auth.write_text(json.dumps({"purchase_authorized": False, "metering_required": True}))
    policy = tmp_path / "budget.json"
    policy.write_text(json.dumps({"batch": "P18_EVALUATION", "cap_micro_usd": 10**12,
                                  "total_call_cap": 100000, "total_input_cap": 10**12,
                                  "total_output_cap": 10**12}))
    monkeypatch.setattr(evidence, "GO_AUTHORIZATION_SHA256", _reference(auth)["sha256"])
    budget = payload["budget"]
    budget["authorization_override"] = {"authorization": _reference(auth), "frozen_budget": _reference(policy)}
    budget["caps"] = evidence.effective_budget_caps(budget, tmp_path)
    budget["original_ledger"]["caps"] = {"hermes_calls": 100000, "codex_calls": 1500, "shared_calls": 101500}
    _seal_reports(tmp_path, payload)
    receipt = tmp_path / "model-receipt.json"
    receipt.write_text(json.dumps(payload))
    assert check.validate_model_receipt(receipt)["status"] == "PASS"
    policy.write_text("{}")
    reasons = check.validate_model_receipt(receipt)["reasons"]
    assert "budget_authorization_binding_invalid" in reasons
    assert "original_ledger_evidence_invalid" in reasons


def _reference(path: Path) -> dict[str, str]:
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _seal_reports(tmp_path: Path, payload: dict) -> None:
    method_path = tmp_path / "method.json"
    method_path.write_bytes((ROOT / "verification/G0/g0-appserver-method-adjudication-v2.json").read_bytes())
    payload["method_adjudication"].update({
        "method_id": check.METHOD_ID, "artifact": _reference(method_path),
    })
    hermes_method_path = tmp_path / "hermes-method.json"
    hermes_method_path.write_bytes((ROOT / "verification/G0/g0-hermes-cli-method-adjudication-v1.json").read_bytes())
    payload["hermes_method"] = {
        "id": check.HERMES_METHOD_ID, "artifact": _reference(hermes_method_path),
    }
    evidence = tmp_path / "synthetic-host-evidence.json"
    evidence.write_text(json.dumps({"fixture": "synthetic unit test only"}), encoding="utf-8")
    gate_path = tmp_path / "g2-review.json"
    gate_path.write_text(json.dumps({
        "gate": "G2", "status": "PASS", "evidence_kind": "real",
        "kind": "independent_gate_review", "source": payload["source"],
        "protocol": payload["protocol"], "method_id": check.METHOD_ID,
        "hermes_method": payload["hermes_method"],
        "unresolved_p0_p1": [], "evidence": [_reference(evidence)],
    }), encoding="utf-8")
    payload["gates"]["G2"]["artifact"] = _reference(gate_path)
    report = {
        "schema": "scope-recall.p18-independent-aggregate.v1",
        "kind": "independent", "run_status": "COMPLETED", "reviewer": "synthetic-test-reviewer",
        "denominators": payload["scorer_report"]["denominators"],
        **{key: payload[key] for key in (
            "source", "protocol", "method_adjudication", "hermes_method", "gates", "coverage", "budget", "formal_execution",
        )},
    }
    report_path = tmp_path / "scorer-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    payload["scorer_report"]["artifact"] = _reference(report_path)


def _formal_receipt(tmp_path: Path) -> dict[str, object]:
    source_manifest = check._source_manifest()
    states = {
        f"{host}/{arm}": {
            "status": "PASS" if arm == "C" else "FAIL",
            "complete": True,
            "audited": True,
            "score": 0.12 if arm == "A" else None,
        }
        for host in check.P18_HOSTS
        for arm in check.P18_ARMS
    }
    states[f"{check.METHOD_ID}/B"]["status"] = "UNSUPPORTED"
    states[f"{check.METHOD_ID}/B"]["reason"] = "baseline plugin unsupported by Codex host"
    ledger = tmp_path / "original-ledger.sqlite3"
    with sqlite3.connect(ledger) as db:
        for table in ("requests", "codex_submissions"):
            db.execute(f"CREATE TABLE {table} (reserved_input INTEGER,reserved_output INTEGER,actual_input INTEGER,actual_output INTEGER,status TEXT,charge_micro_usd INTEGER)")
            db.executemany(f"INSERT INTO {table} VALUES (1,1,1,1,'ok',1)", [()] * 164)
    payload = {
        "schema": check.P18_FORMAL_RECEIPT_SCHEMA,
        "status": "PASS",
        "formal_execution": {"status": "COMPLETED", "completed": True},
        "source": {
            "commit": check._current_source_commit(),
            "source_inputs_sha256": check._source_inputs_sha256(source_manifest),
        },
        "protocol": {"sha256": check.P18_PROTOCOL_SHA256},
        "method_adjudication": {
            "status": "CONDITIONALLY_ACCEPTED_METHOD_REVISION",
            "protocol_sha256": check.P18_PROTOCOL_SHA256,
        },
        "gates": {
            "P18": {"status": "PASS", "completed": True},
            "G2": {"status": "PASS", "evidence_kind": "real"},
        },
        "coverage": {
            "hosts": list(check.P18_HOSTS),
            "arms": list(check.P18_ARMS),
            "denominators": {
                "independent_core": check.P18_INDEPENDENT_CORE,
                "paired_variants": check.P18_PAIRED_VARIANTS,
            },
            "independent_core": {"completed": check.P18_INDEPENDENT_CORE},
            "paired_variants": {"completed": check.P18_PAIRED_VARIANTS},
            "arm_states": states,
            "c_acceptance": {
                host: {
                    "queries": {
                        "denominator": check.P18_C_QUERY_DENOMINATOR,
                        "conditions_per_query": check.P18_C_QUERY_CONDITIONS,
                        "completed": check.P18_C_QUERY_DENOMINATOR,
                        "passed": check.P18_C_MIN_QUERY_PASS,
                    },
                    "journeys": {
                        "denominator": check.P18_C_JOURNEY_DENOMINATOR,
                        "rounds_per_journey": check.P18_C_JOURNEY_ROUNDS,
                        "completed": check.P18_C_JOURNEY_DENOMINATOR,
                        "passed": check.P18_C_MIN_JOURNEY_PASS,
                    },
                    "l3_necessary_evidence_coverage": 0.90,
                    "l3_group_coverage": {f"B{number:02d}": 0.85 for number in range(1, 13)},
                }
                for host in check.P18_HOSTS
            },
            "safety_failures": 0,
            "safety_failure_refs": [],
        },
        "budget": {
            "ledger_snapshot": _reference(ledger),
            "caps": CAPS,
            "accounted": ledger_totals(ledger),
            "codex_monetary_status": "unavailable",
            "original_ledger": {
                "status": "PASS",
                "primary_calls": 288,
                "source_load_calls": 8,
                "auxiliary_calls": 20,
                "tool_model_rounds": 12,
                "actual_total_calls": 328,
                "actual_calls_by_host": {host: 164 for host in check.P18_HOSTS},
                "caps": {
                    "hermes_calls": check.P18_HERMES_CALL_CAP,
                    "codex_calls": check.P18_CODEX_CALL_CAP,
                    "shared_calls": check.P18_SHARED_CALL_CAP,
                },
            }
        },
        "scorer_report": {
            "kind": "independent",
            "run_status": "COMPLETED",
            "denominators": {
                "independent_core": check.P18_INDEPENDENT_CORE,
                "paired_variants": check.P18_PAIRED_VARIANTS,
                "c_query_per_host": check.P18_C_QUERY_DENOMINATOR,
                "c_query_conditions": check.P18_C_QUERY_CONDITIONS,
                "c_journeys_per_host": check.P18_C_JOURNEY_DENOMINATOR,
                "c_rounds_per_journey": check.P18_C_JOURNEY_ROUNDS,
            },
        },
    }
    _seal_reports(tmp_path, payload)
    return payload


def test_model_receipt_requires_formal_p18_and_real_g2(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    payload["gates"] = {"P18": {"status": "PASS", "completed": True}}
    path = tmp_path / "model-receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = check.validate_model_receipt(path)

    assert result["status"] == "REJECTED"
    assert "G2_gate_not_PASS" in result["reasons"]


def test_model_receipt_is_bound_to_current_source_epoch(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    source = payload["source"]
    assert isinstance(source, dict)
    source["source_inputs_sha256"] = "0" * 64
    path = tmp_path / "model-receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = check.validate_model_receipt(path)

    assert result["status"] == "REJECTED"
    assert "source_inputs_sha256_mismatch" in result["reasons"]


def test_model_receipt_allows_a_low_baseline_and_b_codex_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "model-receipt.json"
    path.write_text(json.dumps(_formal_receipt(tmp_path)), encoding="utf-8")

    result = check.validate_model_receipt(path)

    assert result["status"] == "PASS"


def test_model_receipt_rejects_1152_as_independent_samples_and_missing_c_condition(
    tmp_path: Path,
) -> None:
    payload = _formal_receipt(tmp_path)
    coverage = payload["coverage"]
    assert isinstance(coverage, dict)
    coverage["denominators"]["independent_core"] = check.P18_PRIMARY_CALL_UPPER_BOUND
    coverage["c_acceptance"][check.HERMES_METHOD_ID]["queries"]["conditions_per_query"] = 1
    path = tmp_path / "model-receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = check.validate_model_receipt(path)

    assert result["status"] == "REJECTED"
    assert "independent_denominators_mismatch" in result["reasons"]
    assert f"c_query_acceptance_failed:{check.HERMES_METHOD_ID}" in result["reasons"]


def test_model_receipt_rejects_unrecorded_unknown_arm(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    coverage = payload["coverage"]
    assert isinstance(coverage, dict)
    coverage["arm_states"][f"{check.HERMES_METHOD_ID}/A"]["status"] = "UNKNOWN"
    path = tmp_path / "model-receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = check.validate_model_receipt(path)

    assert result["status"] == "REJECTED"
    assert f"arm_state_invalid:{check.HERMES_METHOD_ID}/A" in result["reasons"]


def test_current_epoch_formal_receipt_can_resolve_only_model_gate(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "model-receipt.json"
    path.write_text(json.dumps(_formal_receipt(tmp_path)), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["check.py", "--tier", "release", "--plan", "--model-receipt", str(path)],
    )

    assert check.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["model_receipt_status"] == "PASS"
    assert "model" not in output["missing_gates"]
    assert output["model_receipt"]["status"] == "PASS"


def _validate(tmp_path: Path, payload: dict) -> dict:
    path = tmp_path / "model-receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return check.validate_model_receipt(path)


def test_method_requires_frozen_adjudication_bytes_and_explicit_new_id(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    payload["method_adjudication"].pop("method_id")
    assert "method_artifact_binding_invalid" in _validate(tmp_path, payload)["reasons"]
    payload["method_adjudication"]["method_id"] = check.METHOD_ID
    method = tmp_path / "method.json"
    method.write_text(json.dumps({"method": {"id": check.METHOD_ID}}), encoding="utf-8")
    payload["method_adjudication"]["artifact"] = _reference(method)
    assert "method_artifact_binding_invalid" in _validate(tmp_path, payload)["reasons"]


def test_hermes_cli_requires_its_own_frozen_adjudication(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    payload["hermes_method"]["id"] = "hermes_a2a"
    assert "hermes_method_artifact_binding_invalid" in _validate(tmp_path, payload)["reasons"]
    payload["hermes_method"]["id"] = check.HERMES_METHOD_ID
    path = tmp_path / "hermes-method.json"
    path.write_text(json.dumps({"method": {"id": check.HERMES_METHOD_ID}}), encoding="utf-8")
    payload["hermes_method"]["artifact"] = _reference(path)
    assert "hermes_method_artifact_binding_invalid" in _validate(tmp_path, payload)["reasons"]


def test_g2_and_independent_aggregate_must_bind_hermes_revision(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    for filename, reference, reason in (
        ("g2-review.json", payload["gates"]["G2"], "G2_report_binding_invalid"),
        ("scorer-report.json", payload["scorer_report"], "independent_scorer_content_mismatch"),
    ):
        path = tmp_path / filename
        report = json.loads(path.read_bytes())
        report.pop("hermes_method")
        path.write_text(json.dumps(report), encoding="utf-8")
        reference["artifact"] = _reference(path)
        assert reason in _validate(tmp_path, payload)["reasons"]


def test_matching_placeholder_scorer_hash_does_not_prove_results(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    report = tmp_path / "scorer-report.json"
    report.write_text(json.dumps({"aggregate": "P18 independent scorer output"}), encoding="utf-8")
    payload["scorer_report"]["artifact"] = _reference(report)
    assert "independent_scorer_content_mismatch" in _validate(tmp_path, payload)["reasons"]


def test_complete_matching_scorer_cannot_omit_ten_behavior_groups(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    payload["coverage"]["c_acceptance"][check.HERMES_METHOD_ID]["l3_group_coverage"] = {"B01": 0.9, "B02": 0.9}
    _seal_reports(tmp_path, payload)
    result = _validate(tmp_path, payload)
    assert f"c_l3_group_coverage_failed:{check.HERMES_METHOD_ID}" in result["reasons"]
    assert "independent_scorer_content_mismatch" not in result["reasons"]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True])
def test_nonfinite_or_boolean_coverage_is_rejected(tmp_path: Path, invalid: object) -> None:
    payload = _formal_receipt(tmp_path)
    payload["coverage"]["c_acceptance"][check.HERMES_METHOD_ID]["l3_necessary_evidence_coverage"] = invalid
    _seal_reports(tmp_path, payload)
    assert _validate(tmp_path, payload)["status"] == "REJECTED"


@pytest.mark.parametrize("column,value", [
    ("charge_micro_usd", 1_000_000_000),
    ("actual_input", 1_000_000_000_000),
    ("actual_output", 1_000_000_000_000),
])
def test_matching_report_and_ledger_hash_cannot_exceed_budget(
    tmp_path: Path, column: str, value: int,
) -> None:
    payload = _formal_receipt(tmp_path)
    ledger = tmp_path / "original-ledger.sqlite3"
    with sqlite3.connect(ledger) as db:
        db.execute(f"UPDATE requests SET {column}=? WHERE rowid=1", (value,))
    payload["budget"]["ledger_snapshot"] = _reference(ledger)
    payload["budget"]["accounted"] = ledger_totals(ledger)
    _seal_reports(tmp_path, payload)
    result = _validate(tmp_path, payload)
    assert "original_ledger_evidence_invalid" in result["reasons"]
    assert "independent_scorer_content_mismatch" not in result["reasons"]


def test_claimed_real_g2_requires_bound_review_content(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    gate = tmp_path / "g2-review.json"
    gate.write_text(json.dumps({"status": "PASS", "evidence_kind": "real"}), encoding="utf-8")
    payload["gates"]["G2"]["artifact"] = _reference(gate)
    assert "G2_report_binding_invalid" in _validate(tmp_path, payload)["reasons"]


def test_legacy_desktop_label_does_not_substitute_for_new_method(tmp_path: Path) -> None:
    payload = _formal_receipt(tmp_path)
    payload["coverage"]["hosts"] = ["hermes_a2a", "codex_windows_desktop"]
    assert "host_or_arm_coverage_mismatch" in _validate(tmp_path, payload)["reasons"]


def test_native_aux_ledger_preserves_unknown_bounds_and_requires_authorization(tmp_path, monkeypatch):
    import model_receipt_evidence as evidence
    ledger = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE requests (reserved_input, reserved_output, actual_input, actual_output, status, charge_micro_usd)")
        db.execute("CREATE TABLE codex_submissions (reserved_input, reserved_output, actual_input, actual_output, status, batch)")
        db.execute("INSERT INTO codex_submissions VALUES (131072,3000000,NULL,NULL,'closed_usage_unknown_reserved','P18_NATIVE_A_AUX')")
        db.execute("INSERT INTO codex_submissions VALUES (10,20,NULL,NULL,'reserved','P18_EVALUATION')")
    totals = evidence.ledger_totals(ledger)
    assert totals["codex_calls"] == 1
    assert totals["codex_output_tokens"] == 20
    assert totals["codex_native_aux_output_tokens"] == 3000000
    assert evidence.effective_budget_caps({}, tmp_path)["codex_native_aux_calls"] == 0
    auth = tmp_path / "native.json"
    auth.write_text(json.dumps({"batch":"P18_NATIVE_A_AUX", "purchase_authorized":False,
        "metering_required":True, "unknown_usage_policy":"reservation_retained",
        "technical_caps":{"codex_native_aux_calls":100000,"codex_native_aux_input_tokens":10**12,"codex_native_aux_output_tokens":10**12}}))
    budget = {"native_aux_authorization":_reference(auth)}
    with pytest.raises(ValueError, match="unapproved"):
        evidence.effective_budget_caps(budget, tmp_path)
    monkeypatch.setattr(evidence,"NATIVE_AUX_AUTHORIZATION_SHA256",_reference(auth)["sha256"])
    caps = evidence.effective_budget_caps(budget, tmp_path)
    assert all(totals[key] <= cap for key, cap in caps.items())
    assert caps["codex_calls"] == 1500
    assert caps["codex_output_tokens"] == 2000000


def test_final_validator_accepts_separate_authorized_native_aux_without_changing_main_counts(tmp_path, monkeypatch):
    import model_receipt_evidence as evidence
    payload = _formal_receipt(tmp_path)
    ledger = tmp_path / "original-ledger.sqlite3"
    with sqlite3.connect(ledger) as db:
        db.execute("ALTER TABLE codex_submissions ADD COLUMN batch TEXT DEFAULT 'P18_EVALUATION'")
        db.execute("INSERT INTO codex_submissions VALUES (131072,3000000,NULL,NULL,'closed_usage_unknown_reserved',NULL,'P18_NATIVE_A_AUX')")
    auth = tmp_path / "native-auth.json"
    auth.write_text(json.dumps({"batch":"P18_NATIVE_A_AUX","purchase_authorized":False,
        "metering_required":True,"unknown_usage_policy":"reservation_retained",
        "technical_caps":{"codex_native_aux_calls":100000,"codex_native_aux_input_tokens":10**12,"codex_native_aux_output_tokens":10**12}}))
    monkeypatch.setattr(evidence,"NATIVE_AUX_AUTHORIZATION_SHA256",_reference(auth)["sha256"])
    budget = payload["budget"]
    budget["native_aux_authorization"] = _reference(auth)
    budget["caps"] = evidence.effective_budget_caps(budget,tmp_path)
    budget["accounted"] = evidence.ledger_totals(ledger)
    budget["ledger_snapshot"] = _reference(ledger)
    _seal_reports(tmp_path,payload)
    receipt = tmp_path / "model-receipt.json"
    receipt.write_text(json.dumps(payload))
    assert check.validate_model_receipt(receipt)["status"] == "PASS"
    del budget["native_aux_authorization"]
    _seal_reports(tmp_path,payload)
    receipt.write_text(json.dumps(payload))
    assert "original_ledger_evidence_invalid" in check.validate_model_receipt(receipt)["reasons"]
