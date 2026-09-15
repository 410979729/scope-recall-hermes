from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import pytest


PATH = Path(__file__).resolve().parents[2] / "probes/hermes/budget_proxy.py"
SPEC = importlib.util.spec_from_file_location("p02_budget", PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def request(**updates):
    return json.dumps({"model":"glm-5.3-flash", "max_tokens":1024, "messages":[{"role":"user", "content":"TEST_SCOPE_RECALL synthetic only"}], **updates}).encode()


@pytest.mark.parametrize("updates", [{"model":"other"}, {"max_tokens":1025}, {"max_tokens":True}, {"max_tokens":0}, {"max_tokens":None}, {"max_completion_tokens":4096}, {"n":2}, {"n":True}, {"messages":[{"role":"user", "content":"unmarked"}]}])
def test_rejects_unapproved_request(tmp_path, updates):
    ledger = probe.Ledger(tmp_path)
    with pytest.raises(probe.BudgetDenied):
        ledger.reserve(request(**updates))
    assert ledger.snapshot()["reserved_requests_including_failures"] == 0


def test_failure_and_restart_do_not_refund_request(tmp_path):
    ledger = probe.Ledger(tmp_path)
    for _ in range(12):
        number = ledger.reserve(request())
        ledger.finish(number, "network_error")
    with pytest.raises(probe.BudgetDenied, match="budget_exhausted"):
        probe.Ledger(tmp_path).reserve(request())
    assert ledger.snapshot()["reserved_requests_including_failures"] == 12


def test_concurrent_requests_cannot_overspend(tmp_path):
    ledger = probe.Ledger(tmp_path)
    def reserve(_):
        try:
            ledger.reserve(request())
            return True
        except probe.BudgetDenied:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(24))) == 12


def test_cumulative_request_bytes_are_limited(tmp_path):
    ledger = probe.Ledger(tmp_path)
    body = request(messages=[{"role":"user", "content":"TEST_SCOPE_RECALL " + "x" * 400000}])
    ledger.reserve(body)
    with pytest.raises(probe.BudgetDenied, match="budget_exhausted"):
        ledger.reserve(body)
    assert ledger.snapshot()["request_json_bytes"] == len(body)
