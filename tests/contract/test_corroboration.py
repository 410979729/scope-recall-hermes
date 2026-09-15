"""The same assertion, independently repeated, may become enough.

Covers ``core/corroboration.py`` and the duplicate path in ``core/mutate.py``.
The asymmetry under test: repetition may cure "one source did not establish
it", and must never cure "this is not the kind of statement that establishes
anything".
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from scope_recall.core.claims import RootEvidence
from scope_recall.core.corroboration import (
    CORROBORATED_REASON,
    CORROBORATION_ELIGIBLE_REASONS,
    CORROBORATION_THRESHOLD,
    corroboration_promotes,
    independent_first_hand_sources,
    witness_occasions,
)
from test_v11_claims import app, accept, capture, draft


def _root(ref, *, origin="human_direct", state="complete", gaps=(),
          session=None, principal="principal:TEST-owner"):
    """One cited source.  ``session`` defaults to one per ref, because a witness
    is an occasion rather than a row -- see ``witness_occasions``."""
    return RootEvidence(ref, 1, origin, None, "TEST content", "2026-09-06T12:00:00Z",
                        state, session or f"TEST-session-{ref}", capture_gaps=tuple(gaps),
                        source_principal={"kind": "human", "resolution": "verified",
                                          "principal_ref": principal})


class _Version:
    """The few fields ``corroboration_promotes`` reads off a stored version."""

    def __init__(self, state="proposed", spans=()):
        self.state = state
        self.payload = {"evidence_spans": [
            {"source_ref": ref, "source_revision": 1} for ref in spans
        ]}


def _promotes(*, existing_reason="fact_entailment_unproved",
              incoming_reason="fact_entailment_unproved",
              existing_spans=("TEST-a",), existing_roots=None, incoming_roots=None,
              state="proposed"):
    return corroboration_promotes(
        existing=_Version(state, existing_spans),
        existing_reason=existing_reason,
        incoming_reason=incoming_reason,
        incoming_roots=incoming_roots if incoming_roots is not None else (_root("TEST-b"),),
        existing_roots=existing_roots if existing_roots is not None else (_root("TEST-a"),),
    )


# --------------------------------------------------------------------------
# The policy
# --------------------------------------------------------------------------

def test_a_second_independent_human_source_promotes():
    assert _promotes() is True


def test_the_same_source_arriving_twice_is_not_a_second_witness():
    assert _promotes(incoming_roots=(_root("TEST-a"),)) is False


@pytest.mark.parametrize("reason", sorted(CORROBORATION_ELIGIBLE_REASONS))
def test_every_eligible_reason_can_be_cured(reason):
    assert _promotes(existing_reason=reason, incoming_reason=reason) is True


@pytest.mark.parametrize("reason", [
    "question_not_asserted", "hypothetical_or_undecided", "other_speaker",
    "statement_not_asserted", "subject_not_bound", "value_not_supported_by_quote",
    "negation_not_preserved", "relative_scope_not_preserved", "condition_not_supported",
])
def test_a_refusal_about_the_kind_of_statement_is_never_cured(reason):
    """Asking a question twice does not make it an assertion."""
    assert _promotes(existing_reason=reason, incoming_reason=reason) is False
    assert _promotes(existing_reason=reason) is False
    assert _promotes(incoming_reason=reason) is False


@pytest.mark.parametrize("origin", ["tool_observation", "external_document", "imported"])
def test_only_a_first_hand_human_statement_counts_as_a_witness(origin):
    assert _promotes(incoming_roots=(_root("TEST-b", origin=origin),)) is False


def test_a_partial_capture_is_not_a_whole_witness():
    assert _promotes(incoming_roots=(_root("TEST-b", state="partial"),)) is False
    assert _promotes(incoming_roots=(_root("TEST-b", gaps=("capture_gap:truncated",)),)) is False


def test_an_already_active_claim_is_left_alone():
    assert _promotes(state="active") is False


def test_the_threshold_counts_distinct_first_hand_sources():
    assert corroboration_promotes(
        existing=_Version("proposed", ("TEST-a",)),
        existing_reason="fact_entailment_unproved",
        incoming_reason="fact_entailment_unproved",
        incoming_roots=(_root("TEST-b"),),
        existing_roots=(_root("TEST-a"),),
        threshold=CORROBORATION_THRESHOLD + 1,
    ) is False


def test_one_person_saying_it_twice_in_one_session_is_one_witness():
    """The module promises "two people, or the same person on two separate
    occasions".  Counting stored rows made a single breath look like two."""
    assert _promotes(existing_roots=(_root("TEST-a", session="S1"),),
                     incoming_roots=(_root("TEST-b", session="S1"),)) is False


def test_the_same_person_in_two_sessions_is_two_witnesses():
    assert _promotes(existing_roots=(_root("TEST-a", session="S1"),),
                     incoming_roots=(_root("TEST-b", session="S2"),)) is True


def test_two_people_in_one_session_are_two_witnesses():
    assert _promotes(
        existing_roots=(_root("TEST-a", session="S1", principal="principal:A"),),
        incoming_roots=(_root("TEST-b", session="S1", principal="principal:B"),),
    ) is True


def test_occasions_collapse_repeats_and_keep_distinct_speakers():
    roots = (_root("TEST-a", session="S1"), _root("TEST-b", session="S1"),
             _root("TEST-c", session="S2"),
             _root("TEST-d", session="S1", principal="principal:B"),
             _root("TEST-e", session="S1", origin="tool_observation"))
    assert witness_occasions(roots) == {
        ("principal:TEST-owner", "S1"),
        ("principal:TEST-owner", "S2"),
        ("principal:B", "S1"),
    }


def test_independent_sources_ignores_everything_but_complete_human_capture():
    roots = (_root("TEST-a"), _root("TEST-b", origin="tool_observation"),
             _root("TEST-c", state="partial"), _root("TEST-d"))
    assert independent_first_hand_sources(roots) == {("TEST-a", 1), ("TEST-d", 1)}


# --------------------------------------------------------------------------
# Through the real mutation path
# --------------------------------------------------------------------------

def _state(core, ctx, ref):
    """Head version of a slot, whatever its state.

    ``current_claim`` answers only for claims that are current, which is
    exactly the population these tests need to look past.
    """
    with core.storage.read(ctx) as tx:
        row = tx._check().execute(
            """SELECT state, qualification_reason FROM claim_versions
               WHERE claim_id=? AND recorded_to IS NULL""", (ref,),
        ).fetchone()
    return (row["state"], row["qualification_reason"])


def test_two_people_stating_the_same_fact_make_it_active(app):
    core, ctx = app
    first = capture(core, ctx, "TEST-instrument：SN-4471。", key="TEST-corr/1")
    result = accept(core, ctx, draft(first, "SN-4471", kind="fact", subject="TEST-instrument",
                                     predicate="序列号", statement_kind="assertion"))
    ref = result.items[0].ref
    assert result.items[0].state == "proposed"

    # A second occasion, not a second sentence in the same breath.
    later = replace(ctx, session_id="TEST-session-2")
    second = capture(core, later, "TEST-instrument：SN-4471。", key="TEST-corr/2")
    again = accept(core, later, draft(second, "SN-4471", kind="fact", subject="TEST-instrument",
                                      predicate="序列号", statement_kind="assertion"))
    assert again.items[0].ref == ref
    state, reason = _state(core, ctx, ref)
    assert (state, reason) == ("active", CORROBORATED_REASON)


def test_the_same_source_replayed_does_not_promote(app):
    """Replay must stay idempotent; a repeat of one source is one witness."""
    core, ctx = app
    source = capture(core, ctx, "TEST-instrument：SN-4471。", key="TEST-corr/replay")
    proposal = draft(source, "SN-4471", kind="fact", subject="TEST-instrument",
                     predicate="序列号", statement_kind="assertion")
    ref = accept(core, ctx, proposal).items[0].ref
    accept(core, ctx, proposal)
    assert _state(core, ctx, ref)[0] == "proposed"


def test_a_tool_observation_does_not_corroborate_a_human_statement(app):
    core, ctx = app
    first = capture(core, ctx, "TEST-instrument：SN-4471。", key="TEST-corr/h")
    ref = accept(core, ctx, draft(first, "SN-4471", kind="fact", subject="TEST-instrument",
                                  predicate="序列号", statement_kind="assertion")).items[0].ref
    observed = capture(core, ctx, "TEST-instrument：SN-4471。",
                       key="TEST-corr/t", origin="tool_observation")
    accept(core, ctx, draft(observed, "SN-4471", kind="fact", subject="TEST-instrument",
                            predicate="序列号", statement_kind="assertion"))
    assert _state(core, ctx, ref)[0] == "proposed"


def test_a_question_repeated_twice_is_still_a_question(app):
    core, ctx = app
    for index in (1, 2):
        source = capture(core, ctx, "TEST-instrument 的序列号是 SN-4471 吗？",
                         key=f"TEST-corr/q{index}")
        result = accept(core, ctx, draft(source, "SN-4471", kind="fact",
                                         subject="TEST-instrument", predicate="序列号",
                                         statement_kind="assertion"))
        ref = result.items[0].ref
    state, reason = _state(core, ctx, ref)
    assert state == "proposed" and reason != CORROBORATED_REASON


def test_corroboration_is_not_a_way_around_a_value_conflict(app):
    """Two witnesses for a different value must not silently replace the head.

    An end-to-end property rather than a unit test of the head guard: the
    ordinary conflict path already turns this away before corroboration is
    reached, and the guard in ``apply_claim`` is defence in depth behind it.
    What is pinned here is the outcome a user would notice.
    """
    core, ctx = app
    # The slot's head says SN-4471, and it is believed.
    believed = capture(core, ctx, "TEST-instrument 的序列号是 SN-4471。", key="TEST-conflict/head")
    ref = accept(core, ctx, draft(believed, "SN-4471", kind="fact", subject="TEST-instrument",
                                  predicate="序列号", statement_kind="assertion")).items[0].ref
    assert _state(core, ctx, ref)[0] == "active"

    # Two independent first-hand statements of a *different* value.
    for index in (1, 2):
        other = capture(core, ctx, "TEST-instrument：SN-9999。", key=f"TEST-conflict/{index}")
        accept(core, ctx, draft(other, "SN-9999", kind="fact", subject="TEST-instrument",
                                predicate="序列号", statement_kind="assertion"))
    state, reason = _state(core, ctx, ref)
    assert reason != CORROBORATED_REASON, "the head was not replaced by corroboration"
