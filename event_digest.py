"""Lightweight event evidence packets for conservative memory candidate extraction.

This module intentionally does not write to SQLite. Provider hooks and closeout
workflows can use it to normalize event evidence before a later candidate
extractor decides whether anything is worth proposing for review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .capture_filters import contains_secret_like_text, redact_secret_like_text, sanitize_report_text

_EVENT_KIND_ALIASES = {
    "task-complete": "task_closeout",
    "task_complete": "task_closeout",
    "task-completed": "task_closeout",
    "task_completed": "task_closeout",
    "closeout": "task_closeout",
    "pre-compress": "pre_compress",
    "precompress": "pre_compress",
    "compression": "pre_compress",
    "session-end": "session_end",
    "sessionend": "session_end",
    "release-closeout": "release_closeout",
    "issue-closeout": "issue_closeout",
}
_ALLOWED_EVENT_KINDS = {
    "task_closeout",
    "pre_compress",
    "session_end",
    "release_closeout",
    "issue_closeout",
}


def normalize_event_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    normalized = _EVENT_KIND_ALIASES.get(normalized, normalized)
    if normalized in _ALLOWED_EVENT_KINDS:
        return normalized
    return "unknown"


@dataclass(frozen=True)
class MemoryEvent:
    """Normalized event evidence produced by provider hooks or task closeout code."""

    kind: str
    scope_id: str
    session_id: str
    turn_number: int | None
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", normalize_event_kind(self.kind))
        object.__setattr__(self, "scope_id", str(self.scope_id or ""))
        object.__setattr__(self, "session_id", str(self.session_id or ""))
        object.__setattr__(self, "content", str(self.content or ""))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


@dataclass(frozen=True)
class EvidencePacket:
    """Sanitized, bounded event evidence for later candidate extraction."""

    ok: bool
    event: MemoryEvent
    content: str
    evidence_refs: list[str]
    rejection_reasons: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


def _event_ref(event: MemoryEvent) -> str:
    if event.turn_number is None:
        return f"session:{event.session_id}:event:{event.kind}"
    return f"session:{event.session_id}:turn:{event.turn_number}"


def _sanitize_metadata(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {sanitize_report_text(str(key)): _sanitize_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item) for item in value]
    if isinstance(value, bytes):
        return sanitize_report_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, str):
        return sanitize_report_text(value)
    return sanitize_report_text(str(value))


def build_evidence_packet(event: MemoryEvent, *, min_content_chars: int = 16, max_content_chars: int = 4000) -> EvidencePacket:
    """Build a sanitized evidence packet without performing durable writes."""
    rejection_reasons: list[str] = []
    raw_content = event.content or ""
    secret_like = contains_secret_like_text(raw_content)
    sanitized = redact_secret_like_text(raw_content) if secret_like else sanitize_report_text(raw_content)
    sanitized = sanitized.strip()
    if len(sanitized) > max_content_chars:
        sanitized = sanitized[:max_content_chars].rstrip()
    if event.kind == "unknown":
        rejection_reasons.append("unknown_event_kind")
    if secret_like:
        rejection_reasons.append("plaintext_secret_rejected")
    if len(sanitized) < min_content_chars:
        rejection_reasons.append("low_signal")
    metadata = {
        "event_kind": event.kind,
        "scope_id": event.scope_id,
        "session_id": event.session_id,
        "turn_number": event.turn_number,
        "source_metadata": _sanitize_metadata(event.metadata or {}),
        "secret_like": secret_like,
    }
    return EvidencePacket(
        ok=not rejection_reasons,
        event=event,
        content=sanitized,
        evidence_refs=[_event_ref(event)],
        rejection_reasons=rejection_reasons,
        metadata=metadata,
    )
