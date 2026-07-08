"""Tests for lightweight event digest evidence packets."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_evidence_packet_recursively_redacts_source_metadata():
    secret_value = "tok" * 6
    private_path = "/home/" + "alice/private.txt"
    event = MemoryEvent(
        kind="session_end",
        scope_id="cli:local:user",
        session_id="session-2",
        turn_number=None,
        content="User prefers release summaries.",
        metadata={"nested": {"token": f"token: {secret_value}", "path": private_path}, "items": [private_path]},
    )

    packet = build_evidence_packet(event)
    rendered = str(packet.metadata)

    assert secret_value not in rendered
    assert private_path not in rendered
    assert "[REDACTED_SECRET]" in rendered
    assert "[REDACTED_PATH]" in rendered


def test_evidence_packet_metadata_is_json_safe_and_redacts_unknown_values():
    secret_value = "tok" * 6
    private_path = "/home/" + "alice/private.txt"

    class CustomValue:
        def __str__(self) -> str:
            return f"custom object token: {secret_value} at {private_path}"

    event = MemoryEvent(
        kind="session_end",
        scope_id="cli:local:user",
        session_id="session-2",
        turn_number=None,
        content="User prefers release summaries.",
        metadata={
            "path_obj": Path(private_path),
            "set_value": {f"token: {secret_value}"},
            "bytes_value": private_path.encode(),
            "custom": CustomValue(),
        },
    )

    packet = build_evidence_packet(event)
    rendered = json.dumps(packet.metadata, ensure_ascii=False, sort_keys=True)

    assert private_path not in rendered
    assert secret_value not in rendered
    assert "[REDACTED_PATH]" in rendered
    assert "[REDACTED_SECRET]" in rendered


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
