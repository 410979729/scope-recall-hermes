"""Tests for event evidence candidate extraction dry-run behavior."""

from __future__ import annotations

from scope_recall.candidate_extraction import extract_candidates_from_packet
from scope_recall.event_digest import MemoryEvent, build_evidence_packet


def _packet(content: str):
    return build_evidence_packet(
        MemoryEvent(
            kind="task_closeout",
            scope_id="telegram:agent:user",
            session_id="session-1",
            turn_number=9,
            content=content,
            metadata={"source": "test"},
        )
    )


def test_candidate_extraction_proposes_stable_user_preference():
    result = extract_candidates_from_packet(
        _packet("User prefers concise Chinese release reports with exact verification outputs.")
    )

    assert result.ok is True
    assert result.dry_run is True
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.target == "user"
    assert candidate.memory_type == "preference"
    assert candidate.confidence >= 0.7
    assert candidate.evidence_refs == ["session:session-1:turn:9"]
    assert candidate.risk_flags == []


def test_candidate_extraction_rejects_ephemeral_release_state():
    result = extract_candidates_from_packet(
        _packet("Issue #25 was closed after commit 5e0a33b and 794 tests passed today.")
    )

    assert result.ok is True
    assert result.candidates == []
    assert "ephemeral_release_state" in result.rejection_reasons


def test_candidate_extraction_rejects_generic_chat_transcript():
    result = extract_candidates_from_packet(
        _packet("Please review the last few messages and tell me what changed in this conversation.")
    )

    assert result.ok is True
    assert result.candidates == []
    assert result.rejection_reasons == ["unclassified_event_candidate"]


def test_candidate_extraction_rejects_unhealthy_evidence_packet():
    packet = build_evidence_packet(
        MemoryEvent(
            kind="session_end",
            scope_id="telegram:agent:user",
            session_id="session-2",
            turn_number=None,
            content="API " + "token: " + "sk" + "-abc...CRET should not become memory.",
            metadata={},
        )
    )

    result = extract_candidates_from_packet(packet)

    assert result.ok is False
    assert result.candidates == []
    assert "plaintext_secret_rejected" in result.rejection_reasons
