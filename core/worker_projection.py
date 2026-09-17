"""Derived-layer processors: embed, rebuild_projection and purge.

Owned by the worker drain.  Vector and file I/O stay outside SQLite
transactions, and every physical step is fenced by the work lease.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Callable, Protocol

from ..contracts import ContractError
from .file_lock import advisory_file_lock
from .retained_artifacts import RetainedBlob, erase_retained
from .storage import StoredSource
from .worker_outcomes import (
    _deadline_result,
    _epoch_changed,
    _finalize_work,
    _mark_obsolete,
    _port_failure,
    _remaining,
    _stale,
    _work_result,
    derivation_changed,
    read_derivation_fence,
)


class EmbedPort(Protocol):
    def prepare_source(self, source: StoredSource, *, remaining_seconds: float) -> object: ...
    def publish_source(
        self,
        prepared: object,
        *,
        source: StoredSource,
        lease_token: int,
        lease_owner: str,
        lease_guard: Callable[[], bool],
        remaining_seconds: float,
    ) -> None: ...


class PurgePort(Protocol):
    """Physical active-vector purge boundary.

    Implementations run outside SQLite and must return only after the native
    delete has acknowledged the same operation.  The work lease is checked
    again before the acknowledgement is recorded in SQLite.
    """

    def purge_active(
        self,
        operation_id: str,
        *,
        receipt: dict,
        remaining_seconds: float,
    ) -> bool: ...


def _live_source(tx, ref: str, revision: int) -> StoredSource | None:
    """The source revision while it is visible and still the current one."""
    source = tx.source(ref, revision)
    if source is None:
        return None
    try:
        tx.claims.require_live_source(source.ref, source.revision)
    except ContractError:
        return None
    return source


def _live_claim(tx, ref: str, revision: int):
    """The claim version while it is visible and inside this context."""
    version = tx.claims.version(ref, revision)
    if version is None:
        return None
    try:
        tx.claims.require_target(version)
    except ContractError:
        return None
    return version


@dataclass(frozen=True)
class _EmbedSubject:
    """What differs between embedding a source event and a claim version."""

    keyword: str        # the port's keyword for the subject: publish_*(prepared, source=... | claim=...)
    live: Callable      # (tx, ref, revision) -> the subject while it is live, else None
    prepare: str        # port method names; a port may support sources only
    publish: str


#: One work type, two subjects.  The ref prefix is the discriminator the rest
#: of the core already uses for object identity.
_EMBED_SUBJECTS = {
    "source": _EmbedSubject("source", _live_source, "prepare_source", "publish_source"),
    "claim": _EmbedSubject("claim", _live_claim, "prepare_claim", "publish_claim"),
}


def _process_embed(
    storage,
    clock,
    context,
    item,
    *,
    embed: EmbedPort | None,
    started: float,
    budget: float,
) -> tuple[str, str | None, str]:
    """Embed one source or claim revision so the derived layer is searchable.

    Publication is at-most-once against a live subject.  The worker owns the
    first fence check; a port must call ``lease_guard`` again at its physical
    publication boundary, but it is never entered after a stale or deleted
    subject, or one whose suppression, blocks or identity changed, is observed
    here.  Captures and writes to other objects do not stop publication.
    """
    kind = _EMBED_SUBJECTS["claim" if item.subject_ref.startswith("claim-") else "source"]
    finish = partial(_finalize_work, storage, clock, context, item, started=started, budget=budget)
    with storage.read(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        subject = kind.live(tx, item.subject_ref, item.subject_revision)
        dependencies = None if subject is None else read_derivation_fence(
            tx, scope_id=item.scope_id,
            sources=(subject,) if kind.keyword == "source" else (),
            claims=((subject.ref, subject.revision),) if kind.keyword == "claim" else (),
        )
    if subject is None:
        return finish("obsolete", "authority_revoked")
    prepare = getattr(embed, kind.prepare, None)
    publish = getattr(embed, kind.publish, None)
    if prepare is None or publish is None:
        # A port without support for this subject is a capability gap, not a
        # failure: the item waits instead of burning its attempts.
        return finish("retry", "model_unavailable")
    try:
        prepared = prepare(subject, remaining_seconds=_remaining(started, clock, budget))
    except Exception as exc:
        return finish(*_port_failure(exc))
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)

    dependency_changed = False

    def lease_guard() -> bool:
        nonlocal dependency_changed
        dependency_changed = False
        remaining = _remaining(started, clock, budget)
        if remaining <= 0:
            return False
        try:
            with storage.read(context, remaining_seconds=remaining) as tx:
                if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
                    return False
                current = kind.live(tx, item.subject_ref, item.subject_revision)
                if current is None or current.scope_id not in context.allowed_scope_ids:
                    return False
                if (current.project_id, current.branch_id) != (context.project_id, context.branch_id):
                    return False
                if derivation_changed(tx, dependencies) is not None:
                    dependency_changed = True
                    return False
                return True
        except ContractError:
            return False

    if not lease_guard():
        if _remaining(started, clock, budget) <= 0:
            return _deadline_result(storage, context, item)
        if dependency_changed:
            return finish("retry", "memory_epoch_changed")
        return finish("obsolete", "authority_revoked")
    try:
        publish(
            prepared,
            **{kind.keyword: subject},
            lease_token=item.lease_token,
            lease_owner=item.lease_owner,
            lease_guard=lease_guard,
            remaining_seconds=_remaining(started, clock, budget),
        )
    except Exception as exc:
        if dependency_changed and isinstance(exc, ContractError):
            return finish("retry", "memory_epoch_changed")
        return finish(*_port_failure(exc))
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        if kind.live(tx, item.subject_ref, item.subject_revision) is None:
            return _mark_obsolete(tx, item, now)
        if derivation_changed(tx, dependencies) is not None:
            return _epoch_changed(tx, item, now)
        return _work_result(tx.work.complete(*item.lease, now=now))


def _process_rebuild_projection(storage, clock, context, item, *, started: float, budget: float) -> tuple[str, str | None, str]:
    """Re-index one source, or acknowledge one claim revision's projection."""
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        if not tx.work._verify_lease(*item.lease, now=now):
            return _stale(tx, item)
        source = tx.source(item.subject_ref, item.subject_revision)
        if source is not None:
            tx.index_source(source.ref, source.revision)
        elif _live_claim(tx, item.subject_ref, item.subject_revision) is None:
            # Claims are queried from their versioned SQLite tables, so
            # completing the item is their whole projection; a missing or
            # inaccessible claim revision is obsolete, not an indexing failure.
            return _mark_obsolete(tx, item, now)
        return _work_result(tx.work.complete(*item.lease, now=now))


def _storage_code(exc: BaseException) -> str:
    return (exc.code or "storage_unavailable") if isinstance(exc, ContractError) else "storage_unavailable"


def _process_purge(
    storage,
    clock,
    context,
    item,
    *,
    purge: PurgePort | None,
    started: float,
    budget: float,
) -> tuple[str, str | None, str]:
    """Remove one delete operation's physical layers, then acknowledge them in SQLite."""
    finish = partial(_finalize_work, storage, clock, context, item, started=started, budget=budget)
    try:
        return _purge_layers(storage, clock, context, item, purge=purge, started=started, budget=budget, finish=finish)
    except TimeoutError:
        # The retained-artifact lock is contended by register_artifact; waiting
        # it out is a storage stall, not a purge failure.
        return finish("retry", "storage_unavailable")


def _purge_layers(storage, clock, context, item, *, purge, started, budget, finish):
    operation_id = item.subject_ref.rsplit(":", 1)[0]
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    lock_path = context.binding.data_directory / "scope-recall-retained.lock"
    # The same resource lock is also taken by register_artifact in the core
    # composition.  It spans the attachment inventory snapshot, physical I/O,
    # and final metadata CAS, while no SQLite transaction is held during I/O.
    with advisory_file_lock(lock_path, timeout_seconds=_remaining(started, clock, budget)):
        with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
            if not tx.work._verify_lease(*item.lease, now=now):
                return _stale(tx, item)
            receipt = tx.deletions.receipt(operation_id)
            if receipt is None:
                return _mark_obsolete(tx, item, now)
            physical_members = tx.deletions.physical_members(operation_id)
            attachment_plan = tx.deletions.attachment_plan(operation_id)
            if receipt["layers"].get("sqlite_active") != "removed":
                receipt = tx.deletions.purge_sqlite(operation_id)
            receipt = dict(receipt, physical_members=physical_members)

        if receipt["layers"].get("vector_active") != "removed":
            if purge is None:
                return finish("retry", "storage_unavailable")
            remaining = _remaining(started, clock, budget)
            if remaining <= 0:
                return _deadline_result(storage, context, item)
            try:
                acknowledged = bool(purge.purge_active(operation_id, receipt=receipt, remaining_seconds=remaining))
            except Exception as exc:
                return finish("retry", _storage_code(exc))
            if not acknowledged:
                return finish("retry", "storage_unavailable")

        # Attachment erasure is intentionally outside SQLite.  An empty plan
        # is still an inventory result and can be acknowledged as removed.
        for entry in attachment_plan.get("entries", ()):
            if _remaining(started, clock, budget) <= 0:
                return _deadline_result(storage, context, item)
            if entry.get("shared"):
                continue
            try:
                erase_retained(context.binding, RetainedBlob(**entry["blob"]))
            except Exception as exc:
                return finish("retry", _storage_code(exc))

        remaining = _remaining(started, clock, budget)
        if remaining <= 0:
            return _deadline_result(storage, context, item)
        now = clock.utc_now()
        try:
            with storage.write(context, remaining_seconds=remaining) as tx:
                if not tx.work._verify_lease(*item.lease, now=now):
                    return _stale(tx, item)
                receipt = tx.deletions.receipt(operation_id)
                if receipt is None:
                    return _mark_obsolete(tx, item, now)
                if receipt["layers"].get("vector_active") != "removed":
                    receipt = tx.deletions.mark_vector_active_removed(operation_id)
                receipt = tx.deletions.finalize_attachments(operation_id, attachment_plan, erased=True)
                if not receipt["active_content_removed"]:
                    return _work_result(tx.work.fail(*item.lease, error_code="storage_unavailable", now=now, recoverable=True))
                return _work_result(tx.work.complete(*item.lease, now=now))
        except Exception:
            # Physical layers may already be gone when the final CAS fails.
            # Keep the one work item retryable; the next pass inventories and
            # idempotently acknowledges both native and attachment layers.
            return finish("retry", "storage_unavailable")
