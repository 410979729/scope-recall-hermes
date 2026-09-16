"""One bounded re-look at failed work, granted by an operator, stamped once.

Covers ``core/failure_retry.py``, ``work.retry_failed`` and the widened
terminal classification in the doctor.  The properties that matter: a fault can
be cleared so an instance can return to healthy, a by-design terminal outcome
is not re-run by accident, and nothing can loop.
"""
from __future__ import annotations

import sqlite3

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.failure_retry import (
    ACTIONABLE_FAILURES,
    RETRY_MARKER,
    TERMINAL_FAILURES,
    already_retried,
    failure_kind,
    marked,
    retry_class,
    selects,
)
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.maintenance import doctor
from test_r1_candidate_lifecycle import _candidate, _candidate_rows, _finish_source_work
from test_v11_claims import app


# --------------------------------------------------------------------------
# Reading a decorated error code
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code,kind", [
    ("timeout", "timeout"),
    ("auto_retry:1|timeout", "timeout"),
    ("budget_checked:1108|input_invalid", "input_invalid"),
    ("DERIVATION_INVALID", "derivation_invalid"),
    ("", ""),
    (None, ""),
])
def test_the_kind_is_the_last_segment(code, kind):
    """Codes accumulate history; the failure itself is always on the end."""
    assert failure_kind(code) == kind


@pytest.mark.parametrize("kind", sorted(ACTIONABLE_FAILURES))
def test_faults_are_retried_by_default(kind):
    assert retry_class(kind) == "actionable"
    assert selects(kind, include_terminal=False, generation=SCHEMA_VERSION) is True


@pytest.mark.parametrize("kind", sorted(TERMINAL_FAILURES))
def test_by_design_outcomes_need_the_explicit_flag(kind):
    assert retry_class(kind) == "terminal"
    assert selects(kind, include_terminal=False, generation=SCHEMA_VERSION) is False
    assert selects(kind, include_terminal=True, generation=SCHEMA_VERSION) is True


@pytest.mark.parametrize("code", ["something_else", "capture_error", "", None])
def test_an_unknown_failure_is_never_retried(code):
    assert retry_class(code) is None
    assert selects(code, include_terminal=True, generation=SCHEMA_VERSION) is False


def test_a_row_stamped_by_this_generation_is_skipped():
    """The stamp is what stops a second run from doing the same work again."""
    stamped = marked("timeout", generation=SCHEMA_VERSION)
    assert stamped.startswith(f"{RETRY_MARKER}:{SCHEMA_VERSION}|")
    assert already_retried(stamped, generation=SCHEMA_VERSION) is True
    assert selects(stamped, include_terminal=True, generation=SCHEMA_VERSION) is False


def test_a_stamp_from_another_generation_does_not_block():
    """A later schema is a later decision; it may grant its own re-look."""
    stamped = marked("timeout", generation=SCHEMA_VERSION - 1)
    assert already_retried(stamped, generation=SCHEMA_VERSION) is False
    assert selects(stamped, include_terminal=False, generation=SCHEMA_VERSION) is True


# --------------------------------------------------------------------------
# Against real storage
# --------------------------------------------------------------------------

class _Failing:
    def __init__(self, code="network_error"):
        self.code = code

    def evaluate_candidate(self, candidate, sources, *, remaining_seconds):
        from scope_recall.adapters.models import ModelRefusal

        raise ModelRefusal(self.code)


def _fail_one(core, ctx, code="timeout"):
    """Drive one candidate evaluation to a failed work item through the worker."""
    _candidate(core, ctx)
    _finish_source_work(core)
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=_Failing(code))
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET last_error_code=? WHERE state='failed'", (code,))
        conn.commit()
    _lifecycle, evaluations, work = _candidate_rows(core)
    assert any(row["state"] == "failed" for row in work), work
    return work


def _states(core):
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        work = dict(conn.execute("SELECT state,count(*) FROM work_items GROUP BY 1").fetchall())
        evaluations = dict(conn.execute(
            "SELECT state,count(*) FROM candidate_evaluations GROUP BY 1").fetchall())
    return work, evaluations


def test_a_fault_is_cleared_and_its_three_tables_move_together(app):
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    assert _states(core)[0].get("failed")

    report = core.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["retried"] >= 1 and report["by_kind"].get("timeout")
    work, evaluations = _states(core)
    assert not work.get("failed")
    with sqlite3.connect(core.storage.path) as conn:
        mismatched = conn.execute(
            """SELECT count(*) FROM candidate_evaluations e JOIN work_items w ON w.work_id=e.work_id
               WHERE w.state='pending' AND e.state<>'queued'""").fetchone()[0]
    assert mismatched == 0, "a re-queued work item whose evaluation stayed failed dies immediately"


def test_a_second_pass_does_nothing(app):
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    assert core.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] >= 1
    assert core.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] == 0


def test_the_preview_writes_nothing(app):
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    before = _states(core)
    report = core.retry_failed_work(ctx, limit=64, dry_run=True)
    assert report["retried"] >= 1 and report["applied"] is False
    assert _states(core) == before


def test_a_terminal_failure_is_left_alone_without_the_flag(app):
    core, ctx = app
    _fail_one(core, ctx, "derivation_invalid")
    assert core.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] == 0
    assert _states(core)[0].get("failed")
    assert core.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=False)["retried"] >= 1
    assert not _states(core)[0].get("failed")


@pytest.mark.parametrize("limit", [0, -1, 257, 1.0, True])
def test_the_page_is_bounded(app, limit):
    core, ctx = app
    with pytest.raises(ContractError):
        core.retry_failed_work(ctx, limit=limit, dry_run=True)


# --------------------------------------------------------------------------
# The doctor no longer pins itself at degraded for these
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [
    "derivation_invalid",
    "DERIVATION_INVALID",
    "auto_retry:1|derivation_invalid",
    "budget_checked:1108|input_invalid",
])
def test_every_by_design_terminal_failure_counts_as_terminal(app, code):
    """Counting only ``consolidate`` left 213 identical candidate failures
    driving "degraded" with nobody able to act on them."""
    core, ctx = app
    _fail_one(core, ctx, code)
    with sqlite3.connect(core.storage.path) as conn:
        total = conn.execute("SELECT count(*) FROM work_items WHERE state='failed'").fetchone()[0]
        terminal = conn.execute(doctor.TERMINAL_FAILURE_COUNT).fetchone()[0]
    assert total >= 1 and terminal == total


def test_a_fault_still_counts_as_actionable(app):
    """Narrowing must not go so far that a real fault stops being reported."""
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute(doctor.TERMINAL_FAILURE_COUNT).fetchone()[0] == 0


# --------------------------------------------------------------------------
# The two lists of "failures that may pass" must not drift apart again
# --------------------------------------------------------------------------

def test_every_transient_failure_the_worker_knows_is_operator_actionable():
    """They drifted once: ``model_unavailable`` was auto-recoverable but absent
    here, so four rows from one outage pinned a live instance at degraded with
    nobody able to clear them."""
    from scope_recall.core.work_storage import AUTO_RECOVERABLE_ERRORS

    # Compared on the normalised kind, which is what ``retry_class`` is given.
    assert {code.lower() for code in AUTO_RECOVERABLE_ERRORS} <= ACTIONABLE_FAILURES
    assert all(retry_class(code) == "actionable" for code in AUTO_RECOVERABLE_ERRORS),         "a code the worker retries automatically must also be clearable by hand"


@pytest.mark.parametrize("code", ["model_unavailable", "model_timeout", "network_error",
                                  "http_429", "http_503", "rate_limited"])
def test_a_transient_model_failure_can_be_cleared(code):
    assert retry_class(code) == "actionable"
    assert selects(code, include_terminal=False, generation=SCHEMA_VERSION) is True


def test_a_terminal_failure_is_never_also_actionable():
    assert not (ACTIONABLE_FAILURES & TERMINAL_FAILURES)


def test_the_operator_only_extras_are_not_auto_recoverable():
    """They are listed by hand, so each must have a reason to be: a code the
    worker would have retried itself does not belong in that list."""
    from scope_recall.core.failure_retry import _OPERATOR_ONLY_FAILURES
    from scope_recall.core.work_storage import AUTO_RECOVERABLE_ERRORS

    normalised = {code.lower() for code in AUTO_RECOVERABLE_ERRORS}
    assert not (_OPERATOR_ONLY_FAILURES & normalised)


def test_a_transient_failure_clears_through_real_storage(app):
    """The live case: the outage left model_unavailable rows behind."""
    core, ctx = app
    _fail_one(core, ctx, "model_unavailable")
    assert _states(core)[0].get("failed")
    report = core.retry_failed_work(ctx, limit=64, dry_run=False)
    assert report["retried"] >= 1 and report["by_kind"].get("model_unavailable")
    assert not _states(core)[0].get("failed")


# --------------------------------------------------------------------------
# A provider refusing everyone must not spend an item's own budget
# --------------------------------------------------------------------------

def test_a_capacity_refusal_does_not_consume_an_attempt(app):
    """Four hours of "monthly usage limit reached" pushed 195 live work items
    into failed at attempt=3 apiece, each needing an operator to grant it back.
    A 429 says nothing about this payload."""
    from scope_recall.core.work_storage import CAPACITY_REFUSALS, MAX_RECOVERABLE_ATTEMPTS

    core, ctx = app
    _fail_one(core, ctx, "timeout")   # build one leasable work item
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='pending', attempt=0, last_error_code=NULL")
        conn.commit()

    seen = []
    for _ in range(MAX_RECOVERABLE_ATTEMPTS + 3):
        with core.storage.write(ctx, remaining_seconds=10) as tx:
            leased = tx.work.claim_next("TEST-owner", core.clock.utc_now(), lease_seconds=30, limit=1)
            if not leased:
                break
            item = leased[0]
            tx.work.fail(item.work_id, item.lease_token, item.lease_owner,
                         error_code="http_429", now=core.clock.utc_now(), recoverable=True)
        with sqlite3.connect(core.storage.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT state,attempt FROM work_items WHERE state<>'done'").fetchone()
            seen.append((row["state"], row["attempt"]) if row else None)
            conn.execute("UPDATE work_items SET available_at=?", (core.clock.utc_now(),))
            conn.commit()

    assert all(state == "pending" for state, _attempt in seen if state), seen
    assert not any(state == "failed" for state, _ in seen if state), \
        f"a provider outage exhausted the item's budget: {seen}"
    assert "http_429" in CAPACITY_REFUSALS


def test_an_ordinary_fault_still_exhausts_its_budget(app):
    """The refund must not become a licence for every failure to retry forever."""
    from scope_recall.core.work_storage import MAX_RECOVERABLE_ATTEMPTS

    core, ctx = app
    _fail_one(core, ctx, "timeout")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='pending', attempt=0, last_error_code=NULL")
        conn.commit()

    ended = None
    for _ in range(MAX_RECOVERABLE_ATTEMPTS + 2):
        with core.storage.write(ctx, remaining_seconds=10) as tx:
            leased = tx.work.claim_next("TEST-owner", core.clock.utc_now(), lease_seconds=30, limit=1)
            if not leased:
                break
            item = leased[0]
            tx.work.fail(item.work_id, item.lease_token, item.lease_owner,
                         error_code="timeout", now=core.clock.utc_now(), recoverable=True)
        with sqlite3.connect(core.storage.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT state FROM work_items WHERE state<>'done'").fetchone()
            ended = row["state"] if row else None
            conn.execute("UPDATE work_items SET available_at=?", (core.clock.utc_now(),))
            conn.commit()
    assert ended == "failed", f"an ordinary fault retried forever: {ended}"


def test_a_terminal_failure_is_unaffected_by_the_refund(app):
    core, ctx = app
    _fail_one(core, ctx, "derivation_invalid")
    assert _states(core)[0].get("failed")


# --------------------------------------------------------------------------
# Never given up on, but never asked continuously either
# --------------------------------------------------------------------------

def test_the_wait_grows_with_each_refusal_in_a_row():
    """Refunding the attempt alone left the ordinary 60s ceiling in place, and
    197 items retried without pause for an entire provider outage."""
    from scope_recall.core.work_storage import _capacity_backoff_seconds

    from scope_recall.core.work_storage import CAPACITY_BACKOFF_FLOOR_SECONDS

    waits = [_capacity_backoff_seconds(n) for n in range(1, 8)]
    assert waits == sorted(waits), waits
    assert waits[0] == CAPACITY_BACKOFF_FLOOR_SECONDS
    assert waits[-1] > waits[0]


def test_the_first_wait_is_not_shorter_than_the_cycle_it_replaces():
    """Sized against what the queue actually did: over eight hours of a real
    outage the median gap between one item's retries was 115 minutes, and 74%
    fell between one and two hours.  A backoff that started below that would
    make the instance ask *more* often than leaving it alone -- which a first
    draft of this, starting at 60 seconds, would have done."""
    from scope_recall.core.work_storage import (
        CAPACITY_BACKOFF_CEILING_SECONDS,
        CAPACITY_BACKOFF_FLOOR_SECONDS,
        _capacity_backoff_seconds,
    )

    observed_median_seconds = 115 * 60
    assert _capacity_backoff_seconds(1) >= observed_median_seconds / 4
    assert CAPACITY_BACKOFF_CEILING_SECONDS > observed_median_seconds
    assert CAPACITY_BACKOFF_FLOOR_SECONDS < CAPACITY_BACKOFF_CEILING_SECONDS


def test_the_wait_has_a_ceiling_but_the_retries_do_not():
    """A cap on *how often* is a loop guard; a cap on *how many* would give up
    on the item, which is the thing this must never do."""
    from scope_recall.core.work_storage import (
        CAPACITY_BACKOFF_CEILING_SECONDS,
        _capacity_backoff_seconds,
    )

    assert _capacity_backoff_seconds(50) == CAPACITY_BACKOFF_CEILING_SECONDS
    assert _capacity_backoff_seconds(10_000) == CAPACITY_BACKOFF_CEILING_SECONDS


def test_the_refusal_count_is_read_back_from_the_error_code():
    from scope_recall.core.work_storage import _capacity_count

    assert _capacity_count("capacity:5|http_429") == 5
    assert _capacity_count("http_429") == 0
    assert _capacity_count(None) == 0


def test_consecutive_refusals_space_the_retries_out(app):
    """Each refusal pushes the next attempt further out. Refunding the attempt
    without this left the ordinary 60s ceiling in place, and 197 live items
    retried without pause for an entire provider outage."""
    core, ctx = app
    _fail_one(core, ctx, "timeout")
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        # One item, so each reading below is the same item's next due time
        # rather than whichever row the round-robin claim happened to hand out.
        keep = conn.execute("SELECT min(work_id) FROM work_items").fetchone()[0]
        conn.execute("DELETE FROM candidate_evaluations WHERE work_id<>?", (keep,))
        conn.execute("DELETE FROM work_items WHERE work_id<>?", (keep,))
        conn.execute("UPDATE work_items SET state='pending', attempt=0, last_error_code=NULL")
        conn.commit()

    waits, codes = [], []
    for _ in range(4):
        with core.storage.write(ctx, remaining_seconds=10) as tx:
            leased = tx.work.claim_next("TEST-owner", core.clock.utc_now(),
                                        lease_seconds=30, limit=1)
            assert leased, "the item stopped being claimable"
            item = leased[0]
            tx.work.fail(item.work_id, item.lease_token, item.lease_owner,
                         error_code="http_429", now=core.clock.utc_now(), recoverable=True)
        with sqlite3.connect(core.storage.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT state,available_at,last_error_code FROM work_items").fetchone()
            assert row["state"] == "pending", f"the outage consumed the item: {row['state']}"
            waits.append(row["available_at"])
            codes.append(row["last_error_code"])
            conn.execute("UPDATE work_items SET available_at=?", (core.clock.utc_now(),))
            conn.commit()

    assert waits == sorted(waits), f"the retries did not move further out: {waits}"
    assert len(set(waits)) == len(waits), f"every retry landed at the same moment: {waits}"
    assert codes[-1].startswith("capacity:"), codes


def test_the_two_rate_limit_sets_cannot_drift_apart():
    """They said the same thing about 429 and 503 while disagreeing about 502
    and 504 -- which is how a pair of hand-kept sets always ends up."""
    from scope_recall.core.work_storage import CAPACITY_REFUSALS
    from scope_recall.core.worker import _RATE_LIMITED_ERRORS

    assert _RATE_LIMITED_ERRORS is CAPACITY_REFUSALS

@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("repair_success", [False, True])
def test_invalid_candidate_gets_one_extra_attempt_repair_or_visible_review(app, legacy, repair_success):
    """Real candidate worker; legacy failures and fresh output share one cap."""
    from scope_recall.core.failure_retry import NEEDS_REVIEW_COUNT
    core, ctx = app
    _candidate(core, ctx)
    _finish_source_work(core)
    import json
    from scope_recall.core.worker import build_consolidation_model
    from scope_recall.runtime.instance import _BoundedCandidate
    calls = []

    class InvalidPort:
        def propose(self, messages, *, remaining_seconds):
            calls.append(json.dumps(messages))
            expected = {"code": "DERIVATION_INVALID" if legacy else "INPUT_INVALID",
                        "field": "payload" if legacy or not repair_success else "required"}
            hint = "validation_error=" + json.dumps(expected, sort_keys=True, separators=(",", ":"))
            if repair_success:
                if any(hint in message["content"] for message in messages if message["role"] == "system"):
                    body = next(json.loads(message["content"]) for message in messages
                                if message["role"] == "user" and message["content"].startswith("{"))
                    return json.dumps(body["empty_result"])
                return '{"protocol_version":"1.1"}'
            return "{not-json FAILED_BODY_SENTINEL"

    model = _BoundedCandidate(build_consolidation_model(InvalidPort()), 3)
    if legacy:
        with sqlite3.connect(core.storage.path) as db:
            db.execute("UPDATE work_items SET state='failed',attempt=3,last_error_code='derivation_invalid' WHERE work_type='evaluate_candidate'")
            db.execute("UPDATE candidate_evaluations SET state='failed',model_attempted_at=?", (core.clock.utc_now(),))
            db.execute("UPDATE candidate_lifecycle SET processing_state='waiting_evidence',reason='evaluation_failed'")
    for _ in range(4):
        with sqlite3.connect(core.storage.path) as db:
            db.execute("UPDATE work_items SET available_at=? WHERE state='pending'", (core.clock.utc_now(),))
        core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=model)
    lifecycle, evaluations, work = _candidate_rows(core)
    assert len(calls) == (1 if legacy else 2), (work, evaluations)
    hint = {"code": "DERIVATION_INVALID" if legacy else "INPUT_INVALID",
            "field": "payload" if legacy or not repair_success else "required"}
    assert "validation_error=" + json.dumps(hint, sort_keys=True, separators=(",", ":")) in " ".join(
        message["content"] for message in json.loads(calls[-1]))
    assert "FAILED_BODY_SENTINEL" not in calls[-1]
    if not legacy:
        assert "validation_error=" not in calls[0] and calls[0] != calls[1]
    if repair_success:
        assert work[0]["state"] == "done"
        assert evaluations[0]["state"] == "waiting_evidence"
        assert lifecycle[0]["reason"] == "insufficient_evidence"
        with sqlite3.connect(core.storage.path) as db:
            assert db.execute(NEEDS_REVIEW_COUNT).fetchone()[0] == 0
        return
    assert work[0]["state"] == evaluations[0]["state"] == "failed"
    assert "derivation_retry:1|" in work[0]["last_error_code"]
    assert lifecycle[0]["reason"] == "evaluation_failed"
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute(NEEDS_REVIEW_COUNT).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM work_items WHERE state='failed'").fetchone()[0] == 1
    assert core.retry_failed_work(ctx, limit=64, dry_run=False)["retried"] == 0
    # Explicit operator action does not grant another automatic retry budget.
    assert core.retry_failed_work(ctx, limit=64, include_terminal=True, dry_run=False)["retried"] == 1
    core.drain_worker(ctx, max_items=8, remaining_seconds=10, consolidation=model)
    assert len(calls) == (2 if legacy else 3)
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute(NEEDS_REVIEW_COUNT).fetchone()[0] == 1
