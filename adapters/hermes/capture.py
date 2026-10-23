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
from scope_recall.core.deadline import fits
from scope_recall.core.events import prepare_capture
from scope_recall.core.retrieval import MAX_CURRENT_SOURCE_REFS

from .boundary import SourceIdentity
from .identity import HermesIdentity, HermesRuntimeScope, host_scope_payload, trusted_source_context

if TYPE_CHECKING:
    from .provider import ScopeRecallHermesAdapter

#: The adapter's log name, the provider module's, which this module logs under as well: a host writes it into each
#: line, and a logging configuration may name it.
_log = logging.getLogger("scope_recall.adapters.hermes.provider")

#: How long one capture waits for the store's writer lease.
CAPTURE_TIMEOUT_S = 1.0
GAP_CURRENT_SOURCE_REFS_LIMIT = "degraded:current_source_refs_limit"
#: What the retry buffer keeps of one capture at most (its JSON, bytes).
RETRY_EVENT_BYTES = 262144
#: Captures the retry buffer holds at most.
RETRY_CAPACITY = 16


def label(identity: SourceIdentity) -> str:
    """A capture's key and revision for a log line: never its content."""
    return f"{identity[0]}@{identity[1]}"[:200]


def _may_pass(outcome: CaptureReceipt | Exception | None) -> bool:
    """Whether a write that did not store its capture may store it later: one that met a busy store or ran out of
    time, never one the store refused for good (a rejection, a conflict, a cancellation, a terminal error)."""
    if isinstance(outcome, ContractError):
        return outcome.code not in {"ACCESS_DENIED", "INPUT_INVALID", "VERSION_CONFLICT"}
    if isinstance(outcome, Exception):
        return True
    return outcome is not None and outcome.disposition not in {"rejected", "conflict", "cancelled"}


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
            return self._closed_meanwhile(identity, receipt, replay, snapshot)
        if isinstance(receipt, Exception):
            return self._failed(identity, receipt, snapshot, gaps, replay)
        if receipt.durability == "queued":
            return self._queued(context, identity, receipt, gaps, replay)
        if receipt.durability != "persisted":
            return self._refused(identity, receipt, snapshot, gaps, replay)
        return self._stored(context, identity, receipt, gaps, replay)

    def capture_guard(self, context, remaining_seconds: float, *, observations=()):
        """Read a batch's Core policy snapshot; a failed read cannot authorize a local copy."""
        reader = getattr(self._adapter._require_core(), "private_capture_guard", None)
        if reader is None:
            return None
        try:
            return reader(context, observations=observations, remaining_seconds=max(0.001, remaining_seconds))
        except (ContractError, OSError, RuntimeError, sqlite3.Error) as exc:
            return exc

    def private_state(self, context, event: dict, remaining_seconds: float, *, guard=None) -> str | None:
        """``blocked``, ``unknown`` when the rule table could not be read, or None when capture may go on.

        Test doubles without persistent ingress may omit the optional read.
        """
        if isinstance(guard, Exception):
            return "unknown"
        check = getattr(self._adapter._require_core(), "private_capture_blocked", None)
        if check is None:
            return None
        try:
            options = {"guard": guard} if guard is not None else {}
            blocked = check(context, event, remaining_seconds=max(0.001, min(0.2, remaining_seconds)), **options)
        except (ContractError, OSError, RuntimeError, sqlite3.Error):
            return "unknown"
        return "blocked" if blocked else None

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
        may pass.  Kept before the write, a capture whose store I/O runs without the lock would sit in the buffer while
        it wrote, and a retry pass of the same session would write it a second time."""
        if identity is None or identity in self._adapter._retry.captures:
            return None
        try:
            prepared = prepare_capture(event, context)
        except ContractError:
            return None
        if prepared.rejection:
            return None
        event = {
            **event,
            "content": sanitize_source_capture_text(event["content"]),
            "capture_state": prepared.events[0]["capture_state"],
        }
        gaps = tuple(dict.fromkeys((*gaps, *prepared.gaps)))
        if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > RETRY_EVENT_BYTES:
            return None
        return RetryCapture(context, copy.deepcopy(event), gaps, scope_id, bound.scope, time.monotonic())

    def defer(
        self, context, event, *, identity, gaps: tuple[str, ...], scope_id: str | None, bound, guard=None
    ) -> None:
        """Keep a capture its hook has no time left for, with no store call: in the retry buffer, as one the store
        could not take in time is kept (``_keep``), and said as that one is."""
        adapter = self._adapter
        if event is None:
            if gaps:
                adapter._merge_gaps(gaps)
            return
        if not scope_id:
            self.record_failure(identity, "capability_gap")
            adapter._merge_gaps(gaps, ("capability_gap:no_capture_scope",))
            return
        event = self._with_source_context(event, bound, False)
        self._keep(identity, self._snapshot(context, event, identity, gaps, scope_id, bound), guard=guard)
        self.record_failure(identity, "DEADLINE_EXCEEDED")
        adapter._merge_gaps(gaps, ("capture_gap:write_deferred",))

    def _keep(self, identity, snapshot, *, guard=None) -> None:
        """Keep a capture whose write may pass later in the retry buffer, or on disk while the buffer has no room
        (``CaptureRetry.keep_on_disk``), from where the buffer takes it once it has; or say it could not be kept."""
        retry = self._adapter._retry
        if identity is None or identity in retry.captures:
            return
        if snapshot is not None:
            state = self.private_state(snapshot.context, snapshot.event, 0.2, guard=guard)
            if state is not None:
                gap = "private_mode" if state == "blocked" else "private_mode_unverified"
                self._adapter._merge_gaps((f"capture_gap:{gap}",))
                return
        if snapshot is not None and len(retry.captures) < RETRY_CAPACITY:
            retry.captures[identity] = snapshot
            retry.start()
            return
        if snapshot is not None and retry.keep_on_disk(identity, snapshot, guard=guard) == "kept":
            retry.more_on_disk = True
        self._adapter._merge_gaps(("capture_gap:retry_buffer_full",))

    def _store(
        self, context, event: dict, identity, scope_id: str, host_scope, remaining_seconds, started, release
    ) -> CaptureReceipt | Exception:
        """The write itself: its receipt, or the exception it raised."""
        adapter = self._adapter
        holder = adapter._holder
        if release:
            # Release the session lock during store I/O so other turn hooks can acquire it between captures.
            # The caller (``sync_turn``, a tool hook) holds the lock exactly once; only the store is touched until
            # the lock is acquired again.
            adapter._captures_in_flight += 1
            adapter._holder = None
            adapter._lock.release()
        try:
            if identity is not None:
                looked = time.monotonic()
                self._keep_first_seen(context, event, identity, scope_id, remaining_seconds, started)
                # The lookup estimates the next store call's duration; a capture that cannot fit waits to retry.
                if not fits(started + remaining_seconds, time.monotonic() - looked):
                    raise ContractError("DEADLINE_EXCEEDED", "capture_time")
            core = adapter._require_core()
            left = remaining_seconds - (time.monotonic() - started)
            if isinstance(core, MemoryCore):
                receipt = core.record_host_event(
                    context,
                    event,
                    scope_id=scope_id,
                    host_scope=host_scope_payload(host_scope),
                    remaining_seconds=left,
                )
            else:
                receipt = core.record_event(context, event, scope_id=scope_id, remaining_seconds=left)
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
            remaining_seconds=remaining_seconds - (time.monotonic() - started),
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

    def _closed_meanwhile(self, identity, outcome: CaptureReceipt | Exception, replay: bool, snapshot):
        """A shutdown stopped waiting for this write and closed the session meanwhile.  The write itself may well
        have landed (the store is not closed with the session); its bookkeeping belongs to a closed session, and done
        there it would raise into the host's hook runner.  One that did not land and may pass later is kept on disk
        for the next start, as the shutdown keeps what its buffer holds; a replay's the shutdown kept there already."""
        adapter = self._adapter
        receipt = None if isinstance(outcome, Exception) else outcome
        stored = receipt is not None and receipt.durability in ("persisted", "queued")
        if identity is None:
            return receipt
        if receipt is not None and stored:
            adapter._ledger.confirm(identity)
            adapter._retry.done(identity)
            if replay:
                # Said "still being written" at shutdown: its end is said here.
                _log.info(
                    "scope-recall: %s on retry: %s",
                    "stored" if receipt.durability == "persisted" else "queued",
                    label(identity),
                )
            return receipt
        if not _may_pass(outcome):
            adapter._retry.done(identity)
            return receipt
        retry = adapter._retry
        disposition = retry.keep_on_disk(identity, snapshot) if snapshot is not None else "retained"
        if disposition == "discarded":
            return receipt
        if disposition == "kept":
            _log.warning(
                "scope-recall: not stored before the shutdown, kept on disk for the next start: %s", label(identity)
            )
        else:
            if snapshot is not None:
                retry.captures[identity] = snapshot
            _log.warning("scope-recall: not stored at shutdown, retained in memory: %s", label(identity))
        return receipt

    def _failed(self, identity, failure: Exception, snapshot, gaps, replay: bool) -> None:
        if _may_pass(failure):
            self._keep(identity, snapshot)
        else:
            self._adapter._retry.done(identity)
        self.record_failure(identity, "exception", replay=replay)
        self._adapter._merge_gaps(gaps, ("capture_gap:write_exception",))
        return None

    def _queued(self, context, identity, receipt, gaps, replay: bool):
        adapter = self._adapter
        adapter._retry.done(identity)
        if identity is not None:
            # Durably queued: the worker stores it from the inbox.  Left pending, it would hold one of the
            # ledger's 64 slots until the session ended, and a full ledger refuses every capture.
            adapter._ledger.confirm(identity)
            if replay:
                _log.info("scope-recall: queued on retry: %s", label(identity))
        adapter._merge_gaps(gaps, ("capture_gap:durable_ingress_pending",))
        adapter._binding.wake_worker(context=context)
        return receipt

    def _refused(self, identity, receipt, snapshot, gaps, replay: bool):
        if _may_pass(receipt):
            self._keep(identity, snapshot)
        else:
            self._adapter._retry.done(identity)
        self.record_failure(identity, receipt.error_code or receipt.disposition, replay=replay)
        self._adapter._merge_gaps(gaps, (f"capture_gap:{receipt.disposition}",))
        return receipt

    def _stored(self, context, identity, receipt, gaps, replay: bool):
        adapter = self._adapter
        if identity is not None:
            adapter._ledger.confirm(identity)
            adapter._retry.done(identity)
            if replay:
                # Said once, as the capture's being kept was: the pair shows what the retries saved.
                _log.info("scope-recall: stored on retry: %s", label(identity))
        if context.session_id == adapter._require_identity().stored_session_id():
            self._fence(receipt)
        if gaps:
            adapter._merge_gaps(gaps)
        adapter._binding.wake_worker(context=context)
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
        retried = identity in adapter._retry.captures or identity in adapter._retry.on_disk
        if retried:
            adapter._merge_gaps(("capture_gap:retry_memory_only", "capability_gap:durable_capture_ingress_unavailable"))
        # The source's key and the failure's code only, never its content: without this line, a capture that failed
        # would leave no trace outside this process's memory.  A retry that fails again, and is still kept, is logged at
        # debug level only: as a warning, the retry thread would write a line per capture every 30 s.  Its end is said
        # (stored, dropped, lost).
        (_log.debug if replay and retried else _log.warning)(
            "scope-recall: not stored (%s)%s: %s", reason, ", kept to retry" if retried else "", name[:200]
        )
