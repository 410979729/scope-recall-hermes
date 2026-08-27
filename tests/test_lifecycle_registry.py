"""Observe contracts for lifecycle registry convergence."""

from __future__ import annotations

import ast
from pathlib import Path

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

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_PRODUCER_FILES = (
    "candidate_review.py",
    "auto_adjudication.py",
    "forgetting.py",
    "governance_cleanup.py",
    "fact_executor.py",
    "governance_rollback.py",
    "memory_ops.py",
    "privacy_purge.py",
    "nightly_digest.py",
    "scripts/promote.memory_candidates.py",
    "scripts/migrate.legacy_hygiene.py",
    "scripts/benchmark.golden.py",
    "scripts/benchmark.retrieval_regression.py",
)


def test_registry_is_complete_and_well_formed() -> None:
    assert validate_lifecycle_registry() == ()
    assert len(LIFECYCLE_REGISTRY) == 34
    assert all(operation.operation_id == operation_id for operation_id, operation in LIFECYCLE_REGISTRY.items())
    assert all(operation.allowed_from_states for operation in LIFECYCLE_REGISTRY.values())
    assert all(operation.projection_effects for operation in LIFECYCLE_REGISTRY.values())


def test_current_producer_census_resolves_only_registered_operations() -> None:
    assert len(LIFECYCLE_PRODUCER_CENSUS) == 14
    producer_ids = {
        operation_id
        for binding in LIFECYCLE_PRODUCER_CENSUS
        for operation_id in binding.operation_ids
    }
    assert producer_ids <= set(LIFECYCLE_REGISTRY)
    assert len(producer_ids) == 32


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
        "operation_count": 34,
        "producer_count": 14,
        "archive_coverage_receipt_count": 8,
        "default_rollback_event_count": 4,
        "errors": [],
    }


def test_all_lifecycle_producers_select_operation_id_without_raw_receipt_fields() -> None:
    calls: list[tuple[str, int, str]] = []
    for relative_path in LIFECYCLE_PRODUCER_FILES:
        tree = ast.parse((PLUGIN_ROOT / relative_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = ""
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            if function_name not in {
                "transition_memory_lifecycle",
                "hard_delete_memories",
            }:
                continue
            keyword_names = {keyword.arg for keyword in node.keywords}
            assert "operation_id" in keyword_names, (relative_path, node.lineno)
            assert "event_type" not in keyword_names, (relative_path, node.lineno)
            assert "action" not in keyword_names, (relative_path, node.lineno)
            calls.append((relative_path, node.lineno, function_name))
    assert len(calls) == 19


def test_fact_and_backfill_receipt_producers_derive_legacy_identity_from_registry() -> None:
    fact_source = (PLUGIN_ROOT / "fact_executor.py").read_text(encoding="utf-8")
    cleanup_source = (PLUGIN_ROOT / "governance_cleanup.py").read_text(
        encoding="utf-8"
    )
    assert 'event_type="fact_evolution"' not in fact_source
    assert 'action="enrich"' not in fact_source
    assert 'action="legacy_archive_backfill"' not in cleanup_source
    assert "resolve_lifecycle_operation(LEGACY_ARCHIVE_BACKFILL)" in cleanup_source
