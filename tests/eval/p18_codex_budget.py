"""TEST-only durable budget adapter for P18 Codex submissions.

The adapter deliberately owns only ``codex_submissions`` in the shared ledger.
It never writes the existing auxiliary ``requests`` or any Go/adjustment table,
and it does not invent a monetary price for Codex account usage.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping


CODEX_BATCH = "P18_EVALUATION"
NATIVE_AUX_BATCH = "P18_NATIVE_A_AUX"
ATTEMPT_AUTHORIZATION_SHA256 = "c6912c4d6ae367406bdea27fb944cc9f055d147b2b2aee77515310d971155ba9"
PRIOR_ATTEMPT_AUTHORIZATION_SHA256 = "5f31c11d59e0194f8904d7be8bafdd3b3553c87c94d2964907c465b4a7f40f6f"
CODEX_MODEL_CALL_CAP = 1_500
CODEX_INPUT_CAP = 40_000_000
CODEX_OUTPUT_CAP = 2_000_000
CODEX_RESERVED_INPUT = 32_768
CODEX_RESERVED_OUTPUT = 4_096
MONETARY_UNAVAILABLE = "unavailable"
UNKNOWN_USAGE_POLICY = "reservation_retained"
HISTORICAL_NOT_PRE_DISPATCH = "historical-not-pre-dispatch"


class CodexBudgetError(ValueError):
    """Fail-closed budget or reservation error."""


@dataclass(frozen=True)
class CodexBudgetPolicy:
    batch: str = CODEX_BATCH
    call_cap: int = CODEX_MODEL_CALL_CAP
    input_cap: int = CODEX_INPUT_CAP
    output_cap: int = CODEX_OUTPUT_CAP
    reserved_input: int = CODEX_RESERVED_INPUT
    reserved_output: int = CODEX_RESERVED_OUTPUT

    def __post_init__(self) -> None:
        if not isinstance(self.batch, str) or not self.batch:
            raise CodexBudgetError("batch")
        for name in ("call_cap", "input_cap", "output_cap", "reserved_input", "reserved_output"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise CodexBudgetError(name)
        if self.call_cap < 1 or self.reserved_input < 1 or self.reserved_output < 1:
            raise CodexBudgetError("budget_policy_positive_cap")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS codex_submissions (
    operation_id TEXT PRIMARY KEY,
    batch TEXT NOT NULL,
    model TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    request_bytes INTEGER NOT NULL CHECK (request_bytes > 0),
    reserved_input INTEGER,
    reserved_output INTEGER,
    actual_input INTEGER,
    actual_output INTEGER,
    status TEXT NOT NULL,
    dispatch_status TEXT NOT NULL,
    usage_quality TEXT NOT NULL,
    reservation_origin TEXT NOT NULL,
    audit_source TEXT NOT NULL,
    historical_not_pre_dispatch INTEGER NOT NULL CHECK (historical_not_pre_dispatch IN (0, 1)),
    monetary_status TEXT NOT NULL CHECK (monetary_status = 'unavailable'),
    started_ns INTEGER NOT NULL,
    finished_ns INTEGER
)
"""


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _strict_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodexBudgetError(name)
    return value


def _strict_nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise CodexBudgetError(name)
    return value


def _canonical_usage(usage: Mapping[str, Any]) -> tuple[int, int]:
    aliases = {
        "prompt_tokens": ("prompt_tokens", "promptTokens", "inputTokens", "input_tokens"),
        "completion_tokens": ("completion_tokens", "completionTokens", "outputTokens", "output_tokens"),
    }
    values: dict[str, int] = {}
    for canonical, names in aliases.items():
        present = [usage[name] for name in names if name in usage]
        if len(present) != 1:
            raise CodexBudgetError("usage_missing_or_ambiguous")
        values[canonical] = _strict_nonnegative_int(canonical, present[0])
    total = usage.get("total_tokens", usage.get("totalTokens"))
    if total is not None and _strict_nonnegative_int("total_tokens", total) != values["prompt_tokens"] + values["completion_tokens"]:
        raise CodexBudgetError("usage_total_mismatch")
    return values["prompt_tokens"], values["completion_tokens"]


class CodexSubmissionBudget:
    """Persistent P18 Codex submission budget over an explicitly initialized DB."""

    def __init__(self, path: Path, *, policy: CodexBudgetPolicy | None = None) -> None:
        target = Path(path).expanduser().resolve()
        if not target.is_absolute():
            raise CodexBudgetError("ledger_path_must_be_absolute")
        self.path = target
        auth_path = os.environ.get("SCOPE_RECALL_CODEX_ATTEMPT_AUTHORIZATION")
        auth_hash = os.environ.get("SCOPE_RECALL_CODEX_ATTEMPT_AUTHORIZATION_SHA256")
        self.attempt_authorization = None
        if bool(auth_path) != bool(auth_hash):
            raise CodexBudgetError("attempt_authorization_pair_required")
        if auth_path:
            raw = Path(auth_path).read_bytes()
            if auth_hash not in {ATTEMPT_AUTHORIZATION_SHA256, PRIOR_ATTEMPT_AUTHORIZATION_SHA256} or _sha256(raw) != auth_hash:
                raise CodexBudgetError("attempt_authorization_hash_mismatch")
            self.attempt_authorization = json.loads(raw)
        if policy is None and self.attempt_authorization is not None:
            policy = CodexBudgetPolicy(**{key: self.attempt_authorization[key] for key in
                ("batch", "call_cap", "input_cap", "output_cap", "reserved_input", "reserved_output")})
        self.policy = policy or CodexBudgetPolicy()

    def initialize_schema(self) -> None:
        """Create only the adapter table; never create or alter other ledger tables."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.path), timeout=5) as db:
            db.execute(SCHEMA_SQL)
            db.commit()

    def _connect_rw(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise CodexBudgetError("ledger_not_initialized")
        db = sqlite3.connect(str(self.path), timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _ensure_table(self, db: sqlite3.Connection) -> None:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_submissions'"
        ).fetchone()
        if exists is None:
            raise CodexBudgetError("codex_submissions_schema_not_initialized")

    @staticmethod
    def _row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        if isinstance(row, sqlite3.Row):
            data = dict(row)
        else:
            keys = (
                "operation_id", "batch", "model", "request_sha256", "request_bytes", "reserved_input",
                "reserved_output", "actual_input", "actual_output", "status", "dispatch_status",
                "usage_quality", "reservation_origin", "audit_source", "historical_not_pre_dispatch",
                "monetary_status", "started_ns", "finished_ns",
            )
            data = dict(zip(keys, row))
        data["historical_not_pre_dispatch"] = bool(data["historical_not_pre_dispatch"])
        return data

    def _read_existing(self, db: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM codex_submissions WHERE operation_id=?", (operation_id,)).fetchone()
        return self._row(row) if row is not None else None

    def _accounting_filter(self) -> tuple[str, tuple[str]]:
        # Only the explicitly authorized native background class is separate.
        # Every other batch still shares the original submission/token caps.
        operator = "=" if self.policy.batch == NATIVE_AUX_BATCH else "!="
        return f" WHERE batch {operator} ?", (NATIVE_AUX_BATCH,)

    def reserve(
        self,
        operation_id: str,
        body: bytes,
        *,
        model: str = "gpt-5.6-luna",
        audit_source: str = "codex-appserver-candidate",
    ) -> dict[str, Any]:
        """Atomically reserve one pre-dispatch submission; repeat is idempotent."""

        operation_id = _strict_text("operation_id", operation_id)
        model = _strict_text("model", model)
        audit_source = _strict_text("audit_source", audit_source)
        if not isinstance(body, bytes) or not body:
            raise CodexBudgetError("request_body")
        request_hash = _sha256(body)
        with self._connect_rw() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._ensure_table(db)
                existing = self._read_existing(db, operation_id)
                if existing is not None:
                    if (
                        existing["batch"] != self.policy.batch
                        or existing["model"] != model
                        or existing["request_sha256"] != request_hash
                        or existing["reserved_input"] != self.policy.reserved_input
                        or existing["reserved_output"] != self.policy.reserved_output
                    ):
                        raise CodexBudgetError("operation_id_conflict")
                    existing["idempotent"] = True
                    db.commit()
                    return existing
                unknown_legacy = db.execute(
                    "SELECT 1 FROM codex_submissions WHERE historical_not_pre_dispatch=1 "
                    "AND (reserved_input IS NULL OR reserved_output IS NULL) LIMIT 1"
                ).fetchone()
                if unknown_legacy is not None:
                    raise CodexBudgetError("legacy_unknown_upper_bound")
                db.row_factory = sqlite3.Row
                for breach in db.execute("SELECT * FROM codex_submissions WHERE status='meter_breach'"):
                    authorized = (self.attempt_authorization or {}).get("covered_historical_breaches", [])
                    if not any(all(dict(breach).get(k) == v for k, v in entry.items()) for entry in authorized):
                        raise CodexBudgetError("meter_breach")
                calls, inputs, outputs = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual_input,reserved_input)),0), "
                    "COALESCE(SUM(COALESCE(actual_output,reserved_output)),0) FROM codex_submissions"
                    + self._accounting_filter()[0], self._accounting_filter()[1]
                ).fetchone()
                if (
                    int(calls) >= self.policy.call_cap
                    or int(inputs) + self.policy.reserved_input > self.policy.input_cap
                    or int(outputs) + self.policy.reserved_output > self.policy.output_cap
                ):
                    raise CodexBudgetError("budget_exhausted")
                now = time.time_ns()
                db.execute(
                    "INSERT INTO codex_submissions(operation_id,batch,model,request_sha256,request_bytes,"
                    "reserved_input,reserved_output,actual_input,actual_output,status,dispatch_status,usage_quality,"
                    "reservation_origin,audit_source,historical_not_pre_dispatch,monetary_status,started_ns) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        operation_id, self.policy.batch, model, request_hash, len(body),
                        self.policy.reserved_input, self.policy.reserved_output, None, None,
                        "reserved_before_dispatch", "not_dispatched", "unknown", "pre_dispatch", audit_source, 0,
                        MONETARY_UNAVAILABLE, now,
                    ),
                )
                db.commit()
                row = self._read_existing(db, operation_id)
                assert row is not None
                row["idempotent"] = False
                return row
            except Exception:
                db.rollback()
                raise

    def finish(
        self,
        operation_id: str,
        status: str,
        usage: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Finalize once, retaining the reservation when usage is unknown."""

        operation_id = _strict_text("operation_id", operation_id)
        status = _strict_text("status", status)
        with self._connect_rw() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._ensure_table(db)
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT * FROM codex_submissions WHERE operation_id=?", (operation_id,)).fetchone()
                if row is None:
                    raise CodexBudgetError("unknown_operation_id")
                current = self._row(row)
                if current["finished_ns"] is not None:
                    current["idempotent"] = True
                    db.commit()
                    return current
                if usage is None:
                    final_status = f"{status}_usage_unknown_reserved"
                    db.execute(
                        "UPDATE codex_submissions SET status=?,dispatch_status=?,usage_quality=?,finished_ns=? WHERE operation_id=?",
                        (final_status, "dispatched", "unknown", time.time_ns(), operation_id),
                    )
                else:
                    if not isinstance(usage, Mapping):
                        raise CodexBudgetError("usage")
                    actual_input, actual_output = _canonical_usage(usage)
                    reserved_input = current["reserved_input"]
                    reserved_output = current["reserved_output"]
                    if reserved_input is None or reserved_output is None:
                        raise CodexBudgetError("legacy_usage_upper_bound_unknown")
                    final_status = "meter_breach" if actual_input > reserved_input or actual_output > reserved_output else status
                    db.execute(
                        "UPDATE codex_submissions SET actual_input=?,actual_output=?,status=?,dispatch_status=?,usage_quality=?,finished_ns=? WHERE operation_id=?",
                        (actual_input, actual_output, final_status, "dispatched", "reliable", time.time_ns(), operation_id),
                    )
                db.commit()
                result = self._read_existing(db, operation_id)
                assert result is not None
                result["idempotent"] = False
                return result
            except Exception:
                db.rollback()
                raise

    def backfill_legacy(
        self,
        operation_id: str,
        *,
        audit_source: str,
        model: str = "gpt-5.6-luna",
        request_sha256: str | None = None,
        request_bytes: int = 1,
        input_upper_bound: int | None = None,
        output_upper_bound: int | None = None,
        observed_usage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record historical evidence without relabeling it as pre-dispatch."""

        operation_id = _strict_text("operation_id", operation_id)
        audit_source = _strict_text("audit_source", audit_source)
        model = _strict_text("model", model)
        request_bytes = _strict_nonnegative_int("request_bytes", request_bytes)
        if request_bytes < 1:
            raise CodexBudgetError("request_bytes")
        if request_sha256 is None:
            request_sha256 = "unknown"
        if not isinstance(request_sha256, str) or not request_sha256:
            raise CodexBudgetError("request_sha256")
        for name, value in (("input_upper_bound", input_upper_bound), ("output_upper_bound", output_upper_bound)):
            if value is not None:
                _strict_nonnegative_int(name, value)
        actual_input = actual_output = None
        usage_quality = "unknown"
        status = HISTORICAL_NOT_PRE_DISPATCH
        if observed_usage is not None:
            actual_input, actual_output = _canonical_usage(observed_usage)
            if input_upper_bound is None or output_upper_bound is None:
                raise CodexBudgetError("observed_usage_requires_upper_bound")
            if actual_input > input_upper_bound or actual_output > output_upper_bound:
                status = "meter_breach"
            else:
                usage_quality = "historical_observed"
        with self._connect_rw() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._ensure_table(db)
                existing = self._read_existing(db, operation_id)
                if existing is not None:
                    if existing["reservation_origin"] != "legacy_backfill":
                        raise CodexBudgetError("operation_id_conflict")
                    existing["idempotent"] = True
                    db.commit()
                    return existing
                calls, inputs, outputs = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual_input,reserved_input)),0), "
                    "COALESCE(SUM(COALESCE(actual_output,reserved_output)),0) FROM codex_submissions"
                    + self._accounting_filter()[0], self._accounting_filter()[1]
                ).fetchone()
                projected_input = int(inputs) + (input_upper_bound or 0)
                projected_output = int(outputs) + (output_upper_bound or 0)
                if int(calls) >= self.policy.call_cap or projected_input > self.policy.input_cap or projected_output > self.policy.output_cap:
                    raise CodexBudgetError("budget_exhausted")
                db.execute(
                    "INSERT INTO codex_submissions(operation_id,batch,model,request_sha256,request_bytes,"
                    "reserved_input,reserved_output,actual_input,actual_output,status,dispatch_status,usage_quality,"
                    "reservation_origin,audit_source,historical_not_pre_dispatch,monetary_status,started_ns,finished_ns) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        operation_id, self.policy.batch, model, request_sha256, request_bytes,
                        input_upper_bound, output_upper_bound, actual_input, actual_output, status,
                        HISTORICAL_NOT_PRE_DISPATCH, usage_quality, "legacy_backfill", audit_source, 1,
                        MONETARY_UNAVAILABLE, time.time_ns(), time.time_ns(),
                    ),
                )
                db.commit()
                result = self._read_existing(db, operation_id)
                assert result is not None
                result["idempotent"] = False
                return result
            except Exception:
                db.rollback()
                raise

    def get(self, operation_id: str) -> dict[str, Any] | None:
        operation_id = _strict_text("operation_id", operation_id)
        with self._connect_rw() as db:
            self._ensure_table(db)
            return self._read_existing(db, operation_id)

    def summary(self) -> dict[str, Any]:
        with self._connect_rw() as db:
            self._ensure_table(db)
            calls, inputs, outputs, unknown, breaches = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual_input,reserved_input)),0), "
                "COALESCE(SUM(COALESCE(actual_output,reserved_output)),0), "
                "COALESCE(SUM(CASE WHEN usage_quality='unknown' THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN status='meter_breach' THEN 1 ELSE 0 END),0) FROM codex_submissions"
                + self._accounting_filter()[0], self._accounting_filter()[1]
            ).fetchone()
            return {
                "table": "codex_submissions",
                "batch": self.policy.batch,
                "submissions": int(calls),
                "input_tokens_accounted": int(inputs),
                "output_tokens_accounted": int(outputs),
                "unknown_usage_rows": int(unknown),
                "meter_breach_rows": int(breaches),
                "monetary_status": MONETARY_UNAVAILABLE,
                "caps": {"submissions": self.policy.call_cap, "input_tokens": self.policy.input_cap, "output_tokens": self.policy.output_cap},
            }
