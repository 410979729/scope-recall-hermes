"""Calendar budget boundaries on a real SQLite ledger, without provider calls."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timezone
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from scope_recall.maintenance.doctor import _ledger_headroom
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig, build_auxiliary_runtime
from scope_recall.runtime.model_budget import (
    AuxiliaryBudgetLedger,
    budget_window,
    default_budget_policy,
    initialize_auxiliary_budget_ledger,
    read_auxiliary_budget_status,
)
from scope_recall.runtime.models import AuxiliaryModelError


def instant(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()) * 1_000_000_000


def policy(**overrides):
    budget = {
        "period": "monthly",
        "time_zone": "UTC",
        "cap_micro_usd": 2,
        "total_input_cap": None,
        "total_output_cap": None,
        "total_call_cap": None,
        "pricing": {"model": {"input_usd_per_million": "1", "output_usd_per_million": "1"}},
        "approved_models": ["model"],
    }
    budget.update(overrides)
    return AuxiliaryRuntimeConfig.from_mapping(
        {
            "external_embedding": False,
            "external_consolidation": False,
            "budget": budget,
        }
    ).budget


class MonthlyBudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="monthly-budget-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ledger.sqlite3"

    def ledger(self, budget=None):
        budget = policy() if budget is None else budget
        initialize_auxiliary_budget_ledger(self.path, budget)
        return AuxiliaryBudgetLedger(self.path, budget)

    def reserve(self, ledger, when):
        with patch("scope_recall.runtime.model_budget.time.time_ns", return_value=instant(when)):
            return ledger.reserve("model", b"x", reserved_input=1, reserved_output=1)

    def test_new_month_has_fresh_headroom(self):
        ledger = self.ledger()
        self.reserve(ledger, "2026-01-31T23:59:59")
        self.assertGreater(self.reserve(ledger, "2026-02-01T00:00:00"), 0)

    def test_call_cap_uses_current_month(self):
        ledger = self.ledger(policy(cap_micro_usd=None, total_call_cap=1))
        self.reserve(ledger, "2026-01-31T23:59:59")
        self.assertGreater(self.reserve(ledger, "2026-02-01T00:00:00"), 0)
        with self.assertRaisesRegex(ValueError, "budget_exhausted_or_meter_breach"):
            self.reserve(ledger, "2026-02-01T00:00:01")

    def test_model_token_cap_uses_current_month(self):
        ledger = self.ledger(policy(cap_micro_usd=None, model_token_caps={"model": {"input": 1, "output": 1}}))
        self.reserve(ledger, "2026-01-31T23:59:59")
        self.assertGreater(self.reserve(ledger, "2026-02-01T00:00:00"), 0)
        with self.assertRaisesRegex(ValueError, "budget_exhausted_or_meter_breach"):
            self.reserve(ledger, "2026-02-01T00:00:01")

    def test_status_can_explicitly_read_month_without_losing_lifetime(self):
        ledger = self.ledger()
        self.reserve(ledger, "2026-01-31T23:59:59")
        self.reserve(ledger, "2026-02-01T00:00:00")
        status = read_auxiliary_budget_status(self.path, policy=ledger.policy, now_ns=instant("2026-02-01T00:00:00"))
        self.assertEqual(status["requests"], 1)
        self.assertEqual(status["charge_micro_usd"], 2)
        self.assertEqual(status["period"], "monthly")
        self.assertEqual(status["window_start_ns"], instant("2026-02-01T00:00:00"))
        self.assertEqual(read_auxiliary_budget_status(self.path)["requests"], 2)

    def test_doctor_reports_monthly_pressure_and_lifetime_usage(self):
        ledger = self.ledger(policy(total_call_cap=1))
        self.reserve(ledger, "2026-01-31T23:59:59")
        with patch("scope_recall.runtime.model_budget.time.time_ns", return_value=instant("2026-02-01T00:00:00")):
            result = _ledger_headroom(self.path, ledger.policy)
        self.assertEqual(result["scope"], "monthly")
        self.assertEqual(result["calls"]["used"], 0)
        self.assertEqual(result["charge_micro_usd"], {"used": 0, "cap": 2, "used_ratio": 0.0})
        self.assertEqual(result["lifetime_usage"]["requests"], 1)
        self.assertEqual(result["window_start_ns"], instant("2026-02-01T00:00:00"))

    def test_late_settlement_keeps_original_month_and_historical_rows(self):
        ledger = self.ledger()
        old_id = self.reserve(ledger, "2026-01-31T23:59:59")
        with closing(sqlite3.connect(self.path)) as db:
            original = db.execute("SELECT * FROM requests").fetchall()
        rebuilt = AuxiliaryBudgetLedger(self.path, policy())
        self.reserve(rebuilt, "2026-02-01T00:00:00")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT * FROM requests WHERE id=?", (old_id,)).fetchall(), original)
        self.assertEqual(rebuilt.finish(old_id, "http_200", {"prompt_tokens": 0, "completion_tokens": 0}), "http_200")
        status = read_auxiliary_budget_status(self.path, policy=rebuilt.policy, now_ns=instant("2026-02-02T00:00:00"))
        self.assertEqual(status["charge_micro_usd"], 2)
        with self.assertRaisesRegex(ValueError, "budget_exhausted_or_meter_breach"):
            self.reserve(AuxiliaryBudgetLedger(self.path, policy()), "2026-02-02T00:00:00")

    def test_concurrent_reservations_cannot_overrun_month(self):
        ledger = self.ledger()
        gate = Barrier(2)

        def attempt():
            contender = AuxiliaryBudgetLedger(self.path, ledger.policy)
            gate.wait(timeout=5)
            try:
                return contender.reserve("model", b"x", reserved_input=1, reserved_output=1)
            except ValueError as exc:
                return str(exc)

        with patch("scope_recall.runtime.model_budget.time.time_ns", return_value=instant("2026-02-01T00:00:00")):
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lambda _: attempt(), range(2)))
        self.assertEqual(sum(type(result) is int for result in results), 1)
        self.assertEqual(results.count("budget_exhausted_or_meter_breach"), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*),SUM(charge_micro_usd) FROM requests").fetchone(), (1, 2))

    def test_historical_meter_breach_remains_blocking_next_month(self):
        ledger = self.ledger()
        request = self.reserve(ledger, "2026-01-31T23:59:59")
        self.assertEqual(
            ledger.finish(request, "http_200", {"prompt_tokens": 2, "completion_tokens": 1}), "meter_breach"
        )
        with self.assertRaisesRegex(ValueError, "budget_exhausted_or_meter_breach"):
            self.reserve(ledger, "2026-02-01T00:00:00")

    def test_global_token_caps_are_monthly(self):
        for cap in ("total_input_cap", "total_output_cap"):
            with self.subTest(cap=cap):
                path = Path(self.directory.name) / (cap + ".sqlite3")
                budget = policy(cap_micro_usd=None, **{cap: 1})
                initialize_auxiliary_budget_ledger(path, budget)
                ledger = AuxiliaryBudgetLedger(path, budget)
                self.reserve(ledger, "2026-01-31T23:59:59")
                self.reserve(ledger, "2026-02-01T00:00:00")
                with self.assertRaisesRegex(ValueError, "budget_exhausted_or_meter_breach"):
                    self.reserve(ledger, "2026-02-01T00:00:01")

    def test_lifetime_default_and_zero_null_caps_remain_compatible(self):
        default = default_budget_policy()
        self.assertEqual((default.period, default.time_zone, default.cap_micro_usd), ("lifetime", "UTC", 0))
        budget = policy(period="lifetime")
        ledger = self.ledger(budget)
        self.reserve(ledger, "2026-01-31T23:59:59")
        with self.assertRaisesRegex(ValueError, "budget_exhausted_or_meter_breach"):
            self.reserve(ledger, "2026-02-01T00:00:00")
        for value in (0, None):
            with self.subTest(cap=value):
                budget = policy(cap_micro_usd=value)
                ledger = AuxiliaryBudgetLedger(self.path, budget)
                if value == 0:
                    with self.assertRaisesRegex(ValueError, "budget_exhausted_or_meter_breach"):
                        self.reserve(ledger, "2026-03-01T00:00:00")
                else:
                    self.reserve(ledger, "2026-03-01T00:00:00")
                    self.reserve(ledger, "2026-03-01T00:00:00")

    def test_invalid_period_zone_and_boolean_caps_fail_closed(self):
        for field, values in {
            "period": ("weekly", None, True),
            "time_zone": ("Not/AZone", "", None, True),
            "cap_micro_usd": (True,),
            "total_call_cap": (False,),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    policy(**{field: value})

    def test_lock_wait_samples_month_once_after_acquiring_lock(self):
        ledger = self.ledger()
        self.reserve(ledger, "2026-01-31T23:59:59")
        waiting = Event()

        class WaitingLedger(AuxiliaryBudgetLedger):
            def _connect_rw(self, *, deadline=None):
                db = super()._connect_rw(deadline=deadline)
                db.set_trace_callback(lambda statement: waiting.set() if statement == "BEGIN IMMEDIATE" else None)
                return db

        with closing(sqlite3.connect(self.path)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            with patch(
                "scope_recall.runtime.model_budget.time.time_ns", return_value=instant("2026-02-01T00:00:00")
            ) as clock:
                with ThreadPoolExecutor(max_workers=1) as workers:
                    future = workers.submit(
                        WaitingLedger(self.path, ledger.policy).reserve,
                        "model",
                        b"x",
                        reserved_input=1,
                        reserved_output=1,
                    )
                    try:
                        self.assertTrue(waiting.wait(timeout=5))
                        self.assertEqual(clock.call_count, 0)
                    finally:
                        holder.commit()
                    self.assertGreater(future.result(timeout=5), 0)
                self.assertEqual(clock.call_count, 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT started_ns FROM requests ORDER BY id DESC LIMIT 1").fetchone()[0],
                instant("2026-02-01T00:00:00"),
            )

    def test_a_host_without_a_time_zone_database_still_loads_its_budget(self):
        """Every policy names a zone, the lifetime default too.  A slim Linux image or a Windows Python without
        ``tzdata`` has no database to look one up in, and must still load the default, and a monthly budget in UTC;
        a named zone it cannot look up is refused."""
        from zoneinfo import ZoneInfoNotFoundError

        def no_database(name):
            raise ZoneInfoNotFoundError(name)

        with patch("scope_recall.runtime.model_budget.ZoneInfo", no_database):
            self.assertEqual(default_budget_policy().time_zone, "UTC")
            AuxiliaryRuntimeConfig.from_mapping({"external_embedding": False, "external_consolidation": False})
            self.assertEqual(policy(period="lifetime", time_zone="Asia/Shanghai").period, "lifetime")
            self.assertEqual(
                budget_window(policy(), instant("2026-02-10T12:00:00")),
                (instant("2026-02-01T00:00:00"), instant("2026-03-01T00:00:00")),
            )
            with self.assertRaises(ValueError):
                policy(time_zone="Asia/Shanghai")

    def test_month_length_follows_iana_zone_including_dst(self):
        budget = policy(time_zone="America/New_York")
        self.assertEqual(
            budget_window(budget, instant("2024-03-15T12:00:00")),
            (instant("2024-03-01T05:00:00"), instant("2024-04-01T04:00:00")),
        )
        self.assertEqual(
            budget_window(policy(), instant("2024-02-15T00:00:00")),
            (instant("2024-02-01T00:00:00"), instant("2024-03-01T00:00:00")),
        )

    def test_runtime_status_reports_the_configured_month_not_the_lifetime(self):
        from scope_recall.runtime.auxiliary import auxiliary_runtime_status

        ledger = self.ledger()
        self.reserve(ledger, "2026-01-31T23:59:59")
        self.reserve(ledger, "2026-02-01T00:00:00")
        config = replace(
            AuxiliaryRuntimeConfig.from_mapping(
                {"external_embedding": False, "external_consolidation": False, "ledger_path": str(self.path)}
            ),
            budget=ledger.policy,
        )
        with patch("scope_recall.runtime.model_budget.time.time_ns", return_value=instant("2026-02-02T00:00:00")):
            status = auxiliary_runtime_status(config)["budget"]
        self.assertEqual(status.get("period"), "monthly")
        self.assertEqual(status["requests"], 1)
        self.assertEqual(status["charge_micro_usd"], 2)

    def test_worker_config_loader_preserves_monthly_policy(self):
        from scope_recall.runtime.worker_entry import load_config

        root = Path(self.directory.name)
        raw = {
            "binding": {
                "agent_id": "TEST-month",
                "installation_id": "TEST-month-install",
                "data_directory": str(root),
                "scope_ids": ["TEST-month-scope"],
                "test_mode": True,
            },
            "session_id": "TEST-month-session",
            "allowed_scope_ids": ["TEST-month-scope"],
            "auxiliary": {
                "external_embedding": False,
                "external_consolidation": False,
                "budget": {
                    "period": "monthly",
                    "time_zone": "Asia/Shanghai",
                    "cap_micro_usd": 30000000,
                    "approved_models": [],
                    "pricing": {},
                },
            },
        }
        path = root / "worker.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        loaded = load_config(path)
        self.assertEqual(
            (loaded.auxiliary.budget.period, loaded.auxiliary.budget.time_zone, loaded.auxiliary.budget.cap_micro_usd),
            ("monthly", "Asia/Shanghai", 30000000),
        )

    def test_factory_adapter_enforces_monthly_policy_before_transport(self):
        budget = {
            "period": "monthly",
            "time_zone": "Asia/Shanghai",
            "cap_micro_usd": None,
            "total_input_cap": None,
            "total_output_cap": None,
            "total_call_cap": 1,
            "pricing": {"model": {"input_usd_per_million": "1", "output_usd_per_million": "1"}},
            "approved_models": ["model"],
        }
        config = AuxiliaryRuntimeConfig.from_mapping(
            {
                "external_embedding": False,
                "external_consolidation": True,
                "ledger_path": str(self.path),
                "budget": budget,
                "consolidation_reserve_input": 1,
                "consolidation": {
                    "model": "model",
                    "endpoint": "https://example.test/chat",
                    "credential_env": "SCOPE_MONTHLY_TEST_KEY",
                    "output_limit_field": "max_tokens",
                    "max_output_tokens": 1,
                },
            }
        )
        initialize_auxiliary_budget_ledger(self.path, config.budget)

        class Transport:
            calls = 0

            def post(self, url, **kwargs):
                self.calls += 1
                return 200, json.dumps(
                    {
                        "choices": [{"message": {"role": "assistant", "content": "{}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                ).encode()

        transport = Transport()
        with patch.dict("os.environ", {"SCOPE_MONTHLY_TEST_KEY": "test-key"}):
            runtime = build_auxiliary_runtime(config, transport=transport)
            self.addCleanup(runtime.close)
            with patch("scope_recall.runtime.model_budget.time.time_ns", return_value=instant("2026-01-31T15:59:59")):
                runtime.consolidation.propose([{"role": "user", "content": "hello"}], remaining_seconds=3)
                with self.assertRaises(AuxiliaryModelError) as denied:
                    runtime.consolidation.propose([{"role": "user", "content": "hello"}], remaining_seconds=3)
                self.assertEqual(denied.exception.error_type, "budget_exhausted")
            with patch("scope_recall.runtime.model_budget.time.time_ns", return_value=instant("2026-01-31T16:00:00")):
                runtime.consolidation.propose([{"role": "user", "content": "hello"}], remaining_seconds=3)
        self.assertEqual(transport.calls, 2)

    def test_shanghai_year_boundary_is_exact_to_nanosecond(self):
        ledger = self.ledger(policy(time_zone="Asia/Shanghai"))
        boundary = instant("2026-12-31T16:00:00")
        with patch("scope_recall.runtime.model_budget.time.time_ns", return_value=boundary - 1):
            ledger.reserve("model", b"x", reserved_input=1, reserved_output=1)
        with patch("scope_recall.runtime.model_budget.time.time_ns", return_value=boundary):
            self.assertGreater(ledger.reserve("model", b"x", reserved_input=1, reserved_output=1), 0)
        with self.assertRaisesRegex(ValueError, "budget_exhausted_or_meter_breach"):
            self.reserve(ledger, "2026-12-31T16:00:01")
