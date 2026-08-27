from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scope_recall.durable_work import (
    DURABLE_WORK_ITEM_STATES,
    DURABLE_WORK_RETRY_CLASSES,
    DURABLE_WORK_SCHEMA_VERSION,
    DURABLE_WORK_TERMINAL_STATES,
    DurableWorkBatchResult,
    DurableWorkDescriptor,
    DurableWorkError,
    DurableWorkItem,
    DurableWorkLease,
    canonical_snapshot_hash,
    durable_work_health,
    next_lease_generation,
    validate_item_transition,
    validate_replacement_lease,
)


ROOT = Path(__file__).resolve().parents[1]


def _lease(*, generation: int = 1, token: str = "lease-1") -> DurableWorkLease:
    return DurableWorkLease(
        worker_id="worker-a",
        lease_token=token,
        lease_generation=generation,
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        bounded_item_budget=8,
        bounded_wall_clock_budget=0.5,
    )


def test_descriptor_matches_frozen_canonical_fields_and_snapshots() -> None:
    scope = {"scope_ids": ["scope-a"]}
    authority = {"principal": "primary", "writable": True}
    descriptor = DurableWorkDescriptor(
        work_id="relation:scope-a:7",
        domain_type="relation_policy_generation",
        idempotency_key="scope-a:7:policy-v2",
        scope_snapshot=scope,
        authority_snapshot=authority,
        policy_version="policy-v2",
        generation=7,
        frozen_upper_bound=2,
        item_set_hash=canonical_snapshot_hash({"pairs": [["a", "b"], ["a", "c"]]}),
        created_at="2026-08-27T05:00:00+00:00",
    )

    assert set(descriptor.as_dict()) == {
        "work_id",
        "domain_type",
        "idempotency_key",
        "scope_snapshot",
        "authority_snapshot",
        "policy_version",
        "generation",
        "frozen_upper_bound",
        "item_set_hash",
        "created_at",
    }
    scope["scope_ids"].append("scope-b")
    authority["writable"] = False
    assert descriptor.as_dict()["scope_snapshot"] == {"scope_ids": ["scope-a"]}
    assert descriptor.as_dict()["authority_snapshot"]["writable"] is True
    with pytest.raises(AttributeError):
        descriptor.scope_snapshot["scope_ids"].append("scope-c")
    with pytest.raises(FrozenInstanceError):
        descriptor.generation = 9  # type: ignore[misc]


def test_lease_identity_generation_and_expiry_are_strict() -> None:
    first = _lease()
    assert next_lease_generation(None) == 1
    assert next_lease_generation(first) == 2
    assert first.matches(
        worker_id="worker-a", lease_token="lease-1", lease_generation=1
    )
    assert not first.matches(
        worker_id="worker-a", lease_token="wrong", lease_generation=1
    )
    expired_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    assert not first.matches(
        worker_id="worker-a",
        lease_token="lease-1",
        lease_generation=1,
        now=expired_at,
    )

    replacement = _lease(generation=2, token="lease-2")
    validate_replacement_lease(first, replacement)
    with pytest.raises(ValueError, match="new immutable token"):
        validate_replacement_lease(first, _lease(generation=2, token="lease-1"))
    with pytest.raises(ValueError, match="advance exactly once"):
        validate_replacement_lease(first, _lease(generation=3, token="lease-3"))


def test_terminal_items_never_revive_and_retry_graph_is_explicit() -> None:
    assert DURABLE_WORK_TERMINAL_STATES == {
        "completed",
        "poisoned",
        "cancelled",
        "superseded",
    }
    validate_item_transition("pending", "processing")
    validate_item_transition("processing", "retry")
    validate_item_transition("retry", "processing")
    validate_item_transition("processing", "completed")
    validate_item_transition("completed", "completed")
    with pytest.raises(ValueError, match="completed->processing"):
        validate_item_transition("completed", "processing")
    with pytest.raises(ValueError, match="pending->completed"):
        validate_item_transition("pending", "completed")


def test_item_and_error_class_contracts_are_bounded() -> None:
    item = DurableWorkItem(
        item_identity="scope-a:7:a:b",
        state="retry",
        attempt=2,
        max_attempts=3,
        not_before="2026-08-27T05:01:00+00:00",
        last_error_class="dependency_unavailable",
        last_error_code="embedder_unavailable",
        last_progress_at="2026-08-27T05:00:30+00:00",
        receipt={"generation": 7},
    )
    assert item.terminal is False
    assert set(item.as_dict()) == {
        "item_identity",
        "state",
        "attempt",
        "max_attempts",
        "not_before",
        "last_error_class",
        "last_error_code",
        "last_progress_at",
        "receipt",
    }
    assert DURABLE_WORK_RETRY_CLASSES == {
        "retriable",
        "permanent",
        "poison",
        "authority_revoked",
        "epoch_mismatch",
        "dependency_unavailable",
        "contention",
    }
    error = DurableWorkError(
        "temporary provider fault",
        retry_class="dependency_unavailable",
        code="provider_unavailable",
    )
    assert error.retry_class == "dependency_unavailable"
    assert error.code == "provider_unavailable"
    with pytest.raises(ValueError, match="attempt cannot exceed"):
        DurableWorkItem("bad", "retry", 4, 3)


def test_batch_result_enforces_monotonic_cursor_and_bounded_outcomes() -> None:
    result = DurableWorkBatchResult(
        attempted=3,
        completed=2,
        retried=1,
        poisoned=0,
        cancelled=0,
        superseded=0,
        cursor_before=10,
        cursor_after=13,
    )
    assert result.cursor_after == 13
    with pytest.raises(ValueError, match="cursor must be monotonic"):
        DurableWorkBatchResult(1, 1, 0, 0, 0, 0, 2, 1)
    with pytest.raises(ValueError, match="outcomes cannot exceed"):
        DurableWorkBatchResult(1, 1, 1, 0, 0, 0, 0, 1)


def test_shared_health_envelope_is_content_free_and_complete() -> None:
    health = durable_work_health(
        domain_type="relation_policy_generation",
        state="degraded",
        reason_code="retryable_debt",
        item_counts={"pending": 2, "processing": 1, "completed": 4},
        oldest_age_seconds=12.34567,
        last_progress_at="2026-08-27T05:00:00+00:00",
        progress_rate=0.25,
        lease_expirations=2,
        lock_contention=1,
        auto_recoverable=True,
        operator_action_required=False,
        fairness={"scope": "oldest_first", "foreground_pressure": "bounded"},
    )

    assert health["schema_version"] == DURABLE_WORK_SCHEMA_VERSION
    assert set(health["item_counts"]) == DURABLE_WORK_ITEM_STATES
    assert health["runnable_count"] == 3
    assert health["terminal_count"] == 4
    assert health["oldest_age_seconds"] == 12.346
    assert health["fairness"]["scope"] == "oldest_first"


def test_contract_layer_owns_no_sql_or_universal_payload_table() -> None:
    source = (ROOT / "durable_work.py").read_text(encoding="utf-8").lower()
    assert "import sqlite3" not in source
    assert "create table" not in source
    assert "durable_jobs" not in source
