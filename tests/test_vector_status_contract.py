"""Regression coverage for the public four-state vector contract."""

from __future__ import annotations

import pytest

from scope_recall.vector_status import VECTOR_STATES, vector_status_contract


@pytest.mark.parametrize(
    ("state", "auto_recoverable", "repair_required", "usable_for_query"),
    [
        ("ready", True, False, True),
        ("degraded", True, False, True),
        ("needs_repair", False, True, False),
        ("disabled", False, False, False),
    ],
)
def test_vector_state_boolean_matrix(
    state: str,
    auto_recoverable: bool,
    repair_required: bool,
    usable_for_query: bool,
) -> None:
    payload = vector_status_contract(state=state, reason_code="test")

    assert payload["state"] == state
    assert payload["schema_version"] == "vector_status.v1"
    assert payload["status"] == state
    assert payload["auto_recoverable"] is auto_recoverable
    assert payload["repair_required"] is repair_required
    assert payload["usable_for_query"] is usable_for_query
    assert payload["debt_counts"] == {
        "pending": 0,
        "processing": 0,
        "retry": 0,
        "dead_letter": 0,
        "replayable": 0,
    }


def test_vector_debt_counts_are_stable_and_replayable_is_derived() -> None:
    payload = vector_status_contract(
        state="degraded",
        reason_code="outbox_retryable",
        debt_counts={"pending": 2, "processing": 1, "retry": 3, "dead_letter": 0},
    )

    assert payload["debt_counts"] == {
        "pending": 2,
        "processing": 1,
        "retry": 3,
        "dead_letter": 0,
        "replayable": 6,
    }


def test_noncanonical_top_level_vector_state_is_rejected() -> None:
    assert VECTOR_STATES == {"ready", "degraded", "needs_repair", "disabled"}
    with pytest.raises(ValueError, match="unsupported vector state"):
        vector_status_contract(state="error", reason_code="legacy")


@pytest.mark.parametrize("debt_key", ["pending", "processing", "retry"])
def test_observable_replayable_debt_canonicalizes_ready_to_degraded(
    debt_key: str,
) -> None:
    payload = vector_status_contract(
        state="ready",
        reason_code="healthy",
        debt_counts={debt_key: 1},
    )

    assert payload["state"] == "degraded"
    assert payload["reason_code"] == {
        "pending": "outbox_pending",
        "processing": "outbox_processing",
        "retry": "outbox_retryable",
    }[debt_key]
    assert payload["debt_counts"][debt_key] == 1
    assert payload["auto_recoverable"] is True


def test_short_pending_inside_one_replay_cycle_does_not_flap_ready() -> None:
    payload = vector_status_contract(
        state="ready",
        reason_code="healthy",
        debt_counts={"pending": 1},
        pending_within_replay_cycle=True,
    )

    assert payload["state"] == "ready"
    assert payload["reason_code"] == "healthy"


def test_retry_is_degraded_even_when_pending_is_inside_replay_cycle() -> None:
    payload = vector_status_contract(
        state="ready",
        reason_code="healthy",
        debt_counts={"pending": 1, "retry": 1},
        pending_within_replay_cycle=True,
    )

    assert payload["state"] == "degraded"
    assert payload["reason_code"] == "outbox_retryable"


def test_dead_letter_is_needs_repair() -> None:
    payload = vector_status_contract(
        state="ready",
        reason_code="healthy",
        debt_counts={"dead_letter": 1},
        usable_for_query=True,
    )

    assert payload["state"] == "needs_repair"
    assert payload["reason_code"] == "outbox_dead_letter"
    assert payload["auto_recoverable"] is False
    assert payload["repair_required"] is True
    assert payload["usable_for_query"] is False


def test_audit_mismatch_is_needs_repair() -> None:
    payload = vector_status_contract(
        state="ready",
        reason_code="audit_mismatch",
        usable_for_query=True,
    )

    assert payload["state"] == "needs_repair"
    assert payload["repair_required"] is True
    assert payload["usable_for_query"] is False


def test_zero_debt_healthy_generation_is_ready() -> None:
    payload = vector_status_contract(state="ready", reason_code="healthy")

    assert payload["state"] == "ready"
    assert payload["reason_code"] == "healthy"
    assert payload["debt_counts"]["replayable"] == 0


def test_disabled_has_no_repair_requirement_even_with_dormant_debt() -> None:
    payload = vector_status_contract(
        state="disabled",
        reason_code="disabled_by_config",
        debt_counts={"dead_letter": 1},
    )

    assert payload["state"] == "disabled"
    assert payload["auto_recoverable"] is False
    assert payload["repair_required"] is False
    assert payload["usable_for_query"] is False


def test_degraded_can_remain_query_usable_without_reporting_ready() -> None:
    payload = vector_status_contract(
        state="ready",
        reason_code="healthy",
        debt_counts={"retry": 1},
        usable_for_query=True,
    )

    assert payload["state"] == "degraded"
    assert payload["status"] == "degraded"
    assert payload["usable_for_query"] is True
