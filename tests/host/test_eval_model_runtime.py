import json
import sqlite3
from pathlib import Path

import pytest

from probes.eval_model_runtime import EvalModelRuntime


def messages(text="TEST_SCOPE_RECALL synthetic evaluation"):
    return [{"role": "system", "content": "TEST only"}, {"role": "user", "content": text}]


def test_stub_send_persists_sanitized_request_response_and_usage(tmp_path):
    calls = []
    def transport(body):
        calls.append(json.loads(body))
        return 200, {"choices": [{"message": {"content": "TEST answer"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=transport, key_loader=lambda: "TEST-key", allowance_checker=lambda key: {"ok": True}, ledger_path=tmp_path / "TEST-ledger.sqlite3")
    result = runtime.send(model="deepseek-v4-flash", messages=messages())
    assert result.content == "TEST answer" and result.usage == {"prompt_tokens": 12, "completion_tokens": 3}
    assert result.ledger_id and calls[0]["messages"][-1]["content"].startswith("TEST_SCOPE_RECALL ")
    assert json.loads((tmp_path / "TEST-audit" / f"response-{result.ledger_id}.json").read_text())["content"] == "TEST answer"


def test_mimo_requires_disabled_thinking_and_unknown_usage_is_fail_closed(tmp_path):
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=lambda body: (200, {"choices": [{"message": {"content": "x"}}]}), key_loader=lambda: "TEST-key", allowance_checker=lambda key: {"ok": True}, ledger_path=tmp_path / "TEST-ledger.sqlite3")
    result = runtime.send(model="mimo-v2.5", messages=messages())
    assert "usage_unknown" in result.status and result.content is None and result.usage is None


def test_http_failure_and_malformed_response_are_recorded_without_retry(tmp_path):
    calls = []
    def transport(body):
        calls.append(1)
        return (500, {"error": {"type": "synthetic_failure"}}) if len(calls) == 1 else (200, [])
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=transport, key_loader=lambda: "TEST-key", allowance_checker=lambda key: {"ok": True}, ledger_path=tmp_path / "TEST-ledger.sqlite3")
    failed = runtime.send(model="deepseek-v4-flash", messages=messages())
    assert failed.status.startswith("http_500") and failed.error_type == "provider_error" and failed.content is None
    malformed = runtime.send(model="deepseek-v4-flash", messages=messages())
    assert "usage_unknown" in malformed.status and malformed.content is None and len(calls) == 2


def test_meter_breach_blocks_following_request(tmp_path):
    calls = []
    def transport(body):
        calls.append(1)
        return 200, {"choices": [{"message": {"content": "should not escape"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 4097}}
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=transport, key_loader=lambda: "TEST-key", allowance_checker=lambda key: {"ok": True}, ledger_path=tmp_path / "TEST-ledger.sqlite3")
    first = runtime.send(model="deepseek-v4-flash", messages=messages())
    assert first.status == "meter_breach" and first.content is None
    with pytest.raises(ValueError, match="budget_exhausted"):
        runtime.send(model="deepseek-v4-flash", messages=messages())
    assert len(calls) == 1


def test_preflight_failure_leaves_evidence_and_does_not_post(tmp_path):
    calls = []
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=lambda body: calls.append(body), key_loader=lambda: "TEST-key", allowance_checker=lambda key: (_ for _ in ()).throw(RuntimeError("denied")), ledger_path=tmp_path / "TEST-ledger.sqlite3")
    with pytest.raises(RuntimeError):
        runtime.send(model="deepseek-v4-flash", messages=messages())
    assert calls == []
    records = list((tmp_path / "TEST-audit").glob("preflight-*.json"))
    assert len(records) == 1 and "TEST-key" not in records[0].read_text()


def test_evidence_write_failure_does_not_post(tmp_path, monkeypatch):
    calls = []
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=lambda body: calls.append(body), key_loader=lambda: "TEST-key", allowance_checker=lambda key: {"ok": True}, ledger_path=tmp_path / "TEST-ledger.sqlite3")
    original = runtime._write_exclusive
    def fail_request(name, record):
        if name.startswith("request-"):
            raise OSError("disk full")
        return original(name, record)
    monkeypatch.setattr(runtime, "_write_exclusive", fail_request)
    with pytest.raises(OSError):
        runtime.send(model="deepseek-v4-flash", messages=messages())
    assert calls == []


def test_existing_model_cap_blocks_before_post(tmp_path):
    ledger = tmp_path / "TEST-ledger.sqlite3"
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=lambda body: pytest.fail("post must be blocked"), key_loader=lambda: "TEST-key", allowance_checker=lambda key: {"ok": True}, ledger_path=ledger)
    with sqlite3.connect(ledger) as db:
        db.execute("INSERT INTO requests(batch,model,body_sha256,request_bytes,reserved_input,reserved_output,actual_input,actual_output,charge_micro_usd,status,started_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("P18_EVALUATION", "deepseek-v4-flash", "0" * 64, 1, 8_000_000, 1_000_000, 8_000_000, 1_000_000, 1, "done", 1))
    with pytest.raises(ValueError, match="budget_exhausted"):
        runtime.send(model="deepseek-v4-flash", messages=messages())


def test_tool_choice_dict_and_extended_usage_are_supported(tmp_path):
    seen = []
    def transport(body):
        seen.append(True)
        return 200, {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3, "prompt_tokens_details": {"cached_tokens": 1}}}
    allowance = {name:{"status":"ok","percent":1,"resetsAt":2,"provider_extension":"drop"} for name in ('rolling','weekly','monthly')}
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=transport, key_loader=lambda: "TEST-key", allowance_checker=lambda key: allowance, ledger_path=tmp_path / "TEST-ledger.sqlite3")
    result = runtime.send(model="deepseek-v4-flash", messages=messages(), tool_choice={"type": "function", "function": {"name": "f"}})
    assert result.content == "ok" and result.usage == {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3} and seen
    evidence = json.loads(next((tmp_path / "TEST-audit").glob("request-*.json")).read_text())
    assert evidence["allowance"] == {name:{"status":"ok","percent":1,"resetsAt":2} for name in allowance}


def test_live_path_escape_and_prepost_fsync_order(tmp_path, monkeypatch):
    escaped = (Path(__file__).resolve().parents[3] / "outside-TEST").absolute()
    with pytest.raises(ValueError, match="live_audit"):
        EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=escaped, transport=None)
    fsync_calls = []
    import probes.eval_model_runtime as module
    monkeypatch.setattr(module.os, "fsync", lambda fd: fsync_calls.append(fd))
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=lambda body: (200, {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}), key_loader=lambda: "TEST-key", allowance_checker=lambda key: {"status": "ok"}, ledger_path=tmp_path / "TEST-ledger.sqlite3")
    runtime.send(model="deepseek-v4-flash", messages=messages())
    assert len(fsync_calls) >= 2


@pytest.mark.parametrize("text", ["ordinary text", "TEST_SCOPE_RECALL " + "x" * 40000], ids=["non_synthetic", "oversized"])
def test_input_guard_rejects_non_synthetic_or_oversized(tmp_path, text):
    runtime = EvalModelRuntime(batch="P07_DEVELOPMENT", audit_dir=tmp_path / "TEST-audit", transport=lambda body: (200, {}), key_loader=lambda: "TEST-key", allowance_checker=lambda key: {"ok": True}, ledger_path=tmp_path / "TEST-ledger.sqlite3")
    with pytest.raises(ValueError):
        runtime.send(model="deepseek-v4-flash", messages=messages(text))
