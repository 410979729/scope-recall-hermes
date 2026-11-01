"""Incremental ledger totals agree with authoritative request details."""

from __future__ import annotations

import random
import sqlite3
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

import pytest
from scope_recall.runtime import model_budget as budget
from test_monthly_auxiliary_budget import instant, policy


def open_ledger(path, *, zone="UTC"):
    configured = policy(
        cap_micro_usd=None,
        time_zone=zone,
        approved_models=["model", "other"],
        pricing={
            name: {"input_usd_per_million": "0.7", "output_usd_per_million": "1.3"} for name in ("model", "other")
        },
    )
    budget.initialize_auxiliary_budget_ledger(path, configured)
    return budget.AuxiliaryBudgetLedger(path, configured)


def reserve(ledger, when, model="model"):
    with patch.object(budget.time, "time_ns", return_value=when):
        return ledger.reserve(model, b"TEST", reserved_input=20, reserved_output=10)


def assert_totals(path, windows):
    with closing(sqlite3.connect(path)) as db:
        for window in windows:
            for model in (None, "model", "other", "absent"):
                assert budget.budget_totals(db, window, model=model) == budget.recompute_budget_totals(
                    db, window, model=model
                )


def test_accounting_and_inspection_do_not_scan_details_after_startup(tmp_path):
    ledger = open_ledger(tmp_path / "ledger.sqlite3")
    reserve(ledger, instant("2026-02-10T12:00:00"))
    original = sqlite3.connect
    statements = []

    def configure(db):
        db.set_trace_callback(statements.append)
        db.set_authorizer(
            lambda action, one, two, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_FUNCTION and two in {"sum", "count"}
                else sqlite3.SQLITE_OK
            )
        )
        return db

    def connect(*args, **kwargs):
        return configure(original(*args, **kwargs))

    configure(ledger._connect_rw())
    with patch.object(budget.sqlite3, "connect", side_effect=connect):
        request = reserve(ledger, instant("2026-02-11T12:00:00"))
        ledger.finish(request, "http_200", {"prompt_tokens": 3, "completion_tokens": 2})
        assert budget.read_auxiliary_budget_status(ledger.path)["requests"] == 2
        from scope_recall.maintenance.doctor import _ledger_headroom

        assert _ledger_headroom(ledger.path, ledger.policy)["lifetime_usage"]["requests"] == 2
    full_detail_reads = [
        statement
        for statement in statements
        if "FROM REQUESTS" in statement.upper() and "WHERE" not in statement.upper()
    ]
    assert full_detail_reads == []
    with closing(original(ledger.path)) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM requests WHERE status='meter_breach'").fetchall()
        assert any("INDEX" in row[3] for row in plan), plan


@pytest.mark.parametrize("seed", [19, 71, 203])
def test_random_interleaved_windows_models_and_settlements(tmp_path, seed):
    ledger = open_ledger(tmp_path / "ledger.sqlite3")
    rng = random.Random(seed)
    times = [instant(value) for value in ("2024-02-29T23:59:59", "2024-03-01T00:00:00", "2024-04-01T00:00:00")]
    windows = [None, *(budget.budget_window(ledger.policy, when) for when in times)]
    pending = [reserve(ledger, rng.choice(times), rng.choice(("model", "other"))) for _ in range(90)]
    rng.shuffle(pending)
    for request in pending:
        if rng.randrange(4):
            ledger.finish(
                request,
                "http_200",
                {
                    "prompt_tokens": rng.randrange(21),
                    "completion_tokens": rng.randrange(11),
                    "cached_prompt_tokens": 0,
                    "unreported_output_tokens": rng.randrange(5),
                },
            )
        else:
            ledger.finish_embedding(request, "network_error", None)
        assert_totals(ledger.path, windows)
        if rng.randrange(3) == 0:
            reserve(ledger, rng.choice(times), rng.choice(("model", "other")))
    request = reserve(ledger, times[0])
    assert ledger.finish(request, "http_200", {"prompt_tokens": 21, "completion_tokens": 1}) == "meter_breach"
    assert_totals(ledger.path, windows)
    with pytest.raises(ValueError, match="budget_exhausted_or_meter_breach"):
        reserve(ledger, times[-1])


def test_legacy_upgrade_and_old_writer_are_reconciled(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(budget.REQUESTS_TABLE)
        db.execute(
            "INSERT INTO requests(model,reserved_input,reserved_output,charge_micro_usd,status,started_ns) "
            "VALUES ('model',20,10,27,'reserved_before_network',?)",
            (instant("2026-01-31T23:59:59"),),
        )
    ledger = budget.AuxiliaryBudgetLedger(path, open_ledger(tmp_path / "policy.sqlite3").policy)
    request = reserve(ledger, instant("2026-02-01T00:00:00"))
    windows = [None, budget.budget_window(ledger.policy, instant("2026-01-31T23:59:59"))]
    assert_totals(path, windows)
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] > 0
        db.execute(
            "INSERT INTO requests(model,reserved_input,actual_input,reserved_output,actual_output,charge_micro_usd,"
            "status,started_ns) VALUES ('other',20,2,10,1,3,'http_200',?)",
            (instant("2026-02-02T00:00:00"),),
        )
        db.execute("UPDATE requests SET actual_input=1,actual_output=0,charge_micro_usd=1,status='http_200' WHERE id=1")
    reopened = budget.AuxiliaryBudgetLedger(path, ledger.policy)
    reserve(reopened, instant("2026-02-03T00:00:00"))
    reopened.finish(request, "http_200", {"prompt_tokens": 0, "completion_tokens": 0})
    assert_totals(path, windows)


def test_time_zone_change_and_late_settlement_update_registered_windows(tmp_path):
    ledger = open_ledger(tmp_path / "ledger.sqlite3")
    request = reserve(ledger, instant("2024-03-01T02:00:00"))
    eastern = budget.AuxiliaryBudgetLedger(ledger.path, replace(ledger.policy, time_zone="America/New_York"))
    reserve(eastern, instant("2024-04-01T02:00:00"), "other")
    ledger.finish_embedding(request, "http_200", {"promptTokenCount": 4})
    windows = [None]
    for zone_policy in (ledger.policy, eastern.policy):
        windows.extend(
            budget.budget_window(zone_policy, instant(when))
            for when in ("2024-02-10T00:00:00", "2024-03-10T00:00:00", "2024-04-10T00:00:00")
        )
    assert_totals(ledger.path, windows)


def test_totals_and_details_roll_back_together(tmp_path):
    ledger = open_ledger(tmp_path / "ledger.sqlite3")
    request = reserve(ledger, instant("2026-02-01T00:00:00"))
    with closing(sqlite3.connect(ledger.path)) as db, db:
        db.execute(
            "CREATE TRIGGER reject_settlement BEFORE UPDATE ON requests "
            "WHEN NEW.actual_input IS NOT NULL BEGIN SELECT RAISE(ABORT,'TEST rollback'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="TEST rollback"):
        ledger.finish(request, "http_200", {"prompt_tokens": 0, "completion_tokens": 0})
    assert_totals(ledger.path, [None])
    with closing(sqlite3.connect(ledger.path)) as db:
        assert budget.budget_totals(db)["input_tokens"] == 20


def test_read_only_legacy_inspection_keeps_the_file_unchanged(tmp_path):
    path = tmp_path / "legacy-read.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(budget.REQUESTS_TABLE)
        db.execute(
            "INSERT INTO requests(model,reserved_input,actual_input,reserved_output,"
            "charge_micro_usd,status,started_ns) "
            "VALUES ('model',20,3,10,9,'http_200',?)",
            (instant("2026-02-01T00:00:00"),),
        )
    original = path.read_bytes()
    status = budget.read_auxiliary_budget_status(path)
    assert (status["requests"], status["charge_micro_usd"]) == (1, 9)
    assert path.read_bytes() == original
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='request_totals'").fetchone() is None


def test_summary_failure_rolls_back_the_reservation_and_settlement(tmp_path):
    ledger = open_ledger(tmp_path / "ledger.sqlite3")
    request = reserve(ledger, instant("2026-02-01T00:00:00"))
    with closing(sqlite3.connect(ledger.path)) as db, db:
        db.execute(
            "CREATE TRIGGER reject_totals BEFORE UPDATE ON request_totals "
            "BEGIN SELECT RAISE(ABORT,'TEST summary rollback'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="TEST summary rollback"):
        reserve(ledger, instant("2026-02-01T00:00:00"))
    with pytest.raises(sqlite3.IntegrityError, match="TEST summary rollback"):
        ledger.finish(request, "http_200", {"prompt_tokens": 0, "completion_tokens": 0})
    assert_totals(ledger.path, [None])
    with closing(sqlite3.connect(ledger.path)) as db:
        assert db.execute("SELECT COUNT(*),MIN(status) FROM requests").fetchone() == (1, "reserved_before_network")


def test_clean_but_stale_summary_is_rechecked_by_a_fresh_process(tmp_path, monkeypatch):
    ledger = open_ledger(tmp_path / "ledger.sqlite3")
    reserve(ledger, instant("2026-02-01T00:00:00"))
    with closing(sqlite3.connect(ledger.path)) as db, db:
        db.execute("UPDATE request_totals SET input_tokens=0")
    monkeypatch.setattr(budget, "_VERIFIED_LEDGERS", set())
    reopened = budget.AuxiliaryBudgetLedger(ledger.path, ledger.policy)
    reserve(reopened, instant("2026-02-02T00:00:00"))
    assert_totals(ledger.path, [None, budget.budget_window(ledger.policy, instant("2026-02-01T00:00:00"))])


def test_local_writer_queue_respects_the_caller_deadline(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    ledger = open_ledger(tmp_path / "ledger.sqlite3")
    with ledger._write_lock:
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(
                ledger.reserve, "model", b"TEST", reserved_input=1, reserved_output=1, timeout_seconds=0.02
            )
            with pytest.raises(ValueError, match="ledger_busy_timeout"):
                waiting.result(timeout=2)
    with closing(sqlite3.connect(ledger.path)) as db:
        assert budget.budget_totals(db)["requests"] == 0


def test_one_ledger_reuses_and_releases_its_connection(tmp_path, monkeypatch):
    import gc

    ledger = open_ledger(tmp_path / "ledger.sqlite3")
    original = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(budget.sqlite3, "connect", connect)
    request = reserve(ledger, instant("2026-02-01T00:00:00"))
    ledger.finish(request, "http_200", {"prompt_tokens": 1, "completion_tokens": 1})
    assert len(connections) == 1
    ledger.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    reserve(ledger, instant("2026-02-01T00:00:00"))
    assert len(connections) == 2
    del ledger
    gc.collect()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[1].execute("SELECT 1")
