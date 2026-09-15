"""Offline tests for the P18 formal freeze tool; all fixtures, never evaluation samples."""
from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

import p18_formal_evidence as evidence
from p18_formal_freeze import FreezeError, build_frozen_config, main
from p18_sealed_run_plan import _project_model_input


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest()}


def _query_rows(host, arm):
    rows = []
    for pair in range(1, 41):
        for condition in (1, 2):
            unit_id = f"{host}-{arm}-query-{pair:02d}-c{condition}"
            records = [
                {"event_id": f"{unit_id}-E1", "sequence": 1, "speaker_role": "user",
                 "source_type": "human_direct", "text": f"PUBLIC TEST source {unit_id} one",
                 "occurred_at": "2026-01-01T00:00:00Z"},
                {"event_id": f"{unit_id}-E2", "sequence": 2, "speaker_role": "user",
                 "source_type": "human_direct", "text": f"PUBLIC TEST source {unit_id} two",
                 "occurred_at": "2026-01-01T00:01:00Z"},
            ]
            natural = _project_model_input({"history": records, "query": {"text": f"PUBLIC TEST query {unit_id}"}})
            rows.append({"unit_id": unit_id, "kind": "host_query", "ordinal": len(rows) + 1,
                         "source_sequence": "source_seed_then_new_session_query",
                         "model_input": natural})
    return rows


def _plan(tmp_path):
    plan = tmp_path / "plan"
    for host in ("hermes_a2a", "codex_windows_desktop"):
        rows = _query_rows(host, "A")
        target = plan / "units" / host / "A.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return plan


def _journey_bundle(tmp_path):
    natural = tmp_path / "journey-input.json"
    natural.write_text(json.dumps({"query": "PUBLIC TEST journey turn", "attachments": []}), encoding="utf-8")
    input_ref = {"path": natural.name, "sha256": hashlib.sha256(natural.read_bytes()).hexdigest()}
    bundle = tmp_path / "journeys.json"
    bundle.write_text(json.dumps({"schema": "scope-recall.p18-private-journey-execution.v1", "journeys": [{
        "journey_id": "J01", "actions": [
            {"operation_id": "turn-01", "kind": "host_turn", "source_step_orders": [1, 2, 3, 4, 5, 6],
             "primary_round_ordinal": 1, "parameters": {"input": input_ref}}],
    }]}), encoding="utf-8")
    return bundle


def _freeze_inputs(tmp_path, monkeypatch, *, g2_status="PASS", unresolved=None):
    protocol = _json(tmp_path / "protocol.json", {"fixture": "PUBLIC TEST protocol"})
    monkeypatch.setattr(evidence, "PROTOCOL_SHA256", protocol["sha256"])
    method = _json(tmp_path / "method.json", {"method": {"id": evidence.METHOD_ID},
                                              "original_protocol": {"sha256": protocol["sha256"]}})
    monkeypatch.setattr(evidence, "METHOD_SHA256", method["sha256"])
    hermes_method = _json(tmp_path / "hermes-method.json", {"method": {"id": evidence.HERMES_METHOD_ID},
                                                            "original_protocol": {"sha256": protocol["sha256"]}})
    monkeypatch.setattr(evidence, "HERMES_METHOD_SHA256", hermes_method["sha256"])
    commit = "a" * 40
    manifest = {"core/example.py": "git-blob:" + "c" * 40, "adapters/hermes/boundary.py": "git-blob:" + "d" * 40}
    manifest_path = tmp_path / "source-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source = {"commit": commit,
              "source_inputs_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}
    package_files = []
    source_root = tmp_path / "candidate-src"
    with zipfile.ZipFile(tmp_path / "candidate.whl", "w") as wheel:
        for index in range(2):
            name, raw = f"module_{index}.py", f"# PUBLIC TEST {index}\n".encode()
            target = source_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            wheel.writestr("scope_recall/" + name, raw)
            package_files.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest()})
    wheel_path = tmp_path / "candidate.whl"
    receipt = tmp_path / "build-receipt.json"
    receipt.write_text(json.dumps({"source_commit": commit, "wheel_sha256": hashlib.sha256(wheel_path.read_bytes()).hexdigest(),
                                   "package_files_checked": 2, "package_files": package_files, "mismatches": []}),
                       encoding="utf-8")
    measured = {"path": "artifacts/g2-evidence/00-g2-evidence.json",
                "sha256": _json(tmp_path / "g2-evidence.json", {"fixture": "PUBLIC TEST independent host measurement"})["sha256"]}
    staged_protocol = {"path": "artifacts/protocol.json", "sha256": protocol["sha256"]}
    gate = tmp_path / "g2.json"
    gate.write_text(json.dumps({"gate": "G2", "status": g2_status, "kind": "independent_gate_review",
                                "evidence_kind": "real", "source": source, "protocol": staged_protocol,
                                "method_id": evidence.METHOD_ID, "unresolved_p0_p1": unresolved or [],
                                "hermes_method": {"id": evidence.HERMES_METHOD_ID,
                                                   "artifact": {"path": "artifacts/hermes-method.json",
                                                                 "sha256": hermes_method["sha256"]}},
                                "evidence": [measured]}), encoding="utf-8")
    ledger = tmp_path / "original-call-budget.sqlite3"
    ledger.write_bytes(b"PUBLIC TEST ledger placeholder")
    return {
        "plan_root": _plan(tmp_path), "journey_bundle": _journey_bundle(tmp_path), "journey_ids": ["J01"],
        "protocol_artifact": tmp_path / "protocol.json", "method_artifact": tmp_path / "method.json",
        "hermes_method_artifact": tmp_path / "hermes-method.json", "candidate_receipt": receipt, "candidate_wheel": wheel_path,
        "candidate_source_root": source_root, "candidate_git_root": None, "source_commit": commit,
        "source_manifest": manifest_path, "g2_report": gate,
        "g2_evidence": [tmp_path / "g2-evidence.json"], "ledger": ledger,
        "output_dir": tmp_path / "freeze",
    }


def test_freeze_marks_missing_query_matrix_not_ready(tmp_path, monkeypatch):
    inputs = _freeze_inputs(tmp_path, monkeypatch)
    receipt = build_frozen_config(**inputs)
    assert receipt["status"] == "NOT_READY"
    assert "host_query_matrix_missing" in receipt["readiness"]["reasons"]
    config = json.loads((inputs["output_dir"] / "formal-run-config.json").read_text(encoding="utf-8"))
    operations = config["operations"]
    # 80 query conditions per host-arm file, two host-arms, plus one journey
    # turn per derived journey host-arm (hermes_a2a/A + codex METHOD/A).
    assert receipt["operation_count"] == 162
    assert len(operations) == 162
    query_ops = [op for op in operations.values() if op["unit"]["kind"] == "query"]
    journey_ops = [op for op in operations.values() if op["unit"]["kind"] == "round"]
    assert len(query_ops) == 160 and len(journey_ops) == 2
    hosts = {op["host_id"] for op in operations.values()}
    assert hosts == {evidence.HERMES_METHOD_ID, evidence.METHOD_ID}
    journey = [op for op in operations.values() if op["unit"]["kind"] == "round"][0]
    assert journey["unit"]["journey_id"] == "J01" and journey["unit"]["round_id"] == "1"
    assert config["ledger_path"] == str(inputs["ledger"].resolve())
    assert config["ledger_binding"]["canonical_path"] == str(inputs["ledger"].resolve())
    assert config["candidate"]["source_root"] == "artifacts/candidate-source"
    # The frozen directory is self-contained and re-verifies.
    readiness = evidence.verify_formal_run_config(inputs["output_dir"] / "formal-run-config.json")
    assert readiness.formal_execution_allowed, readiness.reasons


def test_freeze_refuses_existing_output(tmp_path, monkeypatch):
    inputs = _freeze_inputs(tmp_path, monkeypatch)
    inputs["output_dir"].mkdir(parents=True, exist_ok=True)
    (inputs["output_dir"] / "sentinel").write_text("x", encoding="utf-8")
    with pytest.raises(FreezeError, match="freeze_artifact_missing|freeze_output_exists"):
        build_frozen_config(**inputs)


def test_freeze_reports_not_ready_when_g2_unresolved(tmp_path, monkeypatch):
    inputs = _freeze_inputs(tmp_path, monkeypatch, unresolved=["P1-open"])
    receipt = build_frozen_config(**inputs)
    assert receipt["status"] == "NOT_READY"
    assert any("g2_p0_p1_unresolved" in reason for reason in receipt["readiness"]["reasons"])


def test_freeze_accepts_original_ledger_outside_output(tmp_path, monkeypatch):
    inputs = _freeze_inputs(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere.sqlite3"
    outside.write_bytes(b"PUBLIC TEST")
    inputs["ledger"] = outside
    receipt = build_frozen_config(**inputs)
    config = json.loads((inputs["output_dir"] / "formal-run-config.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "NOT_READY"
    assert config["ledger_path"] == str(outside.resolve())
    assert config["ledger_binding"]["canonical_path"] == str(outside.resolve())




def test_cli_missing_input_returns_error(tmp_path, capsys):
    code = main(["--plan-root", str(tmp_path / "nope"), "--journey-bundle", str(tmp_path / "nope.json"),
                 "--journey-ids", "J01", "--protocol-artifact", str(tmp_path / "p.json"),
                 "--method-artifact", str(tmp_path / "m.json"), "--candidate-receipt", str(tmp_path / "r.json"),
                 "--candidate-wheel", str(tmp_path / "w.whl"), "--candidate-source-root", str(tmp_path / "src"),
                 "--source-commit", "a" * 40, "--source-manifest", str(tmp_path / "manifest.json"),
                 "--g2-report", str(tmp_path / "g2.json"), "--ledger", str(tmp_path / "l.sqlite3"),
                 "--output", str(tmp_path / "out")])
    assert code == 2
    assert "FREEZE_ERROR" in capsys.readouterr().out
