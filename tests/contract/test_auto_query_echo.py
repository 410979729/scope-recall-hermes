"""P12 regression: automatic recall must not let an old query echo crowd out facts."""

from __future__ import annotations

from dataclasses import replace
import itertools

import pytest

from scope_recall.core import CoreConfig, MemoryCore
from tests.contract.test_v11_claims import Clock, capture
from tests.v11_support import context, recall_request


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-p12-query-echo"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def _packet(core: MemoryCore, ctx, **changes):
    return core.recall_packet(ctx, recall_request(**changes), deadline_seconds=5)


def _contents(packet):
    return {item["content"] for item in packet["items"]}


def test_auto_query_echoes_do_not_crowd_out_real_fact_but_explicit_modes_keep_them(app):
    core, ctx = app
    # Keep lexical retrieval deterministic: all echoes are newer than the fact,
    # and the long single identifier remains one admissible query term.
    query = "P12-echo-" + ("x" * 220)
    normalized_echo = f"\u3000{query.replace('-', '－')}\u3000"
    for index in range(1, 4):
        capture(
            core,
            ctx,
            normalized_echo if index == 3 else query,
            key=f"TEST-p12/ack/{index}",
            when="2026-09-01T12:00:00Z",
        )
    fact_text = f"{query} fact: staged in vault-sim-42; backup window is 03:20."
    capture(core, ctx, fact_text, key="TEST-p12/fact", when="2026-08-01T12:00:00Z")

    auto = _packet(core, ctx, query=query, mode="auto", request_id="TEST-p12-auto")
    assert fact_text in _contents(auto), auto
    assert query not in _contents(auto)
    assert normalized_echo not in _contents(auto)

    for mode in ("current", "history"):
        explicit = _packet(
            core,
            ctx,
            query=query,
            mode=mode,
            budget_tokens=8000,
            request_id=f"TEST-p12-{mode}",
        )
        assert query in _contents(explicit), (mode, explicit)


@pytest.mark.parametrize(
    "content",
    (
        "P12q-route question: should the archive remain pending?",
        "P12q-route was not approved; keep the archive pending.",
        "P12q-route only after the vault check passes may the archive proceed.",
    ),
)
def test_auto_does_not_broadly_filter_question_negative_or_conditional_facts(app, content):
    core, ctx = app
    capture(core, ctx, content, key="TEST-p12/semantic-variant")

    packet = _packet(core, ctx, query="P12q-route", mode="auto", request_id="TEST-p12-variant")

    assert content in _contents(packet), packet


def test_auto_followup_keeps_original_query_filter(app):
    core, ctx = app
    query = "why P12-followup"
    capture(core, ctx, query, key="TEST-p12/followup-echo", when="2026-09-01T12:00:00Z")
    neutral = "P12-followup why status is pending; no cause recorded."
    capture(core, ctx, neutral, key="TEST-p12/followup-neutral", when="2026-08-01T12:00:00Z")

    packet = _packet(core, ctx, query=query, mode="auto", request_id="TEST-p12-followup")

    # The why query leaves a bounded reason need and exercises the directed
    # followup round; its internal query must not redefine the original echo.
    assert query not in _contents(packet)
    assert neutral in _contents(packet), packet


@pytest.mark.parametrize(
    ("older", "query", "same"),
    (
        ("我家窗外有什么", "我家窗外有什么？", True),
        ("我家窗外有什么？", "我家窗外有什么", True),
        ("  Is P12 done?  ", "Is P12 done?", True),
        ("继续。", "继续", True),
        ("我家窗外有什么", "我家 窗外有什么？", False),
        ("我家窗外有什么", "我家窗外有什么哇", False),
        ("我的航班改到周五早上八点了。", "我的航班改到周五早上八点了？", False),
        ("我的航班" + "改" * 130 + "了。", "我的航班" + "改" * 130 + "了？", False),
        ("你说的是这个？我同意。", "你说的是这个？我同意？", False),
        ("切到分支 Release-2", "切到分支 release-2", False),
        ("继续", "继续?", False),
        ("？", "？", True),
        ("？", "!", False),
    ),
)
def test_an_older_copy_is_the_same_words_whatever_closes_them(older, query, same):
    """Only the closing marks and surrounding spaces may differ, and the two must both end asking or both not: a
    statement asked back as a question is not a copy of it, however long (review of 3.7.1).  The words, their letter
    case included, may not differ."""
    from scope_recall.core.recall_policy import same_message

    assert same_message(older, query) is same


def test_closing_marks_are_read_from_the_end_once():
    """A long run of spaces or marks inside a message costs one pass: a pattern anchored at the end tried it again from
    every position of the run, seconds for one candidate (review of 3.7.1)."""
    import time

    from scope_recall.core.recall_policy import same_message

    text = "P12" + " ." * 40_000 + "x"
    started = time.monotonic()
    assert same_message(text, text + "。") is True
    assert same_message(text, "P12") is False
    assert time.monotonic() - started < 1.0
