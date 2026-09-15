from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from p18_codex_budget import CodexBudgetError, CodexBudgetPolicy, CodexSubmissionBudget


ROOT = Path(__file__).resolve().parents[2]
DRIVER_PATH = ROOT / "probes" / "p12_codex_auto_driver.py"
SPEC = importlib.util.spec_from_file_location("p12_codex_auto_driver_usage_test", DRIVER_PATH)
assert SPEC is not None and SPEC.loader is not None
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


def _report(events: list[dict[str, object]], *, thread: str = "TEST-thread", turn: str = "TEST-turn") -> dict[str, object]:
    return {"send": {"threadId": thread, "turnId": turn, "turnNotifications": {"usageEvents": events}}}


def _event(input_total: int, output_total: int, *, updated: str, last_input: int, last_output: int, thread: str = "TEST-thread", turn: str = "TEST-turn") -> dict[str, object]:
    return {
        "method": "thread/tokenUsage/updated",
        "threadId": thread,
        "turnId": turn,
        "updated": updated,
        "tokenUsage": {
            "last": {"inputTokens": last_input, "outputTokens": last_output, "totalTokens": last_input + last_output},
            "total": {"inputTokens": input_total, "outputTokens": output_total, "totalTokens": input_total + output_total},
        },
    }


def test_p12_usage_uses_cumulative_total_delta_and_ignores_mixed_ids() -> None:
    events = [
        _event(28906, 267, updated="2026-09-06T20:00:02Z", last_input=14525, last_output=146),
        _event(14381, 121, updated="2026-09-06T20:00:01Z", last_input=14381, last_output=121),
        _event(43600, 329, updated="2026-09-06T20:00:03Z", last_input=14694, last_output=62),
        _event(99999, 999, updated="2026-09-06T20:00:04Z", last_input=99, last_output=99, thread="OTHER-thread"),
    ]
    usage, matching = DRIVER.current_turn_usage(_report(events), baseline={"input_tokens": 0, "output_tokens": 0})
    assert usage == {"input_tokens": 43600, "output_tokens": 329}
    assert len(matching) == 3


def test_p12_usage_rejects_last_only_multi_request_and_unknown_resume_baseline() -> None:
    events = [
        _event(0, 0, updated="2026-09-06T20:00:01Z", last_input=11, last_output=3),
        {**_event(0, 0, updated="2026-09-06T20:00:02Z", last_input=17, last_output=5), "tokenUsage": {"last": {"inputTokens": 17, "outputTokens": 5}}},
    ]
    usage, _ = DRIVER.current_turn_usage(_report(events), baseline={"input_tokens": 0, "output_tokens": 0})
    assert usage is None
    resumed = dict(events[0])
    resumed["threadId"] = "RESUMED"
    single, _ = DRIVER.current_turn_usage(_report([resumed], thread="RESUMED"), baseline=None)
    assert single is None


def test_usage_over_reserved_marks_meter_breach_and_blocks_next_dispatch(tmp_path: Path) -> None:
    ledger = CodexSubmissionBudget(
        tmp_path / "TEST-budget.sqlite3",
        policy=CodexBudgetPolicy(call_cap=4, input_cap=100000, output_cap=10000, reserved_input=32768, reserved_output=4096),
    )
    ledger.initialize_schema()
    ledger.reserve("TEST-overrun-1", b"first")
    finished = ledger.finish("TEST-overrun-1", "completed", {"input_tokens": 43600, "output_tokens": 329})
    assert finished["status"] == "meter_breach"
    with pytest.raises(CodexBudgetError, match="meter_breach"):
        ledger.reserve("TEST-overrun-2", b"second")
