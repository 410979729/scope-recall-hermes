"""An evaluation whose attempt no process finished gets one more, once.

The at-most-once fence is committed before the model call, so a worker killed after it --
a gateway stop, a lost lease, a machine restart -- leaves the candidate failed with nothing
recorded: 24 of beta's candidates and 5 of alpha's sat there, and waiting for new
evidence never comes for a candidate whose evidence is already in.  One extra attempt, with
a durable marker, so a repeating interruption cannot become a loop of model calls.
"""
from __future__ import annotations

import sqlite3

from scope_recall.core.work_storage import INTERRUPTED_RETRY_MARKER, MAX_RECOVERABLE_ATTEMPTS
from tests.contract.test_r1_candidate_lifecycle import (  # noqa: F401  (fixture)
    Evaluator,
    _candidate,
    _candidate_rows,
    _finish_source_work,
    app,
)

EVALUATIONS = frozenset({"evaluate_candidate"})


def _interrupt(core, ctx):
    """Begin the one model attempt, then lose the lease the way a killed worker does."""
    with core.storage.write(ctx) as tx:
        item = tx.work.claim_next("TEST-crashed", core.clock.utc_now(), lease_seconds=60, limit=1,
                                  allowed_work_types=EVALUATIONS)[0]
        assert tx.candidates.begin_model_attempt(item.subject_revision, item.work_id, item.lease_token,
                                                 item.lease_owner, now=core.clock.utc_now())
        tx._check(write=True).execute("UPDATE work_items SET lease_until='2026-09-06T11:00:00Z' WHERE work_id=?",
                                      (item.work_id,))
    return item.work_id


def _error_code(core, work_id):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("SELECT last_error_code FROM work_items WHERE work_id=?", (work_id,)).fetchone()[0]


def _interrupted_candidate(core, ctx):
    _candidate(core, ctx)
    _finish_source_work(core)
    work_id = _interrupt(core, ctx)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=Evaluator())
    assert _error_code(core, work_id) == "candidate_attempt_interrupted"
    return work_id


def test_an_interrupted_attempt_is_evaluated_on_a_later_pass(app):
    core, ctx = app
    work_id = _interrupted_candidate(core, ctx)
    evaluator = Evaluator()
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 1, "the next pass gets the attempt nothing finished"
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert work[0]["state"] == "done", _error_code(core, work_id)
    # This evaluator offers no proposal, so the verdict is that the evidence is
    # not enough -- a settled evaluation either way, which is what was missing.
    assert evaluations[0]["state"] == "waiting_evidence"


def test_an_interruption_that_repeats_is_not_a_loop_of_model_calls(app):
    """Interrupted again, the failure is reported as a lost lease and keeps no
    history, so the marker alone would come back; ``attempt`` is what bounds it."""
    core, ctx = app
    work_id = _interrupted_candidate(core, ctx)
    with core.storage.write(ctx) as tx:
        assert tx.work.recover_interrupted_attempts(now=core.clock.utc_now(), allowed_work_types=EVALUATIONS) == 1
    assert INTERRUPTED_RETRY_MARKER in _error_code(core, work_id)

    evaluator = Evaluator()
    for _round in range(6):
        with sqlite3.connect(core.storage.path) as conn:
            state = conn.execute("SELECT state FROM work_items WHERE work_id=?", (work_id,)).fetchone()[0]
        if state == "pending":
            _interrupt(core, ctx)
        core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=evaluator)
    assert evaluator.calls == 0, "every attempt was interrupted before the call, and none was retried forever"
    _lifecycle, _evaluations, work = _candidate_rows(core)
    assert work[0]["state"] == "failed" and work[0]["attempt"] >= MAX_RECOVERABLE_ATTEMPTS
    with core.storage.write(ctx) as tx:
        assert tx.work.recover_interrupted_attempts(now=core.clock.utc_now(), allowed_work_types=EVALUATIONS) == 0


def test_recovery_asks_for_evaluations_only(app):
    core, ctx = app
    work_id = _interrupted_candidate(core, ctx)
    with core.storage.write(ctx) as tx:
        assert tx.work.recover_interrupted_attempts(now=core.clock.utc_now(),
                                                    allowed_work_types=frozenset({"consolidate"})) == 0
    assert _error_code(core, work_id) == "candidate_attempt_interrupted"


def test_a_failure_with_something_to_look_at_is_left_alone(app):
    """An invalid derivation is inspectable; its own recovery decides on it."""
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("""UPDATE work_items SET state='failed',last_error_code='derivation_invalid'
                        WHERE work_type='evaluate_candidate'""")
        conn.commit()
    with core.storage.write(ctx) as tx:
        assert tx.work.recover_interrupted_attempts(now=core.clock.utc_now(), allowed_work_types=EVALUATIONS) == 0
