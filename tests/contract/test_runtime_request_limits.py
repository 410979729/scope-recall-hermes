"""Request-byte refusals remain distinct from recoverable budget pauses."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from scope_recall.core.candidate_lifecycle import CandidateSnapshot
from scope_recall.core.consolidate import CANDIDATE_EVALUATION_INPUT_BUDGET, candidate_evaluation_messages
from scope_recall.core.source_records import StoredSource
from scope_recall.core.worker_outcomes import BUDGET_PAUSE_ERRORS, model_exception_outcome
from scope_recall.runtime.consolidation_models import ConsolidationRouteConfig, OpenAIConsolidationAdapter
from scope_recall.runtime.model_budget import (
    AuxiliaryBudgetLedger,
    ModelPricing,
    default_budget_policy,
    initialize_auxiliary_budget_ledger,
    read_auxiliary_budget_status,
)
from scope_recall.runtime.models import AuxiliaryModelError
from test_candidate_lifecycle import _candidate, _candidate_rows, _finish_source_work
from test_claims import app

MODEL = "TEST-request-model"


def _messages(content):
    sources = tuple(
        StoredSource(
            f"src:TEST-request-{index}",
            1,
            "TEST-scope",
            "TEST-session",
            None,
            None,
            {"content": content, "origin": "tool_observation", "occurred_at": "2026-01-01T00:00:00Z"},
            "TEST-hash",
            False,
        )
        for index in range(16)
    )
    candidate = CandidateSnapshot(
        "clm:TEST-request",
        1,
        "TEST-scope",
        None,
        None,
        "proposed",
        {"kind": "fact", "subject": "TEST-build", "predicate": "result", "value_text": "ready"},
        "pending_evaluation",
        "new_evidence",
        "r1-candidate-v1",
    )
    return candidate_evaluation_messages(candidate, sources)


class Transport:
    """Synthetic HTTP boundary; record the exact bytes, never call a provider."""

    def __init__(self):
        self.bodies = []

    def post(self, url, *, body, headers, timeout_seconds, max_response_bytes):
        self.bodies.append(body)
        return 200, json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": "{}"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 1},
            }
        ).encode()


def _adapter(tmp_path, monkeypatch, **changes):
    # Only monetary/token/call authorization changes; keep the shipped byte cap
    # unless a test deliberately supplies an operator's smaller explicit cap.
    policy = replace(
        default_budget_policy(),
        cap_micro_usd=1_000_000,
        total_input_cap=200_000,
        total_output_cap=10_000,
        total_call_cap=2,
        pricing={MODEL: ModelPricing(Decimal("1"), Decimal("1"))},
        approved_models=frozenset({MODEL}),
    )
    policy = replace(policy, **changes)
    path = tmp_path / "TEST-request-ledger.sqlite3"
    initialize_auxiliary_budget_ledger(path, policy)
    ledger = AuxiliaryBudgetLedger(path, policy)
    transport = Transport()
    monkeypatch.setenv("SCOPE_RECALL_TEST_REQUEST_KEY", "TEST-synthetic-only")
    route = ConsolidationRouteConfig(
        MODEL, "https://example.test/v1/chat/completions", "SCOPE_RECALL_TEST_REQUEST_KEY", "max_tokens", 512
    )
    return (
        OpenAIConsolidationAdapter(route, ledger=ledger, reserve_input=32_768, transport=transport),
        ledger,
        transport,
    )


@pytest.mark.parametrize(
    "content", ["x" * 1500, "汉" * 200 + "x" * 1300, '"' * 500 + "x" * 1000], ids=["ascii", "utf8", "escaped"]
)
def test_default_transport_accepts_the_bounded_candidate_without_losing_sources(tmp_path, monkeypatch, content):
    messages = _messages(content)
    assert sum(len(message["content"].encode()) for message in messages) <= CANDIDATE_EVALUATION_INPUT_BUDGET
    adapter, ledger, transport = _adapter(tmp_path, monkeypatch)
    assert adapter.propose(messages, remaining_seconds=10) == "{}"
    (body,) = transport.bodies
    assert 32_000 < len(body) <= ledger.policy.max_request_bytes
    assert json.loads(body)["messages"] == messages
    assert len(json.loads(messages[2]["content"])["sources"]) == 16
    assert read_auxiliary_budget_status(ledger.path)["requests"] == 1


def test_explicit_smaller_byte_cap_is_a_terminal_payload_error_before_transport(tmp_path, monkeypatch):
    adapter, ledger, transport = _adapter(tmp_path, monkeypatch, max_request_bytes=32_000)
    with pytest.raises(AuxiliaryModelError) as failure:
        adapter.propose(_messages("x" * 1500), remaining_seconds=10)
    assert failure.value.error_type == "request_too_large"
    assert model_exception_outcome(failure.value) == ("failed", "request_too_large")
    assert failure.value.error_type not in BUDGET_PAUSE_ERRORS
    assert transport.bodies == []
    assert read_auxiliary_budget_status(ledger.path)["requests"] == 0


@pytest.mark.parametrize("cap", ["cap_micro_usd", "total_input_cap", "total_output_cap", "total_call_cap"])
def test_actual_budget_exhaustion_still_pauses_before_transport(tmp_path, monkeypatch, cap):
    adapter, ledger, transport = _adapter(tmp_path, monkeypatch, **{cap: 0})
    with pytest.raises(AuxiliaryModelError) as failure:
        adapter.propose([{"role": "user", "content": "TEST-small"}], remaining_seconds=10)
    assert failure.value.error_type == "budget_exhausted"
    assert failure.value.error_type in BUDGET_PAUSE_ERRORS
    assert transport.bodies == []
    assert read_auxiliary_budget_status(ledger.path)["requests"] == 0


def test_unsupported_model_and_empty_body_are_not_size_errors(tmp_path, monkeypatch):
    _port, ledger, _transport = _adapter(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="^unsupported_model$"):
        ledger.reserve("TEST-not-approved", b"{}", reserved_input=1, reserved_output=1)
    with pytest.raises(ValueError, match="^request_empty$"):
        ledger.reserve(MODEL, b"", reserved_input=1, reserved_output=1)
    assert read_auxiliary_budget_status(ledger.path)["requests"] == 0


def _drain_refused(core, ctx, adapter):
    class RefusedEvaluator:
        def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
            return adapter.propose(_messages("x" * 1500), remaining_seconds=remaining_seconds)

    return core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=RefusedEvaluator())


def test_an_oversize_candidate_fails_visibly_and_an_operator_can_reopen_it(app, monkeypatch, tmp_path):
    """#237: an explicit cap below the request fails the evaluation instead of deferring it forever, and once the cap
    is raised ``retry-failures`` re-opens it: a failure no command could clear left an installation degraded."""
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)
    adapter, ledger, transport = _adapter(tmp_path, monkeypatch, max_request_bytes=32_000)
    receipt = _drain_refused(core, ctx, adapter)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert receipt.failed == 1 and receipt.deferred == 0
    assert work[0]["state"] == "failed" and work[0]["attempt"] == 1
    assert work[0]["last_error_code"] == "request_too_large"
    assert evaluations[0]["state"] == "failed"
    assert lifecycle[0]["reason"] == "evaluation_failed"
    assert read_auxiliary_budget_status(ledger.path)["requests"] == 0 and transport.bodies == []
    status = core.status(ctx)
    assert status.failed_work == 1 and status.terminal_failed_work == 0
    report = core.operations.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["retried"] == 1 and report["by_kind"] == {"request_too_large": 1}
    lifecycle, evaluations, work = _candidate_rows(core)
    assert work[0]["state"] == "pending" and evaluations[0]["state"] == "queued"


def test_an_unapproved_model_pauses_the_work_without_spending_an_attempt(app, monkeypatch, tmp_path):
    """A configuration slip parks the queue, named, until the model is approved; it does not fail every item."""
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)
    adapter, ledger, transport = _adapter(tmp_path, monkeypatch, approved_models=frozenset())
    receipt = _drain_refused(core, ctx, adapter)
    _lifecycle, _evaluations, work = _candidate_rows(core)
    assert receipt.failed == 0 and receipt.deferred == 1
    assert work[0]["state"] == "pending" and work[0]["attempt"] == 0
    assert work[0]["last_error_code"] == "unsupported_model"
    assert read_auxiliary_budget_status(ledger.path)["requests"] == 0 and transport.bodies == []


def test_unsupported_model_is_a_pause_before_transport(tmp_path, monkeypatch):
    adapter, ledger, transport = _adapter(tmp_path, monkeypatch, approved_models=frozenset())
    with pytest.raises(AuxiliaryModelError) as failure:
        adapter.propose([{"role": "user", "content": "TEST-small"}], remaining_seconds=10)
    assert failure.value.error_type == "unsupported_model"
    assert failure.value.error_type in BUDGET_PAUSE_ERRORS
    assert read_auxiliary_budget_status(ledger.path)["requests"] == 0 and transport.bodies == []


@pytest.mark.parametrize("code", ["request_too_large", "unsupported_model"])
def test_each_refusal_code_is_one_an_operator_can_clear(code):
    from scope_recall.core.failure_retry import retry_class

    assert retry_class(code) == "actionable"
