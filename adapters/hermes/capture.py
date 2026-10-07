"""One capture of what a Hermes session said, into the store (``CaptureWriter``): its source context, the write
(outside the adapter's lock when the caller asks), the first-seen time a replay keeps, and what the receipt means for
the observation ledger, the retry buffer, the current turn's fence, the diagnostics and the worker."""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scope_recall.contracts import ContractError, TrustedContext
from scope_recall.core import MemoryCore
from scope_recall.core.capture import CaptureReceipt
from scope_recall.core.capture_filters import sanitize_source_capture_text
from scope_recall.core.retrieval import MAX_CURRENT_SOURCE_REFS

from .boundary import SourceIdentity
from .identity import HermesIdentity, HermesRuntimeScope, host_scope_payload, trusted_source_context

if TYPE_CHECKING:
    from .provider import ScopeRecallHermesAdapter

#: The adapter's log name, which these lines carried before the adapter was split: a host writes it into each line,
#: and a logging configuration may name it.
_log = logging.getLogger("scope_recall.adapters.hermes.provider")

#: How long one capture waits for the store's writer lease.
CAPTURE_TIMEOUT_S = 1.0
GAP_CURRENT_SOURCE_REFS_LIMIT = "degraded:current_source_refs_limit"
#: What the retry buffer keeps of one capture at most (its JSON, bytes).
_RETRY_EVENT_BYTES = 262144
#: Captures the retry buffer holds at most.
_RETRY_CAPACITY = 16


def label(identity: SourceIdentity) -> str:
    """A capture's key and revision for a log line: never its content."""
    return f"{identity[0]}@{identity[1]}"[:200]


def _same_stored_content(stored_event: dict, content: object) -> bool:
    """Whether a capture repeats what is already stored under its key.

    Storage keeps the admitted text, not the host's raw text, and splits a long
    one into segments whose first holds the prefix, so the comparison runs on
    the same admitted form.
    """
    if type(content) is not str:
        return False
    admitted = sanitize_source_capture_text(content)
    stored = stored_event.get("content")
    if type(stored) is not str:
        return False
    if "segment" in stored_event:
        return admitted[: len(stored)] == stored
    return admitted == stored


@dataclass(frozen=True)
class RetryCapture:
    """What a failed write keeps to try again: the capture as it was said."""

    context: TrustedContext
    event: dict
    gaps: tuple[str, ...]
    scope_id: str
    host_scope: HermesRuntimeScope
    #: When it was kept (the monotonic clock), for ``capture_retry._RETRY_GIVE_UP_S``.
    kept_at: float


class CaptureWriter:
    """The adapter's captures; one per adapter.  It works on the adapter's state under the adapter's lock
    (``self._adapter``); a capture's own steps are its methods."""

    def __init__(self, adapter: ScopeRecallHermesAdapter) -> None:
        self._adapter = adapter

    def write(
        self,
        context,
        event,
        *,
        identity: SourceIdentity | None,
        gaps: tuple[str, ...],
        scope_id: str | None,
        remaining_seconds: float = CAPTURE_TIMEOUT_S,
        replay: bool = False,
        bound: HermesIdentity | None = None,
        release: bool = False,
    ):
        """Record one host event: the receipt, or None when nothing was written (said in the diagnostics and the
        log).  ``release``: the caller holds the adapter's lock exactly once, and the store I/O runs without it."""
        adapter = self._adapter
        if event is None:
            if gaps:
                adapter._merge_gaps(gaps)
            return None
        # The binding the capture was said under: a turn written after its reply keeps it whatever session switch
        # came in meanwhile (``sync_turn`` passes it).
        bound = adapter._require_identity() if bound is None else bound
        event = self._with_source_context(event, bound, replay)
        if not scope_id:
            self.record_failure(identity, "capability_gap")
            adapter._merge_gaps(gaps, ("capability_gap:no_capture_scope",))
            return None
        started = time.monotonic()
        snapshot = self._snapshot(context, event, identity, gaps, scope_id, bound)
        retry = adapter._retry.captures
        host_scope = retry[identity].host_scope if replay and identity in retry else bound.scope
        receipt = self._store(context, event, identity, scope_id, host_scope, remaining_seconds, started, release)
        if release and not adapter._initialized:
            return self._closed_meanwhile(identity, receipt, replay)
        if isinstance(receipt, Exception):
            return self._failed(identity, receipt, snapshot, gaps, replay)
        if receipt.durability == "queued":
            return self._queued(context, identity, receipt, gaps, replay)
        if receipt.durability != "persisted":
            return self._refused(identity, receipt, snapshot, gaps, replay)
        return self._stored(context, identity, receipt, gaps, replay)

    @staticmethod
    def _with_source_context(event, bound: HermesIdentity, replay: bool) -> dict:
        event = dict(event)
        if not replay:
            source_context = trusted_source_context(bound.scope)
            if source_context is not None:
                event["source_context"] = source_context
            else:
                event.pop("source_context", None)
        return event

    def _snapshot(self, context, event: dict, identity, gaps, scope_id: str, bound: HermesIdentity):
        """What a failed write would keep to try again, made now and kept only once a write failed for a reason that
        may pass.  Kept before the write, a capture whose store I/O runs without the lock sat in the buffer while it
        wrote, and a retry pass of the same session wrote it a second time (review of 3.5.1)."""
        if identity is None or identity in self._adapter._retry.captures:
            return None
        if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > _RETRY_EVENT_BYTES:
            return None
        return RetryCapture(context, copy.deepcopy(event), gaps, scope_id, bound.scope, time.monotonic())

    def _keep(self, identity, snapshot) -> None:
        """Keep a capture whose write may pass later in the retry buffer, or say the buffer is full."""
        retry = self._adapter._retry
        if identity is None or identity in retry.captures:
            return
        if snapshot is not None and len(retry.captures) < _RETRY_CAPACITY:
            retry.captures[identity] = snapshot
            retry.start()
        else:
            self._adapter._merge_gaps(("capture_gap:retry_buffer_full",))

    def _store(
        self, context, event: dict, identity, scope_id: str, host_scope, remaining_seconds, started, release
    ) -> CaptureReceipt | Exception:
        """The write itself: its receipt, or the exception it raised."""
        adapter = self._adapter
        holder = adapter._holder
        if release:
            # The store I/O without the lock, which the caller (``sync_turn``, a tool hook) holds exactly once.  Held
            # across the write, it was taken straight back by this thread as it released it: the next turn's start
            # got in after 2 of 14 such captures (measured on 3.4.9).  Only the store is touched until it is taken
            # again.
            adapter._captures_in_flight += 1
            adapter._holder = None
            adapter._lock.release()
        try:
            if identity is not None:
                self._keep_first_seen(context, event, identity, scope_id, remaining_seconds, started)
            core = adapter._require_core()
            if isinstance(core, MemoryCore):
                receipt = core.record_host_event(
                    context,
                    event,
                    scope_id=scope_id,
                    host_scope=host_scope_payload(host_scope),
                    remaining_seconds=max(0.001, remaining_seconds - (time.monotonic() - started)),
                )
            else:
                receipt = core.record_event(
                    context,
                    event,
                    scope_id=scope_id,
                    remaining_seconds=max(0.001, remaining_seconds - (time.monotonic() - started)),
                )
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            return exc
        finally:
            if release:
                adapter._lock.acquire()
                adapter._holder = holder and (holder[0], time.monotonic(), holder[2])
                adapter._captures_in_flight -= 1
                adapter._captures_done.notify_all()
        return receipt

    def _keep_first_seen(self, context, event: dict, identity, scope_id: str, remaining_seconds, started) -> None:
        """A replay of the same message keeps the time it was first seen.

        The bounded host cache is only an optimization; SQLite retains first-witnessed time after an old identity
        leaves that cache.  Only a replay of the same message inherits it: a restarted gateway numbers turns from 1
        again, so a different message can arrive under an old key.  Storage re-keys that one, and it must keep its
        own time.
        """
        previous = self._adapter._require_core().source_by_event_key(
            context,
            identity[0],
            identity[1],
            remaining_seconds=max(0.001, remaining_seconds - (time.monotonic() - started)),
        )
        if (
            previous is not None
            and previous.scope_id == scope_id
            and previous.session_id == context.session_id
            and previous.project_id == context.project_id
            and previous.branch_id == context.branch_id
            and _same_stored_content(previous.event, event.get("content"))
        ):
            for field in ("occurred_at", "recorded_at", "time_precision"):
                if field in previous.event:
                    event[field] = previous.event[field]

    def _closed_meanwhile(self, identity, outcome: CaptureReceipt | Exception, replay: bool):
        """A shutdown stopped waiting for this write and closed the session meanwhile.  The write itself may well
        have landed (the store is not closed with the session); its bookkeeping belongs to a closed session, and
        raised into the host's hook runner (review of 3.5.1)."""
        adapter = self._adapter
        receipt = None if isinstance(outcome, Exception) else outcome
        stored = receipt is not None and receipt.durability in ("persisted", "queued")
        if stored and identity is not None:
            adapter._ledger.confirm(identity)
            adapter._retry.captures.pop(identity, None)
        if replay and identity is not None:
            # Said "still being written" at shutdown: its end is said here (review of 3.6.1).
            if receipt is not None and stored:
                _log.info(
                    "scope-recall: %s on retry: %s",
                    "stored" if receipt.durability == "persisted" else "queued",
                    label(identity),
                )
            else:
                _log.warning("scope-recall: not stored (still failing at shutdown), lost: %s", label(identity))
        return receipt

    def _failed(self, identity, failure: Exception, snapshot, gaps, replay: bool) -> None:
        if isinstance(failure, ContractError) and failure.code not in {"DEADLINE_EXCEEDED", "STORAGE_UNAVAILABLE"}:
            self._adapter._retry.captures.pop(identity, None)
        else:
            self._keep(identity, snapshot)
        self.record_failure(identity, "exception", replay=replay)
        self._adapter._merge_gaps(gaps, ("capture_gap:write_exception",))
        return None

    def _queued(self, context, identity, receipt, gaps, replay: bool):
        adapter = self._adapter
        adapter._retry.captures.pop(identity, None)
        if identity is not None:
            # Durably queued: the worker stores it from the inbox.  Left pending, it held one of the
            # ledger's 64 slots until the session ended, and a full ledger refused every capture.
            adapter._ledger.confirm(identity)
            if replay:
                _log.info("scope-recall: queued on retry: %s", label(identity))
        adapter._merge_gaps(gaps, ("capture_gap:durable_ingress_pending",))
        adapter._wake_background_worker(context=context)
        return receipt

    def _refused(self, identity, receipt, snapshot, gaps, replay: bool):
        if receipt.disposition in {"rejected", "conflict", "cancelled"}:
            self._adapter._retry.captures.pop(identity, None)
        else:
            self._keep(identity, snapshot)
        self.record_failure(identity, receipt.error_code or receipt.disposition, replay=replay)
        self._adapter._merge_gaps(gaps, (f"capture_gap:{receipt.disposition}",))
        return receipt

    def _stored(self, context, identity, receipt, gaps, replay: bool):
        adapter = self._adapter
        if identity is not None:
            adapter._ledger.confirm(identity)
            adapter._retry.captures.pop(identity, None)
            if replay:
                # Said once, as the capture's being kept was: the pair shows what the retries saved.
                _log.info("scope-recall: stored on retry: %s", label(identity))
        if context.session_id == adapter._require_identity().stored_session_id():
            self._fence(receipt)
        if gaps:
            adapter._merge_gaps(gaps)
        adapter._wake_background_worker(context=context)
        return receipt

    def _fence(self, receipt) -> None:
        """Keep this turn's own sources out of its recall."""
        adapter = self._adapter
        for write in receipt.event_refs:
            ref = f"{write.ref}@{write.revision}"
            if ref in adapter._current_source_refs:
                continue
            if len(adapter._current_source_refs) < MAX_CURRENT_SOURCE_REFS:
                adapter._current_source_refs.append(ref)
            elif not adapter._current_source_refs_overflow:
                # A ref the fence cannot hold would let this turn's own source
                # come back as memory, so recall stays off until the refs
                # reset.  The gap goes where tool replies read it.
                adapter._current_source_refs_overflow = True
                adapter._diagnostics.capability_gaps = (
                    *adapter._diagnostics.capability_gaps,
                    GAP_CURRENT_SOURCE_REFS_LIMIT,
                )

    def record_failure(self, identity: SourceIdentity | None, reason: str, *, replay: bool = False) -> None:
        """Say a capture was not stored: in the diagnostics and the log, by its key and the failure's code only."""
        adapter = self._adapter
        if identity is not None:
            adapter._ledger.rollback(identity)
            name = f"{identity[0]}@{identity[1]}"
        else:
            name = "unknown"
        adapter._diagnostics.capture_failures = tuple(
            dict.fromkeys((*adapter._diagnostics.capture_failures, f"capture_failure:{name}:{reason}"))
        )[-64:]
        retried = identity in adapter._retry.captures
        if retried:
            adapter._merge_gaps(("capture_gap:retry_memory_only", "capability_gap:durable_capture_ingress_unavailable"))
        # The source's key and the failure's code only, never its content: a capture that failed used to leave
        # no trace outside this process's memory.  A retry that fails again, and is still kept, is not said again:
        # driven by the retry thread, that was a line per capture every 30 s; its end is said (stored, dropped, lost).
        (_log.debug if replay and retried else _log.warning)(
            "scope-recall: not stored (%s)%s: %s", reason, ", kept to retry" if retried else "", name[:200]
        )
