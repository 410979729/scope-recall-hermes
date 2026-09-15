from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from p18_hermes_operation_budget import HermesOperationBudget, HermesOperationBudgetError
from probes.hermes.p11_a2a_bridge import Bridge


def _ledger(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE requests (id INTEGER PRIMARY KEY, batch TEXT, model TEXT, body_sha256 TEXT, request_bytes INTEGER, reserved_input INTEGER, reserved_output INTEGER, actual_input INTEGER, actual_output INTEGER, charge_micro_usd INTEGER, status TEXT, started_ns INTEGER)"
        )
        db.execute(
            "INSERT INTO requests(id,batch,model,body_sha256,request_bytes,reserved_input,reserved_output,actual_input,actual_output,charge_micro_usd,status,started_ns) VALUES (1,'TEST-P11','deepseek-v4-flash',?,?,?,?,?,?,?,?,1)",
            (hashlib.sha256(body).hexdigest(), len(body), 32768, 4096, 12, 3, 100, "completed"),
        )
        db.commit()


def _ledger_rows(path: Path, rows: list[tuple[int, int | None, int | None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE requests (id INTEGER PRIMARY KEY, batch TEXT, model TEXT, body_sha256 TEXT, request_bytes INTEGER, reserved_input INTEGER, reserved_output INTEGER, actual_input INTEGER, actual_output INTEGER, charge_micro_usd INTEGER, status TEXT, started_ns INTEGER)"
        )
        for request_id, actual_input, actual_output in rows:
            db.execute(
                "INSERT INTO requests(id,batch,model,body_sha256,request_bytes,reserved_input,reserved_output,actual_input,actual_output,charge_micro_usd,status,started_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                (request_id, "TEST-P18", "deepseek-v4-flash", "0" * 64, 1, 32768, 4096, actual_input, actual_output, 100, "completed"),
            )
        db.commit()


def _archive(state: Path, reservation: dict[str, object], request_id: int, body: bytes, *, tag: str | None = None) -> None:
    archive = state / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    raw = archive / f"bridge-request-{tag or request_id}.body"
    raw.write_bytes(body)
    (archive / f"bridge-request-{tag or request_id}.json").write_text(json.dumps({
        "ledger_request_id": request_id,
        "ledger_status": "completed",
        "p18_active_operation": reservation,
        "request_artifact_path": str(raw),
        "request_artifact_sha256": hashlib.sha256(body).hexdigest(),
    }), encoding="utf-8")


@pytest.mark.parametrize("external_ledger", [False, True])
def test_marker_is_exclusive_and_finish_binds_existing_requests_row(tmp_path: Path, external_ledger: bool) -> None:
    config_root = tmp_path / "TEST-config"
    state = config_root / "bridge-state"
    ledger = (tmp_path if external_ledger else config_root) / "call-budget.sqlite3"
    output = config_root / "TEST-formal-run"
    body = b'{"model":"deepseek-v4-flash","messages":[]}'
    _ledger(ledger, body)
    budget = HermesOperationBudget(state, ledger, freeze_sha256="a" * 64, operation_root=output / "operations", config_root=config_root)
    budget.set_operation(operation_id="op-1", request_id="req-1", task_id="task-1", context_id="ctx-1")
    reservation = budget.reserve("deepseek-v4-flash", b'{"jsonrpc":"2.0"}')
    assert json.loads((state / "active-operation.json").read_text()) ["operation_id"] == "op-1"
    second = HermesOperationBudget(state, ledger, freeze_sha256="a" * 64, operation_root=output / "operations", config_root=config_root)
    second.set_operation(operation_id="op-2", request_id="req-2", task_id="task-2", context_id="ctx-2")
    with pytest.raises(HermesOperationBudgetError, match="busy"):
        second.reserve("deepseek-v4-flash", b"other")
    _archive(state, reservation, 1, body)
    assert budget.finish(reservation, "COMPLETED", {"input_tokens": 12, "output_tokens": 3}) == "completed"
    usage = budget.formal_usage()
    assert usage and usage["status"] == "known"
    assert usage["ledger_path"] == (str(ledger) if external_ledger else ledger.name)
    assert usage["entries"][0]["id"] == 1
    assert (output / "operations" / "op-1" / "model-request-1.json").read_bytes() == body
    assert not (state / "active-operation.json").exists()


def test_finished_snapshot_allows_next_operation_and_keeps_previous_usage(tmp_path: Path) -> None:
    config_root = tmp_path / "TEST-config"
    state = config_root / "bridge-state"
    ledger = config_root / "call-budget.sqlite3"
    output = config_root / "TEST-formal-run"
    body = b'{"request":1}'
    _ledger_rows(ledger, [(1, 12, 3), (2, 7, 2)])
    budget = HermesOperationBudget(state, ledger, freeze_sha256="a" * 64, operation_root=output / "operations", config_root=config_root)
    budget.set_operation(operation_id="op-1", request_id="req-1", task_id="task-1", context_id="ctx-1")
    first = budget.reserve("deepseek-v4-flash", body)
    _archive(state, first, 1, body)
    assert budget.finish(first, "COMPLETED", None) == "completed"
    first_usage = budget.formal_usage()
    assert first_usage and first_usage["input_tokens"] == 12
    budget.set_operation(operation_id="op-2", request_id="req-2", task_id="task-2", context_id="ctx-2")
    second = budget.reserve("deepseek-v4-flash", b'{"request":2}')
    assert second["operation_id"] == "op-2"
    assert budget.formal_usage() is None


def test_new_failed_operation_does_not_report_previous_usage(tmp_path: Path) -> None:
    config_root = tmp_path / "TEST-config"
    state = config_root / "bridge-state"
    ledger = config_root / "call-budget.sqlite3"
    body = b'{"request":1}'
    _ledger_rows(ledger, [(1, 12, 3)])
    budget = HermesOperationBudget(state, ledger, freeze_sha256="a" * 64, operation_root=config_root / "run" / "operations", config_root=config_root)
    budget.set_operation(operation_id="op-1", request_id="req-1", task_id="task-1", context_id="ctx-1")
    first = budget.reserve("deepseek-v4-flash", body)
    _archive(state, first, 1, body)
    budget.finish(first, "COMPLETED", None)
    assert budget.formal_usage() is not None
    budget.set_operation(operation_id="op-2", request_id="req-2", task_id="task-2", context_id="ctx-2")
    assert budget.formal_usage() is None


def test_operation_lease_is_explicitly_bounded(tmp_path: Path) -> None:
    with pytest.raises(HermesOperationBudgetError, match="operation_lease_seconds"):
        HermesOperationBudget(tmp_path / "state", tmp_path / "ledger", freeze_sha256="a" * 64,
                              operation_root=tmp_path / "run", config_root=tmp_path, operation_lease_seconds=121)


def test_formal_usage_aggregates_distinct_requests_and_deduplicates_archive(tmp_path: Path) -> None:
    config_root = tmp_path / "TEST-config"
    state = config_root / "bridge-state"
    ledger = config_root / "call-budget.sqlite3"
    output = config_root / "TEST-formal-run"
    body_one = b'{"request":1}'
    body_two = b'{"request":2}'
    _ledger_rows(ledger, [(1, 12, 3), (2, 7, 2)])
    budget = HermesOperationBudget(state, ledger, freeze_sha256="a" * 64, operation_root=output / "operations", config_root=config_root)
    budget.set_operation(operation_id="op-multi", request_id="req-1", task_id="task-1", context_id="ctx-1")
    reservation = budget.reserve("deepseek-v4-flash", body_one)
    _archive(state, reservation, 1, body_one, tag="one")
    _archive(state, reservation, 2, body_two, tag="two")
    _archive(state, reservation, 1, body_one, tag="one-duplicate")
    assert budget.finish(reservation, "COMPLETED", None) == "completed"
    usage = budget.formal_usage()
    assert usage and usage["status"] == "known"
    assert usage["input_tokens"] == 19 and usage["output_tokens"] == 5
    assert [entry["id"] for entry in usage["entries"]] == [1, 2]
    assert len({entry["request"]["sha256"] for entry in usage["entries"]}) == 2


def test_formal_usage_retains_entries_when_ledger_row_is_unknown(tmp_path: Path) -> None:
    config_root = tmp_path / "TEST-config"
    state = config_root / "bridge-state"
    ledger = config_root / "call-budget.sqlite3"
    body = b'{"request":9}'
    _ledger_rows(ledger, [])
    budget = HermesOperationBudget(state, ledger, freeze_sha256="a" * 64, operation_root=config_root / "run" / "operations", config_root=config_root)
    budget.set_operation(operation_id="op-unknown", request_id="req-9", task_id="task-9", context_id="ctx-9")
    reservation = budget.reserve("deepseek-v4-flash", body)
    _archive(state, reservation, 9, body)
    assert budget.finish(reservation, "COMPLETED", None) == "completed"
    usage = budget.formal_usage()
    assert usage and usage["status"] == "unknown" and usage["entries"][0]["id"] == 9


def test_formal_bridge_rejects_missing_or_wrong_freeze_marker(tmp_path: Path) -> None:
    state = tmp_path / "TEST-bridge"
    bridge = Bridge(state, 29991, formal_active_operation=True, formal_freeze_sha256="b" * 64)
    assert bridge._active_binding() is None
    marker = state / "active-operation.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "schema": "scope-recall.p18.hermes-active-operation.v1",
        "operation_id": "op", "request_id": "req", "task_id": "task", "context_id": "ctx",
        "formal_config_sha256": "c" * 64, "expires_ns": 10**20,
    }), encoding="utf-8")
    assert bridge._active_binding() is None
