"""A recalled question reaches the reply it was given.

Over the benchmark's real questions, alpha answered 21 of 30 and beta 14 of 25: the
question itself came back, and its neighbours, but not the answer.  A reply is joined to
its question only through episode membership, which is spent on every event of the episode
in id order, so a question recalled at all could use the whole relation bound before
reaching what it was told.  The turn a person's message opened is followed first, for
every seed, and costs at most three objects of the bound.
"""
from __future__ import annotations

from dataclasses import replace
import time

from scope_recall.core.retrieval import CandidateRef, SearchContext
from scope_recall.core.retrieval_storage import TURN_REPLY_LIMIT, RetrievalStorage
from tests.contract.test_rc33_recall_accuracy import _packet, _say  # noqa: F401  (helpers)
from tests.contract.test_v11_claims import accept, app, draft  # noqa: F401  (fixture)
from tests.v11_support import recall_request

ASK = "TEST-project 的发布窗口改到几点了？"
TOLD = "周五晚上十一点。"


def _expanded(core, ctx, seed_source, *, relation_objects):
    context = SearchContext.from_request(recall_request(query=ASK, mode="current", max_items=6), ctx,
                                         now=core.clock.utc_now(), deadline=time.monotonic() + 30)
    context = replace(context, limits=replace(context.limits, relation_objects=relation_objects))
    seeds = (CandidateRef("event", seed_source.ref, seed_source.revision, "lexical", rank=1, lexical_score=2.0),)
    with core.storage.read(ctx) as tx:
        return [candidate.ref for candidate in core.recall_pipeline._expand(tx, context, seeds, [])]


def _turn_replies(core, ctx, source):
    with core.storage.read(ctx) as tx:
        seed = CandidateRef("event", source.ref, source.revision, "lexical", rank=1, lexical_score=2.0)
        return [reply.ref for reply in RetrievalStorage().turn_replies(tx, seed)]


def test_a_question_reaches_its_answer_with_the_relation_bound_nearly_spent(app):
    """The reply shares no word with the question, the way a real answer rarely repeats it.

    With two objects to spend -- what an episode of hundreds leaves a recalled
    question -- expansion reached the episode and one arbitrary neighbour of it.
    """
    core, ctx = app
    question = _say(core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/ask")
    answer = _say(core, ctx, TOLD, origin="assistant_visible", role="assistant",
                  when="2026-09-02T09:00:12Z", key="TEST-turn/answer")
    assert answer.ref in _expanded(core, ctx, question, relation_objects=2)


def test_the_turn_takes_at_most_half_the_relation_bound(app):
    """What a question was told is not the only way to answer it.

    Read first and without a bound of its own, a long turn spent the whole
    relation allowance, and the claims and episodes reached from the same seed
    were never inspected at all.
    """
    core, ctx = app
    question = _say(core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/room-ask")
    replies = [_say(core, ctx, f"第{n}段：周五晚上十一点。", origin="assistant_visible", role="assistant",
                    when=f"2026-09-02T09:00:{n + 10}Z", key=f"TEST-turn/room-{n}") for n in range(TURN_REPLY_LIMIT)]
    claim = accept(core, ctx, draft(question, "周五晚上十一点", predicate="发布窗口")).items[0]

    refs = _expanded(core, ctx, question, relation_objects=3)
    assert claim.ref in refs, "the claim behind the question is still reached"
    assert any(reply.ref in refs for reply in replies), "and the turn is still followed"


def test_the_answer_is_delivered_in_the_packet(app):
    core, ctx = app
    question = _say(core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/ask")
    answer = _say(core, ctx, TOLD, origin="assistant_visible", role="assistant",
                  when="2026-09-02T09:00:12Z", key="TEST-turn/answer")
    refs = [item["ref"] for item in _packet(core, ctx, ASK)["items"]]
    assert question.ref in refs and answer.ref in refs


def test_a_reply_belongs_to_the_turn_it_was_written_in(app):
    """Once the person speaks again the turn is over; later replies answer that message."""
    core, ctx = app
    first = _say(core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/ask-1")
    mine = _say(core, ctx, TOLD, origin="assistant_visible", role="assistant",
                when="2026-09-02T09:00:12Z", key="TEST-turn/answer-1")
    second = _say(core, ctx, "那值班表呢", origin="human_direct", role="user",
                  when="2026-09-02T09:01:00Z", key="TEST-turn/ask-2")
    theirs = _say(core, ctx, "值班表还是老样子。", origin="assistant_visible", role="assistant",
                  when="2026-09-02T09:01:09Z", key="TEST-turn/answer-2")

    assert _turn_replies(core, ctx, first) == [mine.ref]
    assert _turn_replies(core, ctx, second) == [theirs.ref]


def test_a_whole_turn_captured_under_one_timestamp_keeps_its_order(app):
    """A gateway can write a turn's rows with one occurred_at; capture order decides."""
    core, ctx = app
    question = _say(core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/flat-ask")
    answer = _say(core, ctx, TOLD, origin="assistant_visible", role="assistant",
                  when="2026-09-02T09:00:00Z", key="TEST-turn/flat-answer")
    assert _turn_replies(core, ctx, question) == [answer.ref]
    assert _turn_replies(core, ctx, answer) == [], "a reply of its own opens no turn"


def test_a_turn_is_not_reopened_by_a_much_later_reply(app):
    """Half an hour on, an assistant message is its own occasion, not this question's answer."""
    core, ctx = app
    question = _say(core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/slow-ask")
    _say(core, ctx, TOLD, origin="assistant_visible", role="assistant",
         when="2026-09-02T09:40:00Z", key="TEST-turn/slow-answer")
    assert _turn_replies(core, ctx, question) == []


def test_a_long_turn_follows_only_its_first_replies(app):
    """A turn can write many messages; the bound belongs to every seed, not to one."""
    core, ctx = app
    question = _say(core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/long-ask")
    replies = [_say(core, ctx, f"第{n}段：周五晚上十一点。", origin="assistant_visible", role="assistant",
                    when=f"2026-09-02T09:00:{n + 10}Z", key=f"TEST-turn/long-{n}") for n in range(TURN_REPLY_LIMIT + 2)]
    assert _turn_replies(core, ctx, question) == [reply.ref for reply in replies[:TURN_REPLY_LIMIT]]


def test_only_a_persons_own_message_opens_a_turn(app):
    """Memory read back into a turn is not a question, and neither is a tool transcript."""
    core, ctx = app
    reinjected = _say(core, ctx, ASK, origin="memory_reinjection", role="tool",
                      when="2026-09-02T09:00:00Z", key="TEST-turn/echo")
    _say(core, ctx, TOLD, origin="assistant_visible", role="assistant",
         when="2026-09-02T09:00:12Z", key="TEST-turn/echo-answer")
    assert _turn_replies(core, ctx, reinjected) == []
