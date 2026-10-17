"""Candidate evaluations are scheduled once a candidate settles, not per source.

Covers ``core/candidate_debounce.py`` and the sweep that schedules a settled
candidate.  The property that matters is that new evidence does not mint a fresh
evaluation per arrival and supersede the ones waiting, which would retire most
evaluations before anyone judged them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from scope_recall.core.candidate_debounce import (
    MAX_DEFERRAL_SECONDS,
    QUIET_SECONDS,
    SLOW_AFTER_QUESTIONS,
    SLOW_INTERVAL_SECONDS,
    parse_stamp,
    settle_reason,
    slowed_until,
)
from scope_recall.core.claims import Qualification
from test_claims import app, capture, draft

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


# --------------------------------------------------------------------------
# The policy, on its own
# --------------------------------------------------------------------------


def test_evidence_still_arriving_is_left_to_settle():
    assert (
        settle_reason(now=NOW.isoformat(), last_evidence_at=_at(QUIET_SECONDS - 1), created_at=_at(QUIET_SECONDS))
        is None
    )


def test_a_quiet_candidate_is_ready():
    assert settle_reason(now=NOW.isoformat(), last_evidence_at=_at(QUIET_SECONDS)) == "evidence_settled"


def test_a_candidate_that_never_settles_is_judged_anyway():
    """Otherwise a busy subject would keep collecting and never be judged."""
    reason = settle_reason(
        now=NOW.isoformat(),
        last_evidence_at=_at(1),
        last_evaluated_at=_at(MAX_DEFERRAL_SECONDS),
    )
    assert reason == "deferral_limit"


def test_the_deferral_limit_counts_from_the_last_verdict_then_creation():
    within = settle_reason(
        now=NOW.isoformat(),
        last_evidence_at=_at(1),
        last_evaluated_at=_at(MAX_DEFERRAL_SECONDS - 1),
        created_at=_at(MAX_DEFERRAL_SECONDS * 10),
    )
    assert within is None, "a recent verdict resets the clock"
    never = settle_reason(now=NOW.isoformat(), last_evidence_at=_at(1), created_at=_at(MAX_DEFERRAL_SECONDS))
    assert never == "deferral_limit", "a candidate never judged falls back to creation"


def test_a_queued_evaluation_outranks_every_other_rule():
    assert (
        settle_reason(
            now=NOW.isoformat(),
            last_evidence_at=_at(10 * MAX_DEFERRAL_SECONDS),
            created_at=_at(10 * MAX_DEFERRAL_SECONDS),
            has_queued_evaluation=True,
        )
        is None
    )


def test_no_evidence_timestamp_leaves_the_decision_to_the_caller():
    assert settle_reason(now=NOW.isoformat(), last_evidence_at=None) == "no_pending_evidence"


@pytest.mark.parametrize("bad", [None, "", "not-a-time", 17])
def test_unreadable_timestamps_never_schedule(bad):
    assert settle_reason(now=bad, last_evidence_at=_at(QUIET_SECONDS)) is None
    assert settle_reason(now=NOW.isoformat(), last_evidence_at=bad) in {None, "no_pending_evidence"}


def test_a_naive_timestamp_is_read_as_utc_rather_than_rejected():
    """Stored stamps are UTC by convention; a missing offset must not mean never."""
    naive = (NOW - timedelta(seconds=QUIET_SECONDS)).replace(tzinfo=None).isoformat()
    assert settle_reason(now=NOW.isoformat(), last_evidence_at=naive) == "evidence_settled"


# --------------------------------------------------------------------------
# The sweep, against real storage
# --------------------------------------------------------------------------


def _register(core, ctx, index):
    subject, predicate = f"entity{index}", f"property{index}"
    source = capture(core, ctx, f"{subject} {predicate} sharedtoken 值{index}。", key=f"TEST-debounce/{index}")
    proposal = draft(source, f"值{index}", subject=subject, predicate=predicate)
    with core.storage.write(ctx) as tx:
        saved = tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
            recorded_at=core.clock.utc_now(),
        )
        tx.candidates.register(saved.ref, saved.revision, observed_at=core.clock.utc_now())
    return saved


def _retire_live_evaluations(core):
    """Simulate the queued evaluations having run, without judging them."""
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_evaluations SET state='failed' WHERE state='queued'")
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='evaluate_candidate'")
        conn.execute(
            "UPDATE candidate_lifecycle SET processing_state='waiting_evidence',reason='insufficient_evidence'"
        )
        conn.commit()


def _settle(core, *, seconds=None):
    elapsed = QUIET_SECONDS + 60 if seconds is None else seconds
    frozen = datetime.fromisoformat(core.clock.utc_now().replace("Z", "+00:00"))
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            "UPDATE candidate_lifecycle SET last_evidence_at=?", ((frozen - timedelta(seconds=elapsed)).isoformat(),)
        )
        conn.commit()


def _sweep(core, ctx, *, limit=16):
    with core.storage.write(ctx) as tx:
        return tx.candidates.schedule_settled_candidates(now=core.clock.utc_now(), limit=limit)


def _queued(core):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("SELECT count(*) FROM candidate_evaluations WHERE state='queued'").fetchone()[0]


def test_the_sweep_pages_and_finishes_across_passes(app):
    """Bounded per pass, and converging: no pass may exceed its page, and
    repeated passes must reach a fixed point rather than rescheduling forever.

    Not every candidate here is schedulable: each registration captures another
    source carrying the shared trigger term, so the earlier candidates collect
    evidence the later ones never get.  A candidate whose evidence is unchanged
    since its last evaluation collides on the fingerprint and is skipped -- which
    is the loop guard doing its job, so the count is a property of the data
    rather than a number to hard-code.
    """
    core, ctx = app
    for index in range(10):
        _register(core, ctx, index)
    _retire_live_evaluations(core)
    _settle(core)

    passes = []
    for _ in range(6):
        scheduled = _sweep(core, ctx, limit=4)
        passes.append(scheduled)
        if scheduled == 0:
            break
    assert all(count <= 4 for count in passes), f"a pass exceeded its page: {passes}"
    assert passes[-1] == 0, f"the sweep did not settle: {passes}"
    assert sum(passes) == _queued(core) > 0, "every scheduled candidate has exactly one live evaluation"
    assert _sweep(core, ctx, limit=16) == 0, "and it stays settled"


def test_settled_candidates_are_found_by_reading_and_queued_by_name(app):
    """The worker finds the settled candidates with reads only, then queues just those under the writer lease.

    Walking every settling candidate inside the write would hold the lease for seconds of each pass on the shared
    store, and captures waiting for it would fail.
    """
    core, ctx = app
    for index in range(6):
        _register(core, ctx, index)
    _retire_live_evaluations(core)
    _settle(core)
    with core.storage.read(ctx) as tx:
        found = tx.candidates.settled_to_schedule(now=core.clock.utc_now(), limit=64)
    assert found, "the fixture leaves settled candidates with new evidence"
    with core.storage.write(ctx) as tx:
        assert tx.candidates.schedule_settled_candidates(now=core.clock.utc_now(), limit=64, refs=()) == 0
    assert _queued(core) == 0, "no names, nothing queued"
    with core.storage.write(ctx) as tx:
        assert tx.candidates.schedule_settled_candidates(now=core.clock.utc_now(), limit=64, refs=found[:1]) == 1
    with core.storage.write(ctx) as tx:
        rest = tx.candidates.schedule_settled_candidates(now=core.clock.utc_now(), limit=64, refs=found)
    assert rest == len(found) - 1, "the one already queued is checked again and skipped"
    assert _sweep(core, ctx, limit=64) == 0, "the named ones were all the sweep had"


def test_an_unchanged_evidence_set_can_never_be_scheduled_twice(app):
    """The fingerprint collision is the loop guard, so the sweep cannot spin."""
    core, ctx = app
    _register(core, ctx, 0)
    _retire_live_evaluations(core)
    _settle(core)
    assert _sweep(core, ctx) == 0, "the same evidence was already judged once"
    assert _queued(core) == 0


def test_a_candidate_still_collecting_is_not_swept(app):
    core, ctx = app
    _register(core, ctx, 0)
    _retire_live_evaluations(core)
    _settle(core, seconds=QUIET_SECONDS // 2)
    assert _sweep(core, ctx) == 0


def test_a_revoked_candidate_is_never_revived(app):
    """``authority_revoked`` is a decision about permission, not a shortage."""
    core, ctx = app
    _register(core, ctx, 0)
    _retire_live_evaluations(core)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET reason='authority_revoked'")
        conn.execute("DELETE FROM candidate_evaluations")
        conn.commit()
    _settle(core)
    assert _sweep(core, ctx) == 0


def test_the_doctor_can_tell_waiting_on_purpose_from_stuck(app):
    core, ctx = app
    for index in range(3):
        _register(core, ctx, index)
    with core.storage.read(ctx) as tx:
        live = tx.candidates.settling_summary(now=core.clock.utc_now())
    assert live["queued"] == 3 and live["collecting"] == 0 and live["settled_waiting_sweep"] == 0

    _retire_live_evaluations(core)
    _settle(core, seconds=QUIET_SECONDS // 2)
    with core.storage.read(ctx) as tx:
        collecting = tx.candidates.settling_summary(now=core.clock.utc_now())
    assert collecting["queued"] == 0 and collecting["collecting"] == 3

    _settle(core)
    with core.storage.read(ctx) as tx:
        settled = tx.candidates.settling_summary(now=core.clock.utc_now())
    assert settled["settled_waiting_sweep"] == _sweep(core, ctx)
    assert settled["settled_waiting_sweep"] > 0
    assert settled["quiet_seconds"] == QUIET_SECONDS
    assert settled["max_deferral_seconds"] == MAX_DEFERRAL_SECONDS


@pytest.mark.parametrize("limit", [0, -1, 65, 1.0, True])
def test_the_sweep_refuses_an_unbounded_page(app, limit):
    from scope_recall.contracts import ContractError

    core, ctx = app
    with pytest.raises(ContractError):
        _sweep(core, ctx, limit=limit)


# --------------------------------------------------------------------------
# The store's own recalled output is not evidence about the store
# --------------------------------------------------------------------------


def _evidence_rows(core, candidate_ref=None):
    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT source_ref FROM candidate_evidence"
        args = ()
        if candidate_ref:
            sql += " WHERE candidate_ref=?"
            args = (candidate_ref,)
        return [row["source_ref"] for row in conn.execute(sql, args).fetchall()]


def test_reinjected_memory_is_refused_as_candidate_evidence(app):
    """It is this system's own output coming back: it adds nothing the store
    did not already hold, and it stops a candidate ever going quiet."""
    core, ctx = app
    _register(core, ctx, 1)
    echo = capture(core, ctx, "entity1 property1 sharedtoken 值1。", origin="memory_reinjection", key="TEST-echo/1")
    before = set(_evidence_rows(core))
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx.candidates.observe_source(echo.ref, echo.revision, observed_at=core.clock.utc_now())
    assert echo.ref not in set(_evidence_rows(core)), "the echo was admitted as evidence"
    assert set(_evidence_rows(core)) == before


def test_an_ordinary_source_matching_the_same_candidate_is_still_admitted(app):
    """The refusal must be about the echo, not about observe_source at all:
    the identical text from an ordinary origin still becomes evidence."""
    core, ctx = app
    _register(core, ctx, 1)
    more = capture(core, ctx, "entity1 property1 sharedtoken 值1。", key="TEST-more/1")
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx.candidates.observe_source(more.ref, more.revision, observed_at=core.clock.utc_now())
    assert more.ref in set(_evidence_rows(core)), "an ordinary source stopped being evidence"


def test_a_settled_candidate_with_nothing_new_to_ask_is_counted_as_waiting_not_as_work(app):
    """Its evidence was already put to the evaluator, so the sweep schedules nothing for it and it
    keeps ``pending_evaluation`` until new evidence arrives.  That can be nearly every candidate, and the
    doctor's bare "pending 1032" would read as a queue that never drains."""
    from scope_recall.maintenance.doctor_report import DoctorReport
    from scope_recall.maintenance.doctor_store import check_candidates

    core, ctx = app
    _register(core, ctx, 0)
    _settle(core)
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE candidate_evaluations SET state='failed'")
        summary = tx.candidates.settling_summary(now=core.clock.utc_now())
        assert tx.candidates.schedule_settled_candidates(now=core.clock.utc_now(), limit=64) == 0
    assert (
        summary["queued"],
        summary["collecting"],
        summary["settled_waiting_sweep"],
        summary["settled_nothing_to_ask"],
    ) == (0, 0, 0, 1)

    report = DoctorReport(host="hermes", status="ok")
    report.candidate_pending_evaluation, report.candidate_settling = 1, summary
    check_candidates(report)
    line = next(check for check in report.checks if check["name"] == "candidate_processing")
    assert (line["result"], line["detail"]) == ("pending", "due=0,nothing_new_to_ask=1,pending_evaluation=1")


@pytest.mark.parametrize(
    "excluded", ["unchanged", "revoked", "blocked", "suppressed", "old_revision", "no_evidence", "blocked_evidence"]
)
def test_doctor_sweep_share_exact_eligibility(app, excluded):
    core, ctx = app
    candidate = _register(core, ctx, 0)
    _retire_live_evaluations(core)
    _settle(core)
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        if excluded != "unchanged":
            conn.execute("DELETE FROM candidate_evaluations")
        if excluded == "revoked":
            conn.execute("UPDATE candidate_lifecycle SET reason='authority_revoked'")
        elif excluded in {"blocked", "suppressed"}:
            column = "read_blocked" if excluded == "blocked" else "suppressed"
            conn.execute(f"UPDATE claims SET {column}=1 WHERE claim_id=?", (candidate.ref,))
        elif excluded == "old_revision":
            conn.execute("UPDATE claims SET current_revision=current_revision+1 WHERE claim_id=?", (candidate.ref,))
        elif excluded == "no_evidence":
            conn.execute("DELETE FROM candidate_evidence")
        elif excluded == "blocked_evidence":
            conn.execute("UPDATE source_events SET read_blocked=1")
        summary = tx.candidates.settling_summary(now=core.clock.utc_now())
        actual = tx.candidates.schedule_settled_candidates(now=core.clock.utc_now(), limit=64)
        assert summary["settled_waiting_sweep"] == actual == 0
        if excluded == "old_revision":
            conn.execute("UPDATE claims SET current_revision=current_revision-1 WHERE claim_id=?", (candidate.ref,))
    # A clean candidate with a new question is counted and enqueued once.
    if excluded == "unchanged":
        with core.storage.write(ctx) as tx:
            tx._check(write=True).execute("UPDATE candidate_evaluations SET evidence_fingerprint='previous-question'")
            assert tx.candidates.settling_summary(now=core.clock.utc_now())["settled_waiting_sweep"] == 1
            assert tx.candidates.schedule_settled_candidates(now=core.clock.utc_now()) == 1
            assert tx.candidates.settling_summary(now=core.clock.utc_now())["settled_waiting_sweep"] == 0


# --------------------------------------------------------------------------
# A candidate asked many times without settling: one question a day
# --------------------------------------------------------------------------


def test_five_questions_that_settled_nothing_hold_a_candidate_a_day_from_the_last():
    last = NOW - timedelta(seconds=10)
    assert slowed_until(SLOW_AFTER_QUESTIONS - 1, _at(10)) is None
    assert slowed_until(SLOW_AFTER_QUESTIONS, _at(10)) == last + timedelta(seconds=SLOW_INTERVAL_SECONDS)
    for unreadable in (None, "", "not-a-time"):
        assert slowed_until(SLOW_AFTER_QUESTIONS, unreadable) is None
    assert slowed_until(float(SLOW_AFTER_QUESTIONS), _at(10)) is None


def _asked(core, times: int, *, hours_ago: float) -> datetime:
    """``times`` questions put to the evaluator about the one candidate, an hour apart and the last ``hours_ago``,
    none of which settled it; the question it holds now is a new one.  The last question's moment is returned."""
    frozen = datetime.fromisoformat(core.clock.utc_now().replace("Z", "+00:00"))
    moments = [frozen - timedelta(hours=hours_ago + times - 1 - index) for index in range(times)]
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            "UPDATE candidate_evaluations SET evidence_fingerprint='TEST-asked-0',model_attempted_at=?",
            (moments[0].isoformat(),),
        )
        for index, moment in enumerate(moments[1:], 1):
            conn.execute(
                """INSERT INTO candidate_evaluations(candidate_ref,candidate_revision,evidence_fingerprint,
                   evidence_refs_json,rule_version,memory_epoch,state,reason,work_id,created_at,completed_at,
                   model_attempted_at)
                   SELECT candidate_ref,candidate_revision,?,evidence_refs_json,rule_version,memory_epoch,
                   'waiting_evidence','insufficient_evidence',NULL,?,?,? FROM candidate_evaluations LIMIT 1""",
                (f"TEST-asked-{index}", *(moment.isoformat(),) * 3),
            )
        conn.commit()
    return moments[-1]


def _restated(core, ctx) -> None:
    """New evidence no earlier question saw that says the candidate again: past two verdicts nothing else is asked
    about (``AUTOMATIC_VERDICTS``), and a candidate that keeps drawing it is the one asked about every hour."""
    capture(core, ctx, "entity0 property0 sharedtoken 值0。", key="TEST-debounce/restated")


@pytest.mark.parametrize(("times", "hours_ago", "asked_now"), [(4, 1, True), (5, 1, False), (5, 25, True)])
def test_the_sweep_asks_a_candidate_asked_many_times_again_only_once_its_day_is_over(app, times, hours_ago, asked_now):
    """Each question was put when new evidence came, about once an hour for one that kept drawing it.  Five that
    settled nothing hold it a day from the last; what comes meanwhile is asked about together then, and the doctor
    tells such a wait from a stuck one."""
    core, ctx = app
    _register(core, ctx, 0)
    _retire_live_evaluations(core)
    _settle(core)
    _asked(core, times, hours_ago=hours_ago)
    _restated(core, ctx)
    _settle(core)
    with core.storage.read(ctx) as tx:
        summary = tx.candidates.settling_summary(now=core.clock.utc_now())
    assert (summary["settled_waiting_sweep"], summary["slowed"]) == ((1, 0) if asked_now else (0, 1))
    assert _sweep(core, ctx) == int(asked_now)


@pytest.mark.parametrize(("times", "asked_now"), [(4, True), (5, False)])
def test_evidence_past_the_deferral_limit_asks_a_slowed_candidate_nothing(app, times, asked_now):
    """New evidence asks at once about a candidate that waited out the deferral limit, unless it is slowed."""
    core, ctx = app
    _register(core, ctx, 0)
    _retire_live_evaluations(core)
    _asked(core, times, hours_ago=2)
    two_hours_ago = datetime.fromisoformat(core.clock.utc_now().replace("Z", "+00:00")) - timedelta(hours=2)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            "UPDATE candidate_lifecycle SET last_evaluated_at=?,created_at=?", (two_hours_ago.isoformat(),) * 2
        )
        conn.commit()
    _restated(core, ctx)
    assert _queued(core) == int(asked_now)


@pytest.mark.parametrize(("times", "slowed"), [(4, False), (5, True)])
def test_a_worker_s_wake_plan_waits_for_a_slowed_candidate_s_day_not_for_its_evidence(app, times, slowed):
    """Woken when its evidence settled, a pass would schedule nothing for a slowed candidate and the plan would name
    the same moment again: the worker would spin."""
    core, ctx = app
    _register(core, ctx, 0)
    _retire_live_evaluations(core)
    _settle(core)
    last = _asked(core, times, hours_ago=1)
    _restated(core, ctx)
    _settle(core)
    now = core.clock.utc_now()
    with core.storage.read(ctx) as tx:
        planned = parse_stamp(tx.candidates.next_settle_at(after=None, now=now))
    if slowed:
        assert planned == last + timedelta(seconds=SLOW_INTERVAL_SECONDS)
    else:
        assert planned is not None and planned <= parse_stamp(now)


def test_a_pass_that_deferred_a_slowed_candidate_leaves_it_its_wake(app):
    """A pass after the evidence settled deferred the candidate to its day.  The plan after that pass read only the
    candidates touched in the quiet window before the pass began and named no wake: with nothing else to do, the
    worker slept through the day, and the doctor called the candidate stuck."""
    core, ctx = app
    _register(core, ctx, 0)
    _retire_live_evaluations(core)
    last = _asked(core, SLOW_AFTER_QUESTIONS, hours_ago=3)
    _restated(core, ctx)
    evidence = last + timedelta(minutes=10)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE candidate_lifecycle SET last_evidence_at=?,updated_at=?", (evidence.isoformat(),) * 2)
        conn.commit()
    deferred_by = evidence + timedelta(hours=1)
    with core.storage.read(ctx) as tx:
        planned = tx.candidates.next_settle_at(after=deferred_by.isoformat(), now=core.clock.utc_now())
    assert parse_stamp(planned) == last + timedelta(seconds=SLOW_INTERVAL_SECONDS)
