"""When a candidate's evidence has settled enough to be worth judging.

The measured problem: every source that mentions a candidate adds evidence,
every addition changes the evidence fingerprint, and every new fingerprint
queued a fresh evaluation while superseding the ones still waiting.  In one store
that produced 11,158 evaluations of which **7,802 were retired before anyone
judged them** -- 69.9%, nearly all ``superseded_by_new_evidence`` -- and exactly
**2** ever reached ``resolved / fact_active``.  The queue could not outrun its
own input.

Replaying the real arrival times of all 11,667 evidence rows across 402
candidates says what a quiet window is worth:

    inter-arrival within one candidate   p25 286s   p50 510s   p90 2,127s
    quiet window   60s ->  11,234 evaluations   (98% of today; useless)
    quiet window  300s ->   8,664               (76%; the obvious guess, too short)
    quiet window  900s ->   3,736               (33%)
    quiet window 1800s ->   1,966               (17%, but p50 verdict latency 30 min)

Hence ``QUIET_SECONDS = 900``.  Five minutes -- the intuitive choice -- sits
below the median gap between two pieces of evidence for the same candidate and
would have absorbed almost nothing.

The window alone still leaves a tail: at 900s the wait for a verdict is p50
900s but p99 3.2 hours, because a candidate that keeps attracting evidence
never settles.  ``MAX_DEFERRAL_SECONDS`` caps that: after an hour of
accumulating, judge it on what is there.

The second rule matters more than the window: **at most one queued evaluation
per candidate**.  That is what actually removes the supersede loop, because a
second one can no longer be created to retire the first.  Evidence that arrives
meanwhile is not lost -- it lands in ``candidate_evidence`` and joins the next
evaluation, whose fingerprint then differs; an unchanged evidence set collides
on that fingerprint and schedules nothing, so this cannot spin.

Not responsible for: reading or writing any of these timestamps
(``core/candidate_intake.py`` and ``core/candidate_sweeps.py`` own the SQL), or
for choosing the evidence set.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: Seconds of no new evidence after which a candidate is judged settled.
QUIET_SECONDS = 900

#: Longest a candidate may keep accumulating before being judged anyway.
MAX_DEFERRAL_SECONDS = 3600

#: Questions put to the evaluator about one candidate version, none of which settled it, after which it is put to the
#: evaluator at most once a day (``slowed_until``).  A candidate that keeps drawing evidence was asked again each time
#: some came, about once an hour.  Over the week to 2026-10-10 on the shared store: 26,909 questions, 91% of them
#: answered "not enough evidence yet" and 112 with a fact; of the 306 candidates ever resolved, 80% were resolved within
#: their first five questions, while 556 had been asked twenty times or more.  A day's pace after five questions would
#: have spared 42% of that week's questions and delayed, by at most a day, the fifth of resolutions that needed more.
SLOW_AFTER_QUESTIONS = 5
SLOW_INTERVAL_SECONDS = 86400


def parse_stamp(value: object) -> datetime | None:
    """A stored time; one written without a zone is UTC, and anything that is not a time is ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def settle_reason(
    *,
    now: object,
    last_evidence_at: object,
    last_evaluated_at: object = None,
    created_at: object = None,
    has_queued_evaluation: bool = False,
    quiet_seconds: int = QUIET_SECONDS,
    max_deferral_seconds: int = MAX_DEFERRAL_SECONDS,
) -> str | None:
    """Why this candidate should be scheduled now, or ``None`` to keep waiting.

    Returning a reason rather than a boolean means the scheduled row records
    which rule fired, so an operator reading the queue can tell "the evidence
    stopped" from "we waited long enough" without re-deriving it.
    """
    if has_queued_evaluation:
        return None
    moment = parse_stamp(now)
    if moment is None:
        return None
    settled = parse_stamp(last_evidence_at)
    if settled is None:
        # No evidence timestamp at all: nothing has arrived to wait for, so the
        # caller's own state decides.  Treating this as "settled" keeps paths
        # that seed a candidate from its existing links working unchanged.
        return "no_pending_evidence"
    if (moment - settled).total_seconds() >= quiet_seconds:
        return "evidence_settled"
    waiting_since = parse_stamp(last_evaluated_at) or parse_stamp(created_at)
    if waiting_since is not None and (moment - waiting_since).total_seconds() >= max_deferral_seconds:
        return "deferral_limit"
    return None


def settles_at(
    *,
    last_evidence_at: object,
    last_evaluated_at: object = None,
    created_at: object = None,
    quiet_seconds: int = QUIET_SECONDS,
    max_deferral_seconds: int = MAX_DEFERRAL_SECONDS,
) -> datetime | None:
    """The moment, once its latest evidence has come, from which ``settle_reason`` gives a reason: the end of the
    quiet window or the deferral limit, whichever comes first, and never before that evidence.  ``None`` when there
    is no evidence timestamp, which is settled already.  A worker's wake plan waits for it
    (``runtime/scheduling.next_wake``).
    """
    evidence = parse_stamp(last_evidence_at)
    if evidence is None:
        return None
    moment = evidence + timedelta(seconds=quiet_seconds)
    waiting_since = parse_stamp(last_evaluated_at) or parse_stamp(created_at)
    if waiting_since is not None:
        moment = min(moment, waiting_since + timedelta(seconds=max_deferral_seconds))
    return max(moment, evidence)


def slowed_until(questions: object, last_asked_at: object) -> datetime | None:
    """When a candidate version put to the evaluator ``questions`` times, the last at ``last_asked_at``, may be put to
    it again: a day after the last, once ``SLOW_AFTER_QUESTIONS`` questions have not settled it; ``None`` when nothing
    holds it back.  The evidence that comes meanwhile is kept and asked about together then, so a slowed candidate
    waits and is never dropped: the settle sweep and a worker's wake plan take it up when its day is over."""
    if type(questions) is not int or questions < SLOW_AFTER_QUESTIONS:
        return None
    last = parse_stamp(last_asked_at)
    return None if last is None else last + timedelta(seconds=SLOW_INTERVAL_SECONDS)


# The timers say *when it is worth looking*: the quiet window and the deferral
# when the evidence has settled, the slow pace how often a candidate that keeps
# not settling is worth another question.  None of them decides whether there is
# anything to ask; that is ``core/evidence_question.py``, a content test.


__all__ = [
    "MAX_DEFERRAL_SECONDS",
    "QUIET_SECONDS",
    "SLOW_AFTER_QUESTIONS",
    "SLOW_INTERVAL_SECONDS",
    "parse_stamp",
    "settle_reason",
    "settles_at",
    "slowed_until",
]
