"""Entity projection processor using the existing work lease and model budget port."""

from __future__ import annotations

import json
from functools import partial

from ..contracts import ContractError
from . import private
from .aliases import ENTITY_KINDS
from .claims import evidence_refs
from .entity_names import backfill_names, checked, index_claim, ineligible, pending_names, reserve_names
from .entity_resolution import entity_messages, live_entity, publish_entity_merge
from .entry_control import entry_fence, paused_since
from .evidence_support import SupportVerdict
from .injection_guard import blocked_refs
from .retrieval import SearchContext, SearchLimits
from .semantic_proposals import _publication_blocked, judgement_result, merge_candidates
from .worker_outcomes import (
    budget_left,
    finalize_work,
    hold_for_entry_state,
    hold_for_private_mode,
    mark_obsolete,
    model_failure,
    record_error_detail,
    stale_result,
    work_result,
)


def defer_held_entity(tx, item, now):
    """Retain the existing projection job while a live fact's evidence is withheld.

    Safety holds are reversible. They must not withdraw names or acknowledge
    entity resolution; deletion fences continue to retire truly absent facts.
    """
    from .visibility import deletion_allows

    if deletion_allows(tx, "claim", item.subject_ref) and blocked_refs(tx, "claim", (item.subject_ref,)):
        return work_result(
            tx.work.defer_without_attempt(*item.lease, now=now, seconds=1, error_code="source_safety_hold"),
            error_code="source_safety_hold",
        )
    return None


def _vector_hits(storage, clock, context, source, seconds):
    """Ask only the existing scoped search interface; no vector storage is owned here."""
    port = storage.entity_vectors() if storage.entity_vectors is not None else None
    if port is None:
        return (), "entity_vector_unavailable"
    search = SearchContext(
        query=source.payload["subject"],
        mode="current",
        as_of=None,
        focus_refs=(),
        limits=SearchLimits(),
        deadline=clock.monotonic() + seconds,
        now=clock.utc_now(),
        trusted_context=context,
    )
    private.require_outbound()
    try:
        return tuple(port.search(search, limit=16, remaining_seconds=seconds)), None
    except (RuntimeError, OSError, TimeoutError):
        return (), "entity_vector_unavailable"


def _entity_outbound_blocked(storage, context, sources, targets, clock, entries):
    """Fence every source owner before entity evidence leaves the process."""
    with storage.read(context, remaining_seconds=1.0) as tx:
        if paused_since(tx, entries):
            raise ContractError("ACCESS_DENIED", "entry_state:paused")
    return _publication_blocked(storage, context, sources, targets, clock)


def _prepare(storage, clock, context, item, model, seconds):
    """Read, nominate and judge outside the writer transaction, rechecking outbound authority."""
    deadline = clock.monotonic() + seconds
    sources = ()
    with storage.write(context, remaining_seconds=seconds) as tx:
        source = tx.claims.version(item.subject_ref, item.subject_revision)
        if source is None:
            raise ContractError("SOURCE_MISSING", "entity")
        sources = tuple(tx.claims.live_sources(evidence_refs(source.payload), source.scope_id))
        entries = entry_fence(tx, sources)
        if paused_since(tx, entries):
            raise ContractError("ACCESS_DENIED", "entry_state:paused")
        index_claim(tx, source)
        ready = backfill_names(tx, source.scope_id)
        names = pending_names(tx, source)
        if ready and not names:
            return source, None, (), None, entries
        if ready:
            source, sources = live_entity(tx, item.subject_ref, item.subject_revision, clock.utc_now())
            reserve_names(tx, source, item, clock.utc_now())
    if not ready:
        raise ContractError("VERSION_CONFLICT", "entity_index_pending")
    targets = ((source.ref, source.revision),)
    check = partial(_entity_outbound_blocked, storage, context, sources, targets, clock, entries)
    with private.using_outbound_check(check):
        hits, gap = _vector_hits(storage, clock, context, source, max(0.001, deadline - clock.monotonic()))
    with storage.read(context, remaining_seconds=max(0.001, deadline - clock.monotonic())) as tx:
        candidates = merge_candidates(
            tx, source.payload, source.scope_id, now=clock.utc_now(), entity=source, vector_hits=hits
        )
        if source.payload.get("entity_names"):
            candidates = (dict(ref=source.ref, revision=source.revision, payload=source.payload), *candidates)[:16]
        eligible = []
        for entry in candidates:
            try:
                version, _ = live_entity(tx, entry["ref"], entry["revision"], clock.utc_now())
            except ContractError as exc:
                if exc.code in {"SOURCE_MISSING", "ACCESS_DENIED"}:
                    continue
                raise
            eligible.append(entry)
            sources += tuple(tx.claims.live_sources(evidence_refs(version.payload), version.scope_id))
        candidates = tuple(eligible)
        for key, value in entry_fence(tx, sources).items():
            entries.setdefault(key, value)
    if not candidates:
        return source, None, names, gap, entries
    targets += tuple((entry["ref"], entry["revision"]) for entry in candidates)
    if deadline <= clock.monotonic():
        raise ContractError("DEADLINE_EXCEEDED")
    with private.using_outbound_check(
        partial(_entity_outbound_blocked, storage, context, sources, targets, clock, entries)
    ):
        private.require_outbound()
        raw = model.judge(
            entity_messages(source, tuple({(s.ref, s.revision): s for s in sources}.values()), candidates, names=names),
            remaining_seconds=max(0.001, deadline - clock.monotonic()),
        )
    answer = judgement_result(raw, candidates)
    if answer["action"] == "update":
        raise ContractError("DERIVATION_INVALID", "entity_decision")
    decision = SupportVerdict("", answer["support"], answer["target_ref"], answer["target_revision"], answer["action"])
    return source, decision, names, gap, entries


def process_entity_projection(storage, clock, context, item, *, model, started: float, budget: float):
    """Finish one fact's identity check atomically with its projection acknowledgement."""
    finish = partial(finalize_work, storage, clock, context, item, started=started, budget=budget)
    try:
        source, decision, names, gap, entries = _prepare(
            storage, clock, context, item, model, budget_left(started, clock, budget)
        )
        return _publish(storage, clock, context, item, (source, decision, names, gap, entries), started, budget)
    except ContractError as exc:
        if exc.field == "entry_state:paused":
            return hold_for_entry_state(storage, clock, context, item, started=started, budget=budget)
        if exc.field in {"entity_name_lease", "entity_index_pending"}:
            code = "entity_name_leased" if exc.field == "entity_name_lease" else exc.field
            with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
                return work_result(
                    tx.work.defer_without_attempt(
                        *item.lease, now=clock.utc_now(), error_code=code, seconds=int(code == "entity_name_leased")
                    ),
                    error_code=code,
                )
        if exc.field == "private_mode":
            return hold_for_private_mode(storage, clock, context, item, started=started, budget=budget)
        if exc.code in {"SOURCE_MISSING", "ACCESS_DENIED"}:
            with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
                deferred = defer_held_entity(tx, item, clock.utc_now())
                if deferred is not None:
                    return deferred
                ineligible(tx, item.subject_ref)
                tx._check(write=True).execute(
                    "UPDATE claim_versions SET entity_resolved=1 WHERE claim_id=? AND revision=?",
                    (item.subject_ref, item.subject_revision),
                )
                return mark_obsolete(tx, item, clock.utc_now())
        code = "memory_epoch_changed" if exc.code == "VERSION_CONFLICT" else exc.code.lower()
        return finish("failed" if exc.code in {"DERIVATION_INVALID", "INPUT_INVALID"} else "retry", code)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        return finish(*model_failure(exc))


def _publish(storage, clock, context, item, prepared, started, budget):
    """Recheck the lease and every endpoint before writing any alias or completion bit."""
    source, decision, names, gap, entries = prepared
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        now = clock.utc_now()
        if not tx.work._verify_lease(*item.lease, now=now):
            return stale_result(tx, item)
        if paused_since(tx, entries):
            return work_result(
                tx.work.defer_without_attempt(*item.lease, now=now, error_code="entry_paused", seconds=60),
                error_code="entry_paused",
            )
        if names:
            live_entity(tx, source.ref, source.revision, now)
        else:
            current = tx.claims.version(source.ref, source.revision)
            if current is None or current.current_revision != source.revision or current.state != "active":
                raise ContractError("VERSION_CONFLICT", "entity")
            if pending_names(tx, current):
                raise ContractError("VERSION_CONFLICT", "entity_names")
        if decision is not None and decision.state == "supported" and decision.action == "duplicate":
            target, _ = live_entity(tx, decision.target_ref, decision.target_revision, now)
            for name in names:
                publish_entity_merge(tx, source, target, decision, now=now, name=name)
        else:
            for name in names:
                checked(tx, source, name)
        tx._check(write=True).execute(
            "UPDATE claim_versions SET entity_resolved=1 WHERE claim_id=? AND revision=?", (source.ref, source.revision)
        )
        if gap:
            record_error_detail(
                tx._check(write=True), item.work_id, item.lease_token, now=now, stage="entity", code=gap
            )
        return work_result(tx.work.complete(*item.lease, now=now))


def needs_entity_projection(tx, item) -> bool:
    """Identify unresolved live fact heads without loading conversation bodies."""
    row = (
        tx._check()
        .execute(
            "SELECT 1 FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision "
            "WHERE c.claim_id=? AND v.revision=? AND c.kind IN (SELECT value FROM json_each(?)) AND "
            "v.state='active' AND v.entity_resolved=0",
            (item.subject_ref, item.subject_revision, json.dumps(ENTITY_KINDS)),
        )
        .fetchone()
    )
    return row is not None
