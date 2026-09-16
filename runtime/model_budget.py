"""Operational auxiliary-model budget ledger; accounting only, not a work queue."""
from __future__ import annotations

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


REQUESTS_TABLE = (
    "CREATE TABLE IF NOT EXISTS requests ("
    "id INTEGER PRIMARY KEY, batch TEXT, model TEXT, body_sha256 TEXT, "
    "request_bytes INTEGER, reserved_input INTEGER, reserved_output INTEGER, "
    "actual_input INTEGER, actual_output INTEGER, charge_micro_usd INTEGER, "
    "status TEXT, started_ns INTEGER)"
)

_LEDGER_BUSY_SLEEP_SECONDS = 0.01


def _strict_nonneg_int(name: str, value: object) -> int:
    if type(value) is bool or type(value) is not int:
        raise ValueError(name)
    if value < 0:
        raise ValueError(name)
    return value


def _strict_positive_int(name: str, value: object) -> int:
    parsed = _strict_nonneg_int(name, value)
    if parsed < 1:
        raise ValueError(name)
    return parsed


def _strict_nonneg_decimal(name: str, value: object) -> Decimal:
    if type(value) is bool:
        raise ValueError(name)
    if isinstance(value, Decimal):
        amount = value
    elif type(value) is int:
        amount = Decimal(value)
    elif type(value) is str:
        if not value or value.strip() != value:
            raise ValueError(name)
        try:
            amount = Decimal(value)
        except Exception as exc:
            raise ValueError(name) from exc
    else:
        raise ValueError(name)
    if not amount.is_finite() or amount < 0:
        raise ValueError(name)
    return amount


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


@dataclass(frozen=True)
class ModelPricing:
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_usd_per_million",
            _strict_nonneg_decimal("input_usd_per_million", self.input_usd_per_million),
        )
        object.__setattr__(
            self,
            "output_usd_per_million",
            _strict_nonneg_decimal("output_usd_per_million", self.output_usd_per_million),
        )

    def charge_micro_usd(self, input_tokens: int, output_tokens: int) -> int:
        if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
            raise ValueError("invalid_meter")
        amount = self.input_usd_per_million * Decimal(input_tokens) + self.output_usd_per_million * Decimal(output_tokens)
        return int(amount.to_integral_value(rounding=ROUND_CEILING))


#: Cumulative caps that may be switched off with ``None``.
#:
#: These are lifetime totals across the whole ledger — the SQL that reads them
#: carries no time window — so headroom only ever shrinks, and reaching one stops
#: the derived layer permanently with no symptom beyond work quietly pausing.
#: They bound how much the instance is *used*, which is not the thing that needs
#: guarding: a runaway loop is already bounded per item by
#: ``MAX_RECOVERABLE_ATTEMPTS``, by the at-most-once ``begin_model_attempt``
#: fence, and by the durable repair markers. Anomalous metering is caught
#: separately by the ``meter_breach`` check, which is not affected here.
#:
#: ``None`` means "not capped". It is deliberately distinct from ``0``, which
#: still means "no spend permitted at all" and remains the default for a policy
#: no installer has configured, so an unconfigured instance stays fail-closed.
_OPTIONAL_CUMULATIVE_CAPS = (
    "cap_micro_usd",
    "total_input_cap",
    "total_output_cap",
    "total_call_cap",
)


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
        if type(self.batch) is not str or not self.batch:
            raise ValueError("batch")
        for name in _OPTIONAL_CUMULATIVE_CAPS:
            value = getattr(self, name)
            if value is not None:
                _strict_nonneg_int(name, value)
        for name in (
            "max_request_bytes",
            "default_reserve_input",
            "default_reserve_output",
        ):
            _strict_nonneg_int(name, getattr(self, name))
        reserve_output = {
            str(model): _strict_positive_int("model_reserve_output", tokens)
            for model, tokens in dict(self.model_reserve_output).items()
        }
        model_caps: dict[str, tuple[int | None, int | None]] = {}
        for model, caps in dict(self.model_token_caps).items():
            if not isinstance(caps, tuple) or len(caps) != 2:
                raise ValueError("model_token_caps")
            # Per-model totals are cumulative in the same way, so None switches
            # them off too. 0 still means this model may spend nothing.
            model_caps[str(model)] = tuple(
                None if side is None else _strict_nonneg_int(name, side)
                for name, side in (
                    ("model_token_caps_input", caps[0]),
                    ("model_token_caps_output", caps[1]),
                )
            )
        pricing = dict(self.pricing)
        approved = frozenset(self.approved_models)
        for model in approved:
            if model not in pricing:
                raise ValueError("approved_model_pricing")
        object.__setattr__(self, "model_reserve_output", MappingProxyType(reserve_output))
        object.__setattr__(self, "model_token_caps", MappingProxyType(model_caps))
        object.__setattr__(self, "pricing", MappingProxyType(pricing))
        object.__setattr__(self, "approved_models", approved)


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
                # SQLite's default busy timeout is too short for two
                # foreground reservations starting together.  Use the same
                # bounded deadline for connection and pager-level waits; the
                # URI remains mode=rw so this path never creates a ledger.
                timeout = 5.0 if remaining is None else max(0.001, min(5.0, remaining))
                db = sqlite3.connect(_readwrite_uri(self.path), uri=True, timeout=timeout)
                db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
                return db
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
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
        if model not in self.policy.approved_models:
            raise ValueError("unsupported_model")
        if not 0 < len(body) <= self.policy.max_request_bytes:
            raise ValueError("unsupported_model_or_size")
        if any(type(value) is not int or value < 0 for value in (reserved_input, reserved_output)):
            raise ValueError("invalid_reservation")
        pricing = self.policy.pricing.get(model)
        if pricing is None:
            raise ValueError("unsupported_model")
        amount = pricing.charge_micro_usd(reserved_input, reserved_output)
        deadline = None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
        _await_ledger_lock(deadline)
        with closing(self._connect_rw(deadline=deadline)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.row_factory = sqlite3.Row
            for breach in db.execute("SELECT * FROM requests WHERE status='meter_breach'"):
                if not _historical_breach_row_authorized(breach, self.covered_historical_breaches):
                    raise ValueError("budget_exhausted_or_meter_breach")
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
            caps = self.policy.model_token_caps.get(model)
            # A cap of None is not enforced. Every comparison below is against a
            # lifetime total, so an enforced one eventually stops the instance
            # for good; None lets the per-item loop guards be the limit instead.
            # The meter_breach check above is untouched — it catches a model
            # burning far more than it reserved, which is the failure this layer
            # genuinely needs to stop.
            exceeded = (
                (self.policy.cap_micro_usd is not None and used + amount > self.policy.cap_micro_usd)
                or (self.policy.total_call_cap is not None and calls >= self.policy.total_call_cap)
                or (self.policy.total_input_cap is not None
                    and inputs + reserved_input > self.policy.total_input_cap)
                or (self.policy.total_output_cap is not None
                    and outputs + reserved_output > self.policy.total_output_cap)
            )
            if caps is not None and not exceeded:
                exceeded = (
                    (caps[0] is not None and model_inputs + reserved_input > caps[0])
                    or (caps[1] is not None and model_outputs + reserved_output > caps[1])
                )
            if exceeded:
                raise ValueError("budget_exhausted_or_meter_breach")
            request_id = db.execute(
                "INSERT INTO requests(batch,model,body_sha256,request_bytes,reserved_input,"
                "reserved_output,charge_micro_usd,status,started_ns) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self.policy.batch,
                    model,
                    hashlib.sha256(body).hexdigest(),
                    len(body),
                    reserved_input,
                    reserved_output,
                    amount,
                    "reserved_before_network",
                    time.time_ns(),
                ),
            ).lastrowid
            if request_id is None:
                raise RuntimeError("ledger_request_id_unavailable")
            return int(request_id)

    def finish(
        self,
        request_id: int,
        status: str,
        usage: Mapping[str, int] | None,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        deadline = None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
        while True:
            _await_ledger_lock(deadline)
            try:
                with closing(self._connect_rw(deadline=deadline)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT model,status,reserved_input,reserved_output FROM requests WHERE id=?",
                        (request_id,),
                    ).fetchone()
                    if row is None or row[1] != "reserved_before_network":
                        raise ValueError("reservation_state")
                    model = row[0]
                    pricing = self.policy.pricing[model]
                    prompt = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
                    completion = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
                    if type(prompt) is int and type(completion) is int and min(prompt, completion) >= 0:
                        final_status = status
                        if prompt > row[2] or completion > row[3]:
                            final_status = "meter_breach"
                        db.execute(
                            "UPDATE requests SET status=?,actual_input=?,actual_output=?,charge_micro_usd=? WHERE id=?",
                            (final_status, prompt, completion, pricing.charge_micro_usd(prompt, completion), request_id),
                        )
                        return final_status
                    final_status = f"{status}_usage_unknown_reserved_charge_retained"
                    db.execute("UPDATE requests SET status=? WHERE id=?", (final_status, request_id))
                    return final_status
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                remaining = _remaining_seconds(deadline)
                if remaining is None or remaining <= 0:
                    return f"{status}_usage_unknown_reserved_charge_retained"
                time.sleep(min(_LEDGER_BUSY_SLEEP_SECONDS, remaining))

    def finish_embedding(
        self,
        request_id: int,
        status: str,
        usage: Mapping[str, int] | None,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        deadline = None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
        while True:
            _await_ledger_lock(deadline)
            try:
                with closing(self._connect_rw(deadline=deadline)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT model,status,reserved_input FROM requests WHERE id=?",
                        (request_id,),
                    ).fetchone()
                    if row is None or row[1] != "reserved_before_network":
                        raise ValueError("reservation_state")
                    model = row[0]
                    pricing = self.policy.pricing[model]
                    prompt = usage.get("promptTokenCount") if isinstance(usage, Mapping) else None
                    if type(prompt) is int and prompt >= 0:
                        final_status = "meter_breach" if prompt > row[2] else status
                        db.execute(
                            "UPDATE requests SET status=?,actual_input=?,actual_output=?,charge_micro_usd=? WHERE id=?",
                            (final_status, prompt, 0, pricing.charge_micro_usd(prompt, 0), request_id),
                        )
                        return final_status
                    final_status = f"{status}_usage_unknown_reserved_charge_retained"
                    db.execute("UPDATE requests SET status=? WHERE id=?", (final_status, request_id))
                    return final_status
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                remaining = _remaining_seconds(deadline)
                if remaining is None or remaining <= 0:
                    return f"{status}_usage_unknown_reserved_charge_retained"
                time.sleep(min(_LEDGER_BUSY_SLEEP_SECONDS, remaining))


#: A provider refusing nearly every call for a sustained stretch is an outage,
#: not a flake.  These bounds are what separate the two.
REFUSAL_WINDOW_SECONDS = 3600
REFUSAL_SHARE = 0.5
REFUSAL_MINIMUM_CALLS = 8


def provider_refusals(ledger_path, *, now: float | None = None) -> list[str]:
    """Models the provider is currently refusing, named by its own code.

    Lives here, beside the ledger it reads, because two components need the
    same answer: the doctor, where an operator looks, and the worker's status
    file, which is what a host agent watching the instance actually polls.  A
    live instance spent four hours reporting every run as degraded with an
    empty gap list while the provider answered all 344 calls with "monthly
    usage limit reached, resets in 13 days"; reporting it in only one of the
    two places would have left the watcher just as uninformed.
    """
    import sqlite3
    import time as _time
    from collections import Counter
    from contextlib import closing
    from pathlib import Path as _Path

    if ledger_path is None:
        return []
    try:
        if not _Path(ledger_path).is_file():
            return []
        since = ((_time.time() if now is None else now) - REFUSAL_WINDOW_SECONDS) * 1_000_000_000
        uri = f"file:{_Path(ledger_path).as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            rows = db.execute("SELECT model, status FROM requests WHERE started_ns >= ?",
                              (since,)).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return []
    totals: dict[str, int] = {}
    refused: dict[str, Counter] = {}
    for model, status in rows:
        name = str(model or "unknown")[:64]
        totals[name] = totals.get(name, 0) + 1
        text = str(status or "")
        if "http_2" in text:
            continue
        if any(marker in text for marker in ("http_4", "http_5", "429")):
            code = text.split(":", 1)[1].split("_", 1)[0] if ":" in text else text.split("_usage", 1)[0]
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


#: The refusals that happen before a request is sent, which nothing else reports.
#:
#: The ledger cannot report this and never will: ``reserve`` raises before the
#: request leaves the process, so a refused reservation writes no row.  Every
#: measurement built on the ledger -- ``provider_refusals`` included -- is blind
#: to it by construction.
#:
#: What the operator sees instead is nothing.  ``reserve`` raises
#: ``unsupported_model``; ``adapters.models`` folds that, ``ledger_not_initialized``
#: and ``unsupported_model_or_size`` into one ``budget_unavailable``; the worker
#: reads that as a budget pause and defers the item for an hour *without* burning
#: an attempt.  Refunding the attempt is right for a transient shortage -- the
#: item must not be abandoned for something that was never its fault.  But a name
#: missing from ``approved_models`` is deterministic: it will still be missing in
#: an hour, and in a year.  So the item retries hourly forever, stays ``pending``
#: forever, spends nothing, and says nothing.  A silent stall, not a silent spend.
#:
#: This is the same shape as the refusal that went unnamed for four hours in
#: ``provider_refusals`` above, one layer down: the cure is the same one, which is
#: to say who was refused and why.  Checking the configuration rather than the
#: work items also means it reports on an installation that has not yet queued
#: anything at all.
#:
#: ``reserve`` raises the same ``unsupported_model`` for an approved model with
#: no price, but ``BudgetPolicy.__post_init__`` refuses to build that policy at
#: all (``approved_model_pricing``), so a configuration cannot reach here in that
#: state and checking for it would be a branch that never runs.
#:
#: A missing ledger file is the same fault wearing a different code.  ``reserve``
#: raises ``ledger_not_initialized`` from ``_connect_rw``, it folds into the same
#: ``budget_unavailable``, and all three readers go quiet in the same way:
#: ``provider_refusals`` returns ``[]`` because it cannot open the file,
#: ``_ledger_headroom`` returns ``{}`` for the same reason, and an allowlist check
#: does not look at files at all.  Every item then defers hourly, forever, in
#: silence -- and unlike an unapproved name, this one can arrive by a file being
#: moved rather than by anyone editing the configuration.
def pre_request_refusals(auxiliary) -> list[str]:
    if auxiliary is None:
        return []
    uses_models = bool(getattr(auxiliary, "external_consolidation", False)
                       or getattr(auxiliary, "external_embedding", False))
    ledger_path = getattr(auxiliary, "ledger_path", None)
    if uses_models and ledger_path is not None and not Path(ledger_path).is_file():
        # The name only, never the directory: the doctor already states where the
        # installation lives, and a gap string travels further than the report.
        return [f"ledger_missing:{Path(ledger_path).name[:64]}"]
    budget = getattr(auxiliary, "budget", None)
    approved = getattr(budget, "approved_models", None)
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
            # ``space()`` is what actually resolves an omitted model to the
            # shipped default, so asking it keeps this from disagreeing with the
            # name the adapter will really send.
            name = route.space().get("model") if route is not None else None
        except Exception:
            name = getattr(route, "model", None) if route is not None else None
        routes.append(("embedding", name))
    gaps = []
    for role, name in routes:
        if type(name) is not str or not name:
            continue
        if name not in approved:
            gaps.append(f"model_not_approved:{role}:{name[:64]}")
    return gaps
