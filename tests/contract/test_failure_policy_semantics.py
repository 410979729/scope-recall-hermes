"""Shared policy keeps history parsing separate from live-request retry decisions."""

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.failure_policy import AUTO_RECOVERABLE_ERRORS
from scope_recall.core.worker_outcomes import model_exception_outcome, port_failure
from scope_recall.runtime.models import AuxiliaryModelError
from test_candidate_lifecycle import _candidate, _candidate_rows, _settle_evidence, _sweep
from test_claims import app


def test_automatic_recovery_keeps_exact_persisted_deadline_spellings():
    assert "DEADLINE_EXCEEDED" in AUTO_RECOVERABLE_ERRORS
    assert "deadline_exceeded" not in AUTO_RECOVERABLE_ERRORS


def test_model_derivation_rejection_still_reaches_the_recorded_repair_attempt():
    assert model_exception_outcome(AuxiliaryModelError("derivation_invalid")) == ("retry", "derivation_invalid")


def test_contract_access_denied_still_uses_the_bounded_port_retry():
    assert port_failure(ContractError("ACCESS_DENIED")) == ("retry", "ACCESS_DENIED")


@pytest.mark.parametrize("code,expected", [("http_429", "retry"), ("TEST_unknown|http_429", "failed")])
def test_candidate_only_clears_the_request_fence_for_an_exact_live_rejection(app, code, expected):
    core, ctx = app
    _candidate(core, ctx)
    _settle_evidence(core)
    _sweep(core, ctx)
    with core.storage.write(ctx) as tx:
        (work,) = tx.work.claim_next(
            "TEST-policy", core.clock.utc_now(), lease_seconds=60, allowed_work_types=frozenset({"evaluate_candidate"})
        )
        assert tx.candidates.begin_model_attempt(work.subject_revision, *work.lease, now=core.clock.utc_now())
        result = tx.candidates.fail(work.subject_revision, work, now=core.clock.utc_now(), code=code)
    assert result.disposition == expected
    _lifecycle, evaluations, _work = _candidate_rows(core)
    assert (evaluations[0]["model_attempted_at"] is None) == (expected == "retry")


@pytest.mark.parametrize("code", ["input_invalid", "TEST_history|ACCESS_DENIED"])
def test_capture_live_rejections_are_matched_exactly(code):
    from scope_recall.core.failure_policy import capture_retryable

    assert capture_retryable(code)
    assert not capture_retryable("ACCESS_DENIED")
