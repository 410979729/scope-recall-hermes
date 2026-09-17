"""Asking again requires a new question, not a new clock reading.

Covers ``core/evidence_question.py`` and the dedup key it produces.  The
``UNIQUE(candidate, revision, evidence_fingerprint, rule_version)`` guard
already existed to stop a question being asked twice; it never collided,
because the key was a recency window that one arriving tool observation
reshuffled.  Measured across Alpha's six most re-judged candidates, 830
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
    """Replayed over Alpha, re-judgements bought by growing non-first-hand
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


# --------------------------------------------------------------------------
# A question no answer could settle is not asked
# --------------------------------------------------------------------------

def _said(content, *, origin="human_direct", complete=True):
    from scope_recall.core.evidence_question import EvidenceText

    return EvidenceText(origin, complete, content)


def _payload(kind="decision", value="蓝色"):
    return {"kind": kind, "subject": "TEST-project", "predicate": "配色", "value_text": value}


def _unanswerable(payload, evidence):
    from scope_recall.core.evidence_question import unanswerable_reason

    return unanswerable_reason(payload, evidence)


def test_a_value_nobody_wrote_down_cannot_be_promoted():
    """Measured on alpha: 46% of one day's evaluations carried evidence that
    never contained the candidate's value, and qualification needs the value
    inside a quote of the supplied sources."""
    evidence = [_said("TEST-project 配色还没定。"), _said("工具输出：配色文件已保存。", origin="tool_observation")]
    assert _unanswerable(_payload(), evidence) == "value_not_in_evidence"


@pytest.mark.parametrize("content", ["TEST-project 配色 蓝色。", "配色：蓝 色", "ＴＥＳＴ配色是蓝色！"])
def test_spacing_width_and_punctuation_never_hide_the_value(content):
    assert _unanswerable(_payload(), [_said(content)]) is None


@pytest.mark.parametrize("value,content", [("2026-09-17", "上线日期 2026.09.17"),
                                           ("Scope Recall", "scope-recall")])
def test_a_value_is_matched_on_letters_and_digits_only(value, content):
    assert _unanswerable(_payload(kind="fact", value=value), [_said(content)]) is None


def test_the_value_may_sit_in_any_supplied_source():
    """A quote may come from a source that lends no authority as long as an
    authoritative one is cited too; only the authority rule reads origins."""
    evidence = [_said("好的，就按这个。"), _said("TEST-project 配色 蓝色。", origin="assistant_visible")]
    assert _unanswerable(_payload(), evidence) is None


@pytest.mark.parametrize("kind", ["preference", "constraint", "decision", "intention", "alias"])
def test_a_person_only_kind_needs_a_person(kind):
    evidence = [_said("配色 蓝色", origin="tool_observation"), _said("配色 蓝色", origin="external_document")]
    assert _unanswerable(_payload(kind=kind), evidence) == "no_authoritative_evidence"


@pytest.mark.parametrize("origin", ["tool_observation", "external_document"])
def test_a_fact_may_rest_on_an_observation(origin):
    assert _unanswerable(_payload(kind="fact"), [_said("配色 蓝色", origin=origin)]) is None


@pytest.mark.parametrize("origin", ["assistant_visible", "memory_reinjection", "host_generated", "origin_unknown"])
def test_nothing_else_lends_authority(origin):
    assert _unanswerable(_payload(kind="fact"), [_said("配色 蓝色", origin=origin)]) == "no_authoritative_evidence"


def test_an_incomplete_capture_lends_no_authority():
    assert _unanswerable(_payload(), [_said("配色 蓝色", complete=False)]) == "no_authoritative_evidence"


@pytest.mark.parametrize("kind", ["procedure", "intention", "alias"])
def test_kinds_proved_without_the_value_are_not_held_to_it(kind):
    assert _unanswerable(_payload(kind=kind), [_said("这里没有那个值。")]) is None


@pytest.mark.parametrize("payload", [{"kind": "novel_kind", "value_text": "蓝色"}, {"value_text": "蓝色"}, None,
                                     {"kind": "decision", "value_text": ""},
                                     {"kind": "decision", "value_text": "——"}])
def test_what_the_rules_cannot_read_is_left_to_the_model(payload):
    assert _unanswerable(payload, [_said("这里没有那个值。")]) is None


def test_the_rules_are_named_once_in_the_qualification_module():
    """The shortcut imports its sets from claims.py so it cannot drift from the rules it skips."""
    from scope_recall.core import claims

    assert claims._VALUE_FREE_KINDS == frozenset({"procedure", "intention", "alias"})
    assert set(claims._AUTHORITY_ORIGINS) == {"human_direct", "tool_observation", "external_document"}
    assert {"preference", "constraint", "decision"} <= claims._HUMAN_ONLY_KINDS


def test_an_imported_source_speaks_with_its_verified_origin(app):
    from scope_recall.core.evidence_question import evidence_text

    core, ctx = app
    verified = capture(core, ctx, "配色 蓝色", origin="imported", attested=True,
                       source_original_origin="human_direct", key="TEST-import/verified")
    assert evidence_text(verified).origin == "human_direct"
    observed = capture(core, ctx, "配色 蓝色", origin="tool_observation", key="TEST-import/observed")
    assert evidence_text(observed).origin == "tool_observation" and evidence_text(observed).complete
