"""Offline contract tests; generated host receipts are fixtures, never evaluation samples."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import zipfile

import pytest

import p18_formal_evidence as evidence
from p18_codex_budget import CodexSubmissionBudget
from probes.eval_model_runtime import RuntimeLedger


def _bytes(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest()}


def _json(path, value):
    return _bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _bundle(tmp_path, monkeypatch, *, host=None, arm="C", unknown=False, count=2, ledger_path=None):
    host = host or evidence.METHOD_ID
    protocol = _json(tmp_path / "protocol.json", {"fixture": "public TEST protocol"})
    monkeypatch.setattr(evidence, "PROTOCOL_SHA256", protocol["sha256"])
    method = _json(tmp_path / "method.json", {"method": {"id": evidence.METHOD_ID}, "original_protocol": {"sha256": protocol["sha256"]}})
    monkeypatch.setattr(evidence, "METHOD_SHA256", method["sha256"])
    source = {"commit": "a" * 40, "source_inputs_sha256": "b" * 64}
    package_files = []
    with zipfile.ZipFile(tmp_path / "candidate.whl", "w") as wheel:
        for index in range(count):
            name, raw = f"module_{index}.py", f"# TEST {index}\n".encode()
            _bytes(tmp_path / "source" / name, raw)
            wheel.writestr("scope_recall/" + name, raw)
            package_files.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest()})
    wheel = {"path": "candidate.whl", "sha256": evidence._sha256(tmp_path / "candidate.whl")}
    build = _json(tmp_path / "build.json", {"source_commit": source["commit"], "wheel_sha256": wheel["sha256"], "package_files_checked": count, "package_files": package_files, "mismatches": []})
    measured = _json(tmp_path / "g2-evidence.json", {"fixture": "TEST independent host measurement"})
    gate = _json(tmp_path / "g2.json", {"gate": "G2", "status": "PASS", "kind": "independent_gate_review", "evidence_kind": "real", "source": source, "protocol": protocol, "method_id": evidence.METHOD_ID, "unresolved_p0_p1": [], "evidence": [measured]})
    unit = {"kind": "query", "query_id": "q01-answerable", "journey_id": None, "round_id": None}
    config = {"schema": evidence.CONFIG_SCHEMA, "protocol": protocol, "method": {"id": evidence.METHOD_ID, "artifact": method}, "source": source,
              "candidate": {"source_commit": source["commit"], "receipt": build, "wheel": wheel, "source_root": "source"},
              "g2_review": {"artifact": gate}, "ledger_path": "ledger.sqlite3",
              "operations": {"TEST-op-1": {"host_id": host, "arm_id": arm, "unit": unit, "request_id": "TEST-request-1"}}}
    request_raw = b'{"model":"glm-5.3-flash","messages":[{"role":"user","content":"TEST public input"}]}'
    model_request = _bytes(tmp_path / "model-request.json", request_raw)
    ledger = ledger_path if ledger_path is not None else tmp_path / "ledger.sqlite3"
    if host == "hermes_a2a":
        RuntimeLedger(ledger, "P18_EVALUATION")  # the existing real Go schema
        with sqlite3.connect(ledger) as db:
            db.execute("INSERT INTO requests(id,batch,model,body_sha256,request_bytes,reserved_input,reserved_output,actual_input,actual_output,charge_micro_usd,status,started_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (1, "P18_EVALUATION", "glm-5.3-flash", model_request["sha256"], len(request_raw), 32768, 4096, None if unknown else 12, None if unknown else 4, 100, "completed_usage_unknown_reserved_charge_retained" if unknown else "completed", 1))
        row_id = 1
    else:
        budget = CodexSubmissionBudget(ledger)
        budget.initialize_schema()
        budget.reserve("TEST-op-1", request_raw)
        budget.finish("TEST-op-1", "completed", None if unknown else {"input_tokens": 12, "output_tokens": 4})
        row_id = "TEST-op-1"
    record = {"operation_id": "TEST-op-1", "attempt": 1, "host_id": host, "arm_id": arm, "unit": unit,
              "ids": {"request_id": "TEST-request-1", "turn_id": "TEST-turn-1", "session_id": "TEST-session-1", "unknown": []},
              "source_capture_refs": ["TEST-source@1"] if arm == "C" else [],
              "delivery": {"status": "not_attempted", "context_sha256": None, "artifact_path": None},
              "answer": {"status": "available", "answer_sha256": hashlib.sha256(b"TEST answer").hexdigest(), "artifact_path": "answer.txt"},
              "usage": {"status": "unknown" if unknown else "known", "input_tokens": None if unknown else 12, "output_tokens": None if unknown else 4,
                        "ledger_path": ledger.name, "entries": [{"id": row_id, "request": model_request}]},
              "status": "COMPLETED", "latency_ms": 12.0, "no_retry": True}
    _bytes(tmp_path / "answer.txt", b"TEST answer")
    if host == "hermes_a2a":
        outer = {"jsonrpc": "2.0", "id": "TEST-request-1", "method": "message/send", "params": {"message": {"contextId": "TEST-context-1"}}}
        _bytes(tmp_path / "host-request.json", json.dumps(outer, ensure_ascii=False, separators=(",", ":")).encode())
        response = {"transport": "hermes_a2a_jsonrpc", "formal_evaluation": True, "fixture_mode": False,
                    "request": outer, "request_sha256": hashlib.sha256(json.dumps(outer, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
                    "status": "COMPLETED", "transport_status": "COMPLETED", "message_send_calls": 1, "retry_count": 0,
                    "task_id": "TEST-turn-1", "context_id": "TEST-context-1", "answer_text": "TEST answer", "answer_truncated": False}
    else:
        response = {"schema": "scope-recall.p18.codex-appserver-candidate-receipt.v1", "transport": "candidate-codex-app-server-stdio-jsonrpc", "formal_evaluation": True,
                    "process": {"pid": 42, "cleanup": "owned-exit"}, "arm": {"arm_id": arm}, "association": {"thread_id": "TEST-session-1", "turn_id": "TEST-turn-1"},
                    "turn": {"completed": {"status": "completed"}}, "errors": [], "io": {"stdout_truncated": False, "parse_error_count": 0}, "rpc_provenance": {"turn_rpcs_dispatched": 1}, "public_output": "TEST answer"}
    response_ref = _json(tmp_path / "response.json", response)
    record["host_response"] = {"response_sha256": response_ref["sha256"], "artifact_path": response_ref["path"], "http_status": 200 if host == "hermes_a2a" else None, "transport": response["transport"], "real_host": True}
    _associate(tmp_path, config, record)
    config_path = tmp_path / "config.json"
    _json(config_path, config)
    return config_path, config, record


def _associate(root, config, record):
    route = "go" if record["host_id"] == "hermes_a2a" else "codex"
    record["association"] = _json(root / "association.json", {"operation_id": record["operation_id"], "source": config["source"], "ids": record["ids"], "source_capture_refs": record["source_capture_refs"], "request_sha256s": [entry["request"]["sha256"] for entry in record["usage"]["entries"]], "ledger_entries": [{"route": route, "id": entry["id"], "request_sha256": entry["request"]["sha256"]} for entry in record["usage"]["entries"]], "context_id": "TEST-context-1"})
    if record["host_id"] == "hermes_a2a":
        association = json.loads((root / "association.json").read_text())
        association["host_request"] = {"path": "host-request.json", "sha256": evidence._sha256(root / "host-request.json")}
        record["association"] = _json(root / "association.json", association)


def _append(root, config_path, record):
    return evidence.FormalEvidenceWriter(root / "receipts", config_path).append_operation(record)


@pytest.mark.parametrize("count", [2, 110])
def test_pre_run_g2_needs_no_p18_aggregate_and_count_is_dynamic(tmp_path, monkeypatch, count):
    path, _, _ = _bundle(tmp_path, monkeypatch, count=count)
    result = evidence.verify_formal_run_config(path)
    assert result.formal_execution_allowed, result.reasons


@pytest.mark.parametrize("host", ["hermes_a2a", None])
@pytest.mark.parametrize("unknown", [False, True])
def test_both_original_ledger_schemas_complete_and_preserve_unknown(tmp_path, monkeypatch, host, unknown):
    path, _, record = _bundle(tmp_path, monkeypatch, host=host, unknown=unknown)
    saved = json.loads(_append(tmp_path, path, record).read_text())
    assert saved["status"] == "COMPLETED"
    assert "semantic_pass" not in saved
    assert saved["accounted_usage"]["effective_input_tokens"] == (32768 if unknown else 12)
    assert saved["run_binding"]["source"]["commit"] == "a" * 40


@pytest.mark.parametrize("host", ["hermes_a2a", None])
def test_baseline_without_scope_recall_delivery_still_has_real_execution(tmp_path, monkeypatch, host):
    path, _, record = _bundle(tmp_path, monkeypatch, host=host, arm="A")
    assert _append(tmp_path, path, record).is_file()


@pytest.mark.parametrize("mutation", ["missing", "source", "method", "artifact"])
def test_g2_fail_closed(tmp_path, monkeypatch, mutation):
    path, config, _ = _bundle(tmp_path, monkeypatch)
    gate_path = tmp_path / "g2.json"
    gate = json.loads(gate_path.read_text())
    if mutation == "missing":
        gate.pop("kind")
    elif mutation == "source":
        gate["source"]["commit"] = "f" * 40
    elif mutation == "method":
        gate["method_id"] = "codex_windows_desktop"
    else:
        (tmp_path / "g2-evidence.json").write_text("changed")
    config["g2_review"]["artifact"] = _json(gate_path, gate)
    _json(path, config)
    assert not evidence.verify_formal_run_config(path).formal_execution_allowed


def test_build_checks_source_and_wheel_bytes_without_status_field(tmp_path, monkeypatch):
    path, _, _ = _bundle(tmp_path, monkeypatch)
    (tmp_path / "source/module_0.py").write_text("changed")
    assert "candidate_source_package_hash_mismatch" in evidence.verify_formal_run_config(path).reasons


def test_ready_flags_cannot_authorize_writer_and_config_drift_rejects(tmp_path, monkeypatch):
    path, config, record = _bundle(tmp_path, monkeypatch)
    with pytest.raises(TypeError):
        evidence.FormalEvidenceWriter(tmp_path / "bad", {"status": "READY"})
    writer = evidence.FormalEvidenceWriter(tmp_path / "receipts", path)
    config["note"] = "changed"
    _json(path, config)
    with pytest.raises(evidence.FormalNotReady):
        writer.append_operation(record)


@pytest.mark.parametrize("mutation", ["no_ledger", "diagnostic", "no_response", "unknown_ids", "host", "arm", "answer", "source"])
def test_previous_fake_completion_counterexamples_reject(tmp_path, monkeypatch, mutation):
    path, config, record = _bundle(tmp_path, monkeypatch)
    if mutation == "no_ledger":
        record["usage"] = {"status": "not_applicable", "input_tokens": None, "output_tokens": None, "ledger_path": None, "entries": []}
    elif mutation == "diagnostic":
        record["status"] = "DIAGNOSTIC"
    elif mutation == "no_response":
        record["host_response"].update(artifact_path=None, response_sha256=None)
    elif mutation == "unknown_ids":
        record["ids"].update(turn_id=None, unknown=["turn_id"])
        _associate(tmp_path, config, record)
    elif mutation in {"host", "arm"}:
        record[mutation + "_id"] = "unfrozen"
    elif mutation == "answer":
        record["answer"]["answer_sha256"] = _bytes(tmp_path / "answer.txt", b"invented")["sha256"]
    else:
        association = json.loads((tmp_path / "association.json").read_text())
        association["source"]["commit"] = "f" * 40
        record["association"] = _json(tmp_path / "association.json", association)
    with pytest.raises(evidence.EvidenceSchemaError):
        _append(tmp_path, path, record)


@pytest.mark.parametrize("host", ["hermes_a2a", None])
def test_diagnostic_transport_receipt_cannot_be_upgraded(tmp_path, monkeypatch, host):
    path, _, record = _bundle(tmp_path, monkeypatch, host=host)
    response = json.loads((tmp_path / "response.json").read_text())
    response["formal_evaluation"] = False
    record["host_response"]["response_sha256"] = _json(tmp_path / "response.json", response)["sha256"]
    with pytest.raises(evidence.EvidenceSchemaError, match="diagnostic_transport"):
        _append(tmp_path, path, record)


def test_two_operation_ids_cannot_count_same_host_execution_twice(tmp_path, monkeypatch):
    path, config, record = _bundle(tmp_path, monkeypatch)
    second = copy.deepcopy(record)
    second.update(operation_id="TEST-op-2", unit={**record["unit"], "query_id": "q02-answerable"})
    second["ids"]["request_id"] = "TEST-request-2"
    config["operations"]["TEST-op-2"] = {"host_id": second["host_id"], "arm_id": second["arm_id"], "unit": second["unit"], "request_id": "TEST-request-2"}
    _json(path, config)
    _append(tmp_path, path, record)
    _associate(tmp_path, config, second)
    with pytest.raises(evidence.EvidenceSchemaError, match="already_recorded"):
        _append(tmp_path, path, second)


def test_unknown_requires_numeric_original_bounds_and_original_ledger(tmp_path, monkeypatch):
    path, _, record = _bundle(tmp_path, monkeypatch, unknown=True)
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        db.execute("UPDATE codex_submissions SET reserved_input=NULL")
    with pytest.raises(evidence.EvidenceSchemaError, match="unknown_upper_bound"):
        _append(tmp_path, path, record)


def test_artifacts_cannot_escape_bundle(tmp_path, monkeypatch):
    path, _, record = _bundle(tmp_path, monkeypatch)
    record["host_response"]["artifact_path"] = "../outside.json"
    with pytest.raises(evidence.EvidenceSchemaError, match="outside"):
        _append(tmp_path, path, record)


def test_diagnostic_is_append_only_and_not_semantic_pass(tmp_path):
    record = {"operation_id": "TEST-diagnostic", "attempt": 1, "host_id": "TEST-host", "arm_id": "C",
              "unit": {"kind": "query", "query_id": "TEST-query", "journey_id": None, "round_id": None},
              "ids": {"request_id": None, "turn_id": None, "session_id": None, "unknown": ["request_id", "turn_id", "session_id"]},
              "source_capture_refs": [], "association": None,
              "delivery": {"status": "not_attempted", "context_sha256": None, "artifact_path": None},
              "answer": {"status": "not_attempted", "answer_sha256": None, "artifact_path": None},
              "usage": None, "status": "DIAGNOSTIC", "latency_ms": 0, "no_retry": True,
              "host_response": {"response_sha256": None, "artifact_path": None, "http_status": None, "transport": "fixture", "real_host": False}}
    writer = evidence.FormalEvidenceWriter(tmp_path)
    saved = json.loads(writer.append_operation(record, diagnostic=True).read_text())
    assert saved["evidence_class"] == "diagnostic"
    with pytest.raises(evidence.EvidenceSchemaError, match="already_recorded"):
        writer.append_operation(record, diagnostic=True)
    record["status"] = "PASS"
    with pytest.raises(evidence.EvidenceSchemaError, match="status_invalid"):
        writer.append_operation(record, diagnostic=True)
