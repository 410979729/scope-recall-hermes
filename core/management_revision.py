"""Create truthful local-management provenance for an explicit owner correction."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from ..contracts import ContractError, validate_capture
from .mutate import revise_from_management


def correct(tx, clock, ref, expected_revision, value):
    """Write one correction and its source atomically through existing repositories."""
    if type(expected_revision) is not int or expected_revision < 1:
        raise ContractError("INPUT_INVALID", "expected_revision")
    if value is not None and (type(value) is not str or not value.strip() or len(value) > 4096):
        raise ContractError("INPUT_INVALID", "value")
    head = tx.claims.version(ref, expected_revision)
    if head is None or head.current_revision != expected_revision:
        raise ContractError("VERSION_CONFLICT", "expected_revision")
    tx.claims.require_target(head)
    now, action_id = clock.utc_now(), "management-" + uuid4().hex
    shared = tx.context.binding.installation_kind == "shared"
    if shared:
        tx.registry.register_entry("local-management", "Local management", "management", now=now)
    context = replace(
        tx.context,
        session_id=action_id,
        actor_origin="host_generated",
        source_principal=None,
        entry_id="local-management" if shared else None,
    )
    view = tx.with_context(context, writable=True)
    event = dict(
        protocol_version="1.1",
        source_event_key=action_id,
        source_revision=1,
        origin="host_generated",
        role="system",
        content=value if value is not None else f"Withdraw {ref}: {head.payload['value_text']}",
        occurred_at=now,
        recorded_at=now,
        time_precision="instant",
        capture_state="complete",
        evidence_refs=[],
        source_context=dict(platform="local_management", chat_type="management"),
    )
    saved = view.sources.put_source(validate_capture(event, context), scope_id=head.scope_id, persisted_at=now)
    view.sources.index_source(saved.ref, saved.revision)
    source = view.source(saved.ref, saved.revision)
    if source is None:
        raise ContractError("SOURCE_MISSING", "management_source")
    request = dict(
        target_ref=ref,
        expected_revision=expected_revision,
        new_value=value,
        conditions=head.payload["conditions"],
        valid_from=now,
    )
    result = revise_from_management(view, clock, request, source)
    return action_id, result, source.ref
