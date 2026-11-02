"""Capture application use case: source, lexical projection and work commit once."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, replace
from typing import Protocol, cast

from ..contracts import ContractError, SourceEvent, TrustedContext, decode_payload, import_source_fingerprint
from . import private
from .admission import AdmissionPolicy, decision_marker
from .candidate_lifecycle import CandidateSourceTrigger
from .events import prepare_capture
from .injection_guard import screen_sources
from .source_admission import admit_source, observe_admitted
from .source_records import SourceWrite
from .storage import SQLiteStorage
from .truth_connection import TruthDatabaseConnectionError
from .writer_lease import TruthWriterBusyError


class CaptureClock(Protocol):
    def utc_now(self) -> str: ...
    def monotonic(self) -> float: ...


@dataclass(frozen=True)
class CaptureReceipt:
    """Capture outcome; masked_secret_count reports replacement spans, never their values."""

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
    masked_secret_count: int = 0


def _inbox_entry_gone(tx, token: str, scope_id: str, context: TrustedContext) -> bool:
    """Whether the inbox entry a capture finishes is gone (cancelled); an entry of another scope, project or branch
    is refused."""
    pending = (
        tx._check()
        .execute("SELECT scope_id,project_id,branch_id FROM capture_inbox WHERE token=?", (token,))
        .fetchone()
    )
    if pending is None:
        return True
    if tuple(pending) != (scope_id, context.project_id, context.branch_id):
        raise ContractError("ACCESS_DENIED")
    return False


def _store_events(tx, clock: CaptureClock, prepared, scope_id: str, admission_policy, scan_budget):
    """Write all parts and screen their stable group before any admission side effect."""
    receipts = [
        tx.sources.put_source(event, scope_id=scope_id, persisted_at=clock.utc_now(), capture_gaps=prepared.gaps)
        for event in prepared.events
    ]
    eligible = screen_sources(tx, receipts, clock.utc_now(), remaining_seconds=scan_budget())
    candidates = []
    fresh = {(source.ref, source.revision) for source in receipts if source.disposition == "inserted"}
    for ref, revision in eligible:
        decision = admit_source(tx, clock, ref, revision, admission_policy, new_activity=(ref, revision) in fresh)
        if decision is not None:
            candidates.append((SourceWrite("inserted", ref, revision), decision))
    markers = [decision_marker(tx, source.ref, source.revision) for source in receipts]
    projections = [tx.sources.source_projection_status(source.ref, source.revision) for source in receipts]
    return receipts, projections, [marker for marker in markers if marker is not None], candidates


def _capture_mutations(tx, clock: CaptureClock, receipts) -> list:
    """The claim each new source corrects or confirms at once, if any."""
    from .mutate import capture_confirmation, capture_correction

    mutations = []
    for receipt in receipts:
        if receipt.disposition == "inserted":
            stored = tx.source(receipt.ref, receipt.revision)
            if stored is None:
                continue
            mutation = capture_correction(tx, stored, clock)
            if mutation is None:
                # Correction revises what is believed; confirmation
                # adopts what is not. One source is never both.
                mutation = capture_confirmation(tx, stored, clock)
            if mutation is not None:
                mutations.append(mutation)
    return mutations


def _persisted_receipt(
    prepared, receipts, projection_states, admission_markers, mutations, candidate_triggers
) -> CaptureReceipt:
    """The receipt of a committed capture: duplicate only when every part was, lexical and semantic projection
    states across the parts."""
    disposition = "duplicate" if all(r.disposition == "duplicate" for r in receipts) else "inserted"
    lexical = "ready" if all(s[0] == "ready" for s in projection_states) else "not_ready"
    semantic_states = {s[1] for s in projection_states}
    semantic = next(iter(semantic_states)) if len(semantic_states) == 1 else "partial"
    return CaptureReceipt(
        disposition,
        tuple(receipts),
        "persisted",
        lexical,
        semantic,
        prepared.gaps,
        mutation="revised" if mutations else "none",
        admission=tuple(dict.fromkeys(admission_markers)),
        candidate_triggers=tuple(candidate_triggers),
        masked_secret_count=prepared.masked_secret_count,
    )


def _private_refusal(prepared) -> CaptureReceipt:
    """The receipt of a capture that a private interval refuses at its event time."""
    return CaptureReceipt(
        "rejected",
        (),
        "not_persisted",
        "not_indexed",
        "not_scheduled",
        (*prepared.gaps, "private_mode"),
        "PRIVATE_MODE",
        masked_secret_count=prepared.masked_secret_count,
    )


def _import_context(value, context, prepared):
    """Bind admitted segments only after the original source passes its exact import attestation."""
    provenance = context.import_provenance
    if provenance is None:
        return context
    original = cast(SourceEvent, decode_payload(value))
    if import_source_fingerprint(original) not in provenance.source_fingerprints:
        raise ContractError("ACCESS_DENIED", "import_provenance")
    return replace(
        context,
        import_provenance=replace(
            provenance, source_fingerprints=frozenset(import_source_fingerprint(event) for event in prepared.events)
        ),
    )


def record_event(
    storage: SQLiteStorage,
    clock: CaptureClock,
    context: TrustedContext,
    value: SourceEvent | dict | str | bytes,
    *,
    scope_id: str,
    remaining_seconds: float = 1.0,
    admission_policy: AdmissionPolicy | None = None,
    _prepared=None,
    _inbox_token: str | None = None,
) -> CaptureReceipt:
    storage._context_check(context)
    if scope_id not in context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED")
    if type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
        raise ContractError("DEADLINE_EXCEEDED")
    started = clock.monotonic()
    prepared = prepare_capture(value, context) if _prepared is None else _prepared
    if prepared.rejection:
        return CaptureReceipt(
            "rejected", (), "not_persisted", "not_indexed", "not_scheduled", prepared.gaps, prepared.rejection
        )
    if _prepared is None:
        context = _import_context(value, context, prepared)
    try:
        with storage.write(context, remaining_seconds=remaining_seconds - (clock.monotonic() - started)) as tx:
            if _inbox_token is not None and _inbox_entry_gone(tx, _inbox_token, scope_id, context):
                return CaptureReceipt(
                    "cancelled",
                    (),
                    "not_persisted",
                    "unchanged",
                    "unchanged",
                    error_code="ACCESS_DENIED",
                    masked_secret_count=prepared.masked_secret_count,
                )
            # The event's own occurrence time decides, not the moment this write runs:
            # a replay, a retry or a transcript read after the interval ended still
            # refuses what was said while it was open.
            if private.capture_blocked(tx, context, prepared):
                if _inbox_token is not None:
                    tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (_inbox_token,))
                return _private_refusal(prepared)
            # Decided on the whole message, before any part is written: the first part alone would let a copy of a
            # deleted message through.
            tx.sources.refuse_under_a_deleted_key(prepared.events, scope_id=scope_id)
            receipts, projection_states, admission_markers, candidate_sources = _store_events(
                tx,
                clock,
                prepared,
                scope_id,
                admission_policy,
                lambda: max(0, remaining_seconds - (clock.monotonic() - started)),
            )
            mutations = _capture_mutations(tx, clock, receipts)
            candidate_triggers = observe_admitted(tx, clock, candidate_sources)
            if _inbox_token is not None:
                tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (_inbox_token,))
    except ContractError as exc:
        if exc.code == "VERSION_CONFLICT":
            return CaptureReceipt(
                "conflict",
                (),
                "not_persisted",
                "unchanged",
                "unchanged",
                error_code=exc.code,
                masked_secret_count=prepared.masked_secret_count,
            )
        raise
    except (sqlite3.Error, TruthDatabaseConnectionError, TruthWriterBusyError):
        # Commit may have succeeded before a close failure. Caller retries using
        # the stable source identity; no success is asserted on this path.
        return CaptureReceipt(
            "unavailable",
            (),
            "unknown",
            "unknown",
            "unknown",
            error_code="STORAGE_UNAVAILABLE",
            masked_secret_count=prepared.masked_secret_count,
        )
    return _persisted_receipt(prepared, receipts, projection_states, admission_markers, mutations, candidate_triggers)
