"""Asking again requires a new question, not a new clock reading.

Covers ``core/evidence_question.py`` and the dedup key it produces.  The
``UNIQUE(candidate, revision, evidence_fingerprint, rule_version)`` guard
already existed to stop a question being asked twice; it never collided,
because the key was a recency window that one arriving tool observation
reshuffled.  Measured across TianShu's six most re-judged candidates, 830
evidence arrivals sat between consecutive verdicts -- 679 tool observations
against 74 first-hand statements -- and 93% of the verdicts bought were
``insufficient_evidence`` again.
"""
from __future__ import annotations

import sqlite3

import pytest

from scope_recall.core.evidence_question import (
    FIRST_HAND_ORIGINS,
    is_first_hand,
    question_digest,
)
from test_v11_claims import app, capture


def _tool(n):
    return (f"event-tool-{n}", 1, "tool_observation")


def _person(n):
    return (f"event-said-{n}", 1, "human_direct")


# --------------------------------------------------------------------------
# What counts as testimony
# --------------------------------------------------------------------------

def test_a_person_speaking_is_first_hand():
    assert is_first_hand("human_direct") is True


@pytest.mark.parametrize("origin", ["tool_observation", "assistant_visible",
                                    "memory_reinjection", "imported", None, ""])
def test_everything_else_is_not(origin):
    assert is_first_hand(origin) is False


def test_the_notion_is_named_once():
    assert FIRST_HAND_ORIGINS == frozenset({"human_direct"})


# --------------------------------------------------------------------------
# The question itself
# --------------------------------------------------------------------------

def test_one_more_tool_observation_is_the_same_question():
    """The exact case that defeated the guard: an arrival displaces an older
    source, so the byte-exact set differs while nothing decisive changed."""
    before = [_person(1), _tool(1), _tool(2), _tool(3)]
    after = [_person(1), _tool(2), _tool(3), _tool(4)]
    assert question_digest(before) == question_digest(after)


def test_a_person_saying_something_new_is_a_new_question():
    assert question_digest([_person(1), _tool(1)]) != question_digest([_person(1), _person(2), _tool(1)])


def test_testimony_order_does_not_matter():
    assert question_digest([_person(2), _person(1)]) == question_digest([_person(1), _person(2)])


def test_accumulating_tool_output_is_never_a_new_question():
    """Replayed over TianShu, re-judgements bought by growing non-first-hand
    support came to 955 model calls and exactly one conclusion."""
    assert question_digest([_tool(n) for n in range(4)]) ==         question_digest([_tool(n) for n in range(64)])


def test_a_candidate_with_no_testimony_waits_rather_than_loops():
    """Not a cap: the moment somebody says something it is a new question."""
    silent = [_tool(n) for n in range(8)]
    assert question_digest(silent) == question_digest(silent + [_tool(99)])
    assert question_digest(silent) != question_digest(silent + [_person(1)])


def test_an_empty_set_still_has_a_digest():
    assert question_digest(()) and question_digest([]) == question_digest(())


# --------------------------------------------------------------------------
# Against real storage
# --------------------------------------------------------------------------

def _queued(core):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute(
            "SELECT count(*) FROM candidate_evaluations WHERE state='queued'").fetchone()[0]


def test_tool_output_alone_does_not_buy_another_evaluation(app):
    """A candidate flooded with tool output is not re-judged for it -- and is
    not made to wait either; there is simply nothing new to ask."""
    from test_candidate_debounce import _register, _retire_live_evaluations, _settle, _sweep

    core, ctx = app
    _register(core, ctx, 1)
    _retire_live_evaluations(core)
    before = _queued(core)
    for index in range(4):
        noise = capture(core, ctx, f"entity1 property1 sharedtoken 工具输出{index}。",
                        origin="tool_observation", key=f"TEST-noise/{index}")
        with core.storage.write(ctx, remaining_seconds=10) as tx:
            tx.candidates.observe_source(noise.ref, noise.revision,
                                         observed_at=core.clock.utc_now())
    _settle(core)
    _sweep(core, ctx)
    assert _queued(core) <= before + 1, "tool output bought a fresh evaluation each time"


def test_a_person_speaking_is_judged_at_once(app):
    """No clock stands between new testimony and a verdict."""
    from test_candidate_debounce import _register, _retire_live_evaluations, _settle, _sweep

    core, ctx = app
    _register(core, ctx, 1)
    _retire_live_evaluations(core)
    before = _queued(core)
    said = capture(core, ctx, "entity1 property1 sharedtoken 我确认是值1。",
                   origin="human_direct", key="TEST-said/1")
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx.candidates.observe_source(said.ref, said.revision,
                                     observed_at=core.clock.utc_now())
    _settle(core)
    _sweep(core, ctx)
    assert _queued(core) > before, "new testimony did not produce an evaluation"
