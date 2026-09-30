"""A question that names a day, and an entry, is read in that scope (core/recall_scope.py).

"9月29日工作机 Claude Code 聊了什么" found a message of the named entry from the named day for 4 of 106 such questions
over two weeks of the shared store: its date and the entry's name were searched as words.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from scope_recall.core.recall_scope import query_scope
from tests.contract.test_rc33_recall_accuracy import _say
from tests.contract.test_v11_claims import app  # noqa: F401  (fixture; its clock says 2026-09-06T12:00:00Z)
from tests.v11_support import recall_request

ENTRIES = {"tianji": "天姬", "claude-code": "Claude Code", "codex": "Codex",
           "workpc-claude-code": "工作机 Claude Code", "workpc-codex": "工作机 Codex"}
NEW_YORK = timezone(timedelta(hours=-4))
NOW = "2026-09-30T09:30:00.000000Z"


def _day(start: str) -> tuple[str, str]:
    day = f"2026-09-{start}"
    following = f"2026-09-{int(start) + 1:02d}" if int(start) < 30 else "2026-10-01"
    return f"{day}T04:00:00.000000Z", f"{following}T04:00:00.000000Z"


@pytest.mark.parametrize(("query", "days", "entries"), (
    ("9月29日工作机 Claude Code聊了什么", ("29",), ("workpc-claude-code",)),
    ("工作机的Codex在9月28号都说了些什么", ("28",), ("workpc-codex",)),
    ("昨天天姬说了什么", ("29",), ("tianji",)),
    ("2026-09-29 claude code 的进度", ("29",), ("claude-code",)),
    ("9月29日和9月28日 Codex", ("29", "28"), ("codex",)),
    ("今天做了什么", ("30",), ()),
))
def test_a_question_s_days_and_entries(query, days, entries):
    scope = query_scope(query, now=NOW, zone=NEW_YORK, entries=ENTRIES)
    assert scope is not None
    assert scope.windows == tuple(_day(day) for day in days)
    assert scope.entry_ids == entries


@pytest.mark.parametrize("query", ("天姬的模型换成什么了", "3.4.2 修了什么", "工作机 Codex 上次说的方案"))
def test_a_question_that_names_no_day_has_no_scope(query):
    """An entry named without a day asks about a subject: reading that entry's messages would crowd out the answer.
    A version number is not a date."""
    assert query_scope(query, now=NOW, zone=NEW_YORK, entries=ENTRIES) is None


def test_a_day_is_read_in_the_zone_the_host_shows_its_model():
    """At 09:30 UTC it is still the 30th in New York and already the 30th in Shanghai, but 29日 starts twelve hours
    apart: each host reads the day in the zone its recalled times are rendered in."""
    shanghai = timezone(timedelta(hours=8))
    new_york = query_scope("9月29日", now=NOW, zone=NEW_YORK, entries={})
    east = query_scope("9月29日", now=NOW, zone=shanghai, entries={})
    assert new_york.windows == (("2026-09-29T04:00:00.000000Z", "2026-09-30T04:00:00.000000Z"),)
    assert east.windows == (("2026-09-28T16:00:00.000000Z", "2026-09-29T16:00:00.000000Z"),)


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


def test_a_question_that_asks_only_what_was_said_is_answered_by_the_whole_day(app):
    """"聊了" is a word of "聊了什么" that no message of the day holds: the question asks only what was said, and
    every message offered is weighted as its answer.  Weighted as filler, as first written, the day's messages ranked
    no higher than other days' messages holding "聊了", and 18 of 106 such questions on a copy of the shared store
    were answered from other days."""
    import time as _time

    from scope_recall.core.recall_scope import SCOPED_ANSWER
    from scope_recall.core.retrieval import SearchContext
    from scope_recall.core.retrieval_storage import RetrievalStorage

    core, ctx = app
    for minute, text in enumerate(("早上看了天气", "中午吃了面", "晚上散步")):
        _say(core, ctx, text, origin="human_direct", role="user", when=f"2026-09-02T1{minute}:00:00Z",
             key=f"TEST-scope/said-{minute}")
    query = "9月2日聊了什么"
    context = SearchContext.from_request(recall_request(query=query), ctx, now=core.clock.utc_now(),
                                         deadline=_time.monotonic() + 30)
    context = replace(context, scope=query_scope(query, now=core.clock.utc_now(), zone=timezone.utc, entries={}))
    with core.storage.read(ctx) as tx:
        found = RetrievalStorage().scoped(tx, context, limit=12)
    assert len(found) == 3
    assert {candidate.lexical_score for candidate in found} == {SCOPED_ANSWER}


def test_a_day_s_messages_that_share_the_rest_of_the_question_come_first(app):
    core, ctx = app
    for minute, text in enumerate(("早上看了天气", "中午吃了面", "下午讨论了金丝雀发布的顺序", "晚上散步")):
        _say(core, ctx, text, origin="human_direct", role="user", when=f"2026-09-02T1{minute}:00:00Z",
             key=f"TEST-scope/rest-{minute}")
    reader = replace(ctx, session_id="TEST-scope-rest-reader")
    packet = core.recall_packet(reader, recall_request(query="9月2日金丝雀发布怎么定的", mode="current"),
                                deadline_seconds=5, zone=timezone.utc)
    assert packet["items"] and "金丝雀" in packet["items"][0]["content"]
