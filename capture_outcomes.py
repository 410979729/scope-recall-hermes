"""Sanitized capture enqueue receipts and caller-visible handling.

Production turn-capture callers must observe every accepted / coalesced /
rejected / deferred result. Rejected and deferred work must leave a durable
counter or sidecar receipt that survives process restart. Receipts store
only status/reason tokens, never user content or filesystem paths.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .capture_filters import sanitize_report_text
from .file_lock import advisory_file_lock

logger = logging.getLogger(__name__)

ENQUEUE_STATUSES = frozenset({"accepted", "coalesced", "rejected", "deferred"})
SANITIZED_OUTCOME_REASONS = frozenset(
    {
        "persisted",
        "duplicate_unconsumed",
        "filtered",
        "queue_full",
        "write_authority",
        "truth_writer_busy",
        "truth_transaction_open",
        "sqlite_busy",
        "control_queue_full",
        "durable_store_unavailable",
        "not_durable",
        "invalid_result",
        "unknown",
    }
)
RECEIPT_FILENAME = "capture_outcome_receipts.json"
RECEIPT_LOCK_FILENAME = "capture_outcome_receipts.lock"
RECEIPT_RING_SIZE = 8
OUTCOME_BUSY_TIMEOUT_MS = 150
OUTCOME_CONNECT_TIMEOUT_SECONDS = 0.15


def sanitize_outcome_reason(reason: Any) -> str:
    """Return a short allow-listed reason token with no path or secret text."""

    cleaned = sanitize_report_text(str(reason or "").strip() or "unknown")
    token = cleaned.replace(" ", "_")[:64]
    if token in SANITIZED_OUTCOME_REASONS:
        return token
    return "unknown"


def provider_db_path(provider: Any) -> Path | None:
    """Return the truth DB path when it is a real filesystem file."""

    raw = getattr(provider, "_db_path", None)
    if raw:
        path = Path(raw)
        text = str(path).strip()
        if text and text != ":memory:":
            return path
    storage = getattr(provider, "_storage_dir", None)
    if storage:
        candidate = Path(storage) / "memory.sqlite3"
        if candidate.exists():
            return candidate
    return None


def sidecar_path(provider: Any) -> Path | None:
    """Return the sanitized receipt file next to the truth store, if any."""

    storage = getattr(provider, "_storage_dir", None)
    if storage:
        return Path(storage) / RECEIPT_FILENAME
    db_path = provider_db_path(provider)
    if db_path is not None:
        return db_path.with_name(RECEIPT_FILENAME)
    return None


def _empty_sidecar() -> dict[str, dict[str, int]]:
    return {"rejected": {}, "deferred": {}}


def _read_sidecar_payload(path: Path) -> dict[str, dict[str, int]]:
    if not path.is_file():
        return _empty_sidecar()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _empty_sidecar()
    payload = _empty_sidecar()
    if not isinstance(raw, dict):
        return payload
    for status in ("rejected", "deferred"):
        bucket = raw.get(status)
        if not isinstance(bucket, dict):
            continue
        for reason, count in bucket.items():
            token = sanitize_outcome_reason(reason)
            try:
                payload[status][token] = payload[status].get(token, 0) + max(
                    0, int(count or 0)
                )
            except (TypeError, ValueError):
                continue
    return payload


def sidecar_counter_totals(provider: Any) -> tuple[int, int]:
    """Return (rejected, deferred) counts from the sidecar only."""

    path = sidecar_path(provider)
    if path is None:
        return 0, 0
    payload = _read_sidecar_payload(path)
    rejected = sum(int(value or 0) for value in payload["rejected"].values())
    deferred = sum(int(value or 0) for value in payload["deferred"].values())
    return rejected, deferred


def _bump_process_counter(provider: Any, status: str, count: int) -> None:
    attr = f"_capture_queue_{status}"
    setattr(provider, attr, int(getattr(provider, attr, 0) or 0) + max(1, int(count)))


def _record_on_sqlite_file(db_path: Path, status: str, reason: str, count: int) -> bool:
    from .capture_intents import record_outcome_on_connection

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=OUTCOME_CONNECT_TIMEOUT_SECONDS)
        conn.execute(f"PRAGMA busy_timeout = {OUTCOME_BUSY_TIMEOUT_MS}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            record_outcome_on_connection(conn, status, reason, count=count)
            conn.commit()
            return True
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def _record_on_sidecar(storage_dir: Path, status: str, reason: str, count: int) -> bool:
    try:
        storage = Path(storage_dir)
        receipt_path = storage / RECEIPT_FILENAME
        lock_path = storage / RECEIPT_LOCK_FILENAME
        with advisory_file_lock(lock_path):
            payload = _read_sidecar_payload(receipt_path)
            bucket = payload.setdefault(status, {})
            bucket[reason] = int(bucket.get(reason) or 0) + max(1, int(count))
            tmp_path = receipt_path.with_name(receipt_path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            tmp_path.replace(receipt_path)
        return True
    except OSError:
        return False


def record_sanitized_outcome(
    provider: Any,
    *,
    status: str,
    reason: str,
    count: int = 1,
) -> bool:
    """Persist a rejected/deferred counter. Never writes user content.

    Uses the truth file only while the provider still holds positive write
    authority. Otherwise a sibling JSON sidecar stores the same sanitized
    tokens without crossing the truth lifecycle boundary.
    """

    if status not in {"rejected", "deferred"}:
        return False
    token = sanitize_outcome_reason(reason)
    amount = max(1, int(count))
    db_path = provider_db_path(provider)
    if db_path is not None:
        from .write_kernel import hold_positive_write_authority

        try:
            with hold_positive_write_authority(provider):
                if _record_on_sqlite_file(db_path, status, token, amount):
                    return True
        except RuntimeError:
            pass
        if _record_on_sidecar(db_path.parent, status, token, amount):
            return True
    storage = getattr(provider, "_storage_dir", None)
    if storage is not None and _record_on_sidecar(Path(storage), status, token, amount):
        return True
    _bump_process_counter(provider, status, amount)
    return False


def ensure_outcome_accounted(provider: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    """Make rejected/deferred results durable and keep accepted honest."""

    accounted = dict(result)
    status = str(accounted.get("status") or "")
    if status == "accepted" and int(accounted.get("intent_id") or 0) <= 0:
        accounted["status"] = "deferred"
        accounted["reason"] = "not_durable"
        accounted["durable_accounted"] = False
        status = "deferred"
    if status in {"rejected", "deferred"} and not accounted.get("durable_accounted"):
        # ``enqueue_store`` accounts before returning and the production
        # observer sees the same result again.  Remember even a failed durable
        # attempt so a transiently unavailable receipt sink cannot count one
        # enqueue twice (once process-local, then again in SQLite/sidecar).
        if not accounted.get("_outcome_accounting_attempted"):
            durable = record_sanitized_outcome(
                provider,
                status=status,
                reason=str(accounted.get("reason") or "unknown"),
            )
            accounted["durable_accounted"] = bool(durable)
            accounted["_outcome_accounting_attempted"] = True
    return accounted


def handle_capture_enqueue(
    provider: Any, result: Any, *, caller: str
) -> dict[str, Any]:
    """Contractual observer for every production enqueue caller.

    Does not retry inline. Surfaces a sanitized receipt on the provider and
    ensures rejected/deferred counters are durable when a store exists.
    """

    if not isinstance(result, dict) or result.get("status") not in ENQUEUE_STATUSES:
        result = {
            "status": "deferred",
            "reason": "invalid_result",
            "intent_id": None,
            "depth": 0,
            "capacity": 0,
            "durable_accounted": False,
        }
    accounted = ensure_outcome_accounted(provider, result)
    accounted.pop("_outcome_accounting_attempted", None)
    receipt = {
        "status": str(accounted.get("status") or ""),
        "reason": sanitize_outcome_reason(accounted.get("reason")),
        "caller": sanitize_report_text(str(caller or "unknown"))[:64],
        "durable_accounted": bool(accounted.get("durable_accounted")),
    }
    provider._last_capture_enqueue = receipt
    ring = list(getattr(provider, "_capture_enqueue_receipts", []) or [])
    ring.append(receipt)
    provider._capture_enqueue_receipts = ring[-RECEIPT_RING_SIZE:]
    if receipt["status"] in {"rejected", "deferred"}:
        logger.warning(
            "Scope Recall capture enqueue %s (%s) from %s",
            receipt["status"],
            receipt["reason"],
            receipt["caller"],
        )
    return accounted


__all__ = [
    "ENQUEUE_STATUSES",
    "RECEIPT_FILENAME",
    "RECEIPT_RING_SIZE",
    "SANITIZED_OUTCOME_REASONS",
    "ensure_outcome_accounted",
    "handle_capture_enqueue",
    "provider_db_path",
    "record_sanitized_outcome",
    "sanitize_outcome_reason",
    "sidecar_counter_totals",
    "sidecar_path",
]
