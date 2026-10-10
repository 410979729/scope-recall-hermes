"""What a busy store kept in memory for the Hermes adapter to write again.

A capture that met a busy store or ran out of time is kept, at most 16 at a time, and written again by a thread of
its own every ``_RETRY_EVERY_S`` while any is kept, at each turn's end, before a compression, at the session's end and
at shutdown; one kept longer than ``_RETRY_GIVE_UP_S`` is dropped and logged as lost.  It works on the adapter's
state under the adapter's lock (``self._adapter``) and writes through the adapter's capture."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from scope_recall.contracts import ContractError
from scope_recall.core import MemoryCore

from .authorization import build_ingress_authorizer
from .boundary import SourceIdentity
from .capture import CAPTURE_TIMEOUT_S, RetryCapture, label
from .identity import (
    HermesIdentity,
    HermesIdentityError,
    resolve_runtime_audience,
)
from .installation import assert_binding_matches_manifest
from .shared_entries import load_binding_for_home

if TYPE_CHECKING:
    from .provider import ScopeRecallHermesAdapter

#: The adapter's log name, which these lines carried before the adapter was split: a host writes it into each line,
#: and a logging configuration may name it.
_log = logging.getLogger("scope_recall.adapters.hermes.provider")

#: What the retry thread's pass may spend, off any hook's time and off Hermes' single memory worker.  At a capture's
#: own ``CAPTURE_TIMEOUT_S`` a pass would write about one of up to 16 buffered tool results, each write 1-4 s on a
#: busy shared store.  A turn's end keeps that 1 s: it runs on Hermes' memory worker, which the next turn's writes
#: queue behind.
_RETRY_PASS_SECONDS = 5.0
#: What a shutdown's last pass may spend: the thread wrote again within the last ``_RETRY_EVERY_S``, and on a busy store
#: a longer pass seldom changes the outcome while it holds up a gateway's planned stop.
SHUTDOWN_RETRY_SECONDS = 2.0
#: How long a capture is kept to retry: one that cannot be written by then is dropped and logged as lost.  Kept for
#: good, a capture of a full inbox or of an installation whose scopes changed under a running gateway was retried every
#: ``_RETRY_EVERY_S`` for the life of the process, and its thread would hold an evicted agent's adapter.
_RETRY_GIVE_UP_S = 1800.0
#: How often the retry thread writes again what the buffer holds, for as long as it holds anything.  Hermes runs
#: ``sync_turn`` only after a turn with a message and a reply: a turn it injected (a watch notification), one it
#: interrupted, or one with no reply ran no retry.  An idle agent evicted from Hermes' cache keeps its adapter without
#: a shutdown, so without this thread nothing writes the buffer again until a gateway restart drops it, and the tool
#: results it held are lost (``capture_failure`` logged once, never in the store).
_RETRY_EVERY_S = 30.0


class CaptureRetry:
    """The adapter's retry buffer and the thread that writes it again; one per adapter."""

    def __init__(self, adapter: ScopeRecallHermesAdapter) -> None:
        self._adapter = adapter
        self.captures: dict[SourceIdentity, RetryCapture] = {}
        #: The retry thread (``_run``), running while the buffer holds anything; None otherwise.  Started and
        #: cleared under the adapter's ``_lock``.
        self.thread: threading.Thread | None = None
        #: Set while a retry pass runs, under the adapter's ``_lock``: one pass at a time writes the buffer (``write_buffered``).
        self.retrying = False
        #: Set by ``shutdown`` to end the retry thread's wait at once.
        self.wake = threading.Event()
        #: Buffered captures a pass is writing without ``_lock``: a shutdown's pass, which may overlap it, skips them.
        self.in_flight: set[SourceIdentity] = set()

    def write_observed(self, *, deadline: float | None = None) -> None:
        """Retry only previously observed DTOs with their original identities.

        Raw history without event IDs is deliberately not promoted to new user
        evidence.  This also avoids re-saving compacted summaries as originals.
        Both retry paths use the caller's deadline, including time already spent
        waiting for the adapter lock; neither starts a fresh wait after it expires.
        """
        deadline = time.monotonic() + 2 * CAPTURE_TIMEOUT_S if deadline is None else deadline
        identity = self._adapter._require_identity()
        core = self._adapter._require_core()
        remaining = min(CAPTURE_TIMEOUT_S, deadline - time.monotonic())
        if remaining <= 0:
            return
        if isinstance(core, MemoryCore) and not identity.read_only:
            try:
                from ...core.capture_inbox import INGRESS_PENDING_GAP, replay_inbox

                receipts = replay_inbox(
                    core.storage,
                    core.clock,
                    identity.trusted_context(),
                    authorize=build_ingress_authorizer(identity.binding),
                    admission_policy=core.config.admission_policy,
                    remaining_seconds=remaining,
                )
            except (ContractError, OSError, RuntimeError, sqlite3.Error, ValueError):
                self._adapter._merge_gaps(("capture_gap:durable_ingress_pending",))
            else:
                # A busy store stops the replay's page with a receipt that says so, where it used to raise.
                if any(INGRESS_PENDING_GAP in receipt.gaps for receipt in receipts):
                    self._adapter._merge_gaps((INGRESS_PENDING_GAP,))
        self.write_buffered(deadline=deadline)

    def write_buffered(
        self,
        *,
        release: bool = False,
        seconds: float = CAPTURE_TIMEOUT_S,
        force: bool = False,
        deadline: float | None = None,
    ) -> None:
        """Write again what a busy store kept in memory; nothing to do, and nothing opened, when it holds none.

        Run at every ``sync_turn``, by the retry thread while the buffer holds anything (``_run``), at the
        session's end, before a compression and at shutdown: waiting for the turns left a capture that timed out on
        the writer lease in memory for hours, and an eviction or a restart lost it.  One pass at a time; ``force`` is
        shutdown's, which may run while another pass waits for the lock between two captures.  One retry per hold of
        the lock; with ``release`` (``sync_turn`` and the retry thread, which do not hold it) each writes without.
        A caller's earlier deadline caps the pass, measured before acquiring the lock.
        """
        pass_deadline = time.monotonic() + seconds
        deadline = pass_deadline if deadline is None else min(deadline, pass_deadline)
        with self._adapter._lock:
            if time.monotonic() >= deadline or not self.captures or (self.retrying and not force):
                return
            identity = self._adapter._require_identity()
            pending_items = tuple(self.captures.items())
            self.retrying = True
        try:
            self._pass(identity, pending_items, deadline=deadline, release=release, force=force)
        finally:
            with self._adapter._lock:
                self.retrying = False

    def _pass(
        self, identity: HermesIdentity, pending_items: tuple, *, deadline: float, release: bool, force: bool
    ) -> None:
        try:
            manifest = load_binding_for_home(identity.hermes_home)
            assert_binding_matches_manifest(identity.binding, manifest)
        except (HermesIdentityError, ContractError, OSError, ValueError, TypeError):
            with self._adapter._lock:
                self._adapter._merge_gaps(("capture_gap:retry_authorization_unverified",))
                expired = self.give_up_expired(pending_items)
            if len(expired) < len(pending_items):
                _log.warning(
                    "scope-recall: %d buffered capture(s) not written again now: their authorization could not be read",
                    len(pending_items) - len(expired),
                )
            return
        for key, pending in pending_items:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with self._adapter._lock, self._adapter._calls.holding("retry_buffered_captures"):
                if self._adapter._identity is not identity or not (self._adapter._initialized or force):
                    # A session switch or a shutdown came in between: what is left stays for the next pass.
                    break
                # A capture another pass is writing (a shutdown's pass may overlap the thread's) is left to it.
                if self.captures.get(key) is not pending or key in self.in_flight:
                    continue
                if self.give_up_expired(((key, pending),)):
                    continue
                # The capture's own audience as the manifest grants it now: a scope taken away since is not written
                # to.  Not the current session's: kept across a session switch, a direct message's tool result written
                # again while the agent served a group would have been dropped as revoked.
                try:
                    audience = resolve_runtime_audience(manifest, pending.host_scope)
                except (HermesIdentityError, ContractError, ValueError, TypeError):
                    audience = None
                allowed_scopes = (
                    pending.context.allowed_scope_ids & audience.writable_scope_ids
                    if audience is not None
                    else frozenset()
                )
                if pending.context.binding != identity.binding or pending.scope_id not in allowed_scopes:
                    self.captures.pop(key, None)
                    self._adapter._ledger.rollback(key)
                    self._adapter._merge_gaps(("capture_gap:retry_authorization_revoked",))
                    _log.warning("scope-recall: not stored (authorization revoked), dropped: %s", label(key))
                    continue
                # Not the current session's read-only state either: a capture is buffered only after a write its own
                # session was allowed, and one kept while a read-only session (an unknown user, a delegated agent)
                # was current waited and was dropped at shutdown.
                # Narrow current authorization only. Original actor, session,
                # project, branch, occurrence time and DTO identity stay intact.
                context = replace(pending.context, allowed_scope_ids=frozenset(allowed_scopes))
                # Lock contention and authorization consume the same pass, not a new write budget.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.in_flight.add(key)
                try:
                    self._adapter._writer.write(
                        context,
                        pending.event,
                        identity=key,
                        gaps=pending.gaps,
                        scope_id=pending.scope_id,
                        remaining_seconds=remaining,
                        replay=True,
                        bound=identity,
                        release=release,
                    )
                finally:
                    self.in_flight.discard(key)

    def give_up_expired(self, items) -> list:
        """Drop the buffered captures kept longer than ``_RETRY_GIVE_UP_S``, each logged as lost; the caller holds
        ``_lock``.  Returns their keys."""
        expired = [
            key
            for key, pending in items
            if self.captures.get(key) is pending
            and key not in self.in_flight
            and time.monotonic() - pending.kept_at > _RETRY_GIVE_UP_S
        ]
        for key in expired:
            self.captures.pop(key, None)
            self._adapter._ledger.rollback(key)
            self._adapter._merge_gaps(("capture_gap:retry_gave_up",))
            _log.warning(
                "scope-recall: not stored (still failing after %d minutes), lost: %s",
                int(_RETRY_GIVE_UP_S // 60),
                label(key),
            )
        return expired

    def start(self) -> None:
        """Start the retry thread when none runs; the caller holds ``_lock``."""
        if self.thread is not None:
            return
        self.wake.clear()
        thread = threading.Thread(target=self._run, name="scope-recall-capture-retry", daemon=True)
        self.thread = thread
        thread.start()

    def _run(self) -> None:
        """Write the buffer again every ``_RETRY_EVERY_S``, off any hook's time, until it is empty or the adapter is
        shut down: Hermes runs no ``sync_turn`` after a turn it injected, interrupted or got no reply for."""
        while True:
            if self.wake.wait(_RETRY_EVERY_S):
                self.wake.clear()
            with self._adapter._lock:
                if not self._adapter._initialized or not self.captures:
                    self.thread = None
                    return
            try:
                self.write_buffered(release=True, seconds=_RETRY_PASS_SECONDS)
            except Exception as exc:  # noqa: BLE001 - the next pass, a turn's end or the shutdown writes it
                _log.warning("scope-recall: a retry of buffered captures failed (%s)", type(exc).__name__)
                # A pass that raised before its captures' own check still gives up the expired ones: it would hold
                # them, and an evicted agent's adapter, for good.
                with self._adapter._lock:
                    self.give_up_expired(tuple(self.captures.items()))

    def durable_pending_count(self):
        if not isinstance(self._adapter._core, MemoryCore) or self._adapter._identity is None:
            return None
        try:
            context = self._adapter._identity.trusted_context()
            scopes = sorted(context.allowed_scope_ids)
            with self._adapter._core.storage.read(context, remaining_seconds=0.1) as tx:
                return (
                    tx._check()
                    .execute(
                        f"SELECT count(*) FROM capture_inbox WHERE scope_id IN ({','.join('?' for _ in scopes)}) AND project_id IS ? AND branch_id IS ?",
                        (*scopes, context.project_id, context.branch_id),
                    )
                    .fetchone()[0]
                )
        except (ContractError, OSError, RuntimeError, sqlite3.Error):
            return None

    def pending_identities(self) -> tuple[SourceIdentity, ...]:
        with self._adapter._lock:
            # Failed writes roll back the observation ledger, but their DTO
            # may still occupy the bounded memory retry buffer.
            return tuple(sorted(set(self._adapter._ledger.pending_identities()) | set(self.captures)))
