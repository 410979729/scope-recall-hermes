"""Prepare adopted decisions outside the consolidation write transaction."""

from __future__ import annotations

import time

from ..contracts import ContractError
from . import private
from .adoption import adoption_messages, decision_messages, decision_proposal, decode_selection
from .adoption_storage import PreparedAdoption, adoption_context, binding_reason, record_binding, source_reason
from .mutate import apply_claim_frames, validate_claims


def _prepare_one(storage, context, snapshot, judge, clock, deadline):
    """Check source visibility and privacy immediately before either outbound stage."""
    sources = (snapshot.confirmation, snapshot.assistant)
    with storage.read(context, remaining_seconds=max(0.001, deadline - time.monotonic())) as tx:
        reason = source_reason(tx, snapshot)
    if reason:
        return PreparedAdoption(snapshot, reason=reason)
    if private.outbound_blocked(storage, clock, context, sources=sources):
        raise ContractError("ACCESS_DENIED", "private_mode")
    try:
        locator = adoption_messages(snapshot.confirmation, snapshot.fragments)
    except ContractError:
        return PreparedAdoption(snapshot, reason="locator_input_budget")
    raw = judge(
        locator,
        remaining_seconds=max(0.001, deadline - time.monotonic()),
    )
    try:
        selection = decode_selection(raw, snapshot.fragments)
    except (ContractError, ValueError, TypeError):
        return PreparedAdoption(snapshot, reason="selection_unbound")
    if selection is None:
        return None
    with storage.read(context, remaining_seconds=max(0.001, deadline - time.monotonic())) as tx:
        reason = binding_reason(tx, snapshot, selection)
    if reason:
        return PreparedAdoption(snapshot, selection, reason=reason)
    if private.outbound_blocked(storage, clock, context, sources=sources):
        raise ContractError("ACCESS_DENIED", "private_mode")
    raw = judge(decision_messages(snapshot, selection), remaining_seconds=max(0.001, deadline - time.monotonic()))
    proposal = decision_proposal(snapshot, selection, raw)
    return PreparedAdoption(snapshot, selection, proposal)


def prepare_adoptions(storage, context, sources, judge, *, clock, remaining_seconds=1.0):
    """Select adjacent owner replies without changing ordinary consolidation input."""
    if judge is None:
        return ()
    deadline = time.monotonic() + remaining_seconds
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        snapshots = tuple(s for source in sources if (s := adoption_context(tx, source)) is not None)
    results = []
    for snapshot in snapshots:
        result = _prepare_one(storage, context, snapshot, judge, clock, deadline)
        if result is not None:
            results.append(result)
    return tuple(results)


def publish_adoption(tx, adoption, *, now):
    """Publish through the existing fact writer after revalidating the binding."""
    reason = adoption.reason or binding_reason(tx, adoption.snapshot, adoption.selection)
    record_binding(tx, adoption, reason=reason, now=now)
    if reason is not None:
        return None
    proposal = adoption.proposal
    source_refs = list(dict.fromkeys(f"{s['source_ref']}@{s['source_revision']}" for s in proposal["evidence_spans"]))
    envelope = dict(
        protocol_version="1.1",
        source_refs=source_refs,
        claim_proposals=[proposal],
        resume_proposals=[],
        reference_proposals=[],
    )
    validated = validate_claims(tx, envelope, adoption.snapshot.confirmation.scope_id)
    item = apply_claim_frames(tx, validated["claim_proposals"][0], adoption.snapshot.confirmation.scope_id, now)[0]
    tx._check(write=True).execute(
        "UPDATE adoption_relations SET claim_ref=?,claim_revision=? "
        "WHERE confirmation_ref=? AND confirmation_revision=?",
        (item.ref, item.revision, adoption.snapshot.confirmation.ref, adoption.snapshot.confirmation.revision),
    )
    return item


def publish_adoptions(tx, adoptions, *, now):
    """Publish all bounded selections while sharing the consolidation transaction."""
    return [item for adoption in adoptions if (item := publish_adoption(tx, adoption, now=now)) is not None]
