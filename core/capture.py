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
from .admission import AdmissionPolicy, decide, decision_marker, store_decision
from .candidate_lifecycle import CandidateSourceTrigger


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
    admission: tuple[str, ...] = ()
    candidate_triggers: tuple[CandidateSourceTrigger, ...] = ()


def record_event(storage: SQLiteStorage, clock: CaptureClock, context: TrustedContext,
                 value: SourceEvent | dict | str | bytes, *, scope_id: str,
                 remaining_seconds: float = 1.0, admission_policy: AdmissionPolicy | None = None,
                 _prepared=None, _inbox_token: str | None = None) -> CaptureReceipt:
    storage._context_check(context)
    if scope_id not in context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED")
    if type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
        raise ContractError("DEADLINE_EXCEEDED")
    started = clock.monotonic()
    prepared = prepare_capture(value, context) if _prepared is None else _prepared
    if prepared.rejection:
        return CaptureReceipt("rejected", (), "not_persisted", "not_indexed", "not_scheduled", prepared.gaps, prepared.rejection)
    receipts = []
    projection_states = []
    mutations = []
    admission_markers = []
    candidate_sources = []
    candidate_triggers = []
    try:
        with storage.write(context, remaining_seconds=remaining_seconds-(clock.monotonic()-started)) as tx:
            if _inbox_token is not None:
                pending = tx._check().execute("SELECT scope_id,project_id,branch_id FROM capture_inbox WHERE token=?", (_inbox_token,)).fetchone()
                if pending is None:
                    return CaptureReceipt("cancelled", (), "not_persisted", "unchanged", "unchanged", error_code="ACCESS_DENIED")
                if tuple(pending) != (scope_id, context.project_id, context.branch_id):
                    raise ContractError("ACCESS_DENIED")
            for event in prepared.events:
                decision = decide(tx, event, scope_id, admission_policy)
                source = tx.put_source(event, scope_id=scope_id, persisted_at=clock.utc_now(), capture_gaps=prepared.gaps)
                receipts.append(source)
                if source.disposition == "inserted":
                    candidate_sources.append((source, decision))
                    store_decision(tx, source.ref, source.revision, decision)
                    tx.claims.link_source(source.ref, source.revision)
                    tx.index_source(source.ref, source.revision)
                    for kind in sorted(decision.work_types):
                        tx.enqueue_source(source.ref, source.revision, work_type=kind, available_at=clock.utc_now())
                    tx.episodes.attach(tx.source(source.ref,source.revision),clock.utc_now())
                marker = decision_marker(tx, source.ref, source.revision)
                if marker is not None:
                    admission_markers.append(marker)
                projection_states.append(tx.source_projection_status(source.ref, source.revision))
            from .mutate import capture_confirmation, capture_correction
            for receipt in receipts:
                if receipt.disposition == "inserted":
                    stored = tx.source(receipt.ref,receipt.revision)
                    mutation = capture_correction(tx,stored,clock)
                    if mutation is None:
                        # Correction revises what is believed; confirmation
                        # adopts what is not. One source is never both.
                        mutation = capture_confirmation(tx,stored,clock)
                    if mutation is not None:
                        mutations.append(mutation)
            for source, decision in candidate_sources:
                stored = tx.source(source.ref, source.revision)
                current = tx.source_current(source.ref)
                if (decision.disposition != "source_only" and stored is not None and not stored.suppressed
                        and current is not None and current.revision == source.revision):
                    candidate_triggers.append(tx.candidates.observe_source(
                        source.ref, source.revision, observed_at=clock.utc_now(),
                    ))
            if _inbox_token is not None:
                tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (_inbox_token,))
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
                          mutation="revised" if mutations else "none", admission=tuple(dict.fromkeys(admission_markers)),
                          candidate_triggers=tuple(candidate_triggers))
