"""Empty upstream envelopes recover without asserting an unbilled refusal."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from scope_recall.core.failure_policy import (
    AUTO_RECOVERABLE_ERRORS,
    CAPACITY_REFUSALS,
    RATE_LIMITED_ERRORS,
    retryable,
)
from scope_recall.runtime.auxiliary import build_auxiliary_runtime
from scope_recall.runtime.model_budget import read_auxiliary_budget_status
from scope_recall.runtime.models import AuxiliaryModelError
from test_candidate_lifecycle import _candidate, _candidate_rows, _finish_source_work
from test_claims import app, capture
from test_failure_retry import _embed_passes, _embed_row, _Embedding
from test_runtime_auxiliary import FakeTransport, _runtime_config

CODE = "upstream_empty_response"


@pytest.mark.parametrize(
    "body,expected",
    [
        pytest.param(b"", CODE, id="empty"),
        pytest.param(b" \n" * 550, CODE, id="long-whitespace"),
        pytest.param(b"\t\r\n", CODE, id="whitespace"),
        pytest.param(b"{not json", "invalid_json", id="malformed-json"),
        pytest.param(b"\xff", "unicode_error", id="invalid-utf8"),
    ],
)
def test_envelope_failure_keeps_its_reservation(tmp_path, monkeypatch, body, expected):
    """A missing answer gives no proof that the provider did not bill the call."""
    config, ledger, _ = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_CHAT_KEY", "TEST-key")
    transport = FakeTransport(lambda **kwargs: (200, body))
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as exc:
        runtime.consolidation.propose([{"role": "user", "content": "TEST bounded input"}], remaining_seconds=2.0)
    assert exc.value.error_type == expected
    assert transport.calls == 1
    assert read_auxiliary_budget_status(ledger)["requests"] == 1
    with closing(sqlite3.connect(ledger)) as conn:
        status = conn.execute("SELECT status FROM requests").fetchone()[0]
    assert "usage_unknown" in status


def test_empty_envelope_is_recoverable_but_not_a_capacity_refusal():
    assert CODE in AUTO_RECOVERABLE_ERRORS
    assert CODE in RATE_LIMITED_ERRORS
    assert CODE not in CAPACITY_REFUSALS
    assert retryable(CODE)
    assert not retryable(CODE, candidate_attempted=True)
    assert "invalid_json" not in AUTO_RECOVERABLE_ERRORS


def test_empty_answers_exhaust_the_bounded_automatic_recovery_budget(app):
    core, ctx = app
    source = capture(core, ctx, "TEST empty envelope recovery source")
    _embed_passes(core, ctx, _Embedding(CODE), passes=3)
    assert _embed_row(core, source) == ("failed", 3, CODE)
    for expected_recovery in (1, 2):
        with core.storage.write(ctx) as tx:
            assert (
                tx.work.recover_transient_failures(now=core.clock.utc_now(), allowed_work_types=frozenset({"embed"}))
                == 0
            )
        moment = datetime.fromisoformat(core.clock.now.replace("Z", "+00:00")) + timedelta(hours=1)
        core.clock.now = moment.isoformat().replace("+00:00", "Z")
        with core.storage.write(ctx) as tx:
            assert (
                tx.work.recover_transient_failures(now=core.clock.utc_now(), allowed_work_types=frozenset({"embed"}))
                == 1
            )
        _embed_passes(core, ctx, _Embedding(CODE))
        state, attempts, error = _embed_row(core, source)
        assert (state, attempts) == ("failed", 3 + expected_recovery)
        assert f"auto_retry:{expected_recovery}|" in error
    moment = datetime.fromisoformat(core.clock.now.replace("Z", "+00:00")) + timedelta(hours=1)
    core.clock.now = moment.isoformat().replace("+00:00", "Z")
    with core.storage.write(ctx) as tx:
        assert (
            tx.work.recover_transient_failures(now=core.clock.utc_now(), allowed_work_types=frozenset({"embed"})) == 0
        )


def test_empty_answer_can_recover_when_the_provider_returns(app):
    core, ctx = app
    source = capture(core, ctx, "TEST recovery after an upstream empty envelope")
    _embed_passes(core, ctx, _Embedding(CODE), passes=3)
    assert _embed_row(core, source)[0] == "failed"
    moment = datetime.fromisoformat(core.clock.now.replace("Z", "+00:00")) + timedelta(hours=1)
    core.clock.now = moment.isoformat().replace("+00:00", "Z")
    recovered = _Embedding(None)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, embed=recovered)
    assert _embed_row(core, source)[0] == "done"
    assert recovered.written == [source.ref]


def test_empty_answer_stands_down_its_type_for_the_pass(app):
    core, ctx = app
    sources = [capture(core, ctx, f"TEST source number {number}") for number in range(2)]

    class EmptyEmbedding(_Embedding):
        calls = 0

        def prepare_source(self, source, *, remaining_seconds=1.0):
            self.calls += 1
            return super().prepare_source(source, remaining_seconds=remaining_seconds)

    port = EmptyEmbedding(CODE)
    _embed_passes(core, ctx, port)
    assert port.calls == 1
    assert sum(_embed_row(core, source)[1] for source in sources) == 1


def test_empty_answer_does_not_clear_the_candidate_attempt_fence(app):
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)

    class EmptyEvaluation:
        calls = 0

        def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
            self.calls += 1
            raise AuxiliaryModelError(CODE)

    port = EmptyEvaluation()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=port)
    _, evaluations, work = _candidate_rows(core)
    assert port.calls == 1 and work[0]["state"] == "failed"
    assert evaluations[0]["model_attempted_at"] is not None
    moment = datetime.fromisoformat(core.clock.now.replace("Z", "+00:00")) + timedelta(hours=1)
    core.clock.now = moment.isoformat().replace("+00:00", "Z")
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=port)
    assert port.calls == 1
