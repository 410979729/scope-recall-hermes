"""Durable CLI subscription accounting in the auxiliary ledger (never USD).

Reservations commit before spawn, including attempts that crash or time out.
Unknown usage retains the reservation. A metering breach fences future calls
until operator review; UTC daily caps reset naturally, not by deleting rows.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

from .validation import mapping, only_keys, positive_int


_TABLE = """CREATE TABLE IF NOT EXISTS codex_subscription_requests (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, model TEXT NOT NULL,
    reserved_input INTEGER NOT NULL, reserved_output INTEGER NOT NULL,
    actual_input INTEGER, actual_output INTEGER, status TEXT NOT NULL,
    meter_breach INTEGER NOT NULL DEFAULT 0, started_ns INTEGER NOT NULL)"""


@dataclass(frozen=True)
class SubscriptionBudgetPolicy:
    """Hard admission limits; token reservations are not provider generation caps."""

    daily_calls: int = 4
    daily_input_tokens: int = 131072
    daily_output_tokens: int = 32768
    reserve_input: int = 32768
    reserve_output: int = 8192
    max_request_bytes: int = 24576

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            positive_int(f"codex_{name}", getattr(self, name))
        if self.reserve_input > self.daily_input_tokens or self.reserve_output > self.daily_output_tokens:
            raise ValueError("codex_reservation_exceeds_daily_cap")

    @classmethod
    def from_mapping(cls, raw):
        if raw is None:
            return cls()
        return cls(**only_keys("codex_subscription_budget", mapping("codex_subscription_budget", raw),
                               cls.__dataclass_fields__))


def _day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class SubscriptionBudgetLedger:
    """Separate call/token table, sharing the configured auxiliary SQLite file."""

    def __init__(self, path: Path, policy: SubscriptionBudgetPolicy):
        if not Path(path).is_absolute():
            raise ValueError("ledger_path_must_be_absolute")
        self.path = Path(path)
        self.policy = policy

    def _connect(self, seconds: float):
        if not self.path.is_file():
            raise ValueError("ledger_not_initialized")
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True,
                               timeout=max(.001, min(seconds, 1.0)))

    def reserve(self, model: str, *, timeout_seconds: float) -> int:
        if timeout_seconds <= 0:
            raise ValueError("ledger_busy_timeout")
        p = self.policy
        with closing(self._connect(timeout_seconds)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            # Schema belongs to this accounting boundary, never the truth DB.
            db.execute(_TABLE)
            if db.execute("SELECT 1 FROM codex_subscription_requests WHERE meter_breach=1 LIMIT 1").fetchone():
                raise ValueError("budget_exhausted_or_meter_breach")
            calls, inputs, outputs = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual_input,reserved_input)),0), "
                "COALESCE(SUM(COALESCE(actual_output,reserved_output)),0) "
                "FROM codex_subscription_requests WHERE day=?", (_day(),)
            ).fetchone()
            if (calls >= p.daily_calls or inputs + p.reserve_input > p.daily_input_tokens
                    or outputs + p.reserve_output > p.daily_output_tokens):
                raise ValueError("budget_exhausted_or_meter_breach")
            cursor = db.execute(
                "INSERT INTO codex_subscription_requests(day,model,reserved_input,reserved_output,status,started_ns) "
                "VALUES(?,?,?,?,?,?)", (_day(), model, p.reserve_input, p.reserve_output,
                                       "reserved_before_spawn", time.time_ns()))
            return int(cursor.lastrowid)

    def finish(self, request_id: int, status: str, usage: tuple[int, int] | None,
               *, timeout_seconds: float) -> bool:
        """Settle once, retaining unknown usage; return whether usage breached."""
        with closing(self._connect(timeout_seconds)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved_input,reserved_output,status FROM codex_subscription_requests WHERE id=?",
                             (request_id,)).fetchone()
            if row is None or row[2] != "reserved_before_spawn":
                raise ValueError("reservation_state")
            if usage is None:
                db.execute("UPDATE codex_subscription_requests SET status=? WHERE id=?",
                           (status + "_usage_unknown_reserved", request_id))
                return False
            if len(usage) != 2 or any(type(v) is not int or v < 0 for v in usage):
                raise ValueError("invalid_meter")
            breach = usage[0] > row[0] or usage[1] > row[1]
            db.execute("UPDATE codex_subscription_requests SET actual_input=?,actual_output=?,status=?,meter_breach=? WHERE id=?",
                       (*usage, status, int(breach), request_id))
            return breach

    def status(self) -> dict:
        """Metadata-only read; no ledger creation, no inferred dollar price."""
        result = dict(accounting="subscription_unpriced", charge_micro_usd=None,
                      day=_day(), daily_calls=0, input_tokens=0, output_tokens=0,
                      meter_breach=False, latest_status=None, ledger_exists=self.path.is_file())
        if not self.path.is_file():
            return result
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='codex_subscription_requests'").fetchone():
                return result
            calls, inputs, outputs = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual_input,reserved_input)),0), "
                "COALESCE(SUM(COALESCE(actual_output,reserved_output)),0) FROM codex_subscription_requests WHERE day=?",
                (_day(),)).fetchone()
            latest = db.execute("SELECT status FROM codex_subscription_requests ORDER BY id DESC LIMIT 1").fetchone()
            breach = db.execute("SELECT 1 FROM codex_subscription_requests WHERE meter_breach=1 LIMIT 1").fetchone()
        result.update(daily_calls=calls, input_tokens=inputs, output_tokens=outputs,
                      meter_breach=bool(breach), latest_status=latest[0] if latest else None)
        return result
