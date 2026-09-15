from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from p18_codex_budget import (
    CODEX_INPUT_CAP,
    CODEX_RESERVED_INPUT,
    CODEX_RESERVED_OUTPUT,
    CodexBudgetError,
    CodexBudgetPolicy,
    CodexSubmissionBudget,
    HISTORICAL_NOT_PRE_DISPATCH,
    MONETARY_UNAVAILABLE,
)


def _budget(tmp_path: Path, *, policy: CodexBudgetPolicy | None = None) -> CodexSubmissionBudget:
    budget = CodexSubmissionBudget(tmp_path / "TEST-shared-ledger.sqlite3", policy=policy)
    budget.initialize_schema()
    return budget


def test_adapter_owns_only_codex_submissions_table(tmp_path: Path) -> None:
    path = tmp_path / "TEST-existing-ledger.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, status TEXT)")
        db.execute("CREATE TABLE adjustments (id INTEGER PRIMARY KEY, note TEXT)")
        db.commit()
    budget = CodexSubmissionBudget(path)
    budget.initialize_schema()
    tables = {row[0] for row in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"requests", "adjustments", "codex_submissions"} <= tables
    assert budget.summary()["submissions"] == 0


def test_reserve_is_atomic_idempotent_and_enforces_caps(tmp_path: Path) -> None:
    budget = _budget(tmp_path, policy=CodexBudgetPolicy(call_cap=1, input_cap=CODEX_RESERVED_INPUT, output_cap=CODEX_RESERVED_OUTPUT))
    first = budget.reserve("op-1", b"TEST request")
    repeat = budget.reserve("op-1", b"TEST request")
    assert first["operation_id"] == repeat["operation_id"] == "op-1"
    assert repeat["idempotent"] is True
    assert first["reserved_input"] == CODEX_RESERVED_INPUT
    assert first["reserved_output"] == CODEX_RESERVED_OUTPUT
    with pytest.raises(CodexBudgetError, match="operation_id_conflict"):
        budget.reserve("op-1", b"TEST different request")
    with pytest.raises(CodexBudgetError, match="budget_exhausted"):
        budget.reserve("op-2", b"TEST second request")


def test_unknown_usage_retains_reservation_and_finish_is_idempotent(tmp_path: Path) -> None:
    budget = _budget(tmp_path)
    budget.reserve("op-unknown", b"TEST request")
    finished = budget.finish("op-unknown", "completed", None)
    repeated = budget.finish("op-unknown", "ignored", None)
    assert finished["status"] == "completed_usage_unknown_reserved"
    assert finished["actual_input"] is None and finished["actual_output"] is None
    assert repeated["status"] == finished["status"] and repeated["idempotent"] is True
    summary = budget.summary()
    assert summary["unknown_usage_rows"] == 1
    assert summary["input_tokens_accounted"] == CODEX_RESERVED_INPUT
    assert summary["monetary_status"] == MONETARY_UNAVAILABLE


def test_usage_is_reliable_only_when_nonnegative_and_consistent(tmp_path: Path) -> None:
    budget = _budget(tmp_path)
    budget.reserve("op-valid", b"TEST request")
    with pytest.raises(CodexBudgetError, match="prompt_tokens"):
        budget.finish("op-valid", "completed", {"prompt_tokens": -1, "completion_tokens": 2})
    with pytest.raises(CodexBudgetError, match="total_mismatch"):
        budget.finish("op-valid", "completed", {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 3})
    valid = budget.finish("op-valid", "completed", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})
    assert valid["actual_input"] == 2 and valid["actual_output"] == 3
    assert valid["usage_quality"] == "reliable"

    budget.reserve("op-over", b"TEST over")
    over = budget.finish("op-over", "completed", {"prompt_tokens": CODEX_RESERVED_INPUT + 1, "completion_tokens": 0})
    assert over["status"] == "meter_breach"
    with pytest.raises(CodexBudgetError, match="meter_breach"):
        budget.reserve("op-after-breach", b"TEST blocked")


def test_legacy_backfill_is_marked_and_unknown_bound_blocks_new_reserve(tmp_path: Path) -> None:
    budget = _budget(tmp_path)
    historical = budget.backfill_legacy("legacy-1", audit_source="P12-callback-receipt", input_upper_bound=None, output_upper_bound=None)
    assert historical["reservation_origin"] == "legacy_backfill"
    assert historical["dispatch_status"] == HISTORICAL_NOT_PRE_DISPATCH
    assert historical["historical_not_pre_dispatch"] is True
    assert historical["actual_input"] is None and historical["actual_output"] is None
    with pytest.raises(CodexBudgetError, match="legacy_unknown_upper_bound"):
        budget.reserve("new-1", b"TEST request")
    with pytest.raises(CodexBudgetError, match="observed_usage_requires_upper_bound"):
        budget.backfill_legacy("legacy-2", audit_source="TEST-audit", observed_usage={"prompt_tokens": 1, "completion_tokens": 1})


def test_legacy_backfill_with_audited_bounds_never_claims_pre_dispatch(tmp_path: Path) -> None:
    policy = CodexBudgetPolicy(call_cap=2, input_cap=CODEX_INPUT_CAP, output_cap=CODEX_RESERVED_OUTPUT * 2)
    budget = _budget(tmp_path, policy=policy)
    historical = budget.backfill_legacy(
        "legacy-observed",
        audit_source="TEST-audited-source",
        input_upper_bound=8,
        output_upper_bound=4,
        observed_usage={"prompt_tokens": 3, "completion_tokens": 2},
    )
    assert historical["dispatch_status"] == HISTORICAL_NOT_PRE_DISPATCH
    assert historical["usage_quality"] == "historical_observed"
    assert historical["actual_input"] == 3 and historical["actual_output"] == 2
    assert historical["monetary_status"] == MONETARY_UNAVAILABLE


def test_native_aux_unknown_reservation_is_separate_but_other_batches_share_cap(tmp_path):
    from p18_codex_budget import NATIVE_AUX_BATCH
    main = _budget(tmp_path, policy=CodexBudgetPolicy(call_cap=1))
    auxiliary = CodexSubmissionBudget(main.path, policy=CodexBudgetPolicy(
        batch=NATIVE_AUX_BATCH, call_cap=100000, input_cap=10**12,
        output_cap=10**12, reserved_output=3000000))
    auxiliary.reserve("aux", b"native source")
    auxiliary.finish("aux", "closed", None)
    main.reserve("query", b"query")
    assert main.summary()["submissions"] == 1
    assert auxiliary.summary()["unknown_usage_rows"] == 1
    assert auxiliary.summary()["output_tokens_accounted"] == 3000000
    other = CodexSubmissionBudget(main.path, policy=CodexBudgetPolicy(batch="OTHER", call_cap=1))
    with pytest.raises(CodexBudgetError, match="budget_exhausted"):
        other.reserve("other", b"other query")
