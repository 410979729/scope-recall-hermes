"""A recalled question reaches the reply it was given.

Over the benchmark's real questions, alpha answered 21 of 30 and beta 14 of 25: the
question itself came back, and its neighbours, but not the answer.  A reply is joined to
its question only through episode membership, which is spent on every event of the episode
in id order, so a question recalled at all could use the whole relation bound before
reaching what it was told.  The turn a person's message opened is followed first, for
every seed, and costs at most three objects of the bound.
"""

from __future__ import annotations

import time
from dataclasses import replace

from scope_recall.core.retrieval import CandidateRef, SearchContext
from scope_recall.core.retrieval_storage import TURN_REPLY_LIMIT, RetrievalStorage

from tests.contract.test_rc33_recall_accuracy import _packet, _say  # noqa: F401  (helpers)
from tests.contract.test_v11_claims import accept, app, draft  # noqa: F401  (fixture)
from tests.v11_support import recall_request

ASK = "TEST-project 的发布窗口改到几点了？"
TOLD = "周五晚上十一点。"


def _expanded(core, ctx, seed_source, *, relation_objects):
    context = SearchContext.from_request(
        recall_request(query=ASK, mode="current", max_items=6),
        ctx,
        now=core.clock.utc_now(),
        deadline=time.monotonic() + 30,
    )
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
    question = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/ask"
    )
    answer = _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:12Z",
        key="TEST-turn/answer",
    )
    assert answer.ref in _expanded(core, ctx, question, relation_objects=2)


def test_the_turn_takes_at_most_half_the_relation_bound(app):
    """What a question was told is not the only way to answer it.

    Read first and without a bound of its own, a long turn spent the whole
    relation allowance, and the claims and episodes reached from the same seed
    were never inspected at all.
    """
    core, ctx = app
    question = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/room-ask"
    )
    replies = [
        _say(
            core,
            ctx,
            f"第{n}段：周五晚上十一点。",
            origin="assistant_visible",
            role="assistant",
            when=f"2026-09-02T09:00:{n + 10}Z",
            key=f"TEST-turn/room-{n}",
        )
        for n in range(TURN_REPLY_LIMIT)
    ]
    claim = accept(core, ctx, draft(question, "周五晚上十一点", predicate="发布窗口")).items[0]

    refs = _expanded(core, ctx, question, relation_objects=3)
    assert claim.ref in refs, "the claim behind the question is still reached"
    assert any(reply.ref in refs for reply in replies), "and the turn is still followed"


def test_the_answer_is_delivered_in_the_packet(app):
    core, ctx = app
    question = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/ask"
    )
    answer = _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:12Z",
        key="TEST-turn/answer",
    )
    refs = [item["ref"] for item in _packet(core, ctx, ASK)["items"]]
    assert question.ref in refs and answer.ref in refs


def test_a_question_asked_again_word_for_word_is_given_what_it_was_told(app):
    """The automatic recall refuses an older copy of the current message: the message already says it.  The copy
    was also the only way to what it had been told, when the answer shares no word with it: over the owner's real
    questions asked again on the shared store, 42 of the 46 answers never reached were behind such a copy (baseline
    of 3.4.2, 2026-09-30).  The copy leads to its turn and is still never delivered."""
    core, ctx = app
    question = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/again-ask"
    )
    answer = _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:12Z",
        key="TEST-turn/again-answer",
    )
    reader = replace(ctx, session_id="TEST-turn-again-reader")
    refs = [item["ref"] for item in _packet(core, reader, ASK, mode="auto")["items"]]
    assert answer.ref in refs
    assert question.ref not in refs


def _asked_again(core, ctx, seeds, copies):
    """What the automatic recall ranks for ``ASK``, given the candidates the channels found (``(source, rank)``, a
    channel's rank per channel that found it) and the older copies of the message."""
    from scope_recall.core.recall_policy import rrf_score

    pipeline = core.recall_pipeline
    context = SearchContext.from_request(
        recall_request(query=ASK, mode="auto", max_items=6),
        ctx,
        now=core.clock.utc_now(),
        deadline=time.monotonic() + 30,
    )
    found = [
        CandidateRef(
            "event",
            source.ref,
            source.revision,
            "lexical",
            rank=ranks[0],
            lexical_score=2.0,
            fusion_score=rrf_score(ranks, k=pipeline.policy.rrf_k),
        )
        for source, ranks in seeds
    ]
    echoes = tuple(
        CandidateRef("event", copy.ref, copy.revision, "lexical", rank=2, lexical_score=2.0) for copy in copies
    )
    with core.storage.read(ctx) as tx:
        hydrated = [(candidate, pipeline.storage_reader.hydrate(tx, candidate, context)) for candidate in found]
        pipeline._hydrate_related(tx, context, hydrated, [], echoes=echoes)
        ranked = pipeline._rank_hydrated(hydrated, context)
    return [candidate.ref for candidate, _obj in ranked], {
        candidate.ref: candidate.fusion_score for candidate, _obj in hydrated
    }


def _turn(core, ctx, day, replies, *, tag):
    """The question asked on ``day`` of 2026-09 and its turn's replies, a few seconds apart."""
    copy = _say(
        core,
        ctx,
        ASK,
        origin="human_direct",
        role="user",
        when=f"2026-09-{day:02d}T09:00:00Z",
        key=f"TEST-turn/{tag}-ask-{day}",
    )
    return copy, [
        _say(
            core,
            ctx,
            text,
            origin="assistant_visible",
            role="assistant",
            when=f"2026-09-{day:02d}T09:00:{10 * (index + 1):02d}Z",
            key=f"TEST-turn/{tag}-{day}-{index}",
        )
        for index, text in enumerate(replies)
    ]


def test_what_the_last_copy_of_a_question_was_told_goes_before_the_best_candidate(app):
    """The last time the question was asked, the last reply of its turn answered it: that reply goes before the best
    candidate.  At a first rank's fixed score it fell below every candidate two channels agreed on (with vectors on,
    the owner's questions asked again lost about 25 of 124 answers); raising every reply of every older copy let an
    agent's opening messages fill the packet instead, and put the first answer beside the one that replaced it
    (review of its first version)."""
    core, ctx = app
    turns = [
        _turn(
            core,
            ctx,
            day,
            [f"我先看一下配置（第{step}步）。" for step in range(3)] + [f"{TOLD}（{day}日）"],
            tag="again",
        )
        for day in (1, 3)
    ]
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:00:00Z",
        key="TEST-turn/again-fresh",
    )
    ranked, scores = _asked_again(core, ctx, [(fresh, (1, 1))], [copy for copy, _replies in turns])
    first_answer, last_answer = turns[0][1][-1], turns[1][1][-1]
    assert ranked[:2] == [last_answer.ref, fresh.ref]
    opening = [reply.ref for _copy, replies in turns for reply in replies[:3]]
    assert all(scores[ref] < scores[fresh.ref] for ref in opening if ref in scores)
    assert scores.get(first_answer.ref, 0.0) < scores[fresh.ref]


def test_a_reply_a_channel_found_in_that_turn_goes_first(app):
    """A reply of the last copy's turn that a channel found says what was asked, wherever it stands in the turn; it
    kept its own score while its turn's other replies were raised above it (review of its first version)."""
    core, ctx = app
    copy, (opening, found, summary) = _turn(
        core,
        ctx,
        2,
        ["我先看一下。", "TEST-project 的发布窗口改到周五晚上十一点了。", "已按新窗口更新值班表，通知了值班组。"],
        tag="found",
    )
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-09-01T09:00:00Z",
        key="TEST-turn/found-fresh",
    )
    ranked, scores = _asked_again(core, ctx, [(fresh, (1, 1)), (found, (3,))], [copy])
    assert ranked[:3] == [found.ref, summary.ref, fresh.ref]
    assert scores[opening.ref] < scores[fresh.ref]


def test_what_was_said_after_the_last_copy_is_not_outranked_by_the_raise(app):
    """The raise goes above the best candidate said up to that turn, not above what was said after it: a newer
    statement two channels found stays above the old answer, which still goes above an older one that outranked it
    before (review of its first version)."""
    core, ctx = app
    copy, (told,) = _turn(core, ctx, 1, [TOLD], tag="stale")
    newer = _say(
        core,
        ctx,
        "TEST-project 的发布窗口改到周六早上八点了。",
        origin="human_direct",
        role="user",
        when="2026-09-04T09:00:00Z",
        key="TEST-turn/stale-newer",
    )
    older = _say(
        core,
        ctx,
        "值班表八月底排好了。",
        origin="human_direct",
        role="user",
        when="2026-08-30T09:00:00Z",
        key="TEST-turn/stale-older",
    )
    ranked, scores = _asked_again(core, ctx, [(newer, (1, 1)), (older, (1,))], [copy])
    assert ranked[:3] == [newer.ref, told.ref, older.ref]
    assert scores[older.ref] < scores[told.ref] < scores[newer.ref]


def test_of_that_turn_at_most_half_the_packet_is_raised(app):
    """An agent that narrates its work names the subject in every message, and a channel finds each: raising them all
    filled the packet with the turn's narration, the answer and everything else left out (review of its second
    version).  Of a six-item packet three are raised: the two replies a channel ranked highest, then the turn's last
    reply; the rest keep their own places."""
    core, ctx = app
    copy, (*narration, answer) = _turn(
        core,
        ctx,
        3,
        [
            "好的，我来查一下 TEST-project 的发布窗口。",
            "TEST-project 的发布窗口写在配置里。",
            "正在核对 TEST-project 的发布窗口。",
            "TEST-project 的发布窗口还有一处要看。",
            TOLD,
        ],
        tag="narrated",
    )
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:00:00Z",
        key="TEST-turn/narrated-fresh",
    )
    ranks = (3, 2, 4, 5)
    ranked, scores = _asked_again(
        core, ctx, [(fresh, (1, 1)), *((reply, (rank,)) for reply, rank in zip(narration, ranks))], [copy]
    )
    best_two = [narration[1].ref, narration[0].ref]
    assert ranked[:4] == [*best_two, answer.ref, fresh.ref]
    assert scores[narration[2].ref] < scores[fresh.ref] and scores[narration[3].ref] < scores[fresh.ref]


def test_the_answer_goes_above_the_turn_s_own_narration_when_nothing_else_is_found(app):
    """The turn's other replies are of its time: with nothing else found, the answer stayed below the turn's own
    narration that a channel found and that was not raised (review of its second version)."""
    core, ctx = app
    copy, (*narration, answer) = _turn(
        core,
        ctx,
        4,
        [
            "好的，我来查一下 TEST-project 的发布窗口。",
            "TEST-project 的发布窗口写在配置里。",
            "正在核对 TEST-project 的发布窗口。",
            TOLD,
        ],
        tag="alone",
    )
    _ranked, scores = _asked_again(core, ctx, [(reply, (rank,)) for reply, rank in zip(narration, (1, 2, 3))], [copy])
    assert scores[answer.ref] > scores[narration[2].ref]


def test_the_last_reply_of_a_turn_that_goes_on_is_not_raised(app):
    """A turn whose rows ran out is whole only once its window has closed and what the session says next is the
    person's, or nothing: the agent still working past the window, or a turn asked minutes ago, had its last narration
    raised above the answer of an older copy (second review of 3.4.7)."""
    core, ctx = app
    copy, (step, last) = _turn(core, ctx, 2, ["正在处理第一部分。", "正在处理第二部分。"], tag="goes-on")
    _say(
        core,
        ctx,
        "第三部分也处理完了，结果如下。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:40:00Z",
        key="TEST-turn/goes-on-after",
    )
    recent, (narration,) = (
        _say(
            core,
            ctx,
            ASK,
            origin="human_direct",
            role="user",
            when="2026-09-06T11:50:00Z",
            key="TEST-turn/goes-on-recent",
        ),
        [
            _say(
                core,
                ctx,
                "我先看一下 TEST 的记录。",
                origin="assistant_visible",
                role="assistant",
                when="2026-09-06T11:50:10Z",
                key="TEST-turn/goes-on-recent-reply",
            )
        ],
    )
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-09-01T09:00:00Z",
        key="TEST-turn/goes-on-fresh",
    )
    for copies, tail in (((copy,), last), ((recent,), narration)):
        _ranked, scores = _asked_again(core, ctx, [(fresh, (1, 1))], list(copies))
        assert scores.get(tail.ref, 0.0) < scores[fresh.ref], tail


def test_a_host_record_after_the_window_is_not_the_person_speaking(app):
    """What the session says next decides whether the replies read were the turn's last.  A record a coding client's
    host writes there (an interrupt, ``role='system'``) is not the person speaking: the agent's long tool call was
    cut, and its last narration is not the turn's answer (review of 3.7.2)."""
    core, ctx = app
    copy, (_step, last) = _turn(core, ctx, 2, ["正在跑一个很长的测试。", "测试还在跑。"], tag="cut-host")
    _say(
        core,
        ctx,
        '{"lifecycle": "interrupted"}',
        origin="host_generated",
        role="system",
        when="2026-09-02T09:35:00Z",
        key="TEST-turn/cut-host-interrupt",
    )
    _say(
        core,
        ctx,
        "怎么样了？",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:36:00Z",
        key="TEST-turn/cut-host-person",
    )
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-09-01T09:00:00Z",
        key="TEST-turn/cut-host-fresh",
    )
    _ranked, scores = _asked_again(core, ctx, [(fresh, (1, 1))], [copy])
    assert scores.get(last.ref, 0.0) < scores[fresh.ref]


def test_the_latest_copy_that_was_answered_is_raised_and_equal_times_go_by_capture(app):
    """A newer copy that received no reply leads nowhere and the latest one answered is raised; of two copies asked
    at the same moment, the one captured last (second review of 3.4.7)."""
    core, ctx = app
    answered, (told,) = _turn(core, ctx, 1, [TOLD], tag="answered")
    unanswered = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-03T09:00:00Z", key="TEST-turn/unanswered-ask"
    )
    _say(
        core,
        ctx,
        "我们换个话题。",
        origin="human_direct",
        role="user",
        when="2026-09-03T09:01:00Z",
        key="TEST-turn/unanswered-next",
    )
    # Answered after the change of subject: a reply to that, never to the question (review of 3.7.2).
    _say(
        core,
        ctx,
        "好的，聊什么？",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-03T09:01:20Z",
        key="TEST-turn/unanswered-next-reply",
    )
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-08-30T09:00:00Z",
        key="TEST-turn/unanswered-fresh",
    )
    ranked, _scores = _asked_again(core, ctx, [(fresh, (1, 1))], [answered, unanswered])
    assert ranked[0] == told.ref
    twin, (twin_told,) = _turn(core, ctx, 4, ["周六早上八点。"], tag="twin-a")
    elsewhere = replace(ctx, session_id="TEST-turn-twin-b")
    later = _say(
        core,
        elsewhere,
        ASK,
        origin="human_direct",
        role="user",
        when="2026-09-04T09:00:00Z",
        key="TEST-turn/twin-b-ask",
    )
    later_told = _say(
        core,
        elsewhere,
        "周日晚上九点。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-04T09:00:20Z",
        key="TEST-turn/twin-b-told",
    )
    ranked, _scores = _asked_again(core, ctx, [(fresh, (1, 1))], [twin, later])
    assert ranked[0] == later_told.ref and twin_told.ref != ranked[0]


def test_a_raise_out_of_time_says_so(app):
    """The raise is skipped when the recall's time is up, and the gap says the relation step was cut."""
    pipeline = app[0].recall_pipeline
    core, ctx = app
    copy, _replies = _turn(core, ctx, 2, [TOLD], tag="late")
    context = SearchContext.from_request(
        recall_request(query=ASK, mode="auto", max_items=6),
        ctx,
        now=core.clock.utc_now(),
        deadline=time.monotonic() - 1,
    )
    gaps: list[str] = []
    with core.storage.read(ctx) as tx:
        pipeline._raise_echo_turn(
            tx, context, [], (CandidateRef("event", copy.ref, copy.revision, "lexical", rank=1),), gaps
        )
    assert gaps == ["deadline_exceeded_relation"]


def test_the_last_reply_of_a_turn_cut_by_the_window_is_not_raised(app):
    """A turn read only to its first 64 rows may go on: its last reply read was narration, raised above an answer a
    channel had found (review of its second version)."""
    core, ctx = app
    copy = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/cut-ask"
    )
    steps = [
        _say(
            core,
            ctx,
            f"第{step}步处理中。",
            origin="assistant_visible",
            role="assistant",
            when=f"2026-09-02T09:{step // 6:02d}:{step % 6 * 10:02d}Z",
            key=f"TEST-turn/cut-{step}",
        )
        for step in range(1, 71)
    ]
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-09-01T09:00:00Z",
        key="TEST-turn/cut-fresh",
    )
    _ranked, scores = _asked_again(core, ctx, [(fresh, (1, 1))], [copy])
    assert all(scores.get(step.ref, 0.0) < scores[fresh.ref] for step in steps)


def test_the_packet_leads_with_what_the_question_was_told(app):
    """Through the channels: an older copy of the question is found and set aside, and the last reply of its turn,
    which shares no word with the question, comes among the first three of the automatic packet, whose other items
    are not all of that turn."""
    core, ctx = app
    _copy, replies = _turn(
        core,
        ctx,
        2,
        [
            "好的，我来查一下 TEST-project 的发布窗口。",
            "TEST-project 的发布窗口写在配置里。",
            "正在核对 TEST-project 的发布窗口。",
            "TEST-project 的发布窗口还有一处要看。",
            TOLD,
        ],
        tag="packet",
    )
    others = [
        _say(
            core,
            ctx,
            text,
            origin="human_direct",
            role="user",
            when=f"2026-09-01T0{index}:00:00Z",
            key=f"TEST-turn/packet-other-{index}",
        )
        for index, text in enumerate(
            (
                "TEST-project 的发布窗口本周不变。",
                "TEST-project 发布窗口的值班表已排好。",
                "TEST-project 的发布窗口要提前通知客户。",
            )
        )
    ]
    reader = replace(ctx, session_id="TEST-turn-packet-reader")
    refs = [item["ref"] for item in _packet(core, reader, ASK, mode="auto")["items"]]
    assert TOLD in [core.source(ctx, ref, 1).event["content"] for ref in refs[:3]]
    assert any(other.ref in refs for other in others)


def test_a_short_command_sent_again_does_not_bring_back_an_old_turn(app):
    """A short command sent again asks nothing an old turn answered, so its older copy leads nowhere.  "继续执行"
    holds three overlapping character pairs, "按你说的做" four: with three the bar, both brought back every old
    turn they had opened (review of 3.4.4).  Ending like a question does not make it one: "按你说的做吗？" and
    "按你说的做呀" read as asking, and a lower bar for questions brought back an old reply for both (review of
    3.7.1)."""
    core, ctx = app
    for index, command in enumerate(("继续", "继续执行", "按你说的做", "按你说的做吗？", "按你说的做呀")):
        _say(
            core,
            ctx,
            command,
            origin="human_direct",
            role="user",
            when=f"2026-09-02T09:{index:02d}:00Z",
            key=f"TEST-turn/short-ask-{index}",
        )
        told = _say(
            core,
            ctx,
            f"{TOLD}（第{index}次）",
            origin="assistant_visible",
            role="assistant",
            when=f"2026-09-02T09:{index:02d}:12Z",
            key=f"TEST-turn/short-answer-{index}",
        )
        reader = replace(ctx, session_id=f"TEST-turn-short-reader-{index}")
        assert told.ref not in [item["ref"] for item in _packet(core, reader, command, mode="auto")["items"]], command


def test_a_short_status_question_asked_again_does_not_put_the_old_answer_first(app):
    """ "测试通过了吗？" holds four terms: what it was told last time is not raised, so the newer message that says the
    tests fail now stays above that old answer.  Raised for four-term questions, the old answer came first and the
    newer one second (review of 3.7.1)."""
    core, ctx = app
    _say(
        core,
        ctx,
        "测试通过以后再合并。",
        origin="human_direct",
        role="user",
        when="2026-09-01T09:00:00Z",
        key="TEST-turn/status-rule",
    )
    _say(
        core,
        ctx,
        "测试通过了吗？",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:00:00Z",
        key="TEST-turn/status-ask",
    )
    stale = _say(
        core,
        ctx,
        "全部绿了，87 个都过。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:12Z",
        key="TEST-turn/status-told",
    )
    newer = _say(
        core,
        ctx,
        "新分支的测试通过不了，还有三个失败。",
        origin="human_direct",
        role="user",
        when="2026-09-05T09:00:00Z",
        key="TEST-turn/status-newer",
    )
    reader = replace(ctx, session_id="TEST-turn-status-reader")
    refs = [item["ref"] for item in _packet(core, reader, "测试通过了吗？", mode="auto")["items"]]
    assert newer.ref in refs
    assert stale.ref not in refs or refs.index(newer.ref) < refs.index(stale.ref)


ASK_AGAIN = "TEST-project 的发布窗口改到几点了呢"


def test_an_older_copy_closed_differently_is_still_an_older_copy(app):
    """The same question asked once without its question mark and once with it, asking both times: the copy is set
    aside and leads to its turn.  Compared character for character it was another message, delivered as if it
    answered.  (The owner's window question of 2026-10-04, "我家窗外有什么" and "我家窗外有什么？", is now set aside
    too; its four terms are too few to lead to its turn.)"""
    core, ctx = app
    question = _say(
        core,
        ctx,
        ASK_AGAIN,
        origin="human_direct",
        role="user",
        when="2026-09-02T09:00:00Z",
        key="TEST-turn/closed-ask",
    )
    answer = _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:12Z",
        key="TEST-turn/closed-answer",
    )
    reader = replace(ctx, session_id="TEST-turn-closed-reader")
    refs = [item["ref"] for item in _packet(core, reader, ASK_AGAIN + "？", mode="auto")["items"]]
    assert question.ref not in refs
    assert answer.ref in refs


def test_a_statement_asked_back_as_a_question_stays_found(app):
    """The person's statement, asked back as a question in the same words, is not an older copy of the question: it
    says what was asked.  Set aside, only the acknowledgement that followed it came back (review of 3.7.1)."""
    core, ctx = app
    statement = _say(
        core,
        ctx,
        "我的航班改到周五早上八点了。",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:00:00Z",
        key="TEST-turn/statement",
    )
    _say(
        core,
        ctx,
        "好的，我记下了。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:12Z",
        key="TEST-turn/statement-ack",
    )
    reader = replace(ctx, session_id="TEST-turn-statement-reader")
    refs = [item["ref"] for item in _packet(core, reader, "我的航班改到周五早上八点了？", mode="auto")["items"]]
    assert statement.ref in refs


def test_a_reply_belongs_to_the_turn_it_was_written_in(app):
    """Once the person speaks again the turn is over; later replies answer that message."""
    core, ctx = app
    first = _say(core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/ask-1")
    mine = _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:12Z",
        key="TEST-turn/answer-1",
    )
    second = _say(
        core, ctx, "那值班表呢", origin="human_direct", role="user", when="2026-09-02T09:01:00Z", key="TEST-turn/ask-2"
    )
    theirs = _say(
        core,
        ctx,
        "值班表还是老样子。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:01:09Z",
        key="TEST-turn/answer-2",
    )

    assert _turn_replies(core, ctx, first) == [mine.ref]
    assert _turn_replies(core, ctx, second) == [theirs.ref]


def test_the_same_message_stored_again_does_not_end_its_turn(app):
    """Until 3.4.4 a rebuilt Hermes provider stored a turn's message a second time, with the reply, under the host's
    ordinal.  The first copy stopped at the second as if the person had spoken again, and never reached the reply."""
    core, ctx = app
    first = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/twice-uuid"
    )
    _say(core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:04:00Z", key="TEST-turn/twice-5")
    told = _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:04:00Z",
        key="TEST-turn/twice-answer",
    )
    other = _say(
        core,
        ctx,
        "那值班表呢",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:05:00Z",
        key="TEST-turn/twice-next",
    )
    _say(
        core,
        ctx,
        "值班表还是老样子。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:05:09Z",
        key="TEST-turn/twice-next-answer",
    )
    assert _turn_replies(core, ctx, first) == [told.ref]
    assert _turn_replies(core, ctx, other) != [], "a different message still opens its own turn"


def test_what_the_person_adds_before_the_reply_stays_in_the_turn(app):
    """The person adds to what they asked while the agent works, and the reply answers both.  Of the owner's 1,242
    messages of 2026-09-20..10-05, 141 had such a follow-up before a reply; their turns read empty, and a question
    asked again never reached what it had been told."""
    core, ctx = app
    question = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/add-ask"
    )
    added = _say(
        core,
        ctx,
        "顺便把回滚方案也确认一下。",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:10:00Z",
        key="TEST-turn/add-more",
    )
    told = _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:11:00Z",
        key="TEST-turn/add-answer",
    )
    assert _turn_replies(core, ctx, question) == [told.ref], "ten minutes on, an addition still joins"
    assert _turn_replies(core, ctx, added) == [told.ref]


def test_a_message_long_after_with_no_reply_opens_its_own_turn(app):
    """Past ten minutes a message is no longer an addition to an unanswered question; what follows answers it."""
    core, ctx = app
    question = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/late-ask"
    )
    later = _say(
        core,
        ctx,
        "那值班表呢",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:10:01Z",
        key="TEST-turn/late-next",
    )
    theirs = _say(
        core,
        ctx,
        "值班表还是老样子。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:10:09Z",
        key="TEST-turn/late-answer",
    )
    assert _turn_replies(core, ctx, question) == []
    assert _turn_replies(core, ctx, later) == [theirs.ref]


def test_what_follows_a_host_message_is_not_the_question_s_answer(app):
    """A background job begun for an earlier request finished after the question was answered, and the agent
    reported on it.  The rows do not say which turn the job began in, so the host's message ends the question's turn
    as the person's would: what the question was told stays its answer (review of 3.7.2)."""
    core, ctx = app
    copy, (told,) = _turn(core, ctx, 2, [TOLD], tag="job")
    _say(
        core,
        ctx,
        "[IMPORTANT: Background process TEST-proc finished (exit code 0).]",
        origin="host_generated",
        role="user",
        when="2026-09-02T09:15:00Z",
        key="TEST-turn/job-notice",
    )
    report = _say(
        core,
        ctx,
        "构建通过了，结果如下。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:15:10Z",
        key="TEST-turn/job-report",
    )
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-09-01T09:00:00Z",
        key="TEST-turn/job-fresh",
    )
    assert _turn_replies(core, ctx, copy) == [told.ref]
    ranked, scores = _asked_again(core, ctx, [(fresh, (1, 1))], [copy])
    assert ranked[0] == told.ref and scores.get(report.ref, 0.0) < scores[fresh.ref]


def test_a_host_message_after_an_unanswered_question_does_not_answer_it(app):
    """The question's turn ended without a reply; then a background job finished and the agent reported on it.  The
    report is not what the question was told: the copy answered before still is (review of 3.7.2)."""
    core, ctx = app
    answered, (told,) = _turn(core, ctx, 1, [TOLD], tag="failed")
    failed = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-03T09:00:00Z", key="TEST-turn/failed-ask"
    )
    _say(
        core,
        ctx,
        "[IMPORTANT: Background process TEST-proc finished (exit code 0).]",
        origin="host_generated",
        role="user",
        when="2026-09-03T09:03:00Z",
        key="TEST-turn/failed-notice",
    )
    _say(
        core,
        ctx,
        "构建通过了。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-03T09:03:20Z",
        key="TEST-turn/failed-report",
    )
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-08-30T09:00:00Z",
        key="TEST-turn/failed-fresh",
    )
    assert _turn_replies(core, ctx, failed) == []
    ranked, _scores = _asked_again(core, ctx, [(fresh, (1, 1))], [answered, failed])
    assert ranked[0] == told.ref


def test_a_question_dropped_for_another_request_does_not_lead_to_that_answer(app):
    """Asked again, the question was dropped within minutes for another request, and the reply answered that one.
    The reply is offered as a candidate of the question's turn, ranked like any other, but never raised as what the
    question was told: the copy answered before still is (review of 3.7.2)."""
    core, ctx = app
    answered, (told,) = _turn(core, ctx, 1, [TOLD], tag="dropped")
    dropped = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-03T09:00:00Z", key="TEST-turn/dropped-ask"
    )
    _say(
        core,
        ctx,
        "算了，先帮我查一下值班表。",
        origin="human_direct",
        role="user",
        when="2026-09-03T09:02:00Z",
        key="TEST-turn/dropped-other",
    )
    roster = _say(
        core,
        ctx,
        "值班表还是老样子。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-03T09:02:30Z",
        key="TEST-turn/dropped-roster",
    )
    fresh = _say(
        core,
        ctx,
        "TEST-project 的发布窗口本周不变。",
        origin="human_direct",
        role="user",
        when="2026-08-30T09:00:00Z",
        key="TEST-turn/dropped-fresh",
    )
    assert _turn_replies(core, ctx, dropped) == [roster.ref], "offered as a candidate of the turn"
    ranked, _scores = _asked_again(core, ctx, [(fresh, (1, 1))], [answered, dropped])
    assert ranked[0] == told.ref


def test_a_message_the_host_writes_opens_a_turn_of_its_own(app):
    """Hermes opens a turn itself with a finished background process; what the agent said back belongs to it, as it
    did when such a message was stored as the person's."""
    core, ctx = app
    notice = _say(
        core,
        ctx,
        "[IMPORTANT: Background process TEST-proc finished (exit code 0).]",
        origin="host_generated",
        role="user",
        when="2026-09-02T09:00:00Z",
        key="TEST-turn/own-notice",
    )
    report = _say(
        core,
        ctx,
        "构建通过了。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:10Z",
        key="TEST-turn/own-report",
    )
    assert _turn_replies(core, ctx, notice) == [report.ref]


def test_a_whole_turn_captured_under_one_timestamp_keeps_its_order(app):
    """A gateway can write a turn's rows with one occurred_at; capture order decides."""
    core, ctx = app
    question = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/flat-ask"
    )
    answer = _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:00Z",
        key="TEST-turn/flat-answer",
    )
    assert _turn_replies(core, ctx, question) == [answer.ref]
    assert _turn_replies(core, ctx, answer) == [], "a reply of its own opens no turn"


def test_a_turn_is_not_reopened_by_a_much_later_reply(app):
    """Half an hour on, an assistant message is its own occasion, not this question's answer."""
    core, ctx = app
    question = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/slow-ask"
    )
    _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:40:00Z",
        key="TEST-turn/slow-answer",
    )
    assert _turn_replies(core, ctx, question) == []


def test_a_long_turn_follows_only_its_first_replies(app):
    """A turn can write many messages; the bound belongs to every seed, not to one."""
    core, ctx = app
    question = _say(
        core, ctx, ASK, origin="human_direct", role="user", when="2026-09-02T09:00:00Z", key="TEST-turn/long-ask"
    )
    replies = [
        _say(
            core,
            ctx,
            f"第{n}段：周五晚上十一点。",
            origin="assistant_visible",
            role="assistant",
            when=f"2026-09-02T09:00:{n + 10}Z",
            key=f"TEST-turn/long-{n}",
        )
        for n in range(TURN_REPLY_LIMIT + 2)
    ]
    assert _turn_replies(core, ctx, question) == [reply.ref for reply in replies[:TURN_REPLY_LIMIT]]


def test_memory_read_back_opens_no_turn(app):
    """Memory read back into a turn is not a question, and neither is a tool transcript."""
    core, ctx = app
    reinjected = _say(
        core, ctx, ASK, origin="memory_reinjection", role="tool", when="2026-09-02T09:00:00Z", key="TEST-turn/echo"
    )
    _say(
        core,
        ctx,
        TOLD,
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T09:00:12Z",
        key="TEST-turn/echo-answer",
    )
    assert _turn_replies(core, ctx, reinjected) == []
