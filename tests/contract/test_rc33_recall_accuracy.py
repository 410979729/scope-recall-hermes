"""rc33 recall accuracy: dated items, questions that do not answer themselves, facts first.

Each case comes from the rc32 field report or the recall benchmark over alpha and
beta: the newest test report dated a day early, "阿乙当前是什么模型" answered with
"你现在是什么模型呀", and facts reachable only through relation expansion.
"""
from __future__ import annotations

from tests.contract.test_v11_claims import Clock, accept, app, capture, draft, initial  # noqa: F401  (fixture)
from tests.v11_support import recall_request


def _packet(core, ctx, query, *, mode="current", max_items=6):
    return core.recall_packet(ctx, recall_request(query=query, mode=mode, max_items=max_items),
                              deadline_seconds=30, background_without_evidence=False)


def _item(packet, ref):
    return next(item for item in packet["items"] if item["ref"] == ref)


# -- when was it said -----------------------------------------------------------

def test_event_items_say_when_they_were_said(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 发布窗口定在周五晚上。", when="2026-09-02T08:30:00Z")
    packet = _packet(core, ctx, "TEST-project 发布窗口")
    assert _item(packet, source.ref)["occurred_at"] == source.event["occurred_at"]


def test_claim_items_are_dated_by_their_newest_evidence(app):
    core, ctx = app
    item, source = initial(core, ctx, value="蓝色", when="2026-09-01T12:00:00Z")
    packet = _packet(core, ctx, "TEST-project 配色")
    claim = _item(packet, item.ref)
    assert claim["kind"] == "claim" and claim["occurred_at"] == source.event["occurred_at"]


def test_a_rekeyed_capture_with_an_inherited_time_is_dated_by_its_write(app):
    """Before rc33 the Hermes adapter copied the time of the unrelated message a
    restarted gateway's reused turn number already named.  The stored rows are
    never rewritten, so recall dates the copy by when it was written."""
    core, ctx = app
    core.clock.now = "2026-09-01T13:15:04Z"
    capture(core, ctx, "TEST-project 整理整个文件夹。", key="TEST-turn-8", when="2026-09-01T13:15:04Z")
    core.clock.now = "2026-09-02T11:08:21Z"
    copied = capture(core, ctx, "TEST-project 召回测试报告。", key="TEST-turn-8#rekey:0123456789abcdef",
                     when="2026-09-01T13:15:04Z")
    own = capture(core, ctx, "TEST-project 召回测试复查。", key="TEST-turn-9#rekey:fedcba9876543210",
                  when="2026-09-02T11:00:00Z")
    packet = _packet(core, ctx, "TEST-project 召回测试")
    assert _item(packet, copied.ref)["occurred_at"] == "2026-09-02T11:08:21Z"
    assert _item(packet, own.ref)["occurred_at"] == own.event["occurred_at"], "a re-key alone changes nothing"
