"""Capture application use case: source, lexical projection and work commit once."""
from __future__ import annotations

from dataclasses import dataclass
import math
import sqlite3
from typing import Protocol

from ..contracts import ContractError, SourceEvent, TrustedContext
from ..truth_connection import TruthDatabaseConnectionError
from ..writer_lease import TruthWriterBusyError
from .events import prepare_capture
from .storage import SQLiteStorage, SourceWrite


class CaptureClock(Protocol):
    def utc_now(self) -> str: ...
    def monotonic(self) -> float: ...


@dataclass(frozen=True)
class CaptureReceipt:
    disposition: str
    event_refs: tuple[SourceWrite, ...]
    durability: str
    lexical_state: str
    semantic_state: str
    gaps: tuple[str, ...] = ()
    error_code: str | None = None
    mutation: str = "none"


def record_event(storage: SQLiteStorage, clock: CaptureClock, context: TrustedContext,
                 value: SourceEvent | dict | str | bytes, *, scope_id: str,
                 remaining_seconds: float = 1.0) -> CaptureReceipt:
    storage._context_check(context)
    if scope_id not in context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED")
    if type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
        raise ContractError("DEADLINE_EXCEEDED")
    started = clock.monotonic()
    prepared = prepare_capture(value, context)
    if prepared.rejection:
        return CaptureReceipt("rejected", (), "not_persisted", "not_indexed", "not_scheduled", prepared.gaps, prepared.rejection)
    receipts = []
    projection_states = []
    mutations = []
    try:
        with storage.write(context, remaining_seconds=remaining_seconds-(clock.monotonic()-started)) as tx:
            for event in prepared.events:
                source = tx.put_source(event, scope_id=scope_id, persisted_at=clock.utc_now(), capture_gaps=prepared.gaps)
                receipts.append(source)
                if source.disposition == "inserted":
                    tx.claims.link_source(source.ref, source.revision)
                    tx.index_source(source.ref, source.revision)
                    tx.enqueue_source(source.ref, source.revision, work_type="consolidate", available_at=clock.utc_now())
                    tx.enqueue_source(source.ref, source.revision, work_type="embed", available_at=clock.utc_now())
                    tx.episodes.attach(tx.source(source.ref,source.revision),clock.utc_now())
                projection_states.append(tx.source_projection_status(source.ref, source.revision))
            from .mutate import capture_correction
            for receipt in receipts:
                if receipt.disposition == "inserted":
                    mutation = capture_correction(tx,tx.source(receipt.ref,receipt.revision),clock)
                    if mutation is not None:
                        mutations.append(mutation)
    except ContractError as exc:
        if exc.code == "VERSION_CONFLICT":
            return CaptureReceipt("conflict", (), "not_persisted", "unchanged", "unchanged", error_code=exc.code)
        raise
    except (sqlite3.Error, TruthDatabaseConnectionError, TruthWriterBusyError):
        # Commit may have succeeded before a close failure. Caller retries using
        # the stable source identity; no success is asserted on this path.
        return CaptureReceipt("unavailable", (), "unknown", "unknown", "unknown", error_code="STORAGE_UNAVAILABLE")
    disposition = "duplicate" if all(r.disposition == "duplicate" for r in receipts) else "inserted"
    lexical = "ready" if all(s[0] == "ready" for s in projection_states) else "not_ready"
    semantic_states = {s[1] for s in projection_states}
    semantic = next(iter(semantic_states)) if len(semantic_states) == 1 else "partial"
    return CaptureReceipt(disposition, tuple(receipts), "persisted", lexical, semantic, prepared.gaps,
                          mutation="revised" if mutations else "none")
