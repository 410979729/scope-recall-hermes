"""A packet slot must not be spent saying the same thing twice.

Covers ``core/duplicate_collapse.py`` and its use in the retrieval pipeline.
The pipeline de-duplicated on ``(kind, ref, revision)`` -- identity, not
content -- which is correct for rows and wrong for packets: on Alpha, where a
legacy import re-delivered the same bodies under fresh identities, one document
took four of six delivered slots and the document that answered the question
was never delivered at all.
"""
from __future__ import annotations

import pytest

from scope_recall.core.duplicate_collapse import (
    DUPLICATE_GAP_PREFIX,
    DistinctContent,
    content_key,
    duplicate_gap,
    note_duplicates,
    parse_duplicate_gap,
)
from scope_recall.core.recall_packet import public_gaps
from tests.contract.test_v11_claims import app, capture
from tests.v11_support import recall_request


class _Obj:
    """The shape the filter reads: a kind and a body."""

    def __init__(self, content, kind="event"):
        self.content = content
        self.kind = kind


# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------

def test_nothing_collapsed_means_no_gap():
    assert duplicate_gap(0) is None
    assert duplicate_gap(-3) is None


@pytest.mark.parametrize("bad", [None, "2", 1.0, True])
def test_only_a_whole_count_makes_a_gap(bad):
    assert duplicate_gap(bad) is None


def test_a_collapse_carries_its_count():
    assert duplicate_gap(7) == f"{DUPLICATE_GAP_PREFIX}:7"
    assert parse_duplicate_gap(duplicate_gap(7)) == 7


@pytest.mark.parametrize("value", [None, 7, "", "coverage_truncated:s:1of2",
                                   "duplicates_collapsed:", "duplicates_collapsed:x"])
def test_anything_else_reads_back_as_none(value):
    assert parse_duplicate_gap(value) is None


def test_note_tolerates_no_list_and_never_duplicates():
    note_duplicates(None, 3)  # must not raise
    gaps: list[str] = []
    for _ in range(3):
        note_duplicates(gaps, 3)
    assert gaps == [f"{DUPLICATE_GAP_PREFIX}:3"]


def test_the_count_survives_into_the_public_packet():
    """``public_marker`` collapses some prefixes to bare names; not this one --
    a count nobody can read is the silent truncation this exists to prevent."""
    gap = duplicate_gap(4)
    assert public_gaps((gap,)) == (gap,)


# --------------------------------------------------------------------------
# What counts as the same thing
# --------------------------------------------------------------------------

def test_the_same_body_is_the_same_thing_whatever_row_carried_it():
    assert content_key(_Obj("同一段话")) == content_key(_Obj("同一段话"))


def test_different_bodies_are_different_things():
    assert content_key(_Obj("甲")) != content_key(_Obj("乙"))


def test_a_claim_is_never_folded_into_an_event_quoting_it():
    """They carry different authority, so identical text is not the same item."""
    assert content_key(_Obj("同一段话", kind="claim")) != content_key(_Obj("同一段话", kind="event"))


@pytest.mark.parametrize("missing", [None, ""])
def test_an_empty_body_still_has_a_key(missing):
    assert content_key(_Obj(missing))[1]


def test_a_non_string_body_does_not_raise():
    assert content_key(_Obj(1234))[1]


# --------------------------------------------------------------------------
# The filter
# --------------------------------------------------------------------------

def test_the_first_copy_is_kept_and_the_rest_are_counted():
    distinct = DistinctContent()
    assert distinct.admits(_Obj("甲")) is True
    assert distinct.admits(_Obj("甲")) is False
    assert distinct.admits(_Obj("甲")) is False
    assert distinct.collapsed == 2


def test_distinct_bodies_all_pass():
    distinct = DistinctContent()
    assert all(distinct.admits(_Obj(body)) for body in ("甲", "乙", "丙"))
    assert distinct.collapsed == 0


def test_filtering_preserves_the_callers_order():
    distinct = DistinctContent()
    pairs = [(n, _Obj(body)) for n, body in enumerate(["甲", "乙", "甲", "丙", "乙"])]
    assert [n for n, _ in distinct.filtered(pairs)] == [0, 1, 3]
    assert distinct.collapsed == 2


def test_one_filter_spans_both_passes():
    """Background that merely repeats the evidence wastes a slot too."""
    distinct = DistinctContent()
    distinct.filtered([(0, _Obj("甲"))])
    assert distinct.admits(_Obj("甲")) is False
    assert distinct.collapsed == 1


# --------------------------------------------------------------------------
# Against the real pipeline
# --------------------------------------------------------------------------

def _recall(core, ctx, query, **changes):
    return core.recall(ctx, recall_request(query=query, **changes))


def _bodies(result):
    return [getattr(item, "content", "") for item in result.items]


def test_a_packet_never_says_the_same_thing_twice(app):
    core, ctx = app
    for index in range(6):
        capture(core, ctx, "TEST-project 配色 蓝色。", key=f"TEST-dup/{index}")
    result = _recall(core, ctx, "TEST-project 配色")
    bodies = _bodies(result)
    assert bodies, "expected the captured sources to be retrievable at all"
    assert len(set(bodies)) == len(bodies), f"a body was delivered twice: {bodies}"


def test_the_copies_that_were_folded_are_reported(app):
    core, ctx = app
    for index in range(6):
        capture(core, ctx, "TEST-project 配色 蓝色。", key=f"TEST-dup/{index}")
    result = _recall(core, ctx, "TEST-project 配色")
    collapsed = [parse_duplicate_gap(gap) for gap in result.gaps]
    found = [count for count in collapsed if count is not None]
    assert found and found[0] >= 1, f"copies were folded in silence: {result.gaps}"


def test_a_store_without_copies_reports_no_collapse(app):
    core, ctx = app
    for index in range(3):
        capture(core, ctx, f"TEST-project 配色 第{index}版 蓝色。", key=f"TEST-one/{index}")
    result = _recall(core, ctx, "TEST-project 配色")
    assert not any(parse_duplicate_gap(gap) is not None for gap in result.gaps)


def test_copies_cannot_crowd_out_a_different_answer(app):
    """The defect itself: six copies filled every slot and the one document
    that said something else never reached the packet."""
    core, ctx = app
    for index in range(6):
        capture(core, ctx, "TEST-project 配色 蓝色。", key=f"TEST-dup/{index}")
    capture(core, ctx, "TEST-project 配色 改走审批流程。", key="TEST-other")
    result = _recall(core, ctx, "TEST-project 配色", max_items=3)
    assert any("审批流程" in body for body in _bodies(result)), \
        f"the distinct document was crowded out: {_bodies(result)}"


# --------------------------------------------------------------------------
# A version is not a copy
# --------------------------------------------------------------------------

class _Versioned(_Obj):
    def __init__(self, content, ref, kind="episode"):
        super().__init__(content, kind)
        self.ref = ref


def test_two_revisions_of_one_object_both_survive():
    """An episode whose status changed but whose wording did not is still two
    facts; ``history`` and ``as_of`` exist to show them side by side."""
    distinct = DistinctContent()
    assert distinct.admits(_Versioned("一样的正文", "episode-a")) is True
    assert distinct.admits(_Versioned("一样的正文", "episode-a")) is True
    assert distinct.collapsed == 0


def test_the_same_body_from_a_different_object_is_still_a_copy():
    distinct = DistinctContent()
    assert distinct.admits(_Versioned("一样的正文", "event-a")) is True
    assert distinct.admits(_Versioned("一样的正文", "event-b")) is False
    assert distinct.collapsed == 1


def test_objects_without_a_ref_are_not_treated_as_one_object():
    """Missing identity must not become a licence to repeat."""
    distinct = DistinctContent()
    assert distinct.admits(_Obj("一样的正文")) is True
    assert distinct.admits(_Obj("一样的正文")) is False
    assert distinct.collapsed == 1
