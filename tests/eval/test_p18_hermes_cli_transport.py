"""Public offline fixtures: no CLI/model invocation and no evaluation samples."""

import copy
import hashlib
import json
import sys

import pytest

from p18_hermes_cli_transport import (
    HermesCLIError,
    METHOD_ID,
    TRANSPORT,
    cli_argv,
    parse_export,
    usage_delta,
    validate_cli_capture,
)


def _ref(root, name, raw):
    (root / name).write_bytes(raw)
    return {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}


def _capture(root):
    query = "TEST public input"
    query_ref = _ref(root, "query.txt", query.encode())
    request = {
        "id": "TEST-request-1",
        "query_file": str((root / "query.txt").resolve()),
        "query_sha256": query_ref["sha256"],
        "argv": cli_argv(sys.executable, "TEST-meter", root / "query.txt"),
    }
    request_ref = _ref(
        root,
        "cli-request.json",
        json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode(),
    )
    row = {
        "id": "TEST-session-1",
        "parent_session_id": None,
        "end_reason": "agent_close",
        "input_tokens": 12,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 4,
        "api_call_count": 1,
        "messages": [
            {"id": 1, "role": "user", "content": query},
            {"id": 2, "role": "assistant", "content": "TEST answer"},
        ],
    }
    exported = _ref(root, "after.jsonl", (json.dumps(row) + "\n").encode())
    return {
        "schema": "scope-recall.p18-hermes-cli.v1",
        "transport": TRANSPORT,
        "method_id": METHOD_ID,
        "formal_evaluation": True,
        "fixture_mode": False,
        "provider": "TEST-meter",
        "status": "COMPLETED",
        "transport_status": "COMPLETED",
        "errors": [],
        "retry_count": 0,
        "process": {"pid": 42, "returncode": 0, "error": None},
        "query_input": query_ref,
        "stdout": _ref(root, "stdout", b"TEST answer\n"),
        "stderr": _ref(root, "stderr", b"session_id: TEST-session-1\n"),
        "request": request,
        "request_artifact": request_ref,
        "request_sha256": request_ref["sha256"],
        "request_id": request["id"],
        "requested_session_id": None,
        "session_id": row["id"],
        "session_lineage": [row["id"]],
        "turn_id": "TEST-session-1:message:2",
        "task_id": "TEST-session-1:message:2",
        "answer_text": "TEST answer",
        "exports": {"before": {}, "after": {row["id"]: exported}},
        "export_usage": usage_delta({}, {row["id"]: row}),
    }


def test_argv_exact_resume_and_no_oneshot_usage_switch(tmp_path):
    fresh = cli_argv(sys.executable, "TEST alias", tmp_path / "空 格.txt")
    assert fresh[4:7] == ["--cli", "chat", "-Q"]
    assert "--resume" not in fresh and "--usage-file" not in fresh and "-z" not in fresh
    assert cli_argv(sys.executable, "TEST alias", tmp_path / "q", "TEST-observed")[
        -2:
    ] == ["--resume", "TEST-observed"]
    with pytest.raises(HermesCLIError):
        cli_argv(sys.executable, "x", tmp_path / "q", "../latest")


def test_capture_requires_actual_export_input_answer_and_message_identity(tmp_path):
    response = _capture(tmp_path)
    assert (
        validate_cli_capture(response, tmp_path)["turn_id"]
        == "TEST-session-1:message:2"
    )
    forged = copy.deepcopy(response)
    forged["turn_id"] = "made-up-turn"
    with pytest.raises(HermesCLIError, match="message_id"):
        validate_cli_capture(forged, tmp_path)
    (tmp_path / "stdout").write_bytes(b"changed")
    with pytest.raises(HermesCLIError, match="hash"):
        validate_cli_capture(response, tmp_path)


def test_capture_windows_multiline_stdout_matches_export_exact_content(tmp_path):
    response = _capture(tmp_path)
    answer = "TEST first line\n\nTEST second line"
    row = json.loads((tmp_path / "after.jsonl").read_text())
    row["messages"][-1]["content"] = answer
    response["exports"]["after"][row["id"]] = _ref(tmp_path, "after.jsonl", (json.dumps(row) + "\n").encode())
    response["stdout"] = _ref(tmp_path, "stdout", (answer.replace("\n", "\r\n") + "\r\n").encode())
    response["answer_text"] = answer
    assert validate_cli_capture(response, tmp_path)["turn_id"] == response["turn_id"]
    response["stdout"] = _ref(tmp_path, "stdout", (answer.replace("\n", "\r\n") + " forged\r\n").encode())
    with pytest.raises(HermesCLIError, match="stdout_export_answer_mismatch"):
        validate_cli_capture(response, tmp_path)


def test_file_mutation_verifier_banner_is_not_part_of_exported_answer(tmp_path):
    response = _capture(tmp_path)
    answer = "TEST pause recorded; output/ is not writable"
    banner = (
        "\n\n⚠️ File-mutation verifier: 1 file(s) were NOT modified this turn "
        "despite any wording above that may suggest otherwise. "
        "Run `git status` or `read_file` to confirm.\n"
        "  • `output/export_pause_notes.txt` — [write_file] Failed to write file: "
        "Permission denied\n"
    )
    row = json.loads((tmp_path / "after.jsonl").read_text())
    row["messages"][-1]["content"] = answer
    response["exports"]["after"][row["id"]] = _ref(
        tmp_path, "after.jsonl", (json.dumps(row) + "\n").encode()
    )
    response["stdout"] = _ref(tmp_path, "stdout", (answer + banner).encode())
    response["answer_text"] = answer
    assert validate_cli_capture(response, tmp_path)["answer_text"] == answer
    response["stdout"] = _ref(tmp_path, "stdout", (answer + "\n\nextra host footer\n").encode())
    with pytest.raises(HermesCLIError, match="stdout_export_answer_mismatch"):
        validate_cli_capture(response, tmp_path)


def test_unknown_cumulative_usage_is_never_zero():
    old = {"s": {"input_tokens": 8, "output_tokens": 2, "api_call_count": 1, "cache_read_tokens": 0, "cache_write_tokens": 0}}
    after = {"s": {"input_tokens": 20, "output_tokens": 6, "api_call_count": 2, "cache_read_tokens": 0, "cache_write_tokens": 0}}
    assert usage_delta(old, after) == {
        "status": "known",
        "input_tokens": 12,
        "output_tokens": 4,
        "api_call_count": 1,
    }
    after["s"]["input_tokens"] = None
    assert usage_delta(old, after)["status"] == "unknown"
    after["s"]["input_tokens"] = True
    assert usage_delta(old, after)["status"] == "unknown"
    with pytest.raises(HermesCLIError):
        parse_export(b'{"id":"s","messages":[]}\n{"id":"s","messages":[]}\n', "s")


def test_native_memory_wire_sidecar_is_observed_separately_from_raw_user(tmp_path):
    response = _capture(tmp_path)
    row = json.loads((tmp_path / "after.jsonl").read_bytes())
    wire = (
        "TEST public input\n\n<memory-context>PUBLIC recalled memory</memory-context>"
    )
    row["messages"][0]["api_content"] = wire
    response["exports"]["after"]["TEST-session-1"] = _ref(
        tmp_path, "after.jsonl", json.dumps(row).encode()
    )
    observed = validate_cli_capture(response, tmp_path)
    assert observed["model_user_content"] == wire
    assert (tmp_path / "query.txt").read_text() == "TEST public input"


def test_fixture_and_unobserved_resume_are_rejected(tmp_path):
    response = _capture(tmp_path)
    response["fixture_mode"] = True
    with pytest.raises(HermesCLIError, match="method_transport"):
        validate_cli_capture(response, tmp_path)
    response["fixture_mode"] = False
    response["requested_session_id"] = "TEST-other"
    with pytest.raises(HermesCLIError):
        validate_cli_capture(response, tmp_path)


def _formal_cli(root, monkeypatch):
    import p18_formal_evidence as evidence
    from tests.eval.test_p18_formal_evidence import _bundle, _json

    path, config, record = _bundle(root, monkeypatch, host="hermes_a2a", arm="A")
    config["operations"]["TEST-op-1"]["host_id"] = record["host_id"] = METHOD_ID
    reference = _json(
        root / "hermes-method.json",
        {
            "method": {"id": METHOD_ID},
            "original_protocol": {"sha256": evidence.PROTOCOL_SHA256},
        },
    )
    monkeypatch.setattr(evidence, "HERMES_METHOD_SHA256", reference["sha256"])
    config["hermes_method"] = {"id": METHOD_ID, "artifact": reference}
    gate = json.loads((root / "g2.json").read_bytes())
    gate["hermes_method"] = config["hermes_method"]
    config["g2_review"]["artifact"] = _json(root / "g2.json", gate)
    response = _capture(root)
    record["ids"]["turn_id"] = response["turn_id"]
    response_ref = _json(root / "response.json", response)
    record["host_response"].update(
        response_sha256=response_ref["sha256"],
        artifact_path=response_ref["path"],
        transport=TRANSPORT,
        http_status=None,
    )
    association = json.loads((root / "association.json").read_bytes())
    association.update(ids=record["ids"], host_request=response["request_artifact"])
    record["association"] = _json(root / "association.json", association)
    _json(path, config)
    return path, config, record


def test_cli_formal_uses_original_go_ledger_and_baseline_needs_no_delivery(
    tmp_path, monkeypatch
):
    import p18_formal_evidence as evidence

    path, _, record = _formal_cli(tmp_path, monkeypatch)
    result = evidence.FormalEvidenceWriter(tmp_path / "records", path).append_operation(
        record
    )
    stored = json.loads(result.read_bytes())
    assert stored["accounted_usage"]["route"] == "go"
    assert (
        stored["delivery"]["status"] == "not_attempted"
        and stored["status"] == "COMPLETED"
    )
    assert stored["run_binding"]["hermes_method_id"] == METHOD_ID


def test_cli_requires_independent_g2_method_binding(tmp_path, monkeypatch):
    import p18_formal_evidence as evidence
    from tests.eval.test_p18_formal_evidence import _json

    path, config, _ = _formal_cli(tmp_path, monkeypatch)
    gate = json.loads((tmp_path / "g2.json").read_bytes())
    del gate["hermes_method"]
    config["g2_review"]["artifact"] = _json(tmp_path / "g2.json", gate)
    _json(path, config)
    ready = evidence.verify_formal_run_config(path)
    assert (
        not ready.formal_execution_allowed
        and "hermes_CLI_G2_method_binding_mismatch" in ready.reasons
    )


def test_cache_buckets_reconstruct_exact_upstream_prompt_total():
    row = {"input_tokens": 6514, "cache_read_tokens": 256, "cache_write_tokens": 0, "output_tokens": 45, "api_call_count": 1}
    assert usage_delta({}, {"s": row}) == {"status": "known", "input_tokens": 6770, "output_tokens": 45, "api_call_count": 1}
    prior = {"s": dict(row)}
    row.update(input_tokens=6524, cache_read_tokens=300, cache_write_tokens=20, output_tokens=50, api_call_count=2)
    assert usage_delta(prior, {"s": row})["input_tokens"] == 74
    row.pop("cache_read_tokens")
    assert usage_delta({}, {"s": row})["status"] == "unknown"
    row["cache_read_tokens"] = 255
    assert usage_delta(prior, {"s": row})["status"] == "unknown"
