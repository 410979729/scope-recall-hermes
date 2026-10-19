"""When a candidate's evidence has settled enough to be judged.

The default 900-second quiet window coalesces evidence arrivals before an
evaluation.  The 3,600-second deferral limit, measured from the last evaluation
or creation, makes the candidate eligible despite continued arrivals.  Allowing
only one queued evaluation per candidate avoids repeatedly superseding pending
work.

Evidence arriving while an evaluation waits remains available for the next
evaluation; an unchanged evidence fingerprint does not queue a duplicate.

Timestamp storage belongs to ``core/candidate_intake.py`` and
``core/candidate_sweeps.py``; choosing the evidence set belongs to the caller.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: Seconds of no new evidence after which a candidate is judged settled.
QUIET_SECONDS = 900

#: Longest a candidate may keep accumulating before being judged anyway.
MAX_DEFERRAL_SECONDS = 3600

#: Questions put to the evaluator about one candidate version, none of which settled it, after which it is put to the
#: evaluator at most once a day (``slowed_until``).  Without it a candidate that keeps drawing evidence is asked again
#: each time some comes, about once an hour.  Over one week on a shared store: 26,909 questions, 91% of them answered
#: "not enough evidence yet" and 112 with a fact; of the 306 candidates ever resolved, 80% were resolved within their
#: first five questions, while 556 had been asked twenty times or more.  A day's pace after five questions would have
#: spared 42% of that week's questions and delayed, by at most a day, the fifth of resolutions that needed more.
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
