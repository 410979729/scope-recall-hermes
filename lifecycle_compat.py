"""Explicit V1 raw event/action compatibility adapter for lifecycle callers."""

from __future__ import annotations

from .lifecycle_registry import (
    LIFECYCLE_REGISTRY,
    LifecycleOperation,
    UnknownLifecycleOperationError,
    resolve_lifecycle_operation,
)


class LegacyLifecycleReceiptError(ValueError):
    """A V1 event/action pair is absent or ambiguous in the canonical registry."""


def resolve_lifecycle_request(
    *,
    operation_id: str,
    legacy_event_type: str,
    legacy_action: str,
    default_operation_id: str = "",
) -> LifecycleOperation:
    """Resolve either the canonical id or one registered V1 receipt identity."""

    normalized_operation_id = str(operation_id or "").strip()
    event_type = str(legacy_event_type or "").strip()
    action = str(legacy_action or "").strip()
    if normalized_operation_id:
        if event_type or action:
            raise LegacyLifecycleReceiptError(
                "operation_id cannot be combined with raw event_type/action"
            )
        return resolve_lifecycle_operation(normalized_operation_id)
    if not event_type and not action and default_operation_id:
        return resolve_lifecycle_operation(default_operation_id)
    if not event_type or not action:
        raise LegacyLifecycleReceiptError(
            "raw lifecycle compatibility requires both event_type and action"
        )
    candidates = [
        operation
        for operation in LIFECYCLE_REGISTRY.values()
        if operation.legacy_event_type == event_type
        and operation.legacy_action == action
    ]
    if len(candidates) != 1:
        detail = "unregistered" if not candidates else "ambiguous"
        raise UnknownLifecycleOperationError(
            f"{detail} legacy lifecycle receipt: {event_type}/{action}"
        )
    return candidates[0]
