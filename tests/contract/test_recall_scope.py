"""A question about what was said on a day, and by an entry, is read in that scope (core/recall_scope.py).

"9月29日工作机 Claude Code 聊了什么" found a message of the named entry from the named day for 4 of 106 such questions
over two weeks of the shared store: its date and the entry's name were searched as words.  Any other message that
names a day is recalled as if it named none (reviews of 3.4.6).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest
from scope_recall.core.recall_scope import query_scope

from tests.contract.test_claims import app  # noqa: F401  (fixture; its clock says 2026-09-06T12:00:00Z)
from tests.contract.test_recall_dating_and_order import _say
from tests.v11_support import recall_request

ENTRIES = {
    "tianji": "天姬",
    "claude-code": "Claude Code",
    "codex": "Codex",
    "workpc-claude-code": "工作机 Claude Code",
    "workpc-codex": "工作机 Codex",
}
NEW_YORK = timezone(timedelta(hours=-4))
SHANGHAI = timezone(timedelta(hours=8))
NOW = "2026-09-30T09:30:00.000000Z"


def _day(start: str) -> tuple[str, str]:
    day = f"2026-09-{start}"
    following = f"2026-09-{int(start) + 1:02d}" if int(start) < 30 else "2026-10-01"
    return f"{day}T04:00:00.000000Z", f"{following}T04:00:00.000000Z"


def _scoped_candidates(core, ctx, query, *, limit=12, **request):
    import time as _time

    from scope_recall.core.retrieval import SearchContext
    from scope_recall.core.retrieval_storage import RetrievalStorage

    context = SearchContext.from_request(
        recall_request(query=query, **request), ctx, now=core.clock.utc_now(), deadline=_time.monotonic() + 30
    )
    context = replace(context, scope=query_scope(query, now=core.clock.utc_now(), zone=timezone.utc, entries={}))
    with core.storage.read(ctx) as tx:
        return [candidate.ref for candidate in RetrievalStorage().scoped(tx, context, limit=limit)]


@pytest.mark.parametrize(
    ("query", "days", "entries"),
    (
        ("9月29日工作机 Claude Code聊了什么", ("29",), ("workpc-claude-code",)),
        ("工作机的Codex在9月28号都说了些什么", ("28",), ("workpc-codex",)),
        ("昨天天姬说了什么", ("29",), ("tianji",)),
        ("2026-09-29 claude code 的进度", ("29",), ("claude-code",)),
        ("9月29日和9月28日 Codex", ("29", "28"), ("codex",)),
        ("今天做了什么", ("30",), ()),
        ("看看today的日志", ("30",), ()),
        ("昨晚聊了什么", ("29",), ()),
        ("今早说了什么", ("30",), ()),
        ("what did we talk about last night", ("29",), ()),
    ),
)
def test_a_question_s_days_and_entries(query, days, entries):
    scope = query_scope(query, now=NOW, zone=NEW_YORK, entries=ENTRIES)
    assert scope is not None
    assert scope.windows == tuple(_day(day) for day in days)
    assert scope.entry_ids == entries


@pytest.mark.parametrize(
    "query",
    (
        "天姬的模型换成什么了",
        "3.4.2 修了什么",
        "工作机 Codex 上次说的方案",
        "如今天下聊了什么",
        "往前天数数聊了什么",
        "之前天天说的那个问题处理了吗",
        "目前天天都在做什么",
        "至今天下聊了什么",
        "以前天天说的事都做了吗",
        "提前天数怎么算",
        "今天" * 20 + "聊了什么",
    ),
)
def test_a_question_that_names_no_day_has_no_scope(query):
    """An entry named without a day asks about a subject: reading that entry's messages would crowd out the answer.
    A version number is not a date, nor 今天 in 如今天下 or 至今 or 前天 in 往前, 之前, 目前; a message naming days
    more than sixteen times is a log, whose mentions took a quadratic scan (review of 3.4.6)."""
    assert query_scope(query, now=NOW, zone=NEW_YORK, entries=ENTRIES) is None


@pytest.mark.parametrize(
    ("query", "zone"),
    (
        ("9月28日到9月30日聊了什么", NEW_YORK),
        ("9月28-30日聊了什么", NEW_YORK),
        ("2026-09-28 至 2026-09-30 聊了什么", NEW_YORK),
        ("昨天到今天聊了什么", NEW_YORK),
        ("9月1日、9月2日、9月3日和9月4日聊了什么", NEW_YORK),
        ("9月27日、9月28日、9月29日和10月1日聊了什么", NEW_YORK),
        ("valid_to 的默认值 9999-12-31 是什么意思", NEW_YORK),
        ("0001-01-01 是什么意思", SHANGHAI),
        ("10月1日聊了什么", NEW_YORK),
    ),
)
def test_a_range_a_long_list_or_a_day_with_no_conversation_is_no_scope(query, zone):
    """A range read as its first and last days missed the rest; more than three days is a list, counted with the
    days that hold nothing; a placeholder date overflowed and emptied the whole recall; a day not yet come holds no
    conversation (reviews of 3.4.6)."""
    assert query_scope(query, now=NOW, zone=zone, entries=ENTRIES) is None


def test_a_month_and_day_is_the_last_one_within_half_a_year():
    scope = query_scope("12月25日聊了什么", now="2027-01-10T12:00:00.000000Z", zone=NEW_YORK, entries={})
    assert scope.windows == (("2026-12-25T04:00:00.000000Z", "2026-12-26T04:00:00.000000Z"),)


def test_an_entry_s_latin_name_is_a_word_of_its_own():
    """ "desk" is no entry in "Claude Desktop" or "xdesk", nor "codex" in "codexbar" or "codex2" (review of 3.4.6);
    a date right after the name leaves it the entry's."""
    entries = {"desk": "desk", "codex": "Codex", "claude-code": "Claude Code"}
    for query, named in (
        ("昨天 Claude Desktop 聊了什么", ()),
        ("昨天 xdesk 聊了什么", ()),
        ("昨天 codexbar 聊了什么", ()),
        ("昨天 codex2 聊了什么", ()),
        ("昨天 codex 聊了什么", ("codex",)),
        ("昨天codex聊了什么", ("codex",)),
        ("Claude Code9月29日做了哪些工作", ("claude-code",)),
        ("Codex2026-09-29 做了哪些工作", ("codex",)),
    ):
        assert query_scope(query, now=NOW, zone=NEW_YORK, entries=entries).entry_ids == named, query


def test_entries_joined_as_days_are_joined_are_all_read():
    """Two entries joined by "和" or "、" are both read and the joiner leaves the question: it had been left in the rest,
    and the question was recalled as if it named no day (review 5 of 3.4.8)."""
    from scope_recall.core.recall_scope import asks_what_was_said

    entries = {
        "tianxuan": "天璇",
        "tianquan": "天权",
        "workpc-claude-code": "工作机 Claude Code",
        "workpc-codex": "工作机 Codex",
    }
    for query, named in (
        ("昨天天璇和天权聊了什么", ("tianxuan", "tianquan")),
        (
            "9月28日、9月29日和9月30日工作机 Claude Code 和工作机 Codex 都聊了什么",
            ("workpc-claude-code", "workpc-codex"),
        ),
        ("昨天和天璇聊了什么", ("tianxuan",)),
    ):
        scope = query_scope(query, now=NOW, zone=NEW_YORK, entries=entries)
        assert scope.entry_ids == named and asks_what_was_said(scope.rest), (query, scope.rest)


def test_yesterday_is_the_host_s_yesterday():
    """At 22:00 on 29 September in New York it is already 30 September in UTC: "昨天" is the 28th there (review 5 of
    3.4.8: taken in UTC, nothing failed)."""
    scope = query_scope("昨天聊了什么", now="2026-09-30T02:00:00.000000Z", zone=NEW_YORK, entries={})
    assert scope.windows == (_day("28"),)


@pytest.mark.parametrize(
    ("rest", "asks"),
    (
        ("聊了什么", True),
        ("都说了些什么", True),
        ("做了哪些事", True),
        ("有什么进展", True),
        ("干了啥", True),
        ("帮我看看 聊了什么", True),
        ("请问 聊了什么", True),
        ("总结一下", True),
        ("做了哪些工作", True),
        ("我们 讨论了哪些问题", True),
        ("聊到哪了", False),
        ("下午的对话聊了什么", True),
        ("下午3点聊了什么？", True),
        ("说说 的进展", True),
        ("我们主要聊了什么", True),
        ("干嘛了", True),
        ("我说了什么", True),
        ("改了什么", True),
        ("聊的什么", True),
        ("聊什么了", True),
        ("我和你聊了什么", True),
        ("的工作总结一下", True),
        ("能不能帮我看看做了什么", True),
        ("可以总结一下吗", True),
        ("能不能都聊了什么", False),
        ("ｗｈａｔ ｄｉｄ ｗｅ ｄｏ", True),
        ("summarize", True),
        ("what were we working on", True),
        ("what have we done", True),
        ("What did we decide?", True),
        ("what did do", True),
        ("what have we been working on", True),
        ("where did we leave off", False),
        ("summary of", True),
        ("any updates from", True),
        ("what we did", True),
        ("我们 总结一下", True),
        ("what we do", False),
        ("What I do", False),
        ("what do", False),
        ("我 总结一下", False),
        ("我总结一下", False),
        ("做什么", False),
        ("聊点什么", False),
        ("说点什么", False),
        ("可以做什么", False),
        ("讨论什么", False),
        ("干嘛呢", False),
        ("忙啥呢", False),
        ("What are we working on", False),
        ("What do I do", False),
        ("在吗", False),
        ("就这样吧", False),
        ("我 在忙", False),
        ("有什么事", False),
        ("呢", False),
        ("", False),
        ("说的那个方案是什么", False),
        ("发布的 3.4.2 修了什么", False),
        ("继续 的任务", False),
        ("那个 bug 修好了吗", False),
        ("天气怎么样", False),
        ("说 TEST-project 表达偏好是什么", False),
        ("请继续 做的", False),
        ("说的继续做吗", False),
        ("我 在忙呢", False),
        ("还在忙呢", False),
        ("你 忙吗", False),
        ("怎么办", False),
        ("这个怎么做", False),
        ("做什么饭", False),
        ("说的是什么药", False),
        ("做了什么梦", False),
        ("说的事办了吗", False),
        ("的工作", False),
        ("How's work", False),
        ("Is it working", False),
    ),
)
def test_what_the_rest_of_a_day_s_question_asks(rest, asks):
    """Only a question or request that is, whole, one of the forms of asking what was said or done then is read in
    its day: a bag of words read "请继续昨天做的", "我今天在忙呢" and "今晚做什么菜" in their day, and lost the current
    task and the owner's preferences; so did asking what to do today ("今天做什么", "今天聊点什么") (reviews of
    3.4.6)."""
    from scope_recall.core.recall_scope import asks_what_was_said

    assert asks_what_was_said(rest) is asks


def test_a_long_rest_is_no_day_question_and_is_read_at_once():
    """Twenty clock times each read two ways took seconds to reject, doubling with each more, and held the process's
    lock; a long rest also made the trailing strip quadratic (review of 3.4.6)."""
    import time as _time

    from scope_recall.core.recall_scope import _DAY_QUESTION, asks_what_was_said

    assert asks_what_was_said("帮我看看" * 16 + "聊了什么") is False
    times = " ".join(f"{hour:02d}:{minute:02d}:00" for hour in range(5, 10) for minute in (0, 15, 30, 45)) + " 哪班车"
    began = _time.perf_counter()
    assert _DAY_QUESTION.fullmatch("".join(times.split())) is None
    # A part of the day before its time was read as one time and as two (review 5 of 3.4.8): 1.2 s at 81 characters.
    assert _DAY_QUESTION.fullmatch("上午1点" * 24 + "X") is None
    assert _time.perf_counter() - began < 1.0


def test_a_long_message_or_one_naming_many_entries_names_no_scope_and_is_read_at_once():
    """Every entry's name was looked for in the whole message before its rest was found too long: a log naming
    instances hundreds of times took a tenth of a second, and a name before a long run of digits a fifth (review 5 of
    3.4.8)."""
    import time as _time

    began = _time.perf_counter()
    assert query_scope("昨天" + "天姬" * 4095, now=NOW, zone=NEW_YORK, entries=ENTRIES) is None
    assert query_scope("今天 " + "codex " * 20, now=NOW, zone=NEW_YORK, entries=ENTRIES) is None
    assert query_scope("今天codex" + "1" * 8000 + "-", now=NOW, zone=NEW_YORK, entries=ENTRIES) is None
    assert _time.perf_counter() - began < 0.5


@pytest.mark.parametrize(
    ("text", "says"),
    (
        ("继续", False),
        ("好的", False),
        ("好的，继续吧", False),
        ("OK 继续执行", False),
        ("按你说的做", False),
        ("可以，就按这个来", False),
        ("确认", False),
        ("同意", False),
        ("下一步", False),
        ("好嘞", False),
        ("y", False),
        ("1", False),
        ("lgtm", False),
        ("sounds good", False),
        ("keep going", False),
        ("好的好的，可以，继续推进吧", False),
        ("知道了", False),
        ("感谢", False),
        ("没错", False),
        ("搞定", False),
        ("太好了", False),
        ("please continue", False),
        ("do it", False),
        ("makes sense", False),
        ("错了", True),
        ("别做了", True),
        ("中午吃了面", True),
        ("晚上散步", True),
        ("我到家了", True),
        ("deploy failed", True),
        ("我没说", True),
        ("你没做", True),
        ("是你说的", True),
        ("我来做", True),
        ("一样", True),
        ("8080", True),
        ("3.4.6", True),
        ("не работает", True),
    ),
)
def test_what_a_short_message_says(text, says):
    """An acknowledgement fills a packet on a day of long prompts; a short correction or report does not."""
    from scope_recall.core.recall_scope import says_something

    assert says_something(text) is says


def test_a_day_is_read_in_the_zone_the_host_shows_its_model():
    """At 09:30 UTC it is still the 30th in New York and already the 30th in Shanghai, but 29日 starts twelve hours
    apart: each host reads the day in the zone its recalled times are rendered in."""
    new_york = query_scope("9月29日", now=NOW, zone=NEW_YORK, entries={})
    east = query_scope("9月29日", now=NOW, zone=SHANGHAI, entries={})
    assert new_york.windows == (("2026-09-29T04:00:00.000000Z", "2026-09-30T04:00:00.000000Z"),)
    assert east.windows == (("2026-09-28T16:00:00.000000Z", "2026-09-29T16:00:00.000000Z"),)


def test_the_machine_s_zone_is_read_with_each_day_s_own_offset():
    """With no zone named (Claude Code, Codex), each day starts at its own local midnight: today's offset moved every
    day on the other side of a daylight-saving change by an hour (review of 3.4.6).  It can fail only on a machine
    whose zone keeps daylight saving."""
    for day in (date(2026, 1, 15), date(2026, 7, 15)):
        scope = query_scope(f"{day.month}月{day.day}日聊了什么", now=NOW, zone=None, entries={})
        start = datetime.combine(day, time(0)).astimezone().astimezone(timezone.utc)
        assert scope.windows[0][0] == start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), day


def test_a_question_naming_a_day_is_given_that_day_s_conversation(app):
    """The day's messages share no word with the question; searched by words the recall found nothing of them."""
    core, ctx = app
    asked = _say(
        core,
        ctx,
        "发布流程要改成先跑金丝雀",
        origin="human_direct",
        role="user",
        when="2026-09-02T14:00:00Z",
        key="TEST-scope/day2-ask",
    )
    told = _say(
        core,
        ctx,
        "好的，先升天枢，再升其余。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T14:00:20Z",
        key="TEST-scope/day2-told",
    )
    other = _say(
        core,
        ctx,
        "明天的会议挪到下午",
        origin="human_direct",
        role="user",
        when="2026-09-03T14:00:00Z",
        key="TEST-scope/day3-ask",
    )
    reader = replace(ctx, session_id="TEST-scope-reader")
    packet = core.recall_packet(
        reader, recall_request(query="9月2日聊了什么", mode="auto"), deadline_seconds=5, zone=timezone.utc
    )
    refs = [item["ref"] for item in packet["items"]]
    assert asked.ref in refs and told.ref in refs
    assert other.ref not in refs
    # A question that names no day is recalled as before: nothing of that day for words it does not share.
    plain = core.recall_packet(
        reader, recall_request(query="那天聊了什么", mode="auto"), deadline_seconds=5, zone=timezone.utc
    )
    assert asked.ref not in [item["ref"] for item in plain["items"]]


def test_the_day_goes_before_other_days_that_hold_the_question_s_words(app):
    """Later messages that say "9月2日聊了..." hold every search word of "9月2日聊了什么", and the day's own messages
    none: the day's still come first.  Unweighted, they ranked below such messages, and 18 of 106 such questions on
    a copy of the shared store were answered from other days."""
    core, ctx = app
    day = [
        _say(
            core,
            ctx,
            text,
            origin="human_direct",
            role="user",
            when=f"2026-09-02T1{minute}:00:00Z",
            key=f"TEST-scope/day-{minute}",
        )
        for minute, text in enumerate(("早上看了天气预报", "中午吃了牛肉面", "晚上出去散步"))
    ]
    for offset, text in enumerate(("上次说9月2日聊了发布的事", "我记得9月2日聊了部署", "9月2日聊了很久的计划")):
        _say(
            core,
            ctx,
            text,
            origin="human_direct",
            role="user",
            when=f"2026-09-0{3 + offset}T10:00:00Z",
            key=f"TEST-scope/other-{offset}",
        )
    reader = replace(ctx, session_id="TEST-scope-competition-reader")
    packet = core.recall_packet(
        reader, recall_request(query="9月2日聊了什么", mode="auto"), deadline_seconds=5, zone=timezone.utc
    )
    assert {item["ref"] for item in packet["items"][:3]} == {said.ref for said in day}


def test_the_spread_leaves_out_acknowledgements_and_puts_what_fits_first(app):
    """A day of long prompts, a short walk, a short reply and acknowledgements of one to thirteen characters: the
    short message first, then the long prompts, then the reply; the acknowledgements never.  They filled the packet
    when only messages under five characters, and then only two word lists, were left out (reviews of 3.4.6)."""
    core, ctx = app
    long = [
        _say(
            core,
            ctx,
            f"第{index}段长消息。" + "内容" * 400,
            origin="human_direct",
            role="user",
            when=f"2026-09-02T1{index}:00:00Z",
            key=f"TEST-scope/long-{index}",
        )
        for index in range(4)
    ]
    walk = _say(
        core, ctx, "晚上散步", origin="human_direct", role="user", when="2026-09-02T15:30:00Z", key="TEST-scope/walk"
    )
    reply = _say(
        core,
        ctx,
        "好的，我把部署脚本改完了。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-02T15:40:00Z",
        key="TEST-scope/reply",
    )
    for index, text in enumerate(
        (
            "继续",
            "好的",
            "好的，继续吧",
            "OK 继续执行",
            "按你说的做",
            "可以，开始吧",
            "y",
            "1",
            "确认",
            "lgtm",
            "下一步",
            "好的好的，可以，继续推进吧",
        )
    ):
        _say(
            core,
            ctx,
            text,
            origin="human_direct",
            role="user",
            when=f"2026-09-02T16:{index:02d}:00Z",
            key=f"TEST-scope/ack-{index}",
        )
    # Each group coarse to fine: the first, the middle, the quarters.
    assert _scoped_candidates(core, ctx, "9月2日聊了什么") == [
        walk.ref,
        *(long[index].ref for index in (0, 2, 1, 3)),
        reply.ref,
    ]


def test_a_day_is_spread_across_its_hours(app):
    """Thirty messages and twelve slots: the first of the day and one near its end are among them, and so are they
    among the first six, which is what an automatic packet keeps: offered in time order, the six were the day's
    morning (review 5 of 3.4.8)."""
    core, ctx = app
    said = [
        _say(
            core,
            ctx,
            f"第{index}件事做完了",
            origin="human_direct",
            role="user",
            when=f"2026-09-02T{index // 2:02d}:{index % 2 * 30:02d}:00Z",
            key=f"TEST-scope/hour-{index}",
        )
        for index in range(30)
    ]
    offered = _scoped_candidates(core, ctx, "9月2日聊了什么")
    places = sorted(next(index for index, message in enumerate(said) if message.ref == ref) for ref in offered[:6])
    assert len(offered) == 12 and places[0] == 0 and places[-1] >= 20 and len(places) == 6


def test_the_packet_holds_the_whole_day(app):
    """Through the pipeline: the automatic packet's items of a day asked about come from its morning and its evening."""
    core, ctx = app
    said = [
        _say(
            core,
            ctx,
            f"第{index}件事做完了",
            origin="human_direct",
            role="user",
            when=f"2026-09-02T{index * 45 // 60:02d}:{index * 45 % 60:02d}:00Z",
            key=f"TEST-scope/packet-{index}",
        )
        for index in range(30)
    ]
    packet = core.recall_packet(
        ctx, recall_request(query="9月2日聊了什么", mode="auto", max_items=6), deadline_seconds=30, zone=timezone.utc
    )
    places = sorted(
        next(index for index, message in enumerate(said) if message.ref == item["ref"])
        for item in packet["items"]
        if any(message.ref == item["ref"] for message in said)
    )
    assert len(places) >= 5 and places[0] <= 3 and places[-1] >= 22


def test_several_days_take_turns_and_hand_on_an_empty_share(app):
    """Two days share the slots and alternate; with one day empty the other takes them all.  The first-named day
    filled the packet (review of 3.4.6)."""
    core, ctx = app
    first = [
        _say(
            core,
            ctx,
            f"二号的第{index}条消息",
            origin="human_direct",
            role="user",
            when=f"2026-09-02T1{index}:00:00Z",
            key=f"TEST-scope/two-{index}",
        )
        for index in range(5)
    ]
    second = [
        _say(
            core,
            ctx,
            f"三号的第{index}条消息",
            origin="human_direct",
            role="user",
            when=f"2026-09-03T1{index}:00:00Z",
            key=f"TEST-scope/three-{index}",
        )
        for index in range(5)
    ]
    assert _scoped_candidates(core, ctx, "9月2日和9月3日聊了什么", limit=4) == [
        first[0].ref,
        second[0].ref,
        first[2].ref,
        second[2].ref,
    ]
    assert _scoped_candidates(core, ctx, "9月1日和9月2日聊了什么", limit=4) == [
        first[index].ref for index in (0, 2, 1, 3)
    ]


def test_as_of_bounds_the_day(app):
    core, ctx = app
    early = _say(
        core,
        ctx,
        "上午改了配置文件",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:00:00Z",
        key="TEST-scope/early",
    )
    _say(
        core,
        ctx,
        "下午跑了回归测试",
        origin="human_direct",
        role="user",
        when="2026-09-02T15:00:00Z",
        key="TEST-scope/late",
    )
    assert _scoped_candidates(core, ctx, "9月2日聊了什么", mode="as_of", as_of="2026-09-02T12:00:00Z") == [early.ref]


def test_a_day_read_for_one_entry_holds_that_entry_s_messages_alone(app):
    """The day's read keeps to the entries the question names: a message of another entry is not offered (review 5
    of 3.4.8: nothing failed with the filter taken out)."""
    import time as _time

    from scope_recall.core.retrieval import SearchContext
    from scope_recall.core.retrieval_storage import RetrievalStorage

    core, ctx = app
    said = _say(
        core,
        ctx,
        "上午改了配置文件",
        origin="human_direct",
        role="user",
        when="2026-09-02T09:00:00Z",
        key="TEST-scope/entry-local",
    )
    context = SearchContext.from_request(
        recall_request(query="9月2日聊了什么"), ctx, now=core.clock.utc_now(), deadline=_time.monotonic() + 30
    )
    scope = query_scope("9月2日聊了什么", now=core.clock.utc_now(), zone=timezone.utc, entries={})
    with core.storage.read(ctx) as tx:
        offered = {
            entries: [
                candidate.ref
                for candidate in RetrievalStorage().scoped(
                    tx, replace(context, scope=replace(scope, entry_ids=entries)), limit=12
                )
            ]
            for entries in (("local",), ("TEST-other-entry",))
        }
    assert offered == {("local",): [said.ref], ("TEST-other-entry",): []}


def test_a_day_question_naming_an_entry_of_a_shared_store_reads_that_entry(tmp_path):
    """Through the pipeline on a shared store: the entry's name comes from the store's entries and is taken out of
    the question, and only that entry's messages of the day are offered (review 5 of 3.4.8: with the entries not
    read, or the name left in the question, it fell back to plain recall and nothing failed)."""
    import time as _time

    from scope_recall.core.recall import recall
    from scope_recall.core.retrieval import SearchContext
    from scope_recall.core.storage import SQLiteStorage

    from tests.contract.test_shared_store import shared_binding, shared_context
    from tests.v11_support import source_event

    binding = shared_binding(tmp_path / "TEST-shared")
    storage = SQLiteStorage(binding)
    storage.initialize()
    with storage.write(shared_context(binding)) as tx:
        tx.registry.register_entry("tianshu", "天枢", "hermes", now="2026-09-01T00:00:00Z")
        tx.registry.register_entry("tianxuan", "天璇", "hermes", now="2026-09-01T00:00:00Z")
    said: dict[str, list[str]] = {}
    for entry in ("tianshu", "tianxuan"):
        with storage.write(shared_context(binding, entry_id=entry)) as tx:
            for index in range(3):
                event = source_event(
                    source_event_key=f"TEST-scope/{entry}-{index}",
                    content=f"{entry} 第{index}件事做完了",
                    occurred_at=f"2026-09-02T1{index}:00:00Z",
                    recorded_at=f"2026-09-02T1{index}:00:00Z",
                )
                said.setdefault(entry, []).append(
                    tx.sources.put_source(event, scope_id="TEST-scope", persisted_at="2026-09-02T20:00:00Z").ref
                )

    def asked(query):
        context = SearchContext.from_request(
            recall_request(query=query, mode="auto", max_items=6),
            shared_context(binding, entry_id="tianshu"),
            now="2026-09-03T12:00:00Z",
            deadline=_time.monotonic() + 30,
            zone=timezone.utc,
        )
        return {item.ref for item in recall(context, storage=storage).items}

    assert asked("9月2日天璇聊了什么") == set(said["tianxuan"])
    assert asked("9月2日聊了什么") == {*said["tianshu"], *said["tianxuan"]}


@pytest.mark.parametrize(
    ("query", "mode", "background"),
    (
        ("2026-09-02 发布的 3.4.2 修了什么", "auto", True),
        ("继续昨天的任务", "auto", True),
        ("9月2日白鹭计划的代号是什么", "current", False),
        ("昨天说的 TEST-project 表达偏好是什么", "current", True),
        ("今天在吗", "auto", True),
        ("今天就这样吧", "auto", True),
        ("我今天在忙", "auto", True),
        ("今天有什么事", "auto", True),
        ("今天呢", "auto", True),
        ("昨天", "auto", True),
        ("请继续昨天做的", "auto", True),
        ("昨天说的继续做吗", "auto", True),
        ("我今天在忙呢", "auto", True),
        ("你今天忙吗", "auto", True),
        ("今天怎么办", "auto", True),
        ("今晚做什么菜", "auto", True),
        ("昨天说的事办了吗", "auto", True),
        ("How's work today?", "auto", True),
        ("今天做什么", "auto", True),
        ("今天聊点什么", "auto", True),
        ("今天可以做什么", "auto", True),
        ("What are we working on today?", "auto", True),
    ),
)
def test_a_message_that_is_no_question_about_its_day_is_recalled_as_if_it_named_none(
    app, monkeypatch, query, mode, background
):
    """Read in its day, a question naming a subject lost the answer said on another day, the current task and the
    claims that answered it, an explicit lookup that had found nothing got the day's unrelated messages, and a
    message that only mentions a day lost what it would otherwise have been given (reviews of 3.4.6).  Each is
    recalled exactly as with no day named."""
    from scope_recall.core import recall as recall_module

    core, ctx = app
    _say(
        core,
        ctx,
        "3.4.2 修了长消息的召回卡顿，已经发布。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-01T10:00:00Z",
        key="TEST-scope/answer",
    )
    for minute, text in enumerate(
        ("今天发布了新的界面", "发布前先备份一下", "发布说明写好了", "表达偏好的问题先放一放")
    ):
        _say(
            core,
            ctx,
            text,
            origin="human_direct",
            role="user",
            when=f"2026-09-02T1{minute}:00:00Z",
            key=f"TEST-scope/passing-{minute}",
        )
    for minute, text in enumerate(("昨天的任务做到一半", "表达方式再简洁一点")):
        _say(
            core,
            ctx,
            text,
            origin="human_direct",
            role="user",
            when=f"2026-09-05T1{minute}:00:00Z",
            key=f"TEST-scope/yesterday-{minute}",
        )
    for minute, text in enumerate(("今天早上改了配置文件", "今天上午跑了回归测试")):
        _say(
            core,
            ctx,
            text,
            origin="human_direct",
            role="user",
            when=f"2026-09-06T0{8 + minute}:00:00Z",
            key=f"TEST-scope/today-{minute}",
        )
    reader = replace(ctx, session_id="TEST-scope-subject-reader")

    def recall():
        packet = core.recall_packet(
            reader,
            recall_request(query=query, mode=mode),
            deadline_seconds=5,
            zone=timezone.utc,
            background_without_evidence=background,
        )
        return packet["status"], [item["ref"] for item in packet["items"]]

    scoped = recall()
    monkeypatch.setattr(recall_module.RetrievalPipeline, "_scoped", staticmethod(lambda tx, working, gaps: working))
    assert scoped == recall()


def test_a_day_that_never_mentions_the_subject_does_not_answer_for_it(app):
    """ "9月2日金丝雀发布怎么定的" of a day of weather and lunch: the other day that settled it comes first.  Weighted
    as the answer because none of its messages held the question's words, as first written, the day went before it."""
    core, ctx = app
    for minute, text in enumerate(("早上看了天气预报", "中午吃了牛肉面", "晚上出去散步")):
        _say(
            core,
            ctx,
            text,
            origin="human_direct",
            role="user",
            when=f"2026-09-02T1{minute}:00:00Z",
            key=f"TEST-scope/unrelated-{minute}",
        )
    settled = _say(
        core,
        ctx,
        "金丝雀发布定了：先升天枢，再升其余。",
        origin="human_direct",
        role="user",
        when="2026-09-03T10:00:00Z",
        key="TEST-scope/settled",
    )
    reader = replace(ctx, session_id="TEST-scope-subject-reader")
    packet = core.recall_packet(
        reader, recall_request(query="9月2日金丝雀发布怎么定的", mode="auto"), deadline_seconds=5, zone=timezone.utc
    )
    assert packet["items"] and packet["items"][0]["ref"] == settled.ref


@pytest.mark.parametrize(
    ("query", "zone"),
    (
        ("valid_to 的默认值 9999-12-31 是什么意思", timezone.utc),
        ("0001-01-01 是什么意思", SHANGHAI),
        # Cut to the hosts' 8,192 characters, then grown past the search's limit by normalising "…" to "...".
        (("昨天跑的日志：" + "日志保留 step done… " * 600)[:8192], timezone.utc),
    ),
)
def test_a_question_the_day_cannot_be_read_from_is_still_recalled(app, query, zone):
    """A placeholder date overflowed the day's end, and a long prompt whose normalised text outgrew the search
    raised; either way the recall came back empty and blamed SQLite (reviews of 3.4.6)."""
    core, ctx = app
    _say(
        core,
        ctx,
        "valid_to 的默认值 9999-12-31 表示一直有效，日志保留 step done。",
        origin="assistant_visible",
        role="assistant",
        when="2026-09-01T10:00:00Z",
        key="TEST-scope/placeholder",
    )
    reader = replace(ctx, session_id="TEST-scope-placeholder-reader")
    packet = core.recall_packet(reader, recall_request(query=query, mode="auto"), deadline_seconds=5, zone=zone)
    assert packet["status"] != "unavailable", packet["gaps"]
    assert not [gap for gap in packet["gaps"] if gap.startswith(("sqlite_unavailable", "scope_unreadable"))], packet[
        "gaps"
    ]


def test_the_day_is_offered_its_full_twelve_slots(app, monkeypatch):
    """A follow-up round never reads the day, so none of its slots is held back for one (review of 3.4.6)."""
    from scope_recall.core import retrieval_storage

    core, ctx = app
    _say(
        core,
        ctx,
        "发布流程要改成先跑金丝雀",
        origin="human_direct",
        role="user",
        when="2026-09-02T14:00:00Z",
        key="TEST-scope/slots",
    )
    asked = []
    scoped = retrieval_storage.RetrievalStorage.scoped

    def spy(self, tx, context, *, limit):
        asked.append(limit)
        return scoped(self, tx, context, limit=limit)

    monkeypatch.setattr(retrieval_storage.RetrievalStorage, "scoped", spy)
    core.recall_packet(
        replace(ctx, session_id="TEST-scope-slots-reader"),
        recall_request(query="9月2日聊了什么"),
        deadline_seconds=5,
        zone=timezone.utc,
    )
    assert asked == [12]


def test_only_a_message_of_the_day_is_spared_the_entry_s_name_as_an_identifier(app):
    """ "昨天pc2聊了什么": a message of the day and entry need not repeat "pc2", whichever channel ranked it higher;
    a message found only by another channel still must (review of 3.4.6)."""
    import time as _time

    from scope_recall.core.recall_scope import QueryScope
    from scope_recall.core.retrieval import CandidateRef, SearchContext

    core, ctx = app
    said = _say(
        core,
        ctx,
        "部署脚本改完了",
        origin="human_direct",
        role="user",
        when="2026-09-05T10:00:00Z",
        key="TEST-scope/pc2",
    )
    context = SearchContext.from_request(
        recall_request(query="昨天pc2聊了什么"), ctx, now=core.clock.utc_now(), deadline=_time.monotonic() + 30
    )
    context = replace(
        context,
        scope=QueryScope((("2026-09-05T00:00:00.000000Z", "2026-09-06T00:00:00.000000Z"),), ("pc2",), "聊了什么"),
    )
    pipeline = core.recall_pipeline
    lexical = CandidateRef("event", said.ref, 1, "lexical", rank=1)
    scoped = CandidateRef("event", said.ref, 1, "scoped", rank=2)
    fused = pipeline._fuse_candidates([lexical, scoped], None)
    assert [candidate.source for candidate in fused] == ["scoped"]
    with core.storage.read(ctx) as tx:
        assert pipeline._hydrate_admit(tx, fused[0], context) is not None
        assert pipeline._hydrate_admit(tx, lexical, context) is None


def test_a_scope_that_cannot_be_read_is_a_gap_not_an_empty_recall(app, monkeypatch):
    from scope_recall.core import recall as recall_module

    core, ctx = app
    _say(
        core,
        ctx,
        "发布流程要改成先跑金丝雀",
        origin="human_direct",
        role="user",
        when="2026-09-02T14:00:00Z",
        key="TEST-scope/unreadable",
    )

    def broken(*_args, **_kwargs):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(recall_module, "query_scope", broken)
    packet = core.recall_packet(
        replace(ctx, session_id="TEST-scope-unreadable-reader"),
        recall_request(query="9月2日金丝雀发布怎么定的"),
        deadline_seconds=5,
        zone=timezone.utc,
    )
    assert "scope_unreadable:RuntimeError" in packet["gaps"]
    assert packet["items"], packet["gaps"]


def test_two_days_joined_in_the_question_are_both_read(app):
    """ "9月2日和9月3日聊了什么": the joiner left in the rest kept every such question from being read at all
    (review of 3.4.6)."""
    core, ctx = app
    first = _say(
        core,
        ctx,
        "二号改了部署脚本",
        origin="human_direct",
        role="user",
        when="2026-09-02T10:00:00Z",
        key="TEST-scope/joined-2",
    )
    second = _say(
        core,
        ctx,
        "三号跑了回归测试",
        origin="human_direct",
        role="user",
        when="2026-09-03T10:00:00Z",
        key="TEST-scope/joined-3",
    )
    for query in ("9月2日和9月3日聊了什么", "9月2日、9月3日都聊了什么"):
        packet = core.recall_packet(
            replace(ctx, session_id="TEST-scope-joined-reader"),
            recall_request(query=query, mode="auto"),
            deadline_seconds=5,
            zone=timezone.utc,
        )
        refs = [item["ref"] for item in packet["items"]]
        assert first.ref in refs and second.ref in refs, query


def test_a_ref_the_caller_named_keeps_its_place_when_the_day_holds_it_too(app):
    from scope_recall.core.retrieval import CandidateRef

    core, ctx = app
    said = _say(
        core,
        ctx,
        "部署脚本改完了",
        origin="human_direct",
        role="user",
        when="2026-09-05T10:00:00Z",
        key="TEST-scope/exact",
    )
    fused = core.recall_pipeline._fuse_candidates(
        [CandidateRef("event", said.ref, 1, "exact_ref", rank=1), CandidateRef("event", said.ref, 1, "scoped", rank=1)],
        None,
    )
    assert [candidate.source for candidate in fused] == ["exact_ref"]
