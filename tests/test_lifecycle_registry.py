"""Program 1B Observe contracts for lifecycle registry convergence."""

from __future__ import annotations

import pytest

from scope_recall.governance_cleanup import RECOGNIZED_ARCHIVE_RECEIPTS
from scope_recall.governance_rollback import DEFAULT_ROLLBACK_EVENT_ACTIONS
from scope_recall.lifecycle_registry import (
    LIFECYCLE_PRODUCER_CENSUS,
    LIFECYCLE_REGISTRY,
    UnknownLifecycleOperationError,
    archive_coverage_receipts,
    default_rollback_event_actions,
    lifecycle_registry_report,
    resolve_lifecycle_operation,
    validate_lifecycle_registry,
)


def test_registry_is_complete_and_well_formed() -> None:
    assert validate_lifecycle_registry() == ()
    assert len(LIFECYCLE_REGISTRY) == 30
    assert all(operation.operation_id == operation_id for operation_id, operation in LIFECYCLE_REGISTRY.items())
    assert all(operation.allowed_from_states for operation in LIFECYCLE_REGISTRY.values())
    assert all(operation.projection_effects for operation in LIFECYCLE_REGISTRY.values())


def test_current_producer_census_resolves_only_registered_operations() -> None:
    assert len(LIFECYCLE_PRODUCER_CENSUS) == 13
    producer_ids = {
        operation_id
        for binding in LIFECYCLE_PRODUCER_CENSUS
        for operation_id in binding.operation_ids
    }
    assert producer_ids <= set(LIFECYCLE_REGISTRY)
    assert len(producer_ids) == 28


def test_observe_archive_coverage_is_exactly_legacy_equivalent() -> None:
    assert archive_coverage_receipts() == RECOGNIZED_ARCHIVE_RECEIPTS


def test_observe_default_rollback_is_exactly_legacy_equivalent() -> None:
    assert dict(default_rollback_event_actions()) == DEFAULT_ROLLBACK_EVENT_ACTIONS
    assert dict(default_rollback_event_actions()) == {
        "memory_cleanup": "soft_archive",
        "forgetting": "soft_archive",
        "scope_recall_forget": "soft_archive",
        "memory_auto_adjudication": "archive",
    }


def test_unknown_operation_id_fails_closed() -> None:
    with pytest.raises(UnknownLifecycleOperationError, match="unregistered"):
        resolve_lifecycle_operation("third.party.archive")


def test_registry_health_report_is_doctor_safe() -> None:
    assert lifecycle_registry_report() == {
        "status": "ready",
        "operation_count": 30,
        "producer_count": 13,
        "archive_coverage_receipt_count": 8,
        "default_rollback_event_count": 4,
        "errors": [],
    }
