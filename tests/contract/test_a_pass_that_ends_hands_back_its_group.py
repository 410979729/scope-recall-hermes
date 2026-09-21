"""A pass that runs out of time hands back what it claimed and never touched, unspent.

Found on a live instance on 2026-09-21.  Its ``max_items`` had been raised to 1000 for a one-off drain
and never put back, so a pass claimed its whole embedding backlog as one group.  Every claim spends one
of an item's three attempts.  The worker does hand the rest of a group back unspent when a group is cut
short, but not when the reason was that the pass had no time left, which is the usual reason: the
items at the tail were leased, never looked at, and released by lease expiry with the attempt gone.
Three passes later they were failed as ``lease_exhausted``; the automatic recovery re-opened them into
the same oversized group four times; 888 embeddings ended ``auto_retry:4|lease_exhausted`` in an hour
without one of them ever having been sent to the provider.
"""
from __future__ import annotations

import sqlite3
import time

from tests.contract.test_rc40_embed_batch import Recording, _sources
from tests.contract.test_v11_claims import app  # noqa: F401  (fixture)


def _rows(core):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("SELECT subject_ref, state, attempt, lease_owner FROM work_items "
                            "WHERE work_type='embed' ORDER BY work_id").fetchall()


def test_the_untouched_rest_of_a_group_is_pending_again_with_its_attempt_unspent(app):
    core, ctx = app
    made = _sources(core, ctx, 6, tag="late")
    late = [0.0]
    core.clock.monotonic = lambda: time.monotonic() + late[0]

    class OutOfTime(Recording):
        """The pass's time is gone while its second member is being published."""

        def publish_source(self, prepared, *, source, **kwargs):
            if len(self.published) == 1:
                late[0] = 100_000.0
            return super().publish_source(prepared, source=source, **kwargs)

    port = OutOfTime()
    core.drain_worker(ctx, max_items=16, remaining_seconds=20, owner_id="late", embed=port)

    rows = _rows(core)
    assert port.groups == [6], "the six were claimed as one group"
    assert len(port.published) == 1, "the pass stopped where its time did, inside its second member"
    assert rows[0][1] == "done" and rows[0][0] in {source.ref for source in made}
    # The second member was being worked on when the time went; what it spent is its own.
    for ref, state, attempt, owner in rows[2:]:
        assert state == "pending" and owner is None, f"{ref} is still {state}, leased to {owner}, until its lease runs out"
        assert attempt == 0, f"{ref} was never looked at and has spent {attempt} attempt(s)"


def test_passes_cut_short_again_and_again_fail_nothing_that_was_never_tried(app):
    """The whole sequence that failed 888 rows: pass after pass out of time after one item, leases expiring between them."""
    core, ctx = app
    made = _sources(core, ctx, 5, tag="again")
    late = [0.0]
    core.clock.monotonic = lambda: time.monotonic() + late[0]

    class OneThenOutOfTime(Recording):
        def publish_source(self, prepared, *, source, **kwargs):
            if self.published:
                late[0] += 100_000.0
            return super().publish_source(prepared, source=source, **kwargs)

    for minute in range(10, 70, 10):
        core.drain_worker(ctx, max_items=16, remaining_seconds=20, owner_id=f"again-{minute}", embed=OneThenOutOfTime())
        core.clock.now = f"2026-09-06T12:{minute}:00Z"  # every lease of the pass before has run out
        states = {row[0]: (row[1], row[3]) for row in _rows(core)}
        assert "failed" not in {state for state, _owner in states.values()}, states
    rows = _rows(core)
    assert [row[1] for row in rows].count("done") >= len(made) - 1, rows
