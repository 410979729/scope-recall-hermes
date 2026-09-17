"""Operational auxiliary-model budget ledger; accounting only, not a work queue."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .validation import nonneg_decimal, nonneg_int, positive_int, text


REQUESTS_TABLE = (
    "CREATE TABLE IF NOT EXISTS requests ("
    "id INTEGER PRIMARY KEY, batch TEXT, model TEXT, body_sha256 TEXT, "
    "request_bytes INTEGER, reserved_input INTEGER, reserved_output INTEGER, "
    "actual_input INTEGER, actual_output INTEGER, charge_micro_usd INTEGER, "
    "status TEXT, started_ns INTEGER, cached_input INTEGER)"
)

_LEDGER_BUSY_SLEEP_SECONDS = 0.01
_RESERVED = "reserved_before_network"


def _readonly_uri(path: Path) -> str:
    return f"file:{path.resolve().as_posix()}?mode=ro"


def _readwrite_uri(path: Path) -> str:
    return f"file:{path.resolve().as_posix()}?mode=rw"


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _await_ledger_lock(deadline: float | None) -> None:
    remaining = _remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        raise ValueError("ledger_busy_timeout")


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()


@dataclass(frozen=True)
class ModelPricing:
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def __post_init__(self) -> None:
        for name in ("input_usd_per_million", "output_usd_per_million"):
            object.__setattr__(self, name, nonneg_decimal(name, getattr(self, name)))

    def charge_micro_usd(self, input_tokens: int, output_tokens: int) -> int:
        if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
            raise ValueError("invalid_meter")
        amount = self.input_usd_per_million * Decimal(input_tokens) + self.output_usd_per_million * Decimal(output_tokens)
        return int(amount.to_integral_value(rounding=ROUND_CEILING))


#: Lifetime totals across the whole ledger that may be switched off with
#: ``None``.  They bound how much the instance is *used*; runaway loops are
#: already bounded per item, and anomalous metering is caught by the separate
#: ``meter_breach`` check.  ``None`` means "not capped" and is deliberately
#: distinct from ``0``, which still means "no spend at all" and stays the
#: default for a policy no installer has configured, so an unconfigured
#: instance is fail-closed.
_OPTIONAL_CUMULATIVE_CAPS = ("cap_micro_usd", "total_input_cap", "total_output_cap", "total_call_cap")
_REQUIRED_NONNEG = ("max_request_bytes", "default_reserve_input", "default_reserve_output")


@dataclass(frozen=True)
class BudgetPolicy:
    batch: str
    cap_micro_usd: int | None
    total_input_cap: int | None
    total_output_cap: int | None
    total_call_cap: int | None
    max_request_bytes: int
    default_reserve_input: int
    default_reserve_output: int
    model_reserve_output: Mapping[str, int]
    model_token_caps: Mapping[str, tuple[int | None, int | None]]
    pricing: Mapping[str, ModelPricing]
    approved_models: frozenset[str]

    def __post_init__(self) -> None:
        text("batch", self.batch)
        for name in _OPTIONAL_CUMULATIVE_CAPS:
            if getattr(self, name) is not None:
                nonneg_int(name, getattr(self, name))
        for name in _REQUIRED_NONNEG:
            nonneg_int(name, getattr(self, name))
        reserve_output = {
            str(model): positive_int("model_reserve_output", tokens)
            for model, tokens in dict(self.model_reserve_output).items()
        }
        model_caps = {
            str(model): _model_cap_pair(caps) for model, caps in dict(self.model_token_caps).items()
        }
        pricing = dict(self.pricing)
        approved = frozenset(self.approved_models)
        if not approved <= pricing.keys():
            raise ValueError("approved_model_pricing")
        object.__setattr__(self, "model_reserve_output", MappingProxyType(reserve_output))
        object.__setattr__(self, "model_token_caps", MappingProxyType(model_caps))
        object.__setattr__(self, "pricing", MappingProxyType(pricing))
        object.__setattr__(self, "approved_models", approved)


def _model_cap_pair(caps: object) -> tuple[int | None, int | None]:
    """Per-model totals are cumulative like the policy caps: ``None`` is uncapped, ``0`` is no spend."""
    if not isinstance(caps, tuple) or len(caps) != 2:
        raise ValueError("model_token_caps")
    return (
        None if caps[0] is None else nonneg_int("model_token_caps_input", caps[0]),
        None if caps[1] is None else nonneg_int("model_token_caps_output", caps[1]),
    )


def default_budget_policy(*, batch: str = "auxiliary") -> BudgetPolicy:
    """Trusted defaults: no spend until an installer raises caps and pricing."""

    return BudgetPolicy(
        batch=batch,
        cap_micro_usd=0,
        total_input_cap=0,
        total_output_cap=0,
        total_call_cap=0,
        max_request_bytes=32_000,
        default_reserve_input=32_768,
        default_reserve_output=4_096,
        model_reserve_output={"mimo-v2.5": 131_072},
        model_token_caps={},
        pricing={},
        approved_models=frozenset(),
    )


def initialize_auxiliary_budget_ledger(path: Path, policy: BudgetPolicy) -> None:
    """Explicit ledger creation for trusted installer/tests; never auto-called on read."""

    target = Path(path)
    if not target.is_absolute():
        raise ValueError("ledger_path_must_be_absolute")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(target), timeout=5)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute(REQUESTS_TABLE)


def read_auxiliary_budget_status(path: Path) -> dict:
    """Read-only ledger inspection; never creates the database."""

    target = Path(path)
    if not target.is_file():
        return {"ledger_exists": False, "requests": 0, "charge_micro_usd": 0, "meter_breach": False}
    with closing(sqlite3.connect(_readonly_uri(target), uri=True, timeout=2)) as db:
        row = db.execute(
            "SELECT COUNT(*) AS requests, COALESCE(SUM(charge_micro_usd),0) AS charge, "
            "MAX(CASE WHEN status='meter_breach' THEN 1 ELSE 0 END) AS meter_breach FROM requests"
        ).fetchone()
    return {
        "ledger_exists": True,
        "requests": int(row[0]),
        "charge_micro_usd": int(row[1] or 0),
        "meter_breach": bool(row[2]),
    }


def load_hermes_attempt_authorization() -> dict | None:
    """Load a hash-bound Hermes amendment; never copy or reset the original ledger."""
    name = os.environ.get("SCOPE_RECALL_HERMES_ATTEMPT_AUTHORIZATION")
    expected = os.environ.get("SCOPE_RECALL_HERMES_ATTEMPT_AUTHORIZATION_SHA256")
    if name is None and expected is None:
        return None
    if not name or not expected:
        raise ValueError("Hermes attempt authorization path and hash must be supplied together")
    path = Path(name)
    if not path.is_absolute() or "budget-authorization" not in path.name:
        raise ValueError("absolute Hermes attempt authorization path required")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected.lower():
        raise ValueError("Hermes attempt authorization hash mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != "scope-recall.hermes-attempt-budget-amendment.v1":
        raise ValueError("Hermes attempt authorization schema required")
    if value.get("historical_attempt_status_unchanged") is not True:
        raise ValueError("historical Hermes meter_breach rows must stay unchanged")
    return value


def _historical_breach_row_authorized(row: Mapping[str, object], authorized: tuple[dict, ...]) -> bool:
    payload = dict(row)
    for entry in authorized:
        fields = {key: value for key, value in entry.items() if key != "table"}
        if fields and all(payload.get(key) == value for key, value in fields.items()):
            return True
    return False


class AuxiliaryBudgetLedger:
    def __init__(
        self,
        path: Path,
        policy: BudgetPolicy,
        covered_historical_breaches: tuple[dict, ...] | list[dict] = (),
    ) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError("ledger_path_must_be_absolute")
        self.path = target
        self.policy = policy
        self.covered_historical_breaches = tuple(dict(entry) for entry in covered_historical_breaches)

    def _connect_rw(self, *, deadline: float | None = None) -> sqlite3.Connection:
        if not self.path.is_file():
            raise ValueError("ledger_not_initialized")
        while True:
            _await_ledger_lock(deadline)
            try:
                remaining = _remaining_seconds(deadline)
                # SQLite's default busy timeout is too short for two foreground
                # reservations starting together, so the same bounded deadline
                # covers connection and pager-level waits.  mode=rw never
                # creates a ledger.
                timeout = 5.0 if remaining is None else max(0.001, min(5.0, remaining))
                db = sqlite3.connect(_readwrite_uri(self.path), uri=True, timeout=timeout)
                db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
                return db
            except sqlite3.OperationalError as exc:
                if not _is_locked(exc):
                    raise
                remaining = _remaining_seconds(deadline)
                if remaining is None:
                    raise ValueError("ledger_busy") from exc
                time.sleep(min(_LEDGER_BUSY_SLEEP_SECONDS, max(remaining, 0)))

    def reserve(
        self,
        model: str,
        body: bytes,
        *,
        reserved_input: int,
        reserved_output: int,
        timeout_seconds: float | None = None,
    ) -> int:
        """Commit a reservation row before any network call; refuse when a cap would be crossed."""
        pricing = self._pricing_for_request(model, body, reserved_input, reserved_output)
        amount = pricing.charge_micro_usd(reserved_input, reserved_output)
        deadline = None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
        _await_ledger_lock(deadline)
        with closing(self._connect_rw(deadline=deadline)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.row_factory = sqlite3.Row
            self._assert_headroom(db, model, amount, reserved_input, reserved_output)
            request_id = db.execute(
                "INSERT INTO requests(batch,model,body_sha256,request_bytes,reserved_input,"
                "reserved_output,charge_micro_usd,status,started_ns) VALUES (?,?,?,?,?,?,?,?,?)",
                (self.policy.batch, model, hashlib.sha256(body).hexdigest(), len(body),
                 reserved_input, reserved_output, amount, _RESERVED, time.time_ns()),
            ).lastrowid
            if request_id is None:
                raise RuntimeError("ledger_request_id_unavailable")
            return int(request_id)

    def _pricing_for_request(self, model: str, body: bytes, reserved_input: int, reserved_output: int) -> ModelPricing:
        if model not in self.policy.approved_models:
            raise ValueError("unsupported_model")
        if not 0 < len(body) <= self.policy.max_request_bytes:
            raise ValueError("unsupported_model_or_size")
        if any(type(value) is not int or value < 0 for value in (reserved_input, reserved_output)):
            raise ValueError("invalid_reservation")
        pricing = self.policy.pricing.get(model)
        if pricing is None:
            raise ValueError("unsupported_model")
        return pricing

    def _assert_headroom(self, db: sqlite3.Connection, model: str, amount: int,
                         reserved_input: int, reserved_output: int) -> None:
        """Every comparison is against a lifetime total; a cap of ``None`` is not enforced."""
        for breach in db.execute("SELECT * FROM requests WHERE status='meter_breach'"):
            if not _historical_breach_row_authorized(breach, self.covered_historical_breaches):
                raise ValueError("budget_exhausted_or_meter_breach")
        policy = self.policy
        used = int(db.execute("SELECT COALESCE(SUM(charge_micro_usd),0) FROM requests").fetchone()[0])
        calls, inputs, outputs = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual_input,reserved_input)),0), "
            "COALESCE(SUM(COALESCE(actual_output,reserved_output)),0) FROM requests"
        ).fetchone()
        model_inputs, model_outputs = db.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_input,reserved_input)),0), "
            "COALESCE(SUM(COALESCE(actual_output,reserved_output)),0) FROM requests WHERE model=?",
            (model,),
        ).fetchone()
        model_input_cap, model_output_cap = policy.model_token_caps.get(model, (None, None))
        checks = (
            (policy.cap_micro_usd, used + amount),
            (policy.total_input_cap, inputs + reserved_input),
            (policy.total_output_cap, outputs + reserved_output),
            (model_input_cap, model_inputs + reserved_input),
            (model_output_cap, model_outputs + reserved_output),
        )
        exceeded = (policy.total_call_cap is not None and calls >= policy.total_call_cap) or any(
            cap is not None and total > cap for cap, total in checks
        )
        if exceeded:
            raise ValueError("budget_exhausted_or_meter_breach")

    def finish(
        self,
        request_id: int,
        status: str,
        usage: Mapping[str, int] | None,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        counts = None
        cached = None
        if isinstance(usage, Mapping):
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if type(prompt) is int and type(completion) is int and min(prompt, completion) >= 0:
                counts = (prompt, completion)
                hit = usage.get("cached_prompt_tokens")
                if type(hit) is int and 0 <= hit <= prompt:
                    cached = hit
        return self._settle(request_id, status, counts, timeout_seconds=timeout_seconds, cached_input=cached)

    def finish_embedding(
        self,
        request_id: int,
        status: str,
        usage: Mapping[str, int] | None,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        counts = None
        if isinstance(usage, Mapping):
            prompt = usage.get("promptTokenCount")
            if type(prompt) is int and prompt >= 0:
                counts = (prompt, 0)
        return self._settle(request_id, status, counts, timeout_seconds=timeout_seconds)

    def _settle(self, request_id: int, status: str, counts: tuple[int, int] | None, *,
                timeout_seconds: float | None, cached_input: int | None = None) -> str:
        """Close the reservation once.  Unknown usage keeps the reserved charge."""
        deadline = None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
        retained = f"{status}_usage_unknown_reserved_charge_retained"
        while True:
            _await_ledger_lock(deadline)
            try:
                with closing(self._connect_rw(deadline=deadline)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT model,status,reserved_input,reserved_output FROM requests WHERE id=?",
                        (request_id,),
                    ).fetchone()
                    if row is None or row[1] != _RESERVED:
                        raise ValueError("reservation_state")
                    pricing = self.policy.pricing[row[0]]
                    if counts is None:
                        db.execute("UPDATE requests SET status=? WHERE id=?", (retained, request_id))
                        return retained
                    prompt, completion = counts
                    final = "meter_breach" if prompt > row[2] or completion > row[3] else status
                    charge = pricing.charge_micro_usd(prompt, completion)
                    db.execute(
                        "UPDATE requests SET status=?,actual_input=?,actual_output=?,charge_micro_usd=? WHERE id=?",
                        (final, prompt, completion, charge, request_id),
                    )
                    if cached_input is not None:
                        _record_cached_input(db, request_id, cached_input)
                    return final
            except sqlite3.OperationalError as exc:
                if not _is_locked(exc):
                    raise
                remaining = _remaining_seconds(deadline)
                if remaining is None or remaining <= 0:
                    return retained
                time.sleep(min(_LEDGER_BUSY_SLEEP_SECONDS, remaining))


def _record_cached_input(db: sqlite3.Connection, request_id: int, cached: int) -> None:
    """Store the provider's cached prompt tokens beside the settled row.

    Observation only; the charge above still prices every prompt token.  A
    ledger created before the column existed gains it here, inside the settling
    write transaction, so two workers cannot both try to add it.
    """
    if "cached_input" not in {row[1] for row in db.execute("PRAGMA table_info(requests)")}:
        db.execute("ALTER TABLE requests ADD COLUMN cached_input INTEGER")
    db.execute("UPDATE requests SET cached_input=? WHERE id=?", (cached, request_id))


#: A provider refusing nearly every call for a sustained stretch is an outage,
#: not a flake.  These bounds are what separate the two.
REFUSAL_WINDOW_SECONDS = 3600
REFUSAL_SHARE = 0.5
REFUSAL_MINIMUM_CALLS = 8


def provider_refusals(ledger_path, *, now: float | None = None) -> list[str]:
    """Models the provider is currently refusing, named by its own code.

    Lives beside the ledger it reads because the doctor and the worker's
    status file need the same answer: a watcher polling the status file
    learns nothing from "degraded" with an empty gap list.
    """
    if ledger_path is None:
        return []
    try:
        path = Path(ledger_path)
        if not path.is_file():
            return []
        since = ((time.time() if now is None else now) - REFUSAL_WINDOW_SECONDS) * 1_000_000_000
        with closing(sqlite3.connect(_readonly_uri(path), uri=True, timeout=5)) as db:
            rows = db.execute("SELECT model, status FROM requests WHERE started_ns >= ?",
                              (since,)).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return []
    totals: dict[str, int] = {}
    refused: dict[str, Counter] = {}
    for model, status in rows:
        name = str(model or "unknown")[:64]
        totals[name] = totals.get(name, 0) + 1
        label = str(status or "")
        if "http_2" in label:
            continue
        if any(marker in label for marker in ("http_4", "http_5", "429")):
            code = label.split(":", 1)[1].split("_", 1)[0] if ":" in label else label.split("_usage", 1)[0]
            refused.setdefault(name, Counter())[code[:64]] += 1
    gaps = []
    for name, total in sorted(totals.items()):
        counted = refused.get(name)
        if not counted or total < REFUSAL_MINIMUM_CALLS:
            continue
        if sum(counted.values()) / total <= REFUSAL_SHARE:
            continue
        gaps.append(f"model_refused:{name}:{counted.most_common(1)[0][0]}")
    return gaps


def pre_request_refusals(auxiliary) -> list[str]:
    """Refusals that happen before a request is sent, which nothing else reports.

    ``reserve`` raises before the request leaves the process, so a refused
    reservation writes no row and every ledger-based measurement, including
    ``provider_refusals``, is blind to it.  ``adapters.models`` folds the
    refusal into ``budget_unavailable``, the worker defers the item for an hour
    without burning an attempt, and a deterministic fault (a model missing
    from ``approved_models``, a ledger file that was moved) therefore stalls
    forever, spending nothing and saying nothing.  Checking the configuration
    names the fault, even on an installation that has queued nothing yet.

    An approved model with no price is not checked: ``BudgetPolicy`` refuses
    to exist in that state (``approved_model_pricing``).
    """
    if auxiliary is None:
        return []
    uses_models = bool(getattr(auxiliary, "external_consolidation", False)
                       or getattr(auxiliary, "external_embedding", False))
    ledger_path = getattr(auxiliary, "ledger_path", None)
    if uses_models and ledger_path is not None and not Path(ledger_path).is_file():
        # The name only, never the directory: the doctor already states where the
        # installation lives, and a gap string travels further than the report.
        return [f"ledger_missing:{Path(ledger_path).name[:64]}"]
    approved = getattr(getattr(auxiliary, "budget", None), "approved_models", None)
    if not approved:
        # An empty allowlist refuses every model, which is a different fault with
        # a different name; reporting each route against it would say it twice.
        return []
    routes = []
    if getattr(auxiliary, "external_consolidation", False):
        route = getattr(auxiliary, "consolidation", None)
        # CLI subscriptions have their own call/token ledger, not invented API
        # prices. Their explicit route validates the approved model itself.
        if getattr(route, "kind", None) != "codex_cli":
            routes.append(("consolidation", getattr(route, "model", None) if route else None))
    if getattr(auxiliary, "external_embedding", False):
        route = getattr(auxiliary, "embedding", None)
        try:
            # ``space()`` is what resolves an omitted model to the shipped
            # default, so asking it keeps this from disagreeing with the name
            # the adapter will really send.
            name = route.space().get("model") if route is not None else None
        except Exception:
            name = getattr(route, "model", None) if route is not None else None
        routes.append(("embedding", name))
    return [
        f"model_not_approved:{role}:{name[:64]}"
        for role, name in routes
        if type(name) is str and name and name not in approved
    ]
