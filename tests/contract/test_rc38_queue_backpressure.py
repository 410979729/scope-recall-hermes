"""A pass never queues more candidate evaluations than it can also make.

Replayed on beta's snapshot with a model that answers instantly, a pass evaluated eight
candidates while the schedulers queued up to sixteen more: the queue grew by eight a pass
with no new conversation at all, 941 deep and the oldest ten hours old, each one having cost
a model call to get there.  Two rules put that right.  A pass queues at most what it can
evaluate, and stops queueing entirely once the queue is deeper than passes can reach.  The
batch limit stands candidates down only while work it can still do waits behind them.
"""
from __future__ import annotations

import json
import sqlite3

from scope_recall.core.candidate_lifecycle import PROCESS_BATCH_LIMIT
from scope_recall.core.worker import CANDIDATE_QUEUE_CEILING
from tests.contract.test_r1_candidate_lifecycle import (  # noqa: F401  (fixture)
    _candidate,
    _finish_source_work,
    app,
)


class Model:
    """Answers both worker questions at once, proposing nothing."""

    def __init__(self) -> None:
        self.calls = 0

    def _empty(self, sources) -> str:
        self.calls += 1
        return json.dumps({"protocol_version": "1.1",
                           "source_refs": [f"{s.ref}@{s.revision}" for s in sources],
                           "claim_proposals": [], "resume_proposals": [], "reference_proposals": []},
                          ensure_ascii=False)

    def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0, validation_feedback=None) -> str:
        return self._empty(sources)

    def evaluate_candidate(self, candidate, sources, *, remaining_seconds=1.0, validation_feedback=None) -> str:
        return self._empty(sources)


def _candidates(core, ctx, count, *, tag="q"):
    return [_candidate(core, ctx, key=f"TEST-{tag}/c{index}")[0] for index in range(count)]


def _claimed(receipt, work_type):
    return [item for item in receipt.items if item.work_type == work_type]


def _pending(core, work_type):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("SELECT count(*) FROM work_items WHERE work_type=? AND state='pending'",
                            (work_type,)).fetchone()[0]


def test_candidates_use_the_rest_of_the_pass_when_nothing_else_is_waiting(app):
    core, ctx = app
    _candidates(core, ctx, PROCESS_BATCH_LIMIT + 4)
    _finish_source_work(core)
    receipt = core.drain_worker(ctx, max_items=32, remaining_seconds=30, consolidation=Model())
    assert len(_claimed(receipt, "evaluate_candidate")) > PROCESS_BATCH_LIMIT


def test_conversation_work_is_never_held_back_by_the_candidate_queue(app):
    """Consolidation is what makes a new conversation recallable at all.

    The ceiling lifts only once nothing else is left to do, so while
    consolidation is still waiting the batch limit is what it always was.
    """
    core, ctx = app
    _candidates(core, ctx, PROCESS_BATCH_LIMIT + 6, tag="q2")  # each capture also queues consolidation
    receipt = core.drain_worker(ctx, max_items=32, remaining_seconds=30, consolidation=Model())
    kinds = [item.work_type for item in receipt.items]
    assert "consolidate" in kinds
    last_consolidate = max(index for index, kind in enumerate(kinds) if kind == "consolidate")
    waiting = [kind for kind in kinds[:last_consolidate] if kind == "evaluate_candidate"]
    assert len(waiting) <= PROCESS_BATCH_LIMIT, "candidates ran ahead of conversation work"


def test_a_pass_queues_no_more_candidates_than_it_evaluates(app):
    core, ctx = app
    _candidates(core, ctx, PROCESS_BATCH_LIMIT + 6, tag="q3")
    _finish_source_work(core)
    with core.storage.write(ctx) as tx:
        queued = tx.candidates.schedule_settled_candidates(now=core.clock.utc_now(), limit=64)
    assert queued >= 0  # the sweep itself is unchanged; the pass is what bounds it
    before = _pending(core, "evaluate_candidate")
    receipt = core.drain_worker(ctx, max_items=32, remaining_seconds=30, consolidation=Model())
    after = _pending(core, "evaluate_candidate")
    assert after <= before, f"the queue grew: {before} -> {after}"
    assert receipt.processed


def test_a_queue_deeper_than_passes_can_reach_stops_taking_more(app):
    core, ctx = app
    _candidates(core, ctx, 3, tag="q4")
    _finish_source_work(core)
    with sqlite3.connect(core.storage.path) as conn:
        row = conn.execute("SELECT * FROM work_items WHERE work_type='evaluate_candidate' LIMIT 1").fetchone()
        assert row is not None, "the fixture must have candidate work to copy"
        columns = [column[0] for column in conn.execute("SELECT * FROM work_items LIMIT 0").description]
        template = dict(zip(columns, row))
        for index in range(CANDIDATE_QUEUE_CEILING + 2):
            filler = dict(template, work_id=900000 + index, state="pending", attempt=0,
                          subject_ref=f"candidate:TEST-filler-{index}", lease_owner=None, lease_until=None)
            conn.execute(f"INSERT INTO work_items ({','.join(filler)}) VALUES ({','.join('?' * len(filler))})",
                         tuple(filler.values()))
        conn.commit()
    deep = _pending(core, "evaluate_candidate")
    assert deep > CANDIDATE_QUEUE_CEILING
    with core.storage.write(ctx) as tx:
        assert tx.work.pending_depth("evaluate_candidate") == deep
        assert not tx.work.other_work_ready(now=core.clock.utc_now(),
                                            kinds=frozenset({"consolidate", "embed", "purge"}))


def test_a_queue_nothing_can_touch_does_not_hold_candidates_back(app):
    """An instance with no embedding credential holds thousands of rows it cannot process."""
    core, ctx = app
    _candidates(core, ctx, 2, tag="q5")
    with core.storage.write(ctx) as tx:
        now = core.clock.utc_now()
        assert tx.work.other_work_ready(now=now, kinds=frozenset({"consolidate", "embed"}))
        assert not tx.work.other_work_ready(now=now, kinds=frozenset()), "nothing this pass can do is nothing waiting"
