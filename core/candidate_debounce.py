"""When a candidate's evidence has settled enough to be worth judging.

The measured problem: every source that mentions a candidate adds evidence,
every addition changes the evidence fingerprint, and every new fingerprint
queued a fresh evaluation while superseding the ones still waiting.  On TianShu
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

from datetime import datetime, timezone

#: Seconds of no new evidence after which a candidate is judged settled.
QUIET_SECONDS = 900

#: Longest a candidate may keep accumulating before being judged anyway.
MAX_DEFERRAL_SECONDS = 3600

def _parse(value: object) -> datetime | None:
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
    moment = _parse(now)
    if moment is None:
        return None
    settled = _parse(last_evidence_at)
    if settled is None:
        # No evidence timestamp at all: nothing has arrived to wait for, so the
        # caller's own state decides.  Treating this as "settled" keeps paths
        # that seed a candidate from its existing links working unchanged.
        return "no_pending_evidence"
    if (moment - settled).total_seconds() >= quiet_seconds:
        return "evidence_settled"
    waiting_since = _parse(last_evaluated_at) or _parse(created_at)
    if waiting_since is not None and (moment - waiting_since).total_seconds() >= max_deferral_seconds:
        return "deferral_limit"
    return None

# The timer says *when it is worth looking*; it does not decide whether there
# is anything to ask. That is ``core/evidence_question.py``, and it is a
# content test, not a rate limit -- a candidate is never made to wait out a
# clock while it holds evidence nobody has judged.


__all__ = ["MAX_DEFERRAL_SECONDS", "QUIET_SECONDS", "settle_reason"]
