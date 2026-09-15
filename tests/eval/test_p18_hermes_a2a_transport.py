"""Offline focused fixtures for the Hermes P18 transport seam."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.eval.p18_hermes_a2a_transport import (
    HermesA2ATransport,
    HermesHTTPResponse,
    HermesTransportConfig,
)


CARD = {"name": "TEST_SCOPE_RECALL_P02", "version": "fixture-1"}
class RecordingBudget:
    def __init__(self, *, reservation="r-1"):
        self.reservation = reservation
        self.calls = []

    def reserve(self, model, request):
        self.calls.append(("reserve", model, request))
        return self.reservation

    def finish(self, reservation, status, usage):
        self.calls.append(("finish", reservation, status, usage))
        return "finished"


def make_transport(exchange, *, budget=None, formal=False, evidence=None):
    root = Path("TEST-P18-HERMES-FIXTURE").resolve()
    config = HermesTransportConfig(
        endpoint="http://127.0.0.1:19921",
        expected_agent_card_identity=CARD,
        isolation_root=root,
        formal_evaluation=formal,
    )
    return HermesA2ATransport(config, budget or RecordingBudget(), exchange=exchange, reservation_evidence=evidence)


def response(payload, status=200):
    return HermesHTTPResponse(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


def successful_payload():
    return {
        "jsonrpc": "2.0", "id": "m-1",
        "result": {"task": {"id": "task-1", "contextId": "ctx-1",
                               "status": {"state": "COMPLETED", "message": {"parts": [{"text": "answer from Hermes"}]}},
                               "artifacts": [{"artifactId": "a-1", "parts": [{"text": "artifact text"}]}]},
                   "usage": {"input_tokens": 4, "output_tokens": 7}},
    }


@pytest.mark.parametrize("completed_state", ["COMPLETED", "TASK_STATE_COMPLETED"])
def test_diagnostic_fixture_parses_answer_ids_hash_and_single_send(completed_state):
    calls = []

    def exchange(method, url, body, timeout):
        calls.append((method, url, body, timeout))
        if method == "GET":
            return response(CARD)
        assert method == "POST"
        request = json.loads(body)
        assert request["method"] == "message/send"
        assert "additionalContext" not in json.dumps(request)
        payload = successful_payload()
        payload["result"]["task"]["status"]["state"] = completed_state
        return response(payload)

    budget = RecordingBudget()
    receipt = make_transport(exchange, budget=budget).execute("TEST question", context_id="ctx-1", message_id="m-1")
    assert receipt["status"] == "DIAGNOSTIC"
    assert receipt["transport_status"] == "COMPLETED"
    assert receipt["task_id"] == "task-1"
    assert receipt["context_id"] == "ctx-1"
    assert receipt["answer_text"] == "answer from Hermes\nartifact text"
    assert receipt["answer_sha256"]
    assert receipt["response_sha256"] and receipt["response_bytes"] > 0
    assert receipt["retry_count"] == 0
    assert receipt["message_send_calls"] == 1
    assert [item[0] for item in calls] == ["GET", "POST"]
    assert [item[0] for item in budget.calls] == ["reserve", "finish"]
    assert budget.calls[1][2] == "COMPLETED"
    assert budget.calls[1][3] == {"input_tokens": 4, "output_tokens": 7}


def test_jsonrpc_error_fails_without_retry_and_finishes_unknown_upper_bound():
    calls = []

    def exchange(method, url, body, timeout):
        calls.append(method)
        return response(CARD if method == "GET" else {
            "jsonrpc": "2.0", "id": "m-1",
            "error": {"code": -32001, "message": "synthetic rejected"},
        })

    budget = RecordingBudget()
    receipt = make_transport(exchange, budget=budget).execute("q", context_id="c", message_id="m-1")
    assert receipt["status"] == "DIAGNOSTIC"
    assert receipt["transport_status"] == "FAILED"
    assert receipt["error"]["code"] == -32001
    assert calls == ["GET", "POST"]
    assert budget.calls[1][2:] == ("FAILED", None)
    assert receipt["budget"]["usage_resolution"] == "UNKNOWN_UPPER_BOUND"


def test_http_200_empty_completed_is_not_success():
    payload = successful_payload()
    payload["result"]["task"]["status"]["message"] = {"parts": []}
    payload["result"]["task"]["artifacts"] = []

    def exchange(method, url, body, timeout):
        return response(CARD if method == "GET" else payload, 200)

    receipt = make_transport(exchange).execute("q", context_id="c", message_id="m-1")
    assert receipt["status"] == "DIAGNOSTIC"
    assert receipt["transport_status"] == "FAILED"
    assert receipt["error"]["reason"] == "empty_completed_answer"


def test_non_2xx_fails_even_when_body_is_completed():
    def exchange(method, url, body, timeout):
        return response(CARD if method == "GET" else successful_payload(), 503 if method == "POST" else 200)

    receipt = make_transport(exchange).execute("q", context_id="c", message_id="m-1")
    assert receipt["status"] == "DIAGNOSTIC"
    assert receipt["transport_status"] == "FAILED"
    assert receipt["http_status"] == 503


def test_budget_reservation_happens_before_card_and_reserve_failure_does_not_call_transport():
    calls = []

    def exchange(method, url, body, timeout):
        calls.append(method)
        return response(CARD)

    class RejectingBudget(RecordingBudget):
        def reserve(self, model, request):
            super().reserve(model, request)
            raise RuntimeError("no reservation")

    budget = RejectingBudget()
    receipt = make_transport(exchange, budget=budget).execute("q", context_id="c", message_id="m-1")
    assert receipt["status"] == "NOT_READY"
    assert calls == []
    assert [item[0] for item in budget.calls] == ["reserve"]


def test_reservation_evidence_is_mapped_without_using_codex_ledger():
    def exchange(method, url, body, timeout):
        return response(CARD if method == "GET" else successful_payload())

    budget = RecordingBudget(reservation={"bridge_id": "B-1", "secret": "do-not-copy"})
    receipt = make_transport(exchange, budget=budget, evidence=lambda value: {"bridge_id": value["bridge_id"], "secret": value["secret"]}).execute(
        "q", context_id="c", message_id="m-1"
    )
    assert receipt["budget"]["owner"] == "external_hermes_primary_bridge"
    assert receipt["budget"]["ledger"] == "not_used"
    assert receipt["budget"]["reservation"] == {"bridge_id": "B-1"}
    assert "do-not-copy" not in json.dumps(receipt)
    assert all("sqlite" not in repr(call).lower() for call in budget.calls)


def test_receipt_path_is_restricted_to_TEST_isolation_root(tmp_path):
    def exchange(method, url, body, timeout):
        return response(CARD if method == "GET" else successful_payload())

    transport = make_transport(exchange)
    with pytest.raises(ValueError):
        transport.execute("q", context_id="c", message_id="m-1", receipt_path=tmp_path / "outside.json")


def test_formal_runtime_without_explicit_formal_config_is_not_ready_and_does_not_reserve():
    calls = []

    def exchange(method, url, body, timeout):
        calls.append(method)
        return response(CARD)

    budget = RecordingBudget()
    receipt = make_transport(exchange, budget=budget, formal=True).execute("q", context_id="c", message_id="m-1")
    assert receipt["status"] == "NOT_READY"
    assert receipt["error"]["reason"] == "P18_G0_G2_candidate_freeze_required"
    assert receipt["formal_readiness"]["reasons"] == ["formal_config_path_required"]
    assert calls == []
    assert budget.calls == []


def test_formal_verifier_must_report_ready_and_fixture_can_never_claim_semantic_pass(monkeypatch, tmp_path):
    def exchange(method, url, body, timeout):
        return response(CARD if method == "GET" else successful_payload())

    from tests.eval.p18_formal_evidence import FormalReadiness

    monkeypatch.setattr(
        "tests.eval.p18_formal_evidence.verify_formal_run_config",
        lambda path: FormalReadiness("READY", (), {"verified": True}),
    )
    transport = make_transport(exchange, budget=RecordingBudget(), formal=True)
    transport.config = HermesTransportConfig(
        endpoint=transport.config.endpoint,
        expected_agent_card_identity=CARD,
        isolation_root=transport.config.isolation_root,
        formal_config_path=tmp_path / "TEST-formal-config.json",
        formal_evaluation=True,
    )
    receipt = transport.execute("q", context_id="c", message_id="m-1")
    assert receipt["status"] == "DIAGNOSTIC"
    assert receipt["transport_status"] == "COMPLETED"
    assert receipt["semantic_pass"] is False


def test_agent_card_identity_mismatch_stops_before_message_send():
    calls = []

    def exchange(method, url, body, timeout):
        calls.append(method)
        return response({"name": "WRONG", "version": "fixture-1"})

    budget = RecordingBudget()
    receipt = make_transport(exchange, budget=budget).execute("q", context_id="c", message_id="m-1")
    assert receipt["status"] == "DIAGNOSTIC"
    assert receipt["transport_status"] == "FAILED"
    assert receipt["error"]["reason"] == "agent_card_identity_mismatch"
    assert calls == ["GET"]
    assert budget.calls[1][2] == "FAILED"


@pytest.mark.parametrize("endpoint", ["http://10.0.0.1:19921", "https://example.test", "http://127.0.0.1:19921?x=1"])
def test_endpoint_must_be_explicit_loopback_without_query(endpoint):
    with pytest.raises(ValueError):
        HermesTransportConfig(endpoint, CARD, Path("TEST-P18-HERMES-FIXTURE"))
