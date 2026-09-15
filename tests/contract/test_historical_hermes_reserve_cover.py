"""Exact historical Hermes meter_breach cover on AuxiliaryBudgetLedger.reserve."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scope_recall.runtime.auxiliary import build_auxiliary_runtime, load_formal_p18_budget_policy
from scope_recall.runtime.model_budget import (
    AuxiliaryBudgetLedger,
    BudgetPolicy,
    ModelPricing,
    initialize_auxiliary_budget_ledger,
    load_hermes_attempt_authorization,
)

from test_runtime_auxiliary import FakeTransport, _runtime_config


EXACT = {
    "id": 4340,
    "batch": "P18_EVALUATION",
    "model": "deepseek-v4-flash",
    "body_sha256": "03a710898af69ac8e9c08ecb226e70792fdfb281574cf8c582835d0239e0153d",
    "request_bytes": 139172,
    "reserved_input": 32768,
    "reserved_output": 4096,
    "actual_input": 36825,
    "actual_output": 3206,
    "charge_micro_usd": 20435,
    "status": "meter_breach",
    "started_ns": 1788913497156226400,
}


def _policy() -> BudgetPolicy:
    pricing = {"deepseek-v4-flash": ModelPricing(Decimal("0.44"), Decimal("1.32"))}
    return BudgetPolicy(
        batch="P18_EVALUATION",
        cap_micro_usd=10**12,
        total_input_cap=64_000_000,
        total_output_cap=8_000_000,
        total_call_cap=8_000,
        max_request_bytes=32_000,
        default_reserve_input=32_768,
        default_reserve_output=4_096,
        model_reserve_output={},
        model_token_caps={},
        pricing=pricing,
        approved_models=frozenset(pricing),
    )


def _seed(path, exact=EXACT):
    policy = _policy()
    initialize_auxiliary_budget_ledger(path, policy)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO requests(id,batch,model,body_sha256,request_bytes,reserved_input,"
            "reserved_output,actual_input,actual_output,charge_micro_usd,status,started_ns) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                exact["id"], exact["batch"], exact["model"], exact["body_sha256"],
                exact["request_bytes"], exact["reserved_input"], exact["reserved_output"],
                exact["actual_input"], exact["actual_output"], exact["charge_micro_usd"],
                exact["status"], exact["started_ns"],
            ),
        )
    return policy


def _breach_status(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT status FROM requests WHERE id=4340").fetchone()[0]


def test_no_cover_still_blocks_reserve(tmp_path):
    ledger = tmp_path / "call-budget.sqlite3"
    policy = _seed(ledger)
    with pytest.raises(ValueError, match="budget_exhausted_or_meter_breach"):
        AuxiliaryBudgetLedger(ledger, policy).reserve(
            "deepseek-v4-flash", b'{"ok":true}', reserved_input=1024, reserved_output=16
        )
    assert _breach_status(ledger) == "meter_breach"


def test_exact_authorized_historical_breach_allows_reserve(tmp_path):
    ledger = tmp_path / "call-budget.sqlite3"
    policy = _seed(ledger)
    request_id = AuxiliaryBudgetLedger(
        ledger, policy, covered_historical_breaches=(EXACT,)
    ).reserve("deepseek-v4-flash", b'{"ok":true}', reserved_input=1024, reserved_output=16)
    assert request_id > 4340
    assert _breach_status(ledger) == "meter_breach"


def test_different_historical_breach_is_rejected(tmp_path):
    ledger = tmp_path / "call-budget.sqlite3"
    policy = _seed(ledger)
    other = dict(EXACT, id=9999)
    with pytest.raises(ValueError, match="budget_exhausted_or_meter_breach"):
        AuxiliaryBudgetLedger(ledger, policy, covered_historical_breaches=(other,)).reserve(
            "deepseek-v4-flash", b'{"ok":true}', reserved_input=1024, reserved_output=16
        )
    assert _breach_status(ledger) == "meter_breach"


def test_altered_historical_row_is_rejected(tmp_path):
    ledger = tmp_path / "call-budget.sqlite3"
    policy = _seed(ledger)
    cover = dict(EXACT, actual_input=36826)
    with pytest.raises(ValueError, match="budget_exhausted_or_meter_breach"):
        AuxiliaryBudgetLedger(ledger, policy, covered_historical_breaches=(cover,)).reserve(
            "deepseek-v4-flash", b'{"ok":true}', reserved_input=1024, reserved_output=16
        )
    assert _breach_status(ledger) == "meter_breach"


def test_frozen_hermes_turn_v1_covers_exact_4340_reserve(tmp_path):
    frozen = Path(r"F:\SCOPERECALL更新项目\.execution\budget-authorization-20260908-hermes-turn-v1.json")
    auth = json.loads(frozen.read_text(encoding="utf-8"))
    exact = auth["covered_historical_breaches"][0]
    ledger = tmp_path / "call-budget.sqlite3"
    policy = _seed(ledger, exact)
    request_id = AuxiliaryBudgetLedger(
        ledger, policy, covered_historical_breaches=tuple(auth["covered_historical_breaches"])
    ).reserve("deepseek-v4-flash", b'{"ok":true}', reserved_input=1024, reserved_output=16)
    assert request_id > exact["id"]
    assert _breach_status(ledger) == "meter_breach"


def test_build_auxiliary_runtime_loads_hash_bound_cover(tmp_path, monkeypatch):
    config, ledger, _budget = _runtime_config(tmp_path)
    _seed(ledger)
    auth = {
        "schema": "scope-recall.hermes-attempt-budget-amendment.v1",
        "historical_attempt_status_unchanged": True,
        "reserved_input": 1048576,
        "covered_historical_breaches": [EXACT],
    }
    path = tmp_path / "budget-authorization-test.json"
    raw = json.dumps(auth, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    monkeypatch.setenv("SCOPE_RECALL_HERMES_ATTEMPT_AUTHORIZATION", str(path))
    monkeypatch.setenv("SCOPE_RECALL_HERMES_ATTEMPT_AUTHORIZATION_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "test-key")
    loaded = load_hermes_attempt_authorization()
    assert loaded["covered_historical_breaches"][0]["id"] == 4340
    captured = {}

    def handler(**kwargs):
        captured["body"] = json.loads(kwargs["body"])
        return 200, json.dumps(
            {
                "usage": {"prompt_tokens": 8, "completion_tokens": 4},
                "choices": [{"message": {"role": "assistant", "content": "{\"claims\":[]}"}, "finish_reason": "stop"}],
            }
        ).encode()

    runtime = build_auxiliary_runtime(config, transport=FakeTransport(handler))
    assert runtime.consolidation is not None
    assert runtime.consolidation._reserve_input == 1048576
    raw_result = runtime.consolidation.propose(
        [{"role": "user", "content": "TEST cover"}], remaining_seconds=2.0
    )
    assert "claims" in raw_result
    assert _breach_status(ledger) == "meter_breach"


def test_build_auxiliary_runtime_uses_hash_bound_formal_p18_caps(tmp_path, monkeypatch):
    config, _ledger, budget = _runtime_config(tmp_path)
    assert budget.total_call_cap != 100000
    payload = {
        "approved_models": ["deepseek-v4-flash", "gemini-embedding-2", "mimo-v2.5"],
        "batch": "P18_EVALUATION",
        "batch_call_cap": 100000,
        "cap_micro_usd": 10**12,
        "default_reserve_input": 32768,
        "default_reserve_output": 4096,
        "max_request_bytes": 786432,
        "pricing": {
            "deepseek-v4-flash": {"input_usd_per_million": "0.44", "output_usd_per_million": "1.32"},
            "gemini-embedding-2": {"input_usd_per_million": "0.20", "output_usd_per_million": "0"},
            "mimo-v2.5": {"input_usd_per_million": "0.14", "output_usd_per_million": "0.28"},
        },
        "total_call_cap": 100000,
        "total_input_cap": 10**12,
        "total_output_cap": 10**12,
    }
    path = tmp_path / "TEST-authorized-budget.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.delenv("SCOPE_RECALL_P18_BUDGET_CONFIG", raising=False)
    monkeypatch.delenv("SCOPE_RECALL_P18_BUDGET_SHA256", raising=False)
    assert load_formal_p18_budget_policy() is None
    monkeypatch.setenv("SCOPE_RECALL_P18_BUDGET_CONFIG", str(path))
    monkeypatch.setenv("SCOPE_RECALL_P18_BUDGET_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    runtime = build_auxiliary_runtime(config, transport=FakeTransport(lambda **kwargs: (200, b"{}")))
    policy = runtime.consolidation._ledger.policy
    assert policy.batch == "P18_EVALUATION"
    assert policy.total_call_cap == 100000
    assert policy.cap_micro_usd == 10**12
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_formal_p18_budget_policy()
