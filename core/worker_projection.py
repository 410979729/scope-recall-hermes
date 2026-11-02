"""Derived-layer processors: embed, rebuild_projection and purge.

Owned by the worker drain.  Vector and file I/O stay outside SQLite
transactions, and every physical step is fenced by the work lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, Protocol

from ..contracts import ContractError
from . import private
from .claims import evidence_refs
from .delete_storage import purge_finished, purge_work_parts
from .entry_control import entry_fence, paused_since, sources_paused, state_for
from .failure_policy import BUDGET_PAUSE_ERRORS, CAPACITY_REFUSALS
from .file_lock import advisory_file_lock
from .reflection_storage import reflection_job
from .retained_artifacts import RetainedBlob, erase_retained
from .source_records import StoredSource
from .worker_entities import defer_held_entity, needs_entity_projection, process_entity_projection
from .worker_outcomes import (
    budget_left,
    deadline_result,
    derivation_changed,
    embedding_prepare_budget,
    epoch_changed,
    finalize_work,
    hold_for_entry_state,
    hold_for_private_mode,
    mark_obsolete,
    port_failure,
    read_derivation_fence,
    stale_result,
    work_result,
)
from .worker_reflection import process_reflection
from .worker_summaries import is_summary_job, process_summary

if TYPE_CHECKING:
    from .worker import WorkerConfig


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

    keyword: str  # the port's keyword for the subject: publish_*(prepared, source=... | claim=...)
    live: Callable  # (tx, ref, revision) -> the subject while it is live, else None
    prepare: str  # port method names; a port may support sources only
    publish: str


#: One work type, two subjects.  The ref prefix is the discriminator the rest
#: of the core already uses for object identity.
_EMBED_SUBJECTS = {
    "source": _EmbedSubject("source", _live_source, "prepare_source", "publish_source"),
    "claim": _EmbedSubject("claim", _live_claim, "prepare_claim", "publish_claim"),
}
#: A group's port methods per subject, and the keyword each group publish takes its subjects by.
_GROUP_METHODS = {
    "source": ("prepare_sources", "publish_sources", "sources"),
    "claim": ("prepare_claims", "publish_claims", "claims"),
}
#: Members recorded per write transaction when a group completes.  One transaction for a
#: thousand would hold the store's write lock for as long as a capture waits for it.
GROUP_RECORD_CHUNK = 200


def _embed_sources(tx, kind, subject):
    """Resolve the entry-owned evidence behind a source or fact projection."""
    if kind == "source":
        return (subject,)
    return tuple(tx.claims.live_sources(evidence_refs(subject.payload), subject.scope_id))


def _embed_entries_blocked(storage, context, subjects, kind):
    """Check each projection's source owner without confusing privacy reasons."""
    with storage.read(context, remaining_seconds=1.0) as tx:
        return any(sources_paused(tx, _embed_sources(tx, kind, subject)) for subject in subjects)


def _embed_hold_reason(storage, clock, context, subject, kind):
    """Keep private and entry refusals distinct before a single preparation."""
    if private.outbound_blocked(storage, clock, context, **{("sources" if kind == "source" else "claims"): (subject,)}):
        return "private_mode"
    return "entry_paused" if _embed_entries_blocked(storage, context, (subject,), kind) else None


def _embed_blocked(storage, clock, context, subjects, kind):
    """Recheck entry and privacy authority before a batch leaves the process."""
    return _embed_entries_blocked(storage, context, subjects, kind) or private.outbound_blocked(
        storage, clock, context, **{("sources" if kind == "source" else "claims"): subjects}
    )


def _subject_kind(item) -> str:
    return "claim" if item.subject_ref.startswith("claim-") else "source"


class PreparedEmbeddingGroup(dict):
    """Prepared vectors with the source-entry versions observed before dispatch."""

    def __init__(self, values=(), *, entry_fences=None):
        super().__init__(values)
        self.entry_fences = entry_fences or {}


class PartialEmbeddingFailure(Exception):
    """A single-request fallback stopped after preparing an identified prefix, before its failing member."""

    def __init__(self, prepared: tuple, cause: Exception) -> None:
        super().__init__(type(cause).__name__)
        self.prepared = prepared
        self.cause = cause


class EmbedGroupFailure(Exception):
    """A stopped preparation retains successful vectors and identifies an attempted failing subject, if any."""

    def __init__(self, error_code: str, *, disposition="retry", prepared=None, failed_subject=None) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.disposition = disposition
        self.prepared = {} if prepared is None else prepared
        self.failed_subject = failed_subject


def _provider_refusal(error_code: str) -> bool:
    """Whether a failed request says "ask less", which the group's size does not change."""
    return error_code in BUDGET_PAUSE_ERRORS or error_code.lower() in CAPACITY_REFUSALS


def prepare_embed_group(storage, clock, context, items, *, embed, started: float, budget: float) -> dict:
    """Embed the group's live sources and claims, each kind in as few requests as it allows.

    The result is keyed by subject.  A subject missing from it is prepared on its own, which is
    also what happens when the port cannot batch a kind or a request fails for its payload's
    sake.  Sharing a request changes what the provider is asked, never what is read, fenced or
    published: every item still answers for itself.

    A refusal that is the provider's -- capacity or budget -- raises ``EmbedGroupFailure``
    instead.  Asking again one member at a time is the same refusal times the group's size:
    a group of five hundred sent that way after one refused request would spend its whole pass
    on sixty-seven members and let the rest of the group's leases run out.
    """
    if len(items) < 2 or budget_left(started, clock, budget) <= 0:
        return {}
    subjects: dict[str, list] = {"source": [], "claim": []}
    entry_fences = {}
    try:
        with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
            intervals = private.active_intervals(tx, clock.utc_now())
            for item in items:
                kind = _subject_kind(item)
                subject = _EMBED_SUBJECTS[kind].live(tx, item.subject_ref, item.subject_revision)
                if subject is None:
                    continue
                blocked = (
                    private.source_blocked(intervals, subject)
                    if kind == "source"
                    else private.claim_blocked(tx, intervals, subject)
                )
                roots = _embed_sources(tx, kind, subject)
                if not blocked and not sources_paused(tx, roots):
                    subjects[kind].append(subject)
                    entry_fences[(subject.ref, subject.revision)] = entry_fence(tx, roots)
    except ContractError:
        return {}
    prepared_group = PreparedEmbeddingGroup(entry_fences=entry_fences)
    for kind, members in subjects.items():
        batch = getattr(embed, _GROUP_METHODS[kind][0], None)
        if callable(batch):
            try:
                prepared_group.update(
                    _prepare_in_halves(
                        batch,
                        members,
                        clock=clock,
                        started=started,
                        budget=budget,
                        blocked=lambda subjects, kind=kind: _embed_blocked(storage, clock, context, subjects, kind),
                    )
                )
            except EmbedGroupFailure as failure:
                failure.prepared = PreparedEmbeddingGroup(prepared_group | failure.prepared, entry_fences=entry_fences)
                raise
    return prepared_group


def _prepare_in_halves(batch, members: list, *, clock, started: float, budget: float, blocked) -> dict:
    """Prepare ``members`` in one request, or, when it fails for its payload, each half the same way.

    One text the request guard refuses -- a message holding something shaped like a key -- or
    one the provider rejects fails the whole request.  Falling back to one request per member
    would make a single such text cost the group's size in requests, and a group larger than its
    lease covers would go back and fail the same way the next pass.  Halving costs a few requests
    and leaves the one bad text, alone, to fail for itself.  A half of one is left to ask for
    itself.
    """
    if len(members) < 2 or budget_left(started, clock, budget) <= 0:
        return {}
    if blocked(members):
        return {}
    try:
        with private.using_outbound_check(lambda: blocked(members)):
            prepared = batch(members, remaining_seconds=budget_left(started, clock, budget))
    except PartialEmbeddingFailure as exc:
        # Only an identified strict prefix proves which member's request failed.
        if len(exc.prepared) >= len(members):
            raise EmbedGroupFailure("DERIVATION_INVALID") from exc
        prefix = _prepared_mapping(members, exc.prepared)
        if len(prefix) != len(exc.prepared):
            raise EmbedGroupFailure("DERIVATION_INVALID") from exc
        disposition, code = port_failure(exc.cause)
        failed = members[len(prefix)]
        raise EmbedGroupFailure(
            code, disposition=disposition, prepared=prefix, failed_subject=(failed.ref, failed.revision)
        ) from exc
    except Exception as exc:
        _disposition, error_code = port_failure(exc)
        if _provider_refusal(error_code):
            raise EmbedGroupFailure(error_code) from exc
        middle = len(members) // 2
        prepared = {}
        for half in (members[:middle], members[middle:]):
            try:
                prepared.update(
                    _prepare_in_halves(batch, half, clock=clock, started=started, budget=budget, blocked=blocked)
                )
            except EmbedGroupFailure as failure:
                failure.prepared = prepared | failure.prepared
                raise
        return prepared
    return _prepared_mapping(members, prepared)


def _prepared_mapping(members, prepared) -> dict:
    """Pair a full positional batch or an identity-checked prefix with its subjects."""
    # A bounded single-request fallback may return an identified prefix. Unidentified or reordered partial
    # results cannot be paired safely, whereas full batches keep the port's ordinary positional contract.
    if len(prepared) != len(members) and (
        len(prepared) > len(members)
        or any(
            (getattr(vector, "source_ref", None), getattr(vector, "source_revision", None))
            != (subject.ref, subject.revision)
            for subject, vector in zip(members, prepared, strict=False)
        )
    ):
        return {}
    return {(subject.ref, subject.revision): vector for subject, vector in zip(members, prepared, strict=False)}


def publish_embed_group(
    storage, clock, context, items, *, embed, prepared_group: dict, started: float, budget: float
) -> dict:
    """Write a group's vectors in one fenced commit per kind; answer with each written member's fence.

    The store's write API is plural; used one row at a time, a pass would pay a lock-held
    handshake and a dataset commit per source -- nine times the cost of one commit carrying
    the group.  The fence does not weaken: the guard verifies every member's lease, subject
    and derivation while the helper holds the lock, and a group it refuses writes nothing,
    leaving every member to publish on its own fence.

    The result maps ``(subject_ref, subject_revision)`` to the derivation fence read before the
    commit, which is what recording the member afterwards checks against.
    """
    published: dict = {}
    if len(items) < 2 or not prepared_group:
        return published
    for kind in ("source", "claim"):
        members = [
            item
            for item in items
            if _subject_kind(item) == kind and (item.subject_ref, item.subject_revision) in prepared_group
        ]
        publish = getattr(embed, _GROUP_METHODS[kind][1], None)
        if callable(publish) and members and budget_left(started, clock, budget) > 0:
            published.update(
                _publish_kind(
                    storage,
                    clock,
                    context,
                    members,
                    kind=kind,
                    publish=publish,
                    prepared_group=prepared_group,
                    started=started,
                    budget=budget,
                )
            )
    return published


def _read_embed_members(tx, items, kind, prepared_group):
    """Keep only authorized prepared members and capture their publication fences."""
    live = _EMBED_SUBJECTS[kind].live
    members, prepared, fences = [], [], {}
    for item in items:
        subject = live(tx, item.subject_ref, item.subject_revision)
        if subject is None:
            continue
        roots = _embed_sources(tx, kind, subject)
        prior = getattr(prepared_group, "entry_fences", {}).get((subject.ref, subject.revision), {})
        if sources_paused(tx, roots) or paused_since(tx, prior):
            continue
        fences[item.work_id] = read_derivation_fence(
            tx,
            scope_id=item.scope_id,
            sources=roots,
            claims=((subject.ref, subject.revision),) if kind == "claim" else (),
        )
        members.append((item, subject))
        prepared.append(prepared_group[(item.subject_ref, item.subject_revision)])
    return members, prepared, fences


def _publish_kind(
    storage, clock, context, items, *, kind: str, publish, prepared_group: dict, started: float, budget: float
) -> dict:
    """One fenced commit for the group's members of one subject kind."""
    live = _EMBED_SUBJECTS[kind].live
    try:
        with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
            members, prepared, fences = _read_embed_members(tx, items, kind, prepared_group)
    except ContractError:
        return {}
    if not members:
        return {}
    owners = {item.lease_owner for item, _ in members}
    if len(owners) != 1:
        return {}

    def lease_guard() -> bool:
        """True only while every member of the group may still be published."""
        remaining = budget_left(started, clock, budget)
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
                    if sources_paused(tx, _embed_sources(tx, kind, current)):
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
            remaining_seconds=budget_left(started, clock, budget),
        )
    except Exception:
        # Nothing was written, or the guard refused the group: each member
        # publishes on its own fence and records its own outcome.
        return {}
    return {(item.subject_ref, item.subject_revision): fences[item.work_id] for item, _subject in members}


def complete_embed_group(storage, clock, context, items, *, published: dict, started: float, budget: float) -> dict:
    """Record the group's published members together; each one's outcome by work id.

    Answering for itself in three transactions -- read it, guard it, complete it -- after the
    group's requests and its one commit had done the work, each member would take about sixty
    milliseconds: thirty seconds for a group of five hundred, and a group of a thousand would
    outlive its sixty-second lease, its vectors already paid for dropped as stale and embedded
    again the next pass.  The checks are the single path's last ones, made under the write
    transaction that records the verdict: the subject is still live, its derivation has not
    moved since the commit's fence was read, and ``complete`` verifies the lease.  A chunk that
    fails records nothing, and its members answer for themselves.
    """
    members = [item for item in items if (item.subject_ref, item.subject_revision) in published]
    outcomes: dict = {}
    for start in range(0, len(members), GROUP_RECORD_CHUNK):
        if budget_left(started, clock, budget) <= 0:
            break
        chunk = members[start : start + GROUP_RECORD_CHUNK]
        recorded: dict = {}
        try:
            with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
                now = clock.utc_now()
                for item in chunk:
                    key = (item.subject_ref, item.subject_revision)
                    if _EMBED_SUBJECTS[_subject_kind(item)].live(tx, *key) is None:
                        recorded[item.work_id] = mark_obsolete(tx, item, now)
                    elif derivation_changed(tx, published[key]) is not None:
                        recorded[item.work_id] = epoch_changed(tx, item, now)
                    else:
                        recorded[item.work_id] = work_result(tx.work.complete(*item.lease, now=now))
        except ContractError:
            continue
        outcomes.update(recorded)
    return outcomes


@dataclass
class _EmbedFence:
    """The fresh lease and derivation check required at physical vector publication."""

    storage: Any
    clock: Any
    context: Any
    item: Any
    kind: _EmbedSubject
    dependencies: Any
    started: float
    budget: float
    dependency_changed: bool = False

    def __call__(self) -> bool:
        self.dependency_changed = False
        remaining = budget_left(self.started, self.clock, self.budget)
        if remaining <= 0:
            return False
        try:
            with self.storage.read(self.context, remaining_seconds=remaining) as tx:
                if not tx.work._verify_lease(*self.item.lease, now=self.clock.utc_now()):
                    return False
                current = self.kind.live(tx, self.item.subject_ref, self.item.subject_revision)
                if current is None or current.scope_id not in self.context.allowed_scope_ids:
                    return False
                if (current.project_id, current.branch_id) != (self.context.project_id, self.context.branch_id):
                    return False
                if sources_paused(tx, _embed_sources(tx, self.kind.keyword, current)):
                    self.dependency_changed = True
                    return False
                if derivation_changed(tx, self.dependencies) is not None:
                    self.dependency_changed = True
                    return False
                return True
        except ContractError:
            return False


def process_embed(
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
    finish = partial(finalize_work, storage, clock, context, item, started=started, budget=budget)
    with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        subject = kind.live(tx, item.subject_ref, item.subject_revision)
        roots = () if subject is None else _embed_sources(tx, kind.keyword, subject)
        prior_entries = getattr(prepared_group, "entry_fences", {}).get((item.subject_ref, item.subject_revision), {})
        entry_paused = sources_paused(tx, roots) or paused_since(tx, prior_entries)
        dependencies = (
            None
            if subject is None
            else read_derivation_fence(
                tx,
                scope_id=item.scope_id,
                sources=roots,
                claims=((subject.ref, subject.revision),) if kind.keyword == "claim" else (),
            )
        )
    if subject is None or entry_paused:
        return (
            finish("obsolete", "authority_revoked")
            if subject is None
            else hold_for_entry_state(storage, clock, context, item, started=started, budget=budget)
        )
    prepare = getattr(embed, kind.prepare, None)
    publish = getattr(embed, kind.publish, None)
    if prepare is None or publish is None:
        # A port without support for this subject is a capability gap, not a
        # failure: the item waits instead of burning its attempts.
        return finish("retry", "model_unavailable")
    prepared = None if prepared_group is None else prepared_group.get((item.subject_ref, item.subject_revision))
    if prepared is None:
        reason = _embed_hold_reason(storage, clock, context, subject, kind.keyword)
        if reason:
            hold = hold_for_private_mode if reason == "private_mode" else hold_for_entry_state
            return hold(storage, clock, context, item, started=started, budget=budget)
        try:
            with private.using_outbound_check(
                lambda: _embed_blocked(storage, clock, context, (subject,), kind.keyword)
            ):
                request_end = embedding_prepare_budget(clock, started, budget)
                prepared = prepare(subject, remaining_seconds=budget_left(started, clock, request_end))
        except Exception as exc:
            return finish(*port_failure(exc), error_detail=_refusal_field(exc))
    if budget_left(started, clock, budget) <= 0:
        return deadline_result(storage, context, item)

    lease_guard = _EmbedFence(storage, clock, context, item, kind, dependencies, started, budget)

    if not lease_guard():
        if budget_left(started, clock, budget) <= 0:
            return deadline_result(storage, context, item)
        disposition, code = (
            ("retry", "memory_epoch_changed") if lease_guard.dependency_changed else ("obsolete", "authority_revoked")
        )
        return finish(disposition, code)
    already_published = published_group is not None and (item.subject_ref, item.subject_revision) in published_group
    try:
        if not already_published:
            publish(
                prepared,
                **{kind.keyword: subject},
                lease_token=item.lease_token,
                lease_owner=item.lease_owner,
                lease_guard=lease_guard,
                remaining_seconds=budget_left(started, clock, budget),
            )
    except Exception as exc:
        if lease_guard.dependency_changed and isinstance(exc, ContractError):
            return finish("retry", "memory_epoch_changed")
        return finish(*port_failure(exc), error_detail=_refusal_field(exc))
    now = clock.utc_now()
    if budget_left(started, clock, budget) <= 0:
        return deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        if kind.live(tx, item.subject_ref, item.subject_revision) is None:
            return mark_obsolete(tx, item, now)
        if derivation_changed(tx, dependencies) is not None:
            return epoch_changed(tx, item, now)
        return work_result(tx.work.complete(*item.lease, now=now))


def _projection_entry_paused(storage, clock, context, item, started, budget):
    """Resolve source ownership without assigning an entry to unrelated work."""
    with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        row = (
            tx._check()
            .execute(
                "SELECT entry_id FROM source_events WHERE event_id=? AND source_revision=? LIMIT 1",
                (item.subject_ref, item.subject_revision),
            )
            .fetchone()
        )
        return row is not None and state_for(tx, context, entry_id=row[0])[0] == "paused"


def process_rebuild_projection(
    storage,
    clock,
    context,
    item,
    *,
    started: float,
    budget: float,
    model=None,
    config: WorkerConfig | None = None,
) -> tuple[str, str | None, str]:
    """Screen a held source before rebuilding its index or projecting a claim's entities."""
    from .source_scan import process_screening

    screening = (
        hold_for_entry_state(storage, clock, context, item, started=started, budget=budget)
        if _projection_entry_paused(storage, clock, context, item, started, budget)
        else process_screening(
            storage,
            clock,
            context,
            item,
            started=started,
            budget=budget,
            admission_policy=config.admission_policy if config is not None else None,
        )
    )
    if screening is not None:
        return screening
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        deferred = defer_held_entity(tx, item, clock.utc_now())
        if deferred is not None:
            return deferred
    with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        summary = is_summary_job(tx, item)
        reflection = reflection_job(tx, item)
    if reflection is not None or summary:
        process = process_reflection if reflection is not None else process_summary
        capable = config is None or (config.reflection_capable if reflection is not None else config.summary_capable)
        return process(
            storage,
            clock,
            context,
            item,
            model=model if capable else None,
            started=started,
            budget=budget,
        )
    if callable(getattr(model, "judge", None)):
        with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
            entity = needs_entity_projection(tx, item)
        if entity:
            return process_entity_projection(storage, clock, context, item, model=model, started=started, budget=budget)
    now = clock.utc_now()
    if budget_left(started, clock, budget) <= 0:
        return deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        if not tx.work._verify_lease(*item.lease, now=now):
            return stale_result(tx, item)
        source = tx.source(item.subject_ref, item.subject_revision)
        if source is not None and sources_paused(tx, (source,)):
            result = work_result(
                tx.work.defer_without_attempt(*item.lease, now=now, error_code="entry_paused", seconds=60),
                error_code="entry_paused",
            )
        elif source is not None:
            tx.sources.index_source(source.ref, source.revision)
            result = work_result(tx.work.complete(*item.lease, now=now))
        elif _live_claim(tx, item.subject_ref, item.subject_revision) is None:
            # Claims are read from their versioned tables; missing ones are obsolete.
            result = mark_obsolete(tx, item, now)
        else:
            result = work_result(tx.work.complete(*item.lease, now=now))
        return result


def _refusal_field(exc: BaseException) -> str | None:
    """The field a port named when it refused, so the receipt says why.

    ``storage_unavailable`` alone tells an operator nothing when the companion
    simply has no fenced write, and the contract field it carries would never
    reach ``work_error_details``.
    """
    field = getattr(exc, "field", None) if isinstance(exc, ContractError) else None
    return field if isinstance(field, str) and field and field != "payload" else None


def _storage_code(exc: BaseException) -> str:
    return (exc.code or "storage_unavailable") if isinstance(exc, ContractError) else "storage_unavailable"


def process_purge(
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
    finish = partial(finalize_work, storage, clock, context, item, started=started, budget=budget)
    try:
        return _purge_layers(storage, clock, context, item, purge=purge, started=started, budget=budget, finish=finish)
    except TimeoutError:
        # The retained-artifact lock is contended by register_artifact; waiting
        # it out is a storage stall, not a purge failure.
        return finish("retry", "storage_unavailable")


def _purge_layers(storage, clock, context, item, *, purge, started, budget, finish):
    operation_id, _scope_id = purge_work_parts(item.subject_ref)
    now = clock.utc_now()
    if budget_left(started, clock, budget) <= 0:
        return deadline_result(storage, context, item)
    lock_path = context.binding.data_directory / "scope-recall-retained.lock"
    # The same resource lock is also taken by register_artifact in the core
    # composition.  It spans the attachment inventory snapshot, physical I/O,
    # and final metadata CAS, while no SQLite transaction is held during I/O.
    with advisory_file_lock(lock_path, timeout_seconds=budget_left(started, clock, budget)):
        with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
            if not tx.work._verify_lease(*item.lease, now=now):
                return stale_result(tx, item)
            receipt = tx.deletions.receipt(operation_id)
            if receipt is None:
                return mark_obsolete(tx, item, now)
            physical_members = tx.deletions.physical_members(operation_id)
            attachment_plan = tx.deletions.attachment_plan(operation_id)
            if receipt["layers"].get("sqlite_active") != "removed":
                receipt = tx.deletions.purge_sqlite(operation_id)
            receipt = dict(receipt, physical_members=physical_members)

        if receipt["layers"].get("vector_active") != "removed":
            if purge is None:
                return finish("retry", "storage_unavailable")
            remaining = budget_left(started, clock, budget)
            if remaining <= 0:
                return deadline_result(storage, context, item)
            try:
                acknowledged = bool(purge.purge_active(operation_id, receipt=receipt, remaining_seconds=remaining))
            except Exception as exc:
                return finish("retry", _storage_code(exc))
            if not acknowledged:
                return finish("retry", "storage_unavailable")

        # Attachment erasure is intentionally outside SQLite.  An empty plan
        # is still an inventory result and can be acknowledged as removed.
        for entry in attachment_plan.get("entries", ()):
            if budget_left(started, clock, budget) <= 0:
                return deadline_result(storage, context, item)
            if entry.get("shared"):
                continue
            try:
                erase_retained(context.binding, RetainedBlob(**entry["blob"]))
            except Exception as exc:
                return finish("retry", _storage_code(exc))

        remaining = budget_left(started, clock, budget)
        if remaining <= 0:
            return deadline_result(storage, context, item)
        try:
            # The scrub is committed: truncated now, the log keeps no frame from before it, so the page images that
            # still held the deleted words leave the store's files; the frames this pass writes after it hold none of
            # them.  A reader on an older snapshot holds the truncation back, and the purge stays queued.
            truncated = storage.truncate_wal(context, remaining_seconds=remaining)
            now = clock.utc_now()
            with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
                if not tx.work._verify_lease(*item.lease, now=now):
                    return stale_result(tx, item)
                receipt = tx.deletions.receipt(operation_id)
                if receipt is None:
                    return mark_obsolete(tx, item, now)
                if receipt["layers"].get("vector_active") != "removed":
                    receipt = tx.deletions.mark_vector_active_removed(operation_id)
                receipt = tx.deletions.finalize_attachments(operation_id, attachment_plan, erased=True)
                receipt = tx.deletions.mark_sqlite_history(operation_id, removed=truncated)
                if not purge_finished(receipt):
                    return work_result(
                        tx.work.fail(*item.lease, error_code="storage_unavailable", now=now, recoverable=True)
                    )
                return work_result(tx.work.complete(*item.lease, now=now))
        except Exception:
            # Physical layers may already be gone when the final CAS fails.
            # Keep the one work item retryable; the next pass inventories and
            # idempotently acknowledges the native and attachment layers, and
            # truncates the log again.
            return finish("retry", "storage_unavailable")
