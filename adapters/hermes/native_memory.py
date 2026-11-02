"""Capture built-in memory notifications with host-reported writer provenance."""

from __future__ import annotations

from dataclasses import replace

from ...contracts import TrustedSourcePrincipal
from ...core.memory_import import digest

# A tool notification reports who edited a memory file, not who uttered its
# contents. Even a human edit can paste someone else's summary.
_ASSISTANT_WRITERS = frozenset({"assistant_tool", "background_review"})


def record_memory_write(adapter, action: str, target: str, content: str, metadata=None) -> None:
    """Record add/replace/remove through the adapter's bounded capture and retry path."""
    if action not in {"add", "replace", "remove"} or target not in {"memory", "user"}:
        adapter._merge_gaps(("capture_gap:memory_write_shape",))
        return
    identity = adapter._require_identity()
    info = dict(metadata or {})
    writer = info.get("write_origin")
    origin = "assistant_visible" if writer in _ASSISTANT_WRITERS else "origin_unknown"
    context = identity.trusted_context(session_id=info.get("session_id") or None, actor_origin=origin, mutation=True)
    if writer in {"user", "human_direct", "user_command"}:
        context = replace(context, source_principal=TrustedSourcePrincipal("human", "unresolved"))
    text = content if action != "remove" else info.get("previous_content") or ""
    # A remove is an operation record, never a renewed assertion of the old entry.
    writer_label = writer if isinstance(writer, str) and len(writer) <= 80 else "unknown"
    text = f"Native memory {action} ({target}; writer={writer_label}):\n{text}"
    token = digest([context.session_id, info.get("tool_call_id"), action, target, writer, text])
    now = adapter._utc_now()
    event = dict(
        protocol_version="1.1",
        source_event_key="native-memory:" + token,
        source_revision=1,
        origin=origin,
        role="assistant" if origin == "assistant_visible" else "unknown",
        content=text,
        occurred_at=now,
        recorded_at=now,
        time_precision="instant",
        capture_state="complete",
        evidence_refs=[],
        source_principal=context.source_principal.to_payload(),
    )
    adapter._writer.write(
        context,
        event,
        identity=(event["source_event_key"], 1),
        gaps=(),
        scope_id=identity.local_scope_id,
        bound=identity,
        release=True,
    )
