import json

import pytest

from v11_support import ROOT, FixedInputs, public_cases, raw_case_inputs


def test_all_features_invariants_behaviors_have_public_synthetic_cases():
    mapping = json.loads((ROOT / "verification/functional_traceability.json").read_text(encoding="utf-8"))
    for key, prefix, count, field in (("features","F",9,"synthetic_cases"), ("invariants","I",15,"synthetic_cases"), ("behaviors","B",12,"cases")):
        assert {x["id"] for x in mapping[key]} == {f"{prefix}{i:02}" for i in range(1,count+1)}
        assert all(x[field] for x in mapping[key])
    assert all(b["v1_1_behavior_verification"] == "not_run" for b in mapping["behaviors"])


def test_public_specs_are_complete_and_not_reported_executed():
    for name, prefix, count in (("acceptance_cases.jsonl","C",40), ("cognitive_cases.jsonl","M",60), ("longitudinal_journeys.jsonl","J",8)):
        cases = public_cases(name)
        assert {c["id"] for c in cases} == {f"{prefix}{i:02}" for i in range(1,count+1)}
        assert all(c["dataset"] == "SYNTHETIC_TEST_ONLY" for c in cases)
        assert all(c["status"] == "specification_not_executed" for c in cases)


def test_inputs_exclude_gold_metadata_and_preserve_raw_negation(tmp_path):
    value = raw_case_inputs("M04", tmp_path, FixedInputs())
    assert set(value) == {"events", "query"}
    assert set(value["query"]) == {"session", "text"}
    assert value["events"][0]["content"] == "不是删除价格，本次对外稿只是不公开最低成交价。"
    assert value["events"][0]["occurred_at"] is None
    assert value["events"][0]["origin"] == "human_direct"
    assert not any(k in value for k in ("then", "must_not", "setup_note", "behavior", "claim_proposals"))


def test_raw_text_routes_all_validate_without_claim_preseeding(tmp_path):
    routes = json.loads((ROOT / "fixtures/input_routes.json").read_text(encoding="utf-8"))
    for case_id, route in routes["cognitive"].items():
        if route["input_status"] == "raw_text_ready":
            original = next(c for c in public_cases("cognitive_cases.jsonl") if c["id"] == case_id)
            value = raw_case_inputs(case_id, tmp_path, FixedInputs())
            assert [e["content"] for e in value["events"]] == [e["content"] for e in original["source_events"]]
            assert all(e["source_event_key"].startswith(f"TEST-{case_id}/") for e in value["events"])
        else:
            with pytest.raises(NotImplementedError, match="requires actual"):
                raw_case_inputs(case_id, tmp_path, FixedInputs())
        assert route["preseeded_claims"] is False
        assert set(route["layers"].values()) == {"not_executed"}


def test_tool_artifact_and_fault_scenarios_cannot_be_loaded_as_human_claims(tmp_path):
    for case_id in ("M09", "M13", "M42", "M47", "M58", "M59", "M60"):
        with pytest.raises(NotImplementedError):
            raw_case_inputs(case_id, tmp_path, FixedInputs())


def test_journey_controller_instructions_are_not_host_context():
    routes = json.loads((ROOT / "fixtures/input_routes.json").read_text(encoding="utf-8"))
    assert len(routes["journeys"]) == 8
    for route in routes["journeys"].values():
        assert route["input_status"] == "setup_required"
        assert route["instructions_are_test_controller_data"]
        assert len(route["host_runs"]) == 2
        assert "actual_consolidator_route" in route["required_evidence"]
