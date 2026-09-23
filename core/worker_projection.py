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
from .work_storage import CAPACITY_REFUSALS
from .worker_outcomes import (
    BUDGET_PAUSE_ERRORS,
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
#: A group's port methods per subject, and the keyword each group publish takes its subjects by.
_GROUP_METHODS = {"source": ("prepare_sources", "publish_sources", "sources"),
                  "claim": ("prepare_claims", "publish_claims", "claims")}
#: Members recorded per write transaction when a group completes.  One transaction for a
#: thousand would hold the store's write lock for as long as a capture waits for it.
GROUP_RECORD_CHUNK = 200


def _subject_kind(item) -> str:
    return "claim" if item.subject_ref.startswith("claim-") else "source"


class EmbedGroupRefused(Exception):
    """The provider refused a group's request for capacity or budget; no member was tried."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _provider_refusal(error_code: str) -> bool:
    """Whether a failed request says "ask less", which the group's size does not change."""
    return error_code in BUDGET_PAUSE_ERRORS or error_code.lower() in CAPACITY_REFUSALS


def prepare_embed_group(storage, clock, context, items, *, embed, started: float, budget: float) -> dict:
    """Embed the group's live sources and claims, each kind in as few requests as it allows.

    The result is keyed by subject.  A subject missing from it is prepared on its own, which is
    also what happens when the port cannot batch a kind or a request fails for its payload's
    sake.  Sharing a request changes what the provider is asked, never what is read, fenced or
    published: every item still answers for itself.

    A refusal that is the provider's -- capacity or budget -- raises ``EmbedGroupRefused``
    instead.  Asking again one member at a time is the same refusal times the group's size:
    a group of five hundred sent that way after one refused request spent its whole pass on
    sixty-seven members and let the rest of the group's leases run out.
    """
    if len(items) < 2 or _remaining(started, clock, budget) <= 0:
        return {}
    subjects: dict[str, list] = {"source": [], "claim": []}
    try:
        with storage.read(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
            for item in items:
                kind = _subject_kind(item)
                subject = _EMBED_SUBJECTS[kind].live(tx, item.subject_ref, item.subject_revision)
                if subject is not None:
                    subjects[kind].append(subject)
    except ContractError:
        return {}
    prepared_group: dict = {}
    for kind, members in subjects.items():
        batch = getattr(embed, _GROUP_METHODS[kind][0], None)
        if callable(batch):
            prepared_group.update(_prepare_in_halves(batch, members, clock=clock, started=started, budget=budget))
    return prepared_group


def _prepare_in_halves(batch, members: list, *, clock, started: float, budget: float) -> dict:
    """Prepare ``members`` in one request, or, when it fails for its payload, each half the same way.

    One text the request guard refuses -- a message holding something shaped like a key -- or
    one the provider rejects fails the whole request.  Falling back to one request per member
    made a single such text cost the group's size in requests, and a group larger than its lease
    covers went back and failed the same way the next pass.  Halving costs a few requests and
    leaves the one bad text, alone, to fail for itself.  A half of one is left to ask for itself.
    """
    if len(members) < 2 or _remaining(started, clock, budget) <= 0:
        return {}
    try:
        prepared = batch(members, remaining_seconds=_remaining(started, clock, budget))
    except Exception as exc:
        _disposition, error_code = _port_failure(exc)
        if _provider_refusal(error_code):
            raise EmbedGroupRefused(error_code) from exc
        middle = len(members) // 2
        return {**_prepare_in_halves(batch, members[:middle], clock=clock, started=started, budget=budget),
                **_prepare_in_halves(batch, members[middle:], clock=clock, started=started, budget=budget)}
    if len(prepared) != len(members):
        return {}
    return {(subject.ref, subject.revision): vector for subject, vector in zip(members, prepared)}


def publish_embed_group(storage, clock, context, items, *, embed, prepared_group: dict,
                        started: float, budget: float) -> dict:
    """Write a group's vectors in one fenced commit per kind; answer with each written member's fence.

    The store's write API is plural and the adapter used it one row at a time, so a pass paid
    a lock-held handshake and a dataset commit per source -- nine times the cost of one commit
    carrying the group.  The fence does not weaken: the guard verifies every member's lease,
    subject and derivation while the helper holds the lock, and a group it refuses writes
    nothing, leaving every member to publish on its own fence exactly as before.

    The result maps ``(subject_ref, subject_revision)`` to the derivation fence read before the
    commit, which is what recording the member afterwards checks against.
    """
    published: dict = {}
    if len(items) < 2 or not prepared_group:
        return published
    for kind in ("source", "claim"):
        members = [item for item in items if _subject_kind(item) == kind
                   and (item.subject_ref, item.subject_revision) in prepared_group]
        publish = getattr(embed, _GROUP_METHODS[kind][1], None)
        if callable(publish) and len(members) >= 2 and _remaining(started, clock, budget) > 0:
            published.update(_publish_kind(storage, clock, context, members, kind=kind, publish=publish,
                                           prepared_group=prepared_group, started=started, budget=budget))
    return published


def _publish_kind(storage, clock, context, items, *, kind: str, publish, prepared_group: dict,
                  started: float, budget: float) -> dict:
    """One fenced commit for the group's members of one subject kind."""
    live = _EMBED_SUBJECTS[kind].live
    members, prepared, fences = [], [], {}
    try:
        with storage.read(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
            for item in items:
                subject = live(tx, item.subject_ref, item.subject_revision)
                if subject is None:
                    continue
                fences[item.work_id] = read_derivation_fence(
                    tx, scope_id=item.scope_id,
                    sources=(subject,) if kind == "source" else (),
                    claims=((subject.ref, subject.revision),) if kind == "claim" else ())
                members.append((item, subject))
                prepared.append(prepared_group[(item.subject_ref, item.subject_revision)])
    except ContractError:
        return {}
    if len(members) < 2:
        return {}
    owners = {item.lease_owner for item, _ in members}
    if len(owners) != 1:
        return {}

    def lease_guard() -> bool:
        """True only while every member of the group may still be published."""
        remaining = _remaining(started, clock, budget)
        if remaining <= 0:
            return False
        try:
            with storage.read(context, remaining_seconds=remaining) as tx:
                for item, _subject in members:
                    if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
                        return False
                    current = live(tx, item.subject_ref, item.subject_revision)
                    if current is None or current.scope_id not in context.allowed_scope_ids:
                        return False
                    if (current.project_id, current.branch_id) != (context.project_id, context.branch_id):
                        return False
                    if derivation_changed(tx, fences[item.work_id]) is not None:
                        return False
                return True
        except ContractError:
            return False

    try:
        publish(
            tuple(prepared),
            **{_GROUP_METHODS[kind][2]: tuple(subject for _item, subject in members)},
            lease_tokens=tuple(item.lease_token for item, _subject in members),
            lease_owner=next(iter(owners)),
            lease_guard=lease_guard,
            remaining_seconds=_remaining(started, clock, budget),
        )
    except Exception:
        # Nothing was written, or the guard refused the group: each member now
        # publishes on its own fence and records its own outcome.
        return {}
    return {(item.subject_ref, item.subject_revision): fences[item.work_id] for item, _subject in members}


def complete_embed_group(storage, clock, context, items, *, published: dict,
                         started: float, budget: float) -> dict:
    """Record the group's published members together; each one's outcome by work id.

    Each member used to answer for itself in three transactions -- read it, guard it, complete
    it -- after the group's requests and its one commit had done the work.  At about sixty
    milliseconds a member, a group of five hundred spent thirty seconds on that, and a group of
    a thousand outlived its sixty-second lease: vectors already paid for were dropped as stale
    and embedded again the next pass.  The checks are the single path's last ones, made under
    the write transaction that records the verdict: the subject is still live, its derivation
    has not moved since the commit's fence was read, and ``complete`` verifies the lease.  A
    chunk that fails records nothing, and its members answer for themselves as before.
    """
    members = [item for item in items if (item.subject_ref, item.subject_revision) in published]
    outcomes: dict = {}
    for start in range(0, len(members), GROUP_RECORD_CHUNK):
        if _remaining(started, clock, budget) <= 0:
            break
        chunk = members[start:start + GROUP_RECORD_CHUNK]
        recorded: dict = {}
        try:
            with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
                now = clock.utc_now()
                for item in chunk:
                    key = (item.subject_ref, item.subject_revision)
                    if _EMBED_SUBJECTS[_subject_kind(item)].live(tx, *key) is None:
                        recorded[item.work_id] = _mark_obsolete(tx, item, now)
                    elif derivation_changed(tx, published[key]) is not None:
                        recorded[item.work_id] = _epoch_changed(tx, item, now)
                    else:
                        recorded[item.work_id] = _work_result(tx.work.complete(*item.lease, now=now))
        except ContractError:
            continue
        outcomes.update(recorded)
    return outcomes


def _process_embed(
    storage,
    clock,
    context,
    item,
    *,
    embed: EmbedPort | None,
    started: float,
    budget: float,
    prepared_group: dict | None = None,
    published_group: frozenset | None = None,
) -> tuple[str, str | None, str]:
    """Embed one source or claim revision so the derived layer is searchable.

    Publication is at-most-once against a live subject.  The worker owns the
    first fence check; a port must call ``lease_guard`` again at its physical
    publication boundary, but it is never entered after a stale or deleted
    subject, or one whose suppression, blocks or identity changed, is observed
    here.  Captures and writes to other objects do not stop publication.
    """
    kind = _EMBED_SUBJECTS[_subject_kind(item)]
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
    prepared = None if prepared_group is None else prepared_group.get((item.subject_ref, item.subject_revision))
    if prepared is None:
        try:
            prepared = prepare(subject, remaining_seconds=_remaining(started, clock, budget))
        except Exception as exc:
            return finish(*_port_failure(exc), error_detail=_refusal_field(exc))
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
    already_published = published_group is not None and (item.subject_ref, item.subject_revision) in published_group
    try:
        if not already_published:
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
        return finish(*_port_failure(exc), error_detail=_refusal_field(exc))
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


def _refusal_field(exc: BaseException) -> str | None:
    """The field a port named when it refused, so the receipt says why.

    ``storage_unavailable`` alone told an operator nothing when the companion
    simply had no fenced write (#85); the contract field it carried never
    reached ``work_error_details``.
    """
    field = getattr(exc, "field", None) if isinstance(exc, ContractError) else None
    return field if isinstance(field, str) and field and field != "payload" else None


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
