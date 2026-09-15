from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from p18_codex_appserver_transport import (
    ARM_HOOK_POLICIES,
    ArmHookPolicy,
    CodexAppServerError,
    CodexAppServerTransport,
    CodexTransportConfig,
    FormalEvaluationBlocked,
    VerifiedUsageBaseline,
    verified_usage_baseline_from_receipt,
    _sha256_text,
)


HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "p18_codex_appserver_fixture.jsonl"


class BudgetSpy:
    def __init__(self) -> None:
        self.reserved: list[tuple[str, bytes]] = []
        self.finished: list[tuple[object, str, object]] = []

    def reserve(self, model: str, request: bytes) -> str:
        self.reserved.append((model, request))
        return "reservation-1"

    def finish(self, reservation: object, status: str, usage: object) -> str:
        self.finished.append((reservation, status, usage))
        return status


def test_candidate_requires_explicit_opt_in_and_formal_gate() -> None:
    config = CodexTransportConfig(codex_exe=FIXTURE, cwd=HERE, arm_id="C", expected_hooks_policy=ARM_HOOK_POLICIES["C"])
    with pytest.raises(CodexAppServerError, match="explicit_diagnostic"):
        CodexAppServerTransport(config)
    with pytest.raises(FormalEvaluationBlocked, match="G0_G2"):
        CodexAppServerTransport(
            CodexTransportConfig(codex_exe=FIXTURE, cwd=HERE, arm_id="C", expected_hooks_policy=ARM_HOOK_POLICIES["C"], allow_candidate_diagnostic=True, formal_evaluation=True)
        )


def test_runtime_requires_budget_adapter_before_owned_process(tmp_path: Path) -> None:
    config = CodexTransportConfig(
        codex_exe=tmp_path / "TEST-no-server.exe",
        cwd=HERE,
        arm_id="C",
        expected_hooks_policy=ARM_HOOK_POLICIES["C"],
        allow_candidate_diagnostic=True,
    )
    with pytest.raises(CodexAppServerError, match="budget_adapter_required"):
        CodexAppServerTransport(config).run_turn("TEST no runtime budget")


def test_resume_usage_requires_opaque_thread_bound_baseline() -> None:
    transport = CodexAppServerTransport.from_fixture(FIXTURE, arm_id="C")
    with pytest.raises(CodexAppServerError, match="usage_baseline_untrusted"):
        transport.run_turn("TEST baseline", thread_id="TEST-thread-001", usage_baseline={"prompt_tokens": 1, "completion_tokens": 1})  # type: ignore[arg-type]
    receipt = transport.run_turn("TEST fresh baseline")
    baseline = verified_usage_baseline_from_receipt(receipt)
    assert isinstance(baseline, VerifiedUsageBaseline)
    assert baseline.thread_id == "TEST-thread-001"


def test_unreliable_receipt_cannot_become_resume_baseline() -> None:
    with pytest.raises(CodexAppServerError, match="receipt_untrusted"):
        verified_usage_baseline_from_receipt({"status": "PASS_USAGE_UNKNOWN", "association": {"thread_id": "TEST"}, "usage": {"known": False}})


def test_fixture_runs_protocol_and_filters_reasoning_without_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    from p18_codex_appserver_transport import _JsonlPeer

    original_send = _JsonlPeer.send

    def capture(self: object, payload: object) -> None:
        sent.append(payload)  # type: ignore[arg-type]
        original_send(self, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(_JsonlPeer, "send", capture)
    receipt = CodexAppServerTransport.from_fixture(FIXTURE, arm_id="C").run_turn("TEST public prompt")

    assert receipt["status"] == "PASS"
    assert receipt["formal_evaluation"] is False
    assert receipt["process"]["cleanup"] == "fixture"
    assert receipt["rpc_provenance"] == {"model_turns_requested": 1, "thread_rpcs_dispatched": 1, "turn_rpcs_dispatched": 1}
    assert receipt["association"] == {"thread_id": "TEST-thread-001", "turn_id": "TEST-turn-001"}
    assert receipt["public_output"] == "已了解"
    assert receipt["usage"]["known"] is True
    assert receipt["usage"]["tokens"] == {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
    assert receipt["usage"]["provenance"] == "official_token_usage_updated_cumulative"
    assert receipt["usage"]["matching_event_count"] == 1
    assert receipt["hooks_list"]["untrustedUserPromptSubmit"] == []
    events = receipt["hook_events"]
    assert [(item["phase"], item["eventName"]) for item in events] == [
        ("started", "userPromptSubmit"),
        ("completed", "userPromptSubmit"),
        ("started", "stop"),
        ("completed", "stop"),
    ]
    context = events[1]["entries"][0]
    assert context == {"kind": "context", "textSha256": _sha256_text("TEST callback context; do not persist this text"), "textLength": 47}
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert "reasoning" not in serialized
    assert "secret reasoning" not in serialized
    assert "TEST callback context; do not persist this text" not in serialized

    turn_starts = [item for item in sent if item.get("method") == "turn/start"]
    assert len(turn_starts) == 1
    assert "additionalContext" not in turn_starts[0]["params"]
    assert turn_starts[0]["params"]["model"] == "gpt-5.6-luna"
    assert turn_starts[0]["params"]["effort"] == "low"
    assert turn_starts[0]["params"]["sandbox"] == "danger-full-access"
    assert turn_starts[0]["params"]["approvalPolicy"] == "never"


def test_fixture_wire_capture_is_bounded_and_separate_from_public_receipt(tmp_path: Path) -> None:
    capture = tmp_path / "TEST-codex-wire" / "response.jsonl"
    receipt = CodexAppServerTransport.from_fixture(FIXTURE, arm_id="C").run_turn(
        "TEST public prompt",
        raw_capture_path=capture,
    )
    assert capture.is_file()
    wire = capture.read_bytes()
    assert wire and len(wire) <= 1_048_576
    assert receipt["wire_capture"] == {
        "path": str(capture.resolve()),
        "sha256": hashlib.sha256(wire).hexdigest(),
        "bytes": len(wire),
    }
    assert b"secret reasoning" in wire
    assert "secret reasoning" not in json.dumps(receipt, ensure_ascii=False)


def test_explicit_thread_id_uses_resume_and_missing_trust_stops_before_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = FIXTURE.read_text(encoding="utf-8").replace('"trustStatus":"trusted"', '"trustStatus":"untrusted"')
    untrusted = tmp_path / "TEST-untrusted.jsonl"
    untrusted.write_text(text, encoding="utf-8")
    receipt = CodexAppServerTransport.from_fixture(untrusted, arm_id="C").run_turn("TEST prompt", thread_id="EXISTING-thread")
    assert receipt["status"] == "FAIL"
    assert receipt["errors"] == [{"kind": "admission", "error_type": "user_prompt_submit_hook_untrusted", "arm_id": "C"}]
    assert receipt["rpc_provenance"]["thread_rpcs_dispatched"] == 0
    assert receipt["rpc_provenance"]["turn_rpcs_dispatched"] == 0

    sent: list[dict[str, object]] = []
    from p18_codex_appserver_transport import _JsonlPeer

    monkeypatch.setattr(_JsonlPeer, "send", lambda self, payload: sent.append(payload))
    receipt = CodexAppServerTransport.from_fixture(FIXTURE, arm_id="C").run_turn("TEST second round", thread_id="EXISTING-thread")
    assert receipt["status"] == "PASS_USAGE_UNKNOWN"
    assert any(item.get("method") == "thread/resume" for item in sent)
    assert not any(item.get("method") == "thread/start" for item in sent)


def test_arm_admission_does_not_force_scope_recall_onto_native_arm(tmp_path: Path) -> None:
    native_fixture = tmp_path / "TEST-native-arm.jsonl"
    native_fixture.write_text(FIXTURE.read_text(encoding="utf-8").replace("scope-recall", "native"), encoding="utf-8")
    receipt = CodexAppServerTransport.from_fixture(native_fixture, arm_id="A").run_turn("TEST native arm")
    assert receipt["status"] == "PASS"
    assert receipt["arm"] == {"arm_id": "A", "expected_hooks_policy": {"arm_id": "A", "require_user_prompt_submit": False, "require_scope_recall_hook": False, "forbid_scope_recall_hook": True}}

    accidental_c = CodexAppServerTransport.from_fixture(FIXTURE, arm_id="A").run_turn("TEST accidental C plugin")
    assert accidental_c["status"] == "FAIL"
    assert accidental_c["errors"] == [{"kind": "admission", "error_type": "scope_recall_hook_enabled_for_arm", "arm_id": "A"}]
    assert accidental_c["rpc_provenance"]["thread_rpcs_dispatched"] == 0
    assert accidental_c["rpc_provenance"]["turn_rpcs_dispatched"] == 0

    c_missing = CodexAppServerTransport.from_fixture(native_fixture, arm_id="C").run_turn("TEST missing C hook")
    assert c_missing["status"] == "FAIL"
    assert c_missing["errors"] == [{"kind": "admission", "error_type": "trusted_scope_recall_user_prompt_submit_missing", "arm_id": "C"}]
    assert all(not policy.require_scope_recall_hook for arm, policy in ARM_HOOK_POLICIES.items() if arm in {"A", "B", "D"})


def test_arm_policy_must_match_explicit_arm_id() -> None:
    with pytest.raises(ValueError, match="mismatched"):
        CodexTransportConfig(
            codex_exe=FIXTURE,
            cwd=HERE,
            arm_id="A",
            expected_hooks_policy=ArmHookPolicy("C", True, True, False),
        )


def test_unknown_usage_finishes_outer_budget_as_unknown(tmp_path: Path) -> None:
    text = FIXTURE.read_text(encoding="utf-8").replace('"usage":{"inputTokens":11,"outputTokens":3,"totalTokens":14},', "")
    text = "\n".join(line for line in text.splitlines() if "tokenUsage/updated" not in line) + "\n"
    fixture = tmp_path / "TEST-unknown-usage.jsonl"
    fixture.write_text(text, encoding="utf-8")
    budget = BudgetSpy()
    receipt = CodexAppServerTransport.from_fixture(fixture, arm_id="C", budget=budget).run_turn("TEST budget prompt")
    assert receipt["status"] == "PASS_USAGE_UNKNOWN"
    assert receipt["usage"]["known"] is False
    assert receipt["usage"]["unknown_policy"] == "outer_budget_reservation_retained"
    assert receipt["usage"]["provenance"] == "unknown_no_verified_cumulative_token_usage"
    assert budget.reserved and budget.finished == [("reservation-1", "completed", None)]


def test_only_matching_thread_and_turn_token_usage_update_is_reconciled(tmp_path: Path) -> None:
    base = [line for line in FIXTURE.read_text(encoding="utf-8").splitlines() if "tokenUsage/updated" not in line]
    updates = [
        {"method": "thread/tokenUsage/updated", "params": {"threadId": "OTHER-thread", "turnId": "TEST-turn-001", "updated": "2026-09-06T20:00:09Z", "tokenUsage": {"last": {"inputTokens": 99, "outputTokens": 99}}}},
        {"method": "thread/tokenUsage/updated", "params": {"threadId": "TEST-thread-001", "turnId": "TEST-turn-001", "updated": "2026-09-06T20:00:01Z", "tokenUsage": {"last": {"inputTokens": 11, "outputTokens": 3}, "total": {"inputTokens": 11, "outputTokens": 3}}}},
        {"method": "thread/tokenUsage/updated", "params": {"threadId": "TEST-thread-001", "turnId": "TEST-turn-001", "updated": "2026-09-06T20:00:02Z", "tokenUsage": {"last": {"inputTokens": 17, "outputTokens": 5}, "total": {"inputTokens": 28, "outputTokens": 8}}}},
    ]
    fixture = tmp_path / "TEST-matched-usage.jsonl"
    fixture.write_text("\n".join(base[:-1] + [json.dumps(row, ensure_ascii=False) for row in updates] + [base[-1]]) + "\n", encoding="utf-8")
    budget = BudgetSpy()
    receipt = CodexAppServerTransport.from_fixture(fixture, arm_id="C", budget=budget).run_turn("TEST usage match")
    assert receipt["status"] == "PASS"
    assert receipt["usage"]["tokens"] == {"prompt_tokens": 28, "completion_tokens": 8, "total_tokens": 36}
    assert receipt["usage"]["matching_event_count"] == 2
    assert budget.finished == [("reservation-1", "completed", {"prompt_tokens": 28, "completion_tokens": 8, "total_tokens": 36})]


def test_trailing_token_usage_update_after_turn_completion_is_collected(tmp_path: Path) -> None:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    trailing = {"method": "thread/tokenUsage/updated", "params": {"threadId": "TEST-thread-001", "turnId": "TEST-turn-001", "updated": "2026-09-06T20:00:03Z", "tokenUsage": {"last": {"inputTokens": 23, "outputTokens": 6}, "total": {"inputTokens": 34, "outputTokens": 9, "totalTokens": 43}}}}
    fixture = tmp_path / "TEST-trailing-usage.jsonl"
    fixture.write_text("\n".join(lines + [json.dumps(trailing)]) + "\n", encoding="utf-8")
    receipt = CodexAppServerTransport.from_fixture(fixture, arm_id="C").run_turn("TEST trailing usage")
    assert receipt["status"] == "PASS"
    assert receipt["usage"]["tokens"] == {"prompt_tokens": 34, "completion_tokens": 9, "total_tokens": 43}
    assert receipt["usage"]["cumulative_tokens"] == {"prompt_tokens": 34, "completion_tokens": 9, "total_tokens": 43}


def test_prestarted_first_turn_reuses_live_peer_then_later_turn_resumes(tmp_path, monkeypatch):
    """Offline protocol regression: an empty thread must never be resumed."""
    import time
    from p18_codex_appserver_transport import _JsonlPeer
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]
    hooks = next(row for row in rows if row.get("id") == 2)
    turn_reply = next(i for i, row in enumerate(rows) if row.get("id") == 4)
    rows[turn_reply]["id"] = 5
    rows.insert(turn_reply, {"id": 4, "result": hooks["result"]})
    fixture = tmp_path / "TEST-live-peer.jsonl"
    fixture.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    peer = _JsonlPeer(None, fixture)
    peer.start()
    deadline = time.monotonic() + 5
    initialized = None
    for method in ("initialize", "hooks/list", "thread/start"):
        request_id = peer.next_id()
        reply = peer.wait_response(request_id, deadline, lambda message: None)
        assert "result" in reply
        if method == "initialize":
            initialized = reply["result"]
    sent = []
    monkeypatch.setattr(peer, "send", lambda payload: sent.append(payload))
    monkeypatch.setattr("p18_codex_appserver_transport._spawn",
                        lambda config: pytest.fail("first turn spawned another app-server"))
    transport = CodexAppServerTransport.from_fixture(FIXTURE, arm_id="C", budget=BudgetSpy())
    transport._started_peers["TEST-thread-001"] = (peer, initialized)
    receipt = transport.run_turn("TEST first turn", thread_id="TEST-thread-001")
    assert receipt["status"] == "PASS"
    assert [row["method"] for row in sent] == ["hooks/list", "turn/start"]
    assert not transport._started_peers
    assert receipt["association"]["thread_id"] == "TEST-thread-001"
    baseline = verified_usage_baseline_from_receipt(receipt)
    # A second operation uses the persisted ID, with normal resume and a
    # verified cumulative baseline. No new-session peer remains to reuse.
    later_sent = []
    monkeypatch.setattr(_JsonlPeer, "send", lambda self, payload: later_sent.append(payload))
    later = transport.run_turn("TEST later turn", thread_id="TEST-thread-001", usage_baseline=baseline)
    assert "thread/resume" in [row["method"] for row in later_sent]
    assert "thread/start" not in [row["method"] for row in later_sent]
    assert later["association"]["thread_id"] == "TEST-thread-001"
