"""A question that names a time ranks what was said then first, and searches that time for its other words
(core/recall_scope.py ``query_time``, ``RetrievalPipeline._rank_hydrated``, ``RetrievalStorage.dated``).

Notes on one subject said on several days share every word of a question about one of those days, and by words alone
the newest came first.  What was said within the time goes first; what was said at other times is still delivered,
since it often answers.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC

import pytest
from scope_recall.core.recall_policy import rrf_score
from scope_recall.core.recall_scope import query_time
from scope_recall.core.retrieval import CandidateRef, RetrievedObject, SearchContext

from tests.contract.test_claims import app  # noqa: F401  (fixture; its clock says 2026-09-06T12:00:00Z)
from tests.contract.test_recall_dating_and_order import _say
from tests.v11_support import recall_request


def _note(core, ctx, text: str, when: str, key: str):
    return _say(core, ctx, text, origin="human_direct", role="user", when=when, key=key)


def _hydrated(ref: str, text: str, when: str, *ranks: int) -> tuple[CandidateRef, RetrievedObject]:
    """A message said at ``when`` as hydration hands it on, fused from a channel rank each."""
    obj = RetrievedObject(
        ref,
        1,
        "event",
        text,
        "human_direct",
        "current",
        "trusted scope",
        (f"{ref}@1",),
        "direct_report",
        True,
        ("event",),
        metadata=(("occurred_at", when),),
    )
    return CandidateRef("event", ref, 1, "lexical", fusion_score=rrf_score(ranks)), obj


def test_within_the_time_a_candidate_one_channel_ranked_goes_before_two_channels_first(app):
    """Weighed, a note of the day one channel ranked third goes before newer notes two channels both ranked first,
    and those follow in their own order; without a time named the order is the fused one."""
    core, ctx = app
    hydrated = [
        _hydrated("event-newest", "Orchard release status: rolled back.", "2026-09-05T10:00:00Z", 1, 1),
        _hydrated("event-newer", "Orchard release status: out to ten percent.", "2026-09-04T10:00:00Z", 2, 2),
        _hydrated("event-day", "Orchard release status: slipped to Thursday.", "2026-09-02T10:00:00Z", 3),
    ]
    query = "Orchard release status on September 2?"
    context = SearchContext.from_request(
        recall_request(query=query, mode="auto"), ctx, now=core.clock.utc_now(), deadline=time.monotonic() + 30
    )
    timed = replace(context, time=query_time(query, now=context.now, zone=UTC))
    ranked = [candidate.ref for candidate, _obj in core.recall_pipeline._rank_hydrated(hydrated, timed)]
    assert ranked == ["event-day", "event-newest", "event-newer"]
    fused = [candidate.ref for candidate, _obj in core.recall_pipeline._rank_hydrated(hydrated, context)]
    assert fused == ["event-newest", "event-newer", "event-day"]


def _refs(core, ctx, query: str, **request) -> list[str]:
    reader = replace(ctx, session_id="TEST-time-reader")
    packet = core.recall_packet(reader, recall_request(query=query, **request), deadline_seconds=5, zone=UTC)
    return [item["ref"] for item in packet["items"]]


def _status_notes(core, ctx):
    return [
        _note(core, ctx, text, when, f"TEST-time/status-{day}")
        for day, text, when in (
            ("2", "Orchard release status: slipped to Thursday, QA found a login bug.", "2026-09-02T10:00:00Z"),
            ("4", "Orchard release status: out to ten percent of users.", "2026-09-04T10:00:00Z"),
            ("5", "Orchard release status: rolled back after error rates spiked.", "2026-09-05T10:00:00Z"),
        )
    ]


def test_what_was_said_on_the_day_asked_about_goes_before_newer_notes(app):
    """Asked without a day, the newest note on the subject comes first; asked about 2 September, that day's note
    does, and the newer ones are still delivered."""
    core, ctx = app
    second, fourth, fifth = _status_notes(core, ctx)
    assert _refs(core, ctx, "Orchard release status?")[0] == fifth.ref
    for mode in ("auto", "current"):
        refs = _refs(core, ctx, "Orchard release status on September 2?", mode=mode)
        assert refs[0] == second.ref, mode
        assert {fourth.ref, fifth.ref} <= set(refs), mode


@pytest.mark.parametrize("request_mode", ({"mode": "history"}, {"mode": "as_of", "as_of": "2026-09-06T00:00:00Z"}))
def test_a_historical_or_as_of_read_is_left_as_it_was(app, monkeypatch, request_mode):
    """Read through history or as of a moment, a question naming a day gets the packet it gets without the day, the
    newest note first, and the same with the time reading gone: no channel searches the day, nothing is weighed by it.
    Asked of what holds now, the day's note goes first."""
    from scope_recall.core import recall as recall_module

    core, ctx = app
    second, _fourth, fifth = _status_notes(core, ctx)
    dated = _refs(core, ctx, "Orchard release status on September 2?", **request_mode)
    assert dated == _refs(core, ctx, "Orchard release status?", **request_mode)
    assert dated[0] == fifth.ref
    assert _refs(core, ctx, "Orchard release status on September 2?")[0] == second.ref
    monkeypatch.setattr(recall_module, "query_time", lambda *_args, **_kwargs: None)
    assert _refs(core, ctx, "Orchard release status on September 2?", **request_mode) == dated


def test_the_answer_said_on_another_day_is_still_delivered(app):
    """The time ranks and never filters: asked about a day that holds nothing on the subject, the notes of the other
    days answer as they would with no day named."""
    core, ctx = app
    notes = _status_notes(core, ctx)
    _note(core, ctx, "Lunch at noon with the design team.", "2026-09-03T12:00:00Z", "TEST-time/lunch")
    assert _refs(core, ctx, "Orchard release status on September 3?") == _refs(core, ctx, "Orchard release status?")
    assert {note.ref for note in notes} <= set(_refs(core, ctx, "Orchard release status on September 3?"))


def test_a_note_of_the_time_asked_about_is_found_by_what_else_the_question_asks(app):
    """ "What did I say about the Orchard release on September 2?" holds four words a note can answer by, and the
    note of that day holds two of them: searched by words, the question would need three.  Within the time it names,
    two of what else it asks find it; a note of another day that holds the same two does not come, nor does a note
    of the day that shares one word."""
    core, ctx = app
    cut = _note(
        core, ctx, "We cut the Orchard release branch and froze the schema.", "2026-09-02T10:00:00Z", "TEST-time/cut"
    )
    lunch = _note(core, ctx, "Lunch with the Orchard team.", "2026-09-02T12:00:00Z", "TEST-time/lunch")
    signed = _note(core, ctx, "QA signed off on the Orchard release candidate.", "2026-09-05T10:00:00Z", "TEST-time/qa")
    assert cut.ref not in _refs(core, ctx, "What did I say about the Orchard release?")
    refs = _refs(core, ctx, "What did I say about the Orchard release on September 2?")
    assert refs[:1] == [cut.ref]
    assert lunch.ref not in refs and signed.ref not in refs


def _steps(conn, call) -> tuple[int, tuple]:
    """The SQLite virtual-machine steps ``call`` takes on ``conn``, and what it returns."""
    steps = [0]

    def count() -> int:
        steps[0] += 1
        return 0

    conn.set_progress_handler(count, 1)
    try:
        found = call()
    finally:
        conn.set_progress_handler(None, 0)
    return steps[0], found


def test_the_dated_channel_does_no_more_work_than_the_plain_search(app):
    """For the same question it searches only terms the plain lexical statement searches, kept to the time: over four
    weeks of daily notes on one subject, asked about one day, it takes fewer SQLite steps, and what it finds matched
    none of the words that name the day."""
    core, ctx = app
    for day in range(1, 29):
        _note(
            core,
            ctx,
            f"Orchard release status: build {day} passed its checks.",
            f"2026-08-{day:02d}T10:00:00Z",
            f"TEST-time/build-{day}",
        )
    query = "Orchard release status on August 3?"
    context = SearchContext.from_request(
        recall_request(query=query, mode="auto"), ctx, now=core.clock.utc_now(), deadline=time.monotonic() + 30
    )
    timed = replace(context, time=query_time(query, now=context.now, zone=UTC))
    reader = core.recall_pipeline.storage_reader
    with core.storage.read(ctx) as tx:
        plain, everything = _steps(tx._check(), lambda: reader.lexical(tx, timed, limit=48))
        dated, of_the_day = _steps(tx._check(), lambda: reader.dated(tx, timed, limit=12))
    assert len(everything) == 28 and len(of_the_day) == 1
    assert dated < plain
    assert {"august"}.isdisjoint(of_the_day[0].matched_query_terms)


def test_a_message_the_asking_model_holds_is_not_brought_back_by_its_time(app, monkeypatch):
    """A message of the asking session said within the time it names is found by its words, within the time and by the
    recent channel.  When the host says its model still holds the session, it is left out all the same and another
    session's note of that week answers first.  When the host says nothing, it keeps its place against that note as
    with no time read: the time weighs both alike."""
    from scope_recall.core import recall as recall_module

    core, ctx = app
    asking, earlier = replace(ctx, session_id="TEST-time-asking"), replace(ctx, session_id="TEST-time-earlier")
    answer = _say(
        core,
        earlier,
        "P249 release status: rolled back after error rates spiked.",
        origin="human_direct",
        role="user",
        when="2026-09-03T10:00:00Z",
        key="TEST-time/answer",
    )
    held = _say(
        core,
        asking,
        "P249 release status: checking the dashboard now.",
        origin="human_direct",
        role="user",
        when="2026-09-06T09:00:00Z",
        key="TEST-time/held",
    )
    reader = core.recall_pipeline.storage_reader
    recent = reader.recent
    offered: list[str] = []

    def spy(tx, context, *, limit):
        found = recent(tx, context, limit=limit)
        offered.extend(candidate.ref for candidate in found)
        return found

    monkeypatch.setattr(reader, "recent", spy)

    def refs(**host) -> list[str]:
        request = recall_request(query="P249 release status this week?")
        packet = core.recall_packet(asking, request, deadline_seconds=5, zone=UTC, **host)
        return [item["ref"] for item in packet["items"]]

    holds = refs(in_context_since="2026-09-06T08:00:00Z")
    assert held.ref in offered and held.ref not in holds and holds[:1] == [answer.ref]
    told_nothing = refs()
    monkeypatch.setattr(recall_module, "query_time", lambda *_args, **_kwargs: None)
    unread = refs()
    assert {held.ref, answer.ref} <= set(told_nothing) & set(unread)
    assert (told_nothing.index(held.ref) < told_nothing.index(answer.ref)) == (
        unread.index(held.ref) < unread.index(answer.ref)
    )


def test_the_identifier_a_day_is_written_with_is_not_asked_of_its_notes(app):
    """ "on the 2nd" holds an identifier ("2nd") that every other candidate has to repeat; a note found within the
    time it names does not, since its time is what the identifier says."""
    core, ctx = app
    plan = _note(core, ctx, "Orchard release plan: ship on Thursday.", "2026-09-02T10:00:00Z", "TEST-time/plan")
    _note(core, ctx, "Orchard release plan: wait for QA.", "2026-09-04T10:00:00Z", "TEST-time/later-plan")
    assert _refs(core, ctx, "What was the Orchard release plan on the 2nd?")[:1] == [plan.ref]
