import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


spec = importlib.util.spec_from_file_location("cheap_probe", Path(__file__).resolve().parents[2] / "probes/cheap_model_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def body():
    return json.dumps(probe.payload("deepseek-v4-flash", "json"), ensure_ascii=False).encode()


def test_unknown_outcome_keeps_reserved_cost_and_survives_reopen(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = probe.Ledger(path)
    request_id = ledger.reserve("deepseek-v4-flash", body())
    amount = ledger.snapshot()["charge_micro_usd"]
    ledger.finish(request_id, "network_error")
    assert probe.Ledger(path).snapshot()["charge_micro_usd"] == amount
    assert probe.Ledger(path).snapshot()["requests"][0]["actual_input"] is None
    path.unlink()


def test_usage_replaces_reservation_once(tmp_path):
    ledger = probe.Ledger(tmp_path / "budget.sqlite3")
    request_id = ledger.reserve("deepseek-v4-flash", body())
    ledger.finish(request_id, "http_200", {"prompt_tokens": 300, "completion_tokens": 80})
    assert ledger.snapshot()["charge_micro_usd"] == probe.cost("deepseek-v4-flash", 300, 80)
    with pytest.raises(ValueError, match="reservation_state"):
        ledger.finish(request_id, "http_200", {"prompt_tokens": 0, "completion_tokens": 0})


def test_four_call_cap_does_not_reset_with_new_ledger_object(tmp_path):
    path = tmp_path / "budget.sqlite3"
    for _ in range(4):
        probe.Ledger(path).reserve("deepseek-v4-flash", body())
    with pytest.raises(ValueError, match="budget_exhausted"):
        probe.Ledger(path).reserve("deepseek-v4-flash", body())
    assert len(probe.Ledger(path).snapshot()["requests"]) == 4


def test_meter_breach_blocks_the_next_request(tmp_path):
    ledger = probe.Ledger(tmp_path / "budget.sqlite3")
    request_id = ledger.reserve("deepseek-v4-flash", body())
    ledger.finish(request_id, "http_200", {"prompt_tokens": 32769, "completion_tokens": 10})
    assert ledger.snapshot()["requests"][0]["status"] == "meter_breach"
    with pytest.raises(ValueError, match="meter_breach"):
        ledger.reserve("deepseek-v4-flash", body())


def test_other_batch_cost_counts_against_project_cap(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = probe.Ledger(path)
    db = sqlite3.connect(path)
    try:
        db.execute("INSERT INTO requests(batch,charge_micro_usd,status) VALUES (?,?,?)", ("other", probe.CAP_MICRO_USD, "reserved_before_network"))
        db.commit()
    finally:
        db.close()
    with pytest.raises(ValueError, match="budget_exhausted"):
        ledger.reserve("deepseek-v4-flash", body())
    assert len(ledger.snapshot()["requests"]) == 1


@pytest.mark.parametrize("change", [{"n": True}, {"n": 2}, {"max_tokens": 4096}, {"max_tokens": True}, {"max_completion_tokens": 131072}, {"stream": True}])
def test_completion_expansion_is_rejected_before_reservation(tmp_path, change):
    ledger = probe.Ledger(tmp_path / "budget.sqlite3")
    value = json.loads(body())
    value.update(change)
    with pytest.raises(ValueError, match="request_limit"):
        ledger.reserve("deepseek-v4-flash", json.dumps(value).encode())
    assert not ledger.snapshot()["requests"]


def test_non_test_content_is_rejected(tmp_path):
    ledger = probe.Ledger(tmp_path / "budget.sqlite3")
    value = json.loads(body())
    value["messages"][1]["content"] = "unapproved source"
    with pytest.raises(ValueError, match="synthetic_input_required"):
        ledger.reserve("deepseek-v4-flash", json.dumps(value).encode())
    assert not ledger.snapshot()["requests"]


def mimo_body():
    value = probe.payload("mimo-v2.5", "tool")
    value["max_completion_tokens"] = value.pop("max_tokens")
    value["thinking"] = {"type": "disabled"}
    value["tool_choice"] = "auto"
    return json.dumps(value, ensure_ascii=False).encode()


def test_unknown_mimo_usage_reserves_the_documented_maximum(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = probe.Ledger(path, "P02_MIMO_COMPAT")
    request_id = ledger.reserve("mimo-v2.5", mimo_body())
    ledger.finish(request_id, "timeout")
    row = probe.Ledger(path, "P02_MIMO_COMPAT").snapshot()["requests"][0]
    assert row["reserved_output"] == 131072
    assert row["charge_micro_usd"] == probe.cost("mimo-v2.5", 32768, 131072)
    assert row["actual_output"] is None


def test_batch_change_does_not_erase_prior_requests(tmp_path):
    path = tmp_path / "budget.sqlite3"
    probe.Ledger(path).reserve("deepseek-v4-flash", body())
    ledger = probe.Ledger(path, "P02_MIMO_COMPAT")
    for _ in range(3):
        ledger.reserve("mimo-v2.5", mimo_body())
    with pytest.raises(ValueError, match="budget_exhausted"):
        probe.Ledger(path, "P02_MIMO_COMPAT").reserve("mimo-v2.5", mimo_body())
    assert len(ledger.snapshot()["requests"]) == 4


def test_model_token_cap_counts_unknown_reservations_from_other_batches(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = probe.Ledger(path)
    db = sqlite3.connect(path)
    try:
        db.execute("INSERT INTO requests(batch,model,reserved_input,reserved_output,charge_micro_usd,status) VALUES (?,?,?,?,?,?)", ("earlier", "deepseek-v4-flash", 8000000, 0, 3520000, "timeout"))
        db.commit()
    finally:
        db.close()
    with pytest.raises(ValueError, match="budget_exhausted"):
        ledger.reserve("deepseek-v4-flash", body())


def test_nonapproved_provider_parameters_are_rejected(tmp_path):
    value = json.loads(mimo_body())
    value["max_tool_calls"] = 2000
    ledger = probe.Ledger(tmp_path / "budget.sqlite3", "P02_MIMO_COMPAT")
    with pytest.raises(ValueError, match="unsupported_request_parameter"):
        ledger.reserve("mimo-v2.5", json.dumps(value).encode())
    assert not ledger.snapshot()["requests"]


def test_concurrent_reservations_share_one_batch_cap(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "budget.sqlite3"
    probe.Ledger(path)

    def reserve(_):
        try:
            probe.Ledger(path).reserve("deepseek-v4-flash", body())
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(reserve, range(12)))
    assert sum(result) == 4
    assert len(probe.Ledger(path).snapshot()["requests"]) == 4


def embedding_body():
    return json.dumps({"model": "gemini-embedding-001", "input": "TEST_SCOPE_RECALL A synthetic blue square", "dimensions": 3072, "encoding_format": "float"}).encode()


def test_embedding_and_chat_share_the_same_money_cap(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = probe.Ledger(path, "P02_EMBEDDING_ROUTE")
    db = sqlite3.connect(path)
    try:
        db.execute("INSERT INTO requests(batch,model,charge_micro_usd,status) VALUES (?,?,?,?)", ("earlier", "mimo-v2.5", probe.CAP_MICRO_USD - 100, "unknown"))
        db.commit()
    finally:
        db.close()
    with pytest.raises(ValueError, match="budget_exhausted"):
        ledger.reserve_embedding(embedding_body())
    assert len(ledger.snapshot()["requests"]) == 1


def test_embedding_timeout_retains_cost_and_prevents_replay(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = probe.Ledger(path, "P02_EMBEDDING_ROUTE")
    request_id = ledger.reserve_embedding(embedding_body())
    ledger.finish(request_id, "timeout")
    other = probe.Ledger(path, "P02_EMBEDDING_ROUTE")
    with pytest.raises(ValueError, match="budget_exhausted"):
        other.reserve_embedding(embedding_body())
    assert other.snapshot()["charge_micro_usd"] == probe.cost("gemini-embedding-001", 8192, 0)


def go_usage(percent=0):
    return {"usage": {name: {"status": "ok", "percent": percent, "resetsAt": "2026-09-07T00:00:00Z"} for name in ("rolling", "weekly", "monthly")}}


def usage_transport(monkeypatch, replies):
    requests, closed = [], []

    class Connection:
        def __init__(self, host, timeout):
            self.status, self.raw = replies.pop(0)

        def request(self, method, path, headers):
            requests.append((method, path))

        def getresponse(self):
            return self

        def read(self, limit):
            return self.raw[:limit]

        def close(self):
            closed.append(True)

    monkeypatch.setattr(probe.http.client, "HTTPSConnection", Connection)
    return requests, closed


def test_allowance_is_read_fresh_when_other_instances_use_the_quota(monkeypatch):
    requests, closed = usage_transport(monkeypatch, [(200, json.dumps(go_usage(53)).encode()), (200, json.dumps(go_usage(80)).encode())])
    assert probe.check_go_allowance("TEST_FAKE_KEY")["weekly"]["percent"] == 53
    with pytest.raises(ValueError, match="account_allowance_guard"):
        probe.check_go_allowance("TEST_FAKE_KEY")
    assert requests == [("GET", "/zen/go/v1/usage"), ("GET", "/zen/go/v1/usage")]
    assert len(closed) == 2


@pytest.mark.parametrize("percent", [None, True, "10", -1, 80, 100, float("nan")])
def test_unknown_or_insufficient_allowance_fails_closed(monkeypatch, percent):
    _, closed = usage_transport(monkeypatch, [(200, json.dumps(go_usage(percent)).encode())])
    with pytest.raises(ValueError, match="account_allowance_guard"):
        probe.check_go_allowance("TEST_FAKE_KEY")
    assert closed == [True]


@pytest.mark.parametrize("status,raw", [(503, b'{}'), (200, b'{}'), (200, b'{"usage":{"rolling":{"status":"ok","percent":0}}}'), (200, b'x' * 32769)], ids=["service_error", "missing_usage", "missing_windows", "oversized_body"])
def test_incomplete_or_unavailable_usage_blocks_admission(monkeypatch, status, raw):
    _, closed = usage_transport(monkeypatch, [(status, raw)])
    with pytest.raises(ValueError):
        probe.check_go_allowance("TEST_FAKE_KEY")
    assert closed == [True]
