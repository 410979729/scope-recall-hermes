"""A question that names a time ranks what was said then first (core/recall_scope.py ``query_time``,
``RetrievalPipeline._rank_hydrated``).

Notes on one subject said on several days share every word of a question about one of those days, and by words alone
the newest came first.  What was said within the time goes first; what was said at other times is still delivered,
since it often answers.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC

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


def _refs(core, ctx, query: str, *, mode: str = "auto") -> list[str]:
    reader = replace(ctx, session_id="TEST-time-reader")
    packet = core.recall_packet(reader, recall_request(query=query, mode=mode), deadline_seconds=5, zone=UTC)
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


def test_the_answer_said_on_another_day_is_still_delivered(app):
    """The time ranks and never filters: asked about a day that holds nothing on the subject, the notes of the other
    days answer as they would with no day named."""
    core, ctx = app
    notes = _status_notes(core, ctx)
    _note(core, ctx, "Lunch at noon with the design team.", "2026-09-03T12:00:00Z", "TEST-time/lunch")
    assert _refs(core, ctx, "Orchard release status on September 3?") == _refs(core, ctx, "Orchard release status?")
    assert {note.ref for note in notes} <= set(_refs(core, ctx, "Orchard release status on September 3?"))
