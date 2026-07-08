"""Tests for lightweight event digest evidence packets."""

from __future__ import annotations

from scope_recall.event_digest import MemoryEvent, build_evidence_packet, normalize_event_kind


def test_memory_event_normalizes_kind_and_builds_evidence_packet():
    event = MemoryEvent(
        kind="task-complete",
        scope_id="telegram:agent:user",
        session_id="session-1",
        turn_number=42,
        content="User prefers concise Chinese release reports after verification.",
        metadata={"source": "closeout"},
    )

    packet = build_evidence_packet(event)

    assert event.kind == "task_closeout"
    assert normalize_event_kind("pre-compress") == "pre_compress"
    assert packet.ok is True
    assert packet.event.kind == "task_closeout"
    assert packet.content == "User prefers concise Chinese release reports after verification."
    assert packet.evidence_refs == ["session:session-1:turn:42"]
    assert packet.rejection_reasons == []


def test_evidence_packet_redacts_secret_like_content():
    event = MemoryEvent(
        kind="session_end",
        scope_id="cli:local:user",
        session_id="session-2",
        turn_number=None,
        content="The deployment " + "token: " + "sk" + "-abc...3456 should never be stored.",
        metadata={},
    )

    packet = build_evidence_packet(event)

    assert packet.ok is False
    assert "plaintext_secret_rejected" in packet.rejection_reasons
    assert "sk" + "-abc...3456" not in packet.content
    assert "[REDACTED_SECRET]" in packet.content


def test_evidence_packet_rejects_short_low_signal_events():
    event = MemoryEvent(
        kind="pre_compress",
        scope_id="cli:local:user",
        session_id="session-3",
        turn_number=3,
        content="ok thanks",
        metadata={},
    )

    packet = build_evidence_packet(event, min_content_chars=20)

    assert packet.ok is False
    assert "low_signal" in packet.rejection_reasons
