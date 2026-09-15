"""A bounded read must say what it did not look at.

Covers ``core/coverage.py`` and the three places background selection used to
truncate in silence: the reserved profile windows, the request deadline, and
the two background slots.
"""
from __future__ import annotations

import pytest

from scope_recall.core.background_context import PROFILE_WINDOW, background_candidates
from scope_recall.core.coverage import (
    COVERAGE_GAP_PREFIX,
    note_truncation,
    parse_coverage_gap,
    truncation_gap,
)
from scope_recall.core.recall_packet import RecallPacketCompiler
from scope_recall.core.retrieval import SearchContext
from tests.contract.test_v11_claims import app, accept, capture, draft
from tests.v11_support import recall_request


def _search(core, ctx, query, *, seconds=5):
    return SearchContext.from_request(recall_request(query=query), ctx,
                                      now=core.clock.utc_now(),
                                      deadline=core.clock.monotonic() + seconds)


def _coverage(gaps, stage=None):
    parsed = [parse_coverage_gap(gap) for gap in gaps]
    found = [item for item in parsed if item is not None]
    return [item for item in found if stage is None or item["stage"] == stage]


# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------

def test_nothing_cut_means_no_gap():
    assert truncation_gap("stage", considered=8, available=8) is None
    assert truncation_gap("stage", considered=8, available=3) is None
    assert truncation_gap("stage", considered=-1, available=99) is None


def test_a_truncation_carries_both_numbers():
    assert truncation_gap("profile_stable", considered=8, available=9) == \
        f"{COVERAGE_GAP_PREFIX}:profile_stable:8of9"
    assert truncation_gap("profile_stable", considered=8, available=9, at_least=True) == \
        f"{COVERAGE_GAP_PREFIX}:profile_stable:8of9+"


@pytest.mark.parametrize("at_least", [False, True])
def test_a_gap_reads_back_into_its_parts(at_least):
    gap = truncation_gap("background_deadline", considered=5, available=17, at_least=at_least)
    assert parse_coverage_gap(gap) == {
        "stage": "background_deadline", "considered": 5, "available": 17, "at_least": at_least,
    }


@pytest.mark.parametrize("value", [None, 7, "", "deadline_exceeded_hydrate",
                                   "coverage_truncated:mangled", "coverage_truncated:s:xofy"])
def test_anything_that_is_not_a_coverage_gap_reads_as_none(value):
    assert parse_coverage_gap(value) is None


def test_note_truncation_tolerates_no_list_and_never_duplicates():
    note_truncation(None, "stage", considered=1, available=9)  # must not raise
    gaps: list[str] = []
    for _ in range(3):
        note_truncation(gaps, "stage", considered=1, available=9)
    assert gaps == [f"{COVERAGE_GAP_PREFIX}:stage:1of9"]


def test_the_counts_survive_into_the_public_packet():
    """``_public_marker`` collapses some prefixes; coverage must not be one."""
    gap = truncation_gap("profile_stable", considered=8, available=9, at_least=True)
    assert RecallPacketCompiler._public_gaps((gap,)) == (gap,)


# --------------------------------------------------------------------------
# The three real truncations
# --------------------------------------------------------------------------

def _fill_preferences(core, ctx, count, *, predicate_prefix="配色"):
    for index in range(count):
        source = capture(core, ctx, f"TEST-project {predicate_prefix}{index} 蓝色。")
        accept(core, ctx, draft(source, "蓝色", kind="preference", predicate=f"{predicate_prefix}{index}"))


def test_a_store_that_fits_reports_no_truncation(app):
    core, ctx = app
    _fill_preferences(core, ctx, 2)
    gaps: list[str] = []
    with core.storage.read(ctx) as tx:
        background_candidates(tx, _search(core, ctx, "配色0 怎么定"), core.recall_pipeline.storage_reader,
                              core.clock, gaps)
    assert _coverage(gaps, "profile_stable") == []
    assert _coverage(gaps, "profile_recent") == []


def test_a_full_profile_window_reports_how_much_it_did_not_see(app):
    core, ctx = app
    _fill_preferences(core, ctx, PROFILE_WINDOW + 4)
    gaps: list[str] = []
    with core.storage.read(ctx) as tx:
        background_candidates(tx, _search(core, ctx, "配色0 配色1 配色2 怎么定"),
                              core.recall_pipeline.storage_reader, core.clock, gaps)
    # Which of the three reserved windows fills depends on the query and on
    # whether the preferences are project-scoped, so the property under test is
    # that a filled window reports itself -- not which one it happened to be.
    windows = [item for item in _coverage(gaps) if item["stage"].startswith("profile_")]
    assert windows, f"expected a profile window truncation, got {gaps}"
    assert all(item["at_least"] for item in windows)
    filled = [item for item in windows if item["considered"] == PROFILE_WINDOW]
    assert filled, f"expected a window that filled to {PROFILE_WINDOW}, got {windows}"
    assert filled[0]["available"] > PROFILE_WINDOW


def test_more_applicable_preferences_than_slots_are_reported(app):
    core, ctx = app
    # Distinct subjects and predicates so none of them supersede each other.
    for index in range(4):
        source = capture(core, ctx, f"TEST-project 语气{index} 简洁。")
        accept(core, ctx, draft(source, "简洁", kind="preference", predicate=f"语气{index}"))
    gaps: list[str] = []
    with core.storage.read(ctx) as tx:
        selected = background_candidates(tx, _search(core, ctx, "语气0 语气1 语气2 语气3 怎么写"),
                                         core.recall_pipeline.storage_reader, core.clock, gaps)
    assert len(selected) <= 3  # two preferences plus at most one task
    slots = _coverage(gaps, "background_slots")
    assert slots, f"expected a background_slots truncation, got {gaps}"
    assert slots[0]["considered"] == 2 and slots[0]["available"] > 2


def test_an_exhausted_deadline_is_reported_rather_than_looking_like_an_empty_store(app):
    core, ctx = app
    _fill_preferences(core, ctx, 3)
    gaps: list[str] = []
    # A deadline already in the past: the loop stops before examining anything.
    search = _search(core, ctx, "配色0 怎么定", seconds=-1)
    with core.storage.read(ctx) as tx:
        selected = background_candidates(tx, search, core.recall_pipeline.storage_reader, core.clock, gaps)
    assert selected == ()
    deadline = _coverage(gaps, "background_deadline")
    assert deadline, f"expected a background_deadline truncation, got {gaps}"
    assert deadline[0]["considered"] == 0 and deadline[0]["available"] >= 3


def test_background_selection_still_works_without_a_gap_list(app):
    """Callers that do not collect gaps must not have to pass one."""
    core, ctx = app
    _fill_preferences(core, ctx, 2)
    with core.storage.read(ctx) as tx:
        background_candidates(tx, _search(core, ctx, "配色0 怎么定"),
                              core.recall_pipeline.storage_reader, core.clock)


# --------------------------------------------------------------------------
# A vector failure says which failure it was
# --------------------------------------------------------------------------

def test_a_vector_failure_carries_the_auxiliary_error_type(app):
    """Every auxiliary failure is an ``AuxiliaryModelError``; whether it was
    the connection or the request decides both whether the read path retries
    and whether an operator should act."""
    from scope_recall.adapters.models import AuxiliaryModelError

    core, ctx = app

    class _Port:
        def search(self, *_args, **_kwargs):
            raise AuxiliaryModelError("transport_unavailable")

    pipeline = core.recall_pipeline
    original, pipeline.vector_port = pipeline.vector_port, _Port()
    gaps: list[str] = []
    try:
        assert pipeline._vector_candidates(_search(core, ctx, "TEST 配色"), gaps) == ()
    finally:
        pipeline.vector_port = original
    assert "vector_unavailable" in gaps
    assert "vector_error:AuxiliaryModelError:transport_unavailable" in gaps, gaps


def test_a_vector_failure_without_a_kind_is_still_named(app):
    core, ctx = app

    class _Port:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    pipeline = core.recall_pipeline
    original, pipeline.vector_port = pipeline.vector_port, _Port()
    gaps: list[str] = []
    try:
        pipeline._vector_candidates(_search(core, ctx, "TEST 配色"), gaps)
    finally:
        pipeline.vector_port = original
    assert "vector_error:RuntimeError" in gaps, gaps


# --------------------------------------------------------------------------
# A blown deadline degrades the packet; it must not empty it
# --------------------------------------------------------------------------

def test_an_optional_channel_that_overruns_does_not_empty_the_packet(app):
    """Measured: a vector port that respects its allowance and then fails still
    returns a full packet, while one that overruns it by a second returned
    *zero* items -- the lexical and recent candidates were all in hand and the
    hydrate loop abandoned every one of them because the clock was gone."""
    import time

    from scope_recall.adapters.models import AuxiliaryModelError

    core, ctx = app
    for index in range(8):
        capture(core, ctx, f"TEST-project 配色 第{index}版 蓝色。", key=f"TEST-slow/{index}")

    class _Overrunning:
        def search(self, context, *, limit, remaining_seconds):
            # Ignore the allowance, the way a transport that does not enforce
            # its own timeout does.
            time.sleep(max(remaining_seconds, 0) + 0.25)
            raise AuxiliaryModelError("timeout")

    pipeline = core.recall_pipeline
    original, pipeline.vector_port = pipeline.vector_port, _Overrunning()
    try:
        result = core.recall(ctx, recall_request(query="TEST-project 配色", max_items=6),
                             deadline_seconds=0.6)
    finally:
        pipeline.vector_port = original

    assert result.items, f"a slow optional channel emptied the packet: {result.gaps}"
    assert any(gap.startswith("deadline_exceeded") for gap in result.gaps), \
        "the overrun was hidden rather than reported"


def test_the_floor_is_the_packet_the_caller_asked_for(app):
    """Hydrating a fixed three when the packet holds eight only moves the
    emptiness to the byte budget, which discards two of the three."""
    from scope_recall.core.recall import MINIMUM_HYDRATION_CAP

    assert MINIMUM_HYDRATION_CAP >= 8


def test_a_healthy_read_is_unchanged(app):
    """The floor must not change what a recall with time to spare returns."""
    core, ctx = app
    for index in range(4):
        capture(core, ctx, f"TEST-project 配色 第{index}版 蓝色。", key=f"TEST-ok/{index}")
    result = core.recall(ctx, recall_request(query="TEST-project 配色", max_items=6))
    assert result.items
    assert not any(gap.startswith("deadline_exceeded") for gap in result.gaps), result.gaps

