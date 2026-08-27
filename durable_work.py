"""Shared contracts for bounded, resumable background work.

The contract deliberately owns no SQL schema and no payload table.  Relation,
vector, journal, nightly, governance, and future domains retain their existing
transaction, provenance, retention, and indexing boundaries while exposing the
same identity, lease, retry, terminal-state, cursor, and health semantics.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable


DURABLE_WORK_SCHEMA_VERSION = "durable_work.v1"


class DurableWorkItemState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    COMPLETED = "completed"
    POISONED = "poisoned"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class DurableWorkRetryClass(StrEnum):
    RETRIABLE = "retriable"
    PERMANENT = "permanent"
    POISON = "poison"
    AUTHORITY_REVOKED = "authority_revoked"
    EPOCH_MISMATCH = "epoch_mismatch"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    CONTENTION = "contention"


DURABLE_WORK_ITEM_STATES = frozenset(item.value for item in DurableWorkItemState)
DURABLE_WORK_TERMINAL_STATES = frozenset(
    {
        DurableWorkItemState.COMPLETED.value,
        DurableWorkItemState.POISONED.value,
        DurableWorkItemState.CANCELLED.value,
        DurableWorkItemState.SUPERSEDED.value,
    }
)
DURABLE_WORK_RETRY_CLASSES = frozenset(item.value for item in DurableWorkRetryClass)
DURABLE_WORK_HEALTH_STATES = frozenset(
    {"ready", "degraded", "blocked", "needs_repair", "disabled"}
)

_ALLOWED_ITEM_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "pending": frozenset({"processing", "cancelled", "superseded"}),
        "processing": frozenset(
            {"retry", "completed", "poisoned", "cancelled", "superseded"}
        ),
        "retry": frozenset(
            {"processing", "poisoned", "cancelled", "superseded"}
        ),
        "completed": frozenset(),
        "poisoned": frozenset(),
        "cancelled": frozenset(),
        "superseded": frozenset(),
    }
)


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _non_negative_int(value: Any, field_name: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return parsed


def _positive_int(value: Any, field_name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{field_name} must be positive")
    return parsed


def _utc_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_required_text(value, field_name))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("durable work snapshot keys must be strings")
        return MappingProxyType(
            {key: _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("durable work snapshots require finite numbers")
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"durable work snapshots must be JSON-compatible, got {type(value).__name__}")


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def _frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    frozen = _freeze_value(dict(value or {}))
    if not isinstance(frozen, Mapping):  # pragma: no cover - construction invariant
        raise TypeError("snapshot must be a mapping")
    return frozen


def canonical_snapshot_hash(value: Mapping[str, Any]) -> str:
    """Return the stable SHA-256 identity for a JSON-compatible snapshot."""

    frozen = _freeze_value(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - type contract
        raise TypeError("snapshot must be a mapping")
    encoded = json.dumps(
        _thaw_value(frozen),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_item_transition(current: str, target: str) -> None:
    """Reject revival or any other transition outside the shared state graph.

    Repeating the same state is an idempotent no-op.  In particular, a caller
    may confirm a terminal state but cannot move a terminal item back to work.
    """

    source = str(current or "").strip().lower()
    destination = str(target or "").strip().lower()
    if source not in DURABLE_WORK_ITEM_STATES:
        raise ValueError(f"unsupported current item state: {source or '<empty>'}")
    if destination not in DURABLE_WORK_ITEM_STATES:
        raise ValueError(
            f"unsupported target item state: {destination or '<empty>'}"
        )
    if source == destination:
        return
    if destination not in _ALLOWED_ITEM_TRANSITIONS[source]:
        raise ValueError(f"durable work item transition refused: {source}->{destination}")


@dataclass(frozen=True, slots=True)
class DurableWorkDescriptor:
    work_id: str
    domain_type: str
    idempotency_key: str
    scope_snapshot: Mapping[str, Any]
    authority_snapshot: Mapping[str, Any]
    policy_version: str
    generation: int
    frozen_upper_bound: int
    item_set_hash: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("work_id", "domain_type", "idempotency_key", "policy_version"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(
            self, "generation", _non_negative_int(self.generation, "generation")
        )
        object.__setattr__(
            self,
            "frozen_upper_bound",
            _non_negative_int(self.frozen_upper_bound, "frozen_upper_bound"),
        )
        digest = str(self.item_set_hash or "").strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("item_set_hash must be a lowercase SHA-256 digest")
        object.__setattr__(self, "item_set_hash", digest)
        _utc_datetime(self.created_at, "created_at")
        object.__setattr__(self, "scope_snapshot", _frozen_mapping(self.scope_snapshot))
        object.__setattr__(
            self, "authority_snapshot", _frozen_mapping(self.authority_snapshot)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "domain_type": self.domain_type,
            "idempotency_key": self.idempotency_key,
            "scope_snapshot": _thaw_value(self.scope_snapshot),
            "authority_snapshot": _thaw_value(self.authority_snapshot),
            "policy_version": self.policy_version,
            "generation": self.generation,
            "frozen_upper_bound": self.frozen_upper_bound,
            "item_set_hash": self.item_set_hash,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class DurableWorkLease:
    worker_id: str
    lease_token: str
    lease_generation: int
    lease_expires_at: str
    bounded_item_budget: int
    bounded_wall_clock_budget: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", _required_text(self.worker_id, "worker_id"))
        object.__setattr__(
            self, "lease_token", _required_text(self.lease_token, "lease_token")
        )
        object.__setattr__(
            self,
            "lease_generation",
            _positive_int(self.lease_generation, "lease_generation"),
        )
        _utc_datetime(self.lease_expires_at, "lease_expires_at")
        object.__setattr__(
            self,
            "bounded_item_budget",
            _positive_int(self.bounded_item_budget, "bounded_item_budget"),
        )
        wall_clock = float(self.bounded_wall_clock_budget)
        if not math.isfinite(wall_clock) or wall_clock <= 0:
            raise ValueError("bounded_wall_clock_budget must be finite and positive")
        object.__setattr__(self, "bounded_wall_clock_budget", wall_clock)

    def expired(self, *, now: datetime | None = None) -> bool:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return current >= _utc_datetime(self.lease_expires_at, "lease_expires_at")

    def matches(
        self,
        *,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        now: datetime | None = None,
    ) -> bool:
        return (
            not self.expired(now=now)
            and self.worker_id == str(worker_id or "")
            and self.lease_token == str(lease_token or "")
            and self.lease_generation == int(lease_generation)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "lease_token": self.lease_token,
            "lease_generation": self.lease_generation,
            "lease_expires_at": self.lease_expires_at,
            "bounded_item_budget": self.bounded_item_budget,
            "bounded_wall_clock_budget": self.bounded_wall_clock_budget,
        }


def next_lease_generation(previous: DurableWorkLease | None) -> int:
    """Return the only valid generation for a new or reclaimed lease."""

    return 1 if previous is None else previous.lease_generation + 1


def validate_replacement_lease(
    previous: DurableWorkLease | None, replacement: DurableWorkLease
) -> None:
    expected_generation = next_lease_generation(previous)
    if replacement.lease_generation != expected_generation:
        raise ValueError(
            "replacement lease generation must advance exactly once: "
            f"expected {expected_generation}, got {replacement.lease_generation}"
        )
    if previous is not None and replacement.lease_token == previous.lease_token:
        raise ValueError("replacement lease must use a new immutable token")


@dataclass(frozen=True, slots=True)
class DurableWorkItem:
    item_identity: str
    state: str
    attempt: int
    max_attempts: int
    not_before: str = ""
    last_error_class: str = ""
    last_error_code: str = ""
    last_progress_at: str = ""
    receipt: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "item_identity",
            _required_text(self.item_identity, "item_identity"),
        )
        state = str(self.state or "").strip().lower()
        if state not in DURABLE_WORK_ITEM_STATES:
            raise ValueError(f"unsupported durable work item state: {state or '<empty>'}")
        object.__setattr__(self, "state", state)
        attempt = _non_negative_int(self.attempt, "attempt")
        max_attempts = _positive_int(self.max_attempts, "max_attempts")
        if attempt > max_attempts:
            raise ValueError("attempt cannot exceed max_attempts")
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "max_attempts", max_attempts)
        if self.not_before:
            _utc_datetime(self.not_before, "not_before")
        if self.last_progress_at:
            _utc_datetime(self.last_progress_at, "last_progress_at")
        error_class = str(self.last_error_class or "").strip().lower()
        if error_class and error_class not in DURABLE_WORK_RETRY_CLASSES:
            raise ValueError(f"unsupported durable work error class: {error_class}")
        object.__setattr__(self, "last_error_class", error_class)
        object.__setattr__(self, "last_error_code", str(self.last_error_code or ""))
        object.__setattr__(self, "receipt", _frozen_mapping(self.receipt))

    @property
    def terminal(self) -> bool:
        return self.state in DURABLE_WORK_TERMINAL_STATES

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_identity": self.item_identity,
            "state": self.state,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "not_before": self.not_before,
            "last_error_class": self.last_error_class,
            "last_error_code": self.last_error_code,
            "last_progress_at": self.last_progress_at,
            "receipt": _thaw_value(self.receipt),
        }


@dataclass(frozen=True, slots=True)
class DurableWorkBatchResult:
    attempted: int
    completed: int
    retried: int
    poisoned: int
    cancelled: int
    superseded: int
    cursor_before: int
    cursor_after: int

    def __post_init__(self) -> None:
        for name in (
            "attempted",
            "completed",
            "retried",
            "poisoned",
            "cancelled",
            "superseded",
            "cursor_before",
            "cursor_after",
        ):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))
        if self.cursor_after < self.cursor_before:
            raise ValueError("durable work cursor must be monotonic")
        outcomes = (
            self.completed
            + self.retried
            + self.poisoned
            + self.cancelled
            + self.superseded
        )
        if outcomes > self.attempted:
            raise ValueError("batch outcomes cannot exceed attempted items")


class DurableWorkError(RuntimeError):
    """Domain-neutral failure carrying a stable retry class and safe code."""

    def __init__(
        self, message: str, *, retry_class: str | DurableWorkRetryClass, code: str
    ) -> None:
        normalized = str(retry_class).strip().lower()
        if normalized not in DURABLE_WORK_RETRY_CLASSES:
            raise ValueError(f"unsupported durable work retry class: {normalized}")
        self.retry_class = normalized
        self.code = _required_text(code, "code")
        super().__init__(str(message or self.code))


def durable_work_health(
    *,
    domain_type: str,
    state: str,
    reason_code: str,
    item_counts: Mapping[str, Any] | None = None,
    oldest_age_seconds: float = 0.0,
    last_progress_at: str = "",
    progress_rate: float = 0.0,
    lease_expirations: int = 0,
    lock_contention: int = 0,
    auto_recoverable: bool,
    operator_action_required: bool,
    fairness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shared content-free Doctor envelope for a durable domain."""

    domain = _required_text(domain_type, "domain_type")
    normalized_state = str(state or "").strip().lower()
    if normalized_state not in DURABLE_WORK_HEALTH_STATES:
        raise ValueError(f"unsupported durable work health state: {normalized_state}")
    if last_progress_at:
        _utc_datetime(last_progress_at, "last_progress_at")
    age = float(oldest_age_seconds)
    rate = float(progress_rate)
    if not math.isfinite(age) or not math.isfinite(rate) or age < 0 or rate < 0:
        raise ValueError(
            "oldest_age_seconds and progress_rate must be finite and non-negative"
        )
    counts = {
        item_state: _non_negative_int(
            (item_counts or {}).get(item_state, 0), f"item_counts.{item_state}"
        )
        for item_state in sorted(DURABLE_WORK_ITEM_STATES)
    }
    runnable = sum(counts[key] for key in ("pending", "processing", "retry"))
    terminal = sum(counts[key] for key in DURABLE_WORK_TERMINAL_STATES)
    return {
        "schema_version": DURABLE_WORK_SCHEMA_VERSION,
        "domain_type": domain,
        "state": normalized_state,
        "reason_code": str(reason_code or "unspecified").strip().lower(),
        "item_counts": counts,
        "runnable_count": runnable,
        "terminal_count": terminal,
        "oldest_age_seconds": round(age, 3),
        "last_progress_at": str(last_progress_at or ""),
        "progress_rate": round(rate, 6),
        "lease_expirations": _non_negative_int(
            lease_expirations, "lease_expirations"
        ),
        "lock_contention": _non_negative_int(lock_contention, "lock_contention"),
        "auto_recoverable": bool(auto_recoverable),
        "operator_action_required": bool(operator_action_required),
        "fairness": dict(fairness or {}),
    }


@runtime_checkable
class DurableWorkAdapter(Protocol):
    """Behavioral adapter; each domain retains its own physical persistence."""

    domain_type: str

    def describe(self, work_id: str) -> DurableWorkDescriptor | None: ...

    def claim(
        self,
        *,
        worker_id: str,
        bounded_item_budget: int,
        bounded_wall_clock_budget: float,
    ) -> DurableWorkLease | None: ...

    def process(self, lease: DurableWorkLease) -> DurableWorkBatchResult: ...

    def health(self) -> Mapping[str, Any]: ...

    def shutdown(self) -> None: ...


__all__ = [
    "DURABLE_WORK_HEALTH_STATES",
    "DURABLE_WORK_ITEM_STATES",
    "DURABLE_WORK_RETRY_CLASSES",
    "DURABLE_WORK_SCHEMA_VERSION",
    "DURABLE_WORK_TERMINAL_STATES",
    "DurableWorkAdapter",
    "DurableWorkBatchResult",
    "DurableWorkDescriptor",
    "DurableWorkError",
    "DurableWorkItem",
    "DurableWorkItemState",
    "DurableWorkLease",
    "DurableWorkRetryClass",
    "canonical_snapshot_hash",
    "durable_work_health",
    "next_lease_generation",
    "validate_item_transition",
    "validate_replacement_lease",
]
