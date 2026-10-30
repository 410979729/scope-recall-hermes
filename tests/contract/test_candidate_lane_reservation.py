"""A backlog lane that is always ready must not starve candidate evaluation.

``_CLAIM_ORDER`` deliberately ranks candidate evaluation last, so an instance
with any older ready embed or consolidate row never reaches it.  A pass then
claims ``max_items`` rows that are all backlog, and ``candidate_batch_limit``
bounds a batch that is never started.
"""

from __future__ import annotations

import sqlite3

from scope_recall.core.worker import WorkerConfig, drain_worker
from scope_recall.core.work_storage import ALLOWED_WORK_TYPES
from test_claims import app, capture

#: What a reserved claim may reach: evaluation plus purge, which keeps its
#: absolute priority inside the reservation.
RESERVED = frozenset({"purge", "evaluate_candidate"})


def _backlog_with_one_candidate(core, ctx, events=20):
    """Real backlog rows plus one ready candidate row behind all of them."""
    # Deeper than any pass in this test, so the backlog alone fills one.
    for index in range(events):
        capture(core, ctx, f"TEST 排队 {index}。", key=f"TEST-lane/{index}")
    with sqlite3.connect(core.storage.path) as conn:
        oldest = conn.execute(
            "SELECT work_id FROM work_items ORDER BY work_id LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE work_items SET work_type='evaluate_candidate',state='pending',attempt=0,"
            "last_error_code=NULL WHERE work_id=?",
            (oldest,),
        )
        conn.commit()
    return oldest


def _claim(core, ctx, allowed, owner):
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        return tx.work.claim_next(
            owner, core.clock.utc_now(), lease_seconds=30, limit=1, allowed_work_types=allowed
        )


def test_fifo_alone_never_reaches_the_candidate(app):
    core, ctx = app
    candidate = _backlog_with_one_candidate(core, ctx)
    claimed = {}
    for index in range(4):
        for item in _claim(core, ctx, ALLOWED_WORK_TYPES, f"TEST-fifo-{index}"):
            claimed[item.work_id] = item.work_type
    assert candidate not in claimed, "FIFO alone already reached the candidate; the premise is gone"
    assert claimed, "the pass claimed nothing at all; the fixture is wrong"


def test_reserved_lane_reaches_the_candidate(app):
    core, ctx = app
    candidate = _backlog_with_one_candidate(core, ctx)
    for index in range(4):
        _claim(core, ctx, ALLOWED_WORK_TYPES, f"TEST-fifo-{index}")
    leased = _claim(core, ctx, RESERVED, "TEST-reserved")
    assert [item.work_id for item in leased] == [candidate]


class RefusingEmbed:
    """A present embedding port, so the backlog lane stays claimable.

    Without it the pass has no backlog to claim at all: every embed and
    consolidate type drops out of the allowed set and the candidate is the only
    claimable row, which would hide the starvation the test is about.
    """

    def prepare_source(self, source, *, remaining_seconds):
        raise RuntimeError("TEST embed refusal")


class RefusingCandidate:
    """A port that is present (so evaluation is allowed) and always refuses."""

    calls = 0

    def evaluate_candidate(self, *args, **kwargs):
        RefusingCandidate.calls += 1
        raise RuntimeError("TEST candidate refusal")


def _attempt_of(core, work_id):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("SELECT attempt FROM work_items WHERE work_id=?", (work_id,)).fetchone()[0]


def test_a_pass_claims_the_candidate_behind_a_backlog(app):
    core, ctx = app
    candidate = _backlog_with_one_candidate(core, ctx)
    RefusingCandidate.calls = 0
    drain_worker(
        core.storage,
        core.clock,
        ctx,
        # One embedding per claim keeps the pass at one page per receipt, so the
        # every-fourth claim is reachable inside a small pass.  A default pass
        # fills a whole embedding group from one page, and that is exactly why
        # the reservation counts claim pages rather than receipt items.
        config=WorkerConfig(owner_id="TEST-lane", max_items=8, embed_batch_limit=1),
        candidate=RefusingCandidate(),
        embed=RefusingEmbed(),
        remaining_seconds=20,
    )
    assert _attempt_of(core, candidate) > 0, "the pass never claimed the candidate"
