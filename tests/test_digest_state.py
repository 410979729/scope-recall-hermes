"""Leave-state partition and per-session resume-cursor helpers for issue #46."""

from __future__ import annotations

from scope_recall.digest_state import (
    attach_digestible_tool_provenance,
    leave_plan_receipt_actions,
    loaded_leave_sets,
    next_session_resume_after_id,
    plan_loaded_leave,
)
from scope_recall.journal_candidates import JournalDigestCandidate
from scope_recall.journal_extractors import JournalCandidateList


def test_plan_loaded_leave_puts_uncited_parsed_ids_in_pending():
    plan = plan_loaded_leave(
        loaded_ids=[1, 2],
        candidate_ids=[1],
        reviewed_ids=[],
        unresolved_ids=[],
        retryable_unresolved_ids=[],
        deferred_ids=[],
        applied_ids=[1],
    )
    assert plan.applied_ids == [1]
    assert plan.pending_ids == [2]
    assert plan.deferred_ids == []
    assert plan.quarantined_ids == []
    sets = loaded_leave_sets(plan)
    assert set(sets["processed_ids"]) | set(sets["retryable_pending_ids"]) == {1, 2}
    assert not (set(sets["processed_ids"]) & set(sets["retryable_pending_ids"]))


def test_next_session_resume_after_id_skips_deferred_overflow():
    assert next_session_resume_after_id(covered_ids=[1, 2], loaded_ids=[1, 2, 3, 4, 5]) == 2
    assert next_session_resume_after_id(covered_ids=[], loaded_ids=[3, 4, 5]) == 5
    assert next_session_resume_after_id(covered_ids=[], loaded_ids=[]) == 0


def test_leave_plan_receipt_actions_keep_budget_defer_off_provider_retry():
    plan = plan_loaded_leave(
        loaded_ids=[1, 2, 3, 4],
        candidate_ids=[],
        reviewed_ids=[],
        unresolved_ids=[1],
        retryable_unresolved_ids=[1],
        deferred_ids=[3, 4],
        applied_ids=[],
    )
    actions = leave_plan_receipt_actions(
        plan,
        unresolved_ids=[1],
        retryable_unresolved_ids=[1],
        quarantine_threshold=99,
    )
    pending_actions = [item for item in actions if item["action"] == "pending"]
    assert any(item["reason"] == "chunk extraction unresolved" for item in pending_actions)
    assert any(
        item["reason"] == "parsed chunk did not cite this loaded entry" for item in pending_actions
    )
    leftover_pending = [
        item
        for item in pending_actions
        if item.get("reason") == "parsed chunk did not cite this loaded entry"
    ]
    assert leftover_pending and leftover_pending[0]["entry_ids"] == [2]
    deferred_actions = [item for item in actions if item["action"] == "deferred"]
    assert deferred_actions and deferred_actions[0]["reason"] == "per-session chunk budget reached"


def test_loaded_leave_sets_are_exclusive_and_complete():
    plan = plan_loaded_leave(
        loaded_ids=[1, 2, 3, 4, 5],
        candidate_ids=[1],
        reviewed_ids=[2],
        unresolved_ids=[3],
        retryable_unresolved_ids=[3],
        deferred_ids=[4, 5],
        applied_ids=[1],
        attempts_after={3: 1},
        quarantine_threshold=3,
    )
    sets = loaded_leave_sets(plan, admission_ids=[])
    groups = [
        set(sets["processed_ids"]),
        set(sets["retryable_pending_ids"]),
        set(sets["deferred_ids"]),
        set(sets["quarantined_ids"]),
    ]
    assert set().union(*groups) == {1, 2, 3, 4, 5}
    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            assert not (left & right)
    assert sets["deferred_ids"] == [4, 5]
    assert 3 in sets["retryable_pending_ids"]


def test_unapplied_candidate_stays_pending_not_disappeared():
    plan = plan_loaded_leave(
        loaded_ids=[1],
        candidate_ids=[1],
        reviewed_ids=[],
        unresolved_ids=[],
        retryable_unresolved_ids=[],
        deferred_ids=[],
        applied_ids=[],
    )
    assert plan.pending_ids == [1]
    assert plan.applied_ids == []
    assert plan.quarantined_ids == []
    sets = loaded_leave_sets(plan)
    assert sets["retryable_pending_ids"] == [1]
    assert sets["processed_ids"] == []


def test_retryable_budget_quarantine_shares_leave_plan():
    plan = plan_loaded_leave(
        loaded_ids=[1, 2],
        candidate_ids=[],
        reviewed_ids=[],
        unresolved_ids=[1, 2],
        retryable_unresolved_ids=[1],
        deferred_ids=[],
        applied_ids=[],
        attempts_after={2: 3},
        quarantine_threshold=3,
        retryable_failures_after={1: 3},
        retryable_failures_threshold=3,
    )
    assert plan.retryable_quarantined_ids == [1]
    assert plan.attempts_quarantined_ids == [2]
    assert plan.quarantined_ids == [1, 2]
    sets = loaded_leave_sets(plan)
    assert set(sets["quarantined_ids"]) == {1, 2}
    assert not (set(plan.retryable_quarantined_ids) & set(plan.attempts_quarantined_ids))


def test_plan_loaded_leave_exhaustive_one_id_partition():
    """Every reachable one-ID flag combination stays exclusive and complete."""

    flags = (False, True)
    for candidate in flags:
        for reviewed in flags:
            for unresolved in flags:
                for retryable in flags:
                    for deferred in flags:
                        for applied in flags:
                            for pollution in flags:
                                for attempts in (0, 3):
                                    for retryable_count in (0, 3):
                                        plan = plan_loaded_leave(
                                            loaded_ids=[1],
                                            candidate_ids=[1] if candidate else [],
                                            reviewed_ids=[1] if reviewed else [],
                                            unresolved_ids=[1] if unresolved else [],
                                            retryable_unresolved_ids=[1] if retryable else [],
                                            deferred_ids=[1] if deferred else [],
                                            applied_ids=[1] if applied else [],
                                            pollution_ids=[1] if pollution else [],
                                            attempts_after={1: attempts},
                                            quarantine_threshold=3,
                                            retryable_failures_after={1: retryable_count},
                                            retryable_failures_threshold=3,
                                        )
                                        sets = loaded_leave_sets(plan, admission_ids=[])
                                        groups = [
                                            set(sets["processed_ids"]),
                                            set(sets["retryable_pending_ids"]),
                                            set(sets["deferred_ids"]),
                                            set(sets["quarantined_ids"]),
                                        ]
                                        assert set().union(*groups) == {1}
                                        for index, left in enumerate(groups):
                                            for right in groups[index + 1 :]:
                                                assert not (left & right), (
                                                    candidate,
                                                    reviewed,
                                                    unresolved,
                                                    retryable,
                                                    deferred,
                                                    applied,
                                                    pollution,
                                                    attempts,
                                                    retryable_count,
                                                    sets,
                                                )
                                        if pollution:
                                            assert sets["quarantined_ids"] == [1]
                                            assert sets["processed_ids"] == []


class _ToolEntry:
    def __init__(
        self,
        entry_id: int,
        role: str = "tool",
        *,
        scope_id: str = "scope-a",
        session_id: str = "session-a",
    ) -> None:
        self.id = entry_id
        self.role = role
        self.scope_id = scope_id
        self.session_id = session_id


def test_attach_digestible_tool_provenance_fails_closed_without_attempted_metadata():
    candidate = JournalDigestCandidate(
        content="plain list has no extractor coverage metadata",
        entry_ids=[1],
    )
    attached = attach_digestible_tool_provenance(
        [candidate],
        [_ToolEntry(2), _ToolEntry(4)],
    )
    assert attached[0].entry_ids == [1]


def test_attach_digestible_tool_provenance_fails_closed_without_exact_coverage():
    """A numeric attempted span is not coverage. Missing exact tool IDs attach nothing."""

    candidate = JournalDigestCandidate(
        content="global min/max must not invent tool provenance",
        entry_ids=[1, 3],
    )
    candidates = JournalCandidateList(
        [candidate],
        attempted_entry_ids={1, 3},
        deferred_entry_ids={5},
    )
    attached = attach_digestible_tool_provenance(
        candidates,
        [
            _ToolEntry(1, role="user"),
            _ToolEntry(2),
            _ToolEntry(3, role="user"),
            _ToolEntry(4),
            _ToolEntry(5),
        ],
    )
    assert attached[0].entry_ids == [1, 3]


def test_attach_digestible_tool_provenance_keeps_only_same_chunk_tools():
    candidate = JournalDigestCandidate(
        content="exact same-chunk tool provenance stays; deferred suffix does not",
        entry_ids=[1, 3],
    )
    candidate.covered_tool_ids = [2]
    candidates = JournalCandidateList(
        [candidate],
        attempted_entry_ids={1, 3, 8},
        deferred_entry_ids={5},
    )
    attached = attach_digestible_tool_provenance(
        candidates,
        [
            _ToolEntry(1, role="user"),
            _ToolEntry(2),
            _ToolEntry(3, role="user"),
            _ToolEntry(4),
            _ToolEntry(5),
            _ToolEntry(6, role="user"),
            _ToolEntry(8, role="user"),
        ],
    )
    assert attached[0].entry_ids == [1, 3, 2]


def test_attach_digestible_tool_provenance_does_not_cross_session_or_scope():
    candidate = JournalDigestCandidate(
        content="session A must not receive the interleaved session B tool",
        entry_ids=[1, 3],
    )
    candidate.covered_tool_ids = [2]
    candidates = JournalCandidateList(
        [candidate],
        attempted_entry_ids={1, 3, 4},
    )
    attached = attach_digestible_tool_provenance(
        candidates,
        [
            _ToolEntry(1, role="user", session_id="session-a"),
            _ToolEntry(2, scope_id="scope-b", session_id="session-b"),
            _ToolEntry(3, role="user", session_id="session-a"),
            _ToolEntry(4, role="user", scope_id="scope-b", session_id="session-b"),
        ],
    )
    assert attached[0].entry_ids == [1, 3]


def test_attach_digestible_tool_provenance_does_not_dump_every_tool_on_first_candidate():
    first = JournalDigestCandidate(
        content="first chunk keeps only its own covered tool",
        entry_ids=[1, 3],
    )
    first.covered_tool_ids = [2]
    second = JournalDigestCandidate(
        content="second chunk keeps only its own covered tool",
        entry_ids=[6, 8],
    )
    second.covered_tool_ids = [7]
    candidates = JournalCandidateList(
        [first, second],
        attempted_entry_ids={1, 3, 6, 8},
    )
    attached = attach_digestible_tool_provenance(
        candidates,
        [
            _ToolEntry(1, role="user", session_id="session-a"),
            _ToolEntry(2, session_id="session-a"),
            _ToolEntry(3, role="user", session_id="session-a"),
            _ToolEntry(6, role="user", session_id="session-b"),
            _ToolEntry(7, session_id="session-b"),
            _ToolEntry(8, role="user", session_id="session-b"),
        ],
    )
    assert attached[0].entry_ids == [1, 3, 2]
    assert attached[1].entry_ids == [6, 8, 7]
