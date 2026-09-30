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
from tests.contract.test_rc33_recall_accuracy import _say
from tests.contract.test_v11_claims import app  # noqa: F401  (fixture; its clock says 2026-09-06T12:00:00Z)
from tests.v11_support import recall_request

ENTRIES = {"tianji": "天姬", "claude-code": "Claude Code", "codex": "Codex",
           "workpc-claude-code": "工作机 Claude Code", "workpc-codex": "工作机 Codex"}
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

    context = SearchContext.from_request(recall_request(query=query, **request), ctx, now=core.clock.utc_now(),
                                         deadline=_time.monotonic() + 30)
    context = replace(context, scope=query_scope(query, now=core.clock.utc_now(), zone=timezone.utc, entries={}))
    with core.storage.read(ctx) as tx:
        return [candidate.ref for candidate in RetrievalStorage().scoped(tx, context, limit=limit)]


@pytest.mark.parametrize(("query", "days", "entries"), (
    ("9月29日工作机 Claude Code聊了什么", ("29",), ("workpc-claude-code",)),
    ("工作机的Codex在9月28号都说了些什么", ("28",), ("workpc-codex",)),
    ("昨天天姬说了什么", ("29",), ("tianji",)),
    ("2026-09-29 claude code 的进度", ("29",), ("claude-code",)),
    ("9月29日和9月28日 Codex", ("29", "28"), ("codex",)),
    ("今天做了什么", ("30",), ()),
    ("看看today的日志", ("30",), ()),
    ("昨晚聊了什么", ("29",), ()),
    ("今早说了什么", ("30",), ()),
))
def test_a_question_s_days_and_entries(query, days, entries):
    scope = query_scope(query, now=NOW, zone=NEW_YORK, entries=ENTRIES)
    assert scope is not None
    assert scope.windows == tuple(_day(day) for day in days)
    assert scope.entry_ids == entries


@pytest.mark.parametrize("query", ("天姬的模型换成什么了", "3.4.2 修了什么", "工作机 Codex 上次说的方案",
                                   "如今天下聊了什么", "往前天数数聊了什么"))
def test_a_question_that_names_no_day_has_no_scope(query):
    """An entry named without a day asks about a subject: reading that entry's messages would crowd out the answer.
    A version number is not a date, nor 今天 in 如今天下 or 前天 in 往前天数."""
    assert query_scope(query, now=NOW, zone=NEW_YORK, entries=ENTRIES) is None


@pytest.mark.parametrize(("query", "zone"), (
    ("9月28日到9月30日聊了什么", NEW_YORK),
    ("9月28-30日聊了什么", NEW_YORK),
    ("2026-09-28 至 2026-09-30 聊了什么", NEW_YORK),
    ("昨天到今天聊了什么", NEW_YORK),
    ("9月1日、9月2日、9月3日和9月4日聊了什么", NEW_YORK),
    ("9月27日、9月28日、9月29日和10月1日聊了什么", NEW_YORK),
    ("valid_to 的默认值 9999-12-31 是什么意思", NEW_YORK),
    ("0001-01-01 是什么意思", SHANGHAI),
    ("10月1日聊了什么", NEW_YORK),
))
def test_a_range_a_long_list_or_a_day_with_no_conversation_is_no_scope(query, zone):
    """A range read as its first and last days missed the rest; more than three days is a list, counted with the
    days that hold nothing; a placeholder date overflowed and emptied the whole recall; a day not yet come holds no
    conversation (reviews of 3.4.6)."""
    assert query_scope(query, now=NOW, zone=zone, entries=ENTRIES) is None


def test_a_month_and_day_is_the_last_one_within_half_a_year():
    scope = query_scope("12月25日聊了什么", now="2027-01-10T12:00:00.000000Z", zone=NEW_YORK, entries={})
    assert scope.windows == (("2026-12-25T04:00:00.000000Z", "2026-12-26T04:00:00.000000Z"),)


def test_an_entry_s_latin_name_is_a_word_of_its_own():
    """"desk" is no entry in "Claude Desktop" or "xdesk", nor "codex" in "codexbar" or "codex2" (review of 3.4.6);
    a date right after the name leaves it the entry's."""
    entries = {"desk": "desk", "codex": "Codex", "claude-code": "Claude Code"}
    for query, named in (("昨天 Claude Desktop 聊了什么", ()), ("昨天 xdesk 聊了什么", ()),
                         ("昨天 codexbar 聊了什么", ()), ("昨天 codex2 聊了什么", ()),
                         ("昨天 codex 聊了什么", ("codex",)), ("昨天codex聊了什么", ("codex",)),
                         ("Claude Code9月29日做了哪些工作", ("claude-code",)),
                         ("Codex2026-09-29 做了哪些工作", ("codex",))):
        assert query_scope(query, now=NOW, zone=NEW_YORK, entries=entries).entry_ids == named, query


@pytest.mark.parametrize(("rest", "asks"), (
    ("聊了什么", True), ("都说了些什么", True), ("做了哪些事", True), ("有什么进展", True), ("干了啥", True),
    ("帮我看看 聊了什么", True), ("请问 聊了什么", True), ("总结一下", True), ("做了哪些工作", True),
    ("我们 讨论了哪些问题", True), ("聊到哪了", True), ("下午的对话聊了什么", True), ("summarize", True),
    ("what were we working on", True), ("what have we done", True),
    ("在吗", False), ("就这样吧", False), ("我 在忙", False), ("有什么事", False), ("呢", False),
    ("说的那个方案是什么", False), ("发布的 3.4.2 修了什么", False), ("继续 的任务", False),
    ("那个 bug 修好了吗", False), ("天气怎么样", False), ("说 TEST-project 表达偏好是什么", False),
))
def test_what_the_rest_of_a_day_s_question_asks(rest, asks):
    """Only a question or request about what was said or done then, naming no subject, is read in its day: a request
    word ("帮我看看", "总结一下") or a generic noun ("工作", "问题") no longer switched it off, and a message that only
    mentions a day ("我今天在忙") no longer switched it on (review of 3.4.6)."""
    from scope_recall.core.recall_policy import meaningful_query_terms
    from scope_recall.core.recall_scope import asks_what_was_said

    assert asks_what_was_said(rest, meaningful_query_terms(rest) if rest.strip() else ()) is asks


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
    asked = _say(core, ctx, "发布流程要改成先跑金丝雀", origin="human_direct", role="user",
                 when="2026-09-02T14:00:00Z", key="TEST-scope/day2-ask")
    told = _say(core, ctx, "好的，先升天枢，再升其余。", origin="assistant_visible", role="assistant",
                when="2026-09-02T14:00:20Z", key="TEST-scope/day2-told")
    other = _say(core, ctx, "明天的会议挪到下午", origin="human_direct", role="user",
                 when="2026-09-03T14:00:00Z", key="TEST-scope/day3-ask")
    reader = replace(ctx, session_id="TEST-scope-reader")
    packet = core.recall_packet(reader, recall_request(query="9月2日聊了什么", mode="auto"), deadline_seconds=5,
                                zone=timezone.utc)
    refs = [item["ref"] for item in packet["items"]]
    assert asked.ref in refs and told.ref in refs
    assert other.ref not in refs
    # A question that names no day is recalled as before: nothing of that day for words it does not share.
    plain = core.recall_packet(reader, recall_request(query="那天聊了什么", mode="auto"), deadline_seconds=5,
                               zone=timezone.utc)
    assert asked.ref not in [item["ref"] for item in plain["items"]]


def test_the_day_goes_before_other_days_that_hold_the_question_s_words(app):
    """Later messages that say "9月2日聊了..." hold every search word of "9月2日聊了什么", and the day's own messages
    none: the day's still come first.  Unweighted, they ranked below such messages, and 18 of 106 such questions on
    a copy of the shared store were answered from other days."""
    core, ctx = app
    day = [_say(core, ctx, text, origin="human_direct", role="user", when=f"2026-09-02T1{minute}:00:00Z",
                key=f"TEST-scope/day-{minute}")
           for minute, text in enumerate(("早上看了天气预报", "中午吃了牛肉面", "晚上出去散步"))]
    for offset, text in enumerate(("上次说9月2日聊了发布的事", "我记得9月2日聊了部署", "9月2日聊了很久的计划")):
        _say(core, ctx, text, origin="human_direct", role="user", when=f"2026-09-0{3 + offset}T10:00:00Z",
             key=f"TEST-scope/other-{offset}")
    reader = replace(ctx, session_id="TEST-scope-competition-reader")
    packet = core.recall_packet(reader, recall_request(query="9月2日聊了什么", mode="auto"), deadline_seconds=5,
                                zone=timezone.utc)
    assert {item["ref"] for item in packet["items"][:3]} == {said.ref for said in day}


def test_the_spread_leaves_out_acknowledgements_and_puts_what_fits_first(app):
    """A day of long prompts, a short walk, a short reply and acknowledgements of up to seven characters: the short
    message first, then the long prompts, then the reply; the acknowledgements never.  They filled the packet when
    only messages under five characters were left out (review of 3.4.6)."""
    core, ctx = app
    long = [_say(core, ctx, f"第{index}段长消息。" + "内容" * 400, origin="human_direct", role="user",
                 when=f"2026-09-02T1{index}:00:00Z", key=f"TEST-scope/long-{index}") for index in range(4)]
    walk = _say(core, ctx, "晚上散步", origin="human_direct", role="user", when="2026-09-02T15:30:00Z",
                key="TEST-scope/walk")
    reply = _say(core, ctx, "好的，我把部署脚本改完了。", origin="assistant_visible", role="assistant",
                 when="2026-09-02T15:40:00Z", key="TEST-scope/reply")
    for index, text in enumerate(("继续", "好的", "好的，继续吧", "OK 继续执行", "按你说的做", "可以，开始吧")):
        _say(core, ctx, text, origin="human_direct", role="user", when=f"2026-09-02T16:0{index}:00Z",
             key=f"TEST-scope/ack-{index}")
    assert _scoped_candidates(core, ctx, "9月2日聊了什么") == [walk.ref, *(message.ref for message in long), reply.ref]


def test_a_day_is_spread_across_its_hours(app):
    """Thirty messages and twelve slots: the first of the day and one near its end are among them."""
    core, ctx = app
    said = [_say(core, ctx, f"第{index}件事做完了", origin="human_direct", role="user",
                 when=f"2026-09-02T{index // 2:02d}:{index % 2 * 30:02d}:00Z", key=f"TEST-scope/hour-{index}")
            for index in range(30)]
    offered = _scoped_candidates(core, ctx, "9月2日聊了什么")
    assert len(offered) == 12 and offered[0] == said[0].ref and said.index(next(
        message for message in said if message.ref == offered[-1])) >= 26


def test_several_days_take_turns_and_hand_on_an_empty_share(app):
    """Two days share the slots and alternate; with one day empty the other takes them all.  The first-named day
    filled the packet (review of 3.4.6)."""
    core, ctx = app
    first = [_say(core, ctx, f"二号的第{index}条消息", origin="human_direct", role="user",
                  when=f"2026-09-02T1{index}:00:00Z", key=f"TEST-scope/two-{index}") for index in range(5)]
    second = [_say(core, ctx, f"三号的第{index}条消息", origin="human_direct", role="user",
                   when=f"2026-09-03T1{index}:00:00Z", key=f"TEST-scope/three-{index}") for index in range(5)]
    assert _scoped_candidates(core, ctx, "9月2日和9月3日聊了什么", limit=4) == [
        first[0].ref, second[0].ref, first[2].ref, second[2].ref]
    assert _scoped_candidates(core, ctx, "9月1日和9月2日聊了什么", limit=4) == [message.ref for message in first[:4]]


def test_as_of_bounds_the_day(app):
    core, ctx = app
    early = _say(core, ctx, "上午改了配置文件", origin="human_direct", role="user", when="2026-09-02T09:00:00Z",
                 key="TEST-scope/early")
    _say(core, ctx, "下午跑了回归测试", origin="human_direct", role="user", when="2026-09-02T15:00:00Z",
         key="TEST-scope/late")
    assert _scoped_candidates(core, ctx, "9月2日聊了什么", mode="as_of", as_of="2026-09-02T12:00:00Z") == [early.ref]


@pytest.mark.parametrize(("query", "mode", "background"), (
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
))
def test_a_message_that_is_no_question_about_its_day_is_recalled_as_if_it_named_none(
        app, monkeypatch, query, mode, background):
    """Read in its day, a question naming a subject lost the answer said on another day, the current task and the
    claims that answered it, an explicit lookup that had found nothing got the day's unrelated messages, and a
    message that only mentions a day lost what it would otherwise have been given (reviews of 3.4.6).  Each is
    recalled exactly as with no day named."""
    from scope_recall.core import recall as recall_module

    core, ctx = app
    _say(core, ctx, "3.4.2 修了长消息的召回卡顿，已经发布。", origin="assistant_visible", role="assistant",
         when="2026-09-01T10:00:00Z", key="TEST-scope/answer")
    for minute, text in enumerate(("今天发布了新的界面", "发布前先备份一下", "发布说明写好了", "表达偏好的问题先放一放")):
        _say(core, ctx, text, origin="human_direct", role="user", when=f"2026-09-02T1{minute}:00:00Z",
             key=f"TEST-scope/passing-{minute}")
    for minute, text in enumerate(("昨天的任务做到一半", "表达方式再简洁一点")):
        _say(core, ctx, text, origin="human_direct", role="user", when=f"2026-09-05T1{minute}:00:00Z",
             key=f"TEST-scope/yesterday-{minute}")
    for minute, text in enumerate(("今天早上改了配置文件", "今天上午跑了回归测试")):
        _say(core, ctx, text, origin="human_direct", role="user", when=f"2026-09-06T0{8 + minute}:00:00Z",
             key=f"TEST-scope/today-{minute}")
    reader = replace(ctx, session_id="TEST-scope-subject-reader")

    def recall():
        packet = core.recall_packet(reader, recall_request(query=query, mode=mode), deadline_seconds=5,
                                    zone=timezone.utc, background_without_evidence=background)
        return packet["status"], [item["ref"] for item in packet["items"]]

    scoped = recall()
    monkeypatch.setattr(recall_module.RetrievalPipeline, "_scoped", staticmethod(lambda tx, working, gaps: working))
    assert scoped == recall()


def test_a_day_that_never_mentions_the_subject_does_not_answer_for_it(app):
    """"9月2日金丝雀发布怎么定的" of a day of weather and lunch: the other day that settled it comes first.  Weighted
    as the answer because none of its messages held the question's words, as first written, the day went before it."""
    core, ctx = app
    for minute, text in enumerate(("早上看了天气预报", "中午吃了牛肉面", "晚上出去散步")):
        _say(core, ctx, text, origin="human_direct", role="user", when=f"2026-09-02T1{minute}:00:00Z",
             key=f"TEST-scope/unrelated-{minute}")
    settled = _say(core, ctx, "金丝雀发布定了：先升天枢，再升其余。", origin="human_direct", role="user",
                   when="2026-09-03T10:00:00Z", key="TEST-scope/settled")
    reader = replace(ctx, session_id="TEST-scope-subject-reader")
    packet = core.recall_packet(reader, recall_request(query="9月2日金丝雀发布怎么定的", mode="auto"),
                                deadline_seconds=5, zone=timezone.utc)
    assert packet["items"] and packet["items"][0]["ref"] == settled.ref


@pytest.mark.parametrize(("query", "zone"), (
    ("valid_to 的默认值 9999-12-31 是什么意思", timezone.utc),
    ("0001-01-01 是什么意思", SHANGHAI),
    # Cut to the hosts' 8,192 characters, then grown past the search's limit by normalising "…" to "...".
    (("昨天跑的日志：" + "日志保留 step done… " * 600)[:8192], timezone.utc),
))
def test_a_question_the_day_cannot_be_read_from_is_still_recalled(app, query, zone):
    """A placeholder date overflowed the day's end, and a long prompt whose normalised text outgrew the search
    raised; either way the recall came back empty and blamed SQLite (reviews of 3.4.6)."""
    core, ctx = app
    _say(core, ctx, "valid_to 的默认值 9999-12-31 表示一直有效，日志保留 step done。", origin="assistant_visible",
         role="assistant", when="2026-09-01T10:00:00Z", key="TEST-scope/placeholder")
    reader = replace(ctx, session_id="TEST-scope-placeholder-reader")
    packet = core.recall_packet(reader, recall_request(query=query, mode="auto"), deadline_seconds=5, zone=zone)
    assert packet["status"] != "unavailable", packet["gaps"]
    assert not [gap for gap in packet["gaps"] if gap.startswith(("sqlite_unavailable", "scope_unreadable"))], \
        packet["gaps"]
