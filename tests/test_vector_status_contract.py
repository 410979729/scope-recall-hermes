"""Program 0A regression coverage for the public four-state vector contract."""

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


def test_fresh_pending_debt_does_not_change_an_explicit_ready_state() -> None:
    payload = vector_status_contract(
        state="ready",
        reason_code="healthy",
        debt_counts={"pending": 1},
    )

    assert payload["state"] == "ready"
    assert payload["debt_counts"]["pending"] == 1
    assert payload["auto_recoverable"] is True
