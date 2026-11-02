"""Stage adopted decisions outside write transactions and publish with source-bound support."""

from __future__ import annotations

import json
import time

from ..contracts import ContractError
from . import private
from .adoption import adoption_messages, decision_messages, decision_proposal, decode_selection
from .adoption_storage import (
    PreparedAdoption,
    adoption_context,
    binding_reason,
    owner_selection_reason,
    record_binding,
    source_reason,
)
from .entry_control import entry_fence, paused_since
from .evidence_support import SupportVerdict, support_fingerprint, using_support
from .mutate import apply_claim_frames, validate_claims
from .semantic_proposals import prepare_support


def advance_adoption(storage, context, snapshot, state, judge, *, clock, remaining_seconds):
    """Run at most one model stage; return a checkpoint and an optional publishable result.

    The same transition serves durable worker passes and bounded synchronous callers.
    A failed recognition raises; only a decoded adoption can become a review item.
    """
    selection = state.get("selection")
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        reason = source_reason(tx, snapshot)
        if reason is None and selection is not None:
            reason = binding_reason(tx, snapshot, selection)
    if reason:
        if selection is None:
            raise ContractError("SOURCE_MISSING", "adoption_source")
        return dict(stage="done"), PreparedAdoption(snapshot, selection, reason=reason)
    stage = state["stage"]
    if stage == "publish":
        support = SupportVerdict(**state["support"])
        return dict(stage="done"), PreparedAdoption(snapshot, selection, state["proposal"], support=support)
    sources = (snapshot.confirmation, snapshot.assistant)
    if private.outbound_blocked(storage, clock, context, sources=sources):
        raise ContractError("ACCESS_DENIED", "private_mode")
    if stage == "locate":
        locator = adoption_messages(snapshot.confirmation, snapshot.fragments)
        raw = judge(locator, remaining_seconds=remaining_seconds)
        # A model cannot select an omitted id even if it guesses its original number.
        selection = decode_selection(raw, json.loads(locator[-1]["content"])["fragments"])
        if selection is None:
            return dict(stage="done"), None
        with storage.read(context, remaining_seconds=remaining_seconds) as tx:
            reason = binding_reason(tx, snapshot, selection)
        if reason:
            return dict(stage="done"), PreparedAdoption(snapshot, selection, reason=reason)
        return dict(stage="describe", selection=selection), None
    if stage == "describe":
        try:
            messages = decision_messages(snapshot, selection)
        except ContractError as exc:
            if exc.field != "adoption_input_budget":
                raise
            return dict(stage="done"), PreparedAdoption(snapshot, selection, reason="decision_input_budget")
        raw = judge(messages, remaining_seconds=remaining_seconds)
        proposal = decision_proposal(snapshot, selection, raw)
        return dict(state, stage="support", proposal=proposal), None
    if stage != "support":
        raise ContractError("DERIVATION_INVALID", "adoption_stage")
    verdict = prepare_support(
        dict(claim_proposals=[state["proposal"]]), sources, judge, remaining_seconds=remaining_seconds, adoption=True
    )[0]
    if verdict.failure_code:
        raise ContractError("STORAGE_UNAVAILABLE", verdict.failure_code)
    if verdict.state.startswith("unavailable"):
        raise ContractError("STORAGE_UNAVAILABLE", "adoption_support_unavailable")
    from dataclasses import asdict

    return dict(state, stage="publish", support=asdict(verdict)), None


def prepare_adoptions(storage, context, sources, judge, *, clock, remaining_seconds=1.0):
    """Prepare adjacent owner choices with the same transitions used by worker checkpoints."""
    if judge is None:
        return ()
    deadline = time.monotonic() + remaining_seconds
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        snapshots = tuple(
            (s, entry_fence(tx, (s.confirmation, s.assistant)))
            for source in sources
            if (s := adoption_context(tx, source)) is not None
        )
    results = []
    for snapshot, fence in snapshots:
        state = dict(stage="locate")
        while state["stage"] != "done":
            with storage.read(context, remaining_seconds=max(0.001, deadline - time.monotonic())) as tx:
                if paused_since(tx, fence):
                    raise ContractError("ACCESS_DENIED", "entry_paused")
            state, result = advance_adoption(
                storage,
                context,
                snapshot,
                state,
                judge,
                clock=clock,
                remaining_seconds=max(0.001, deadline - time.monotonic()),
            )
            if result is not None:
                from dataclasses import replace

                results.append(replace(result, entry_fence=fence))
    return tuple(results)


def publish_adoption(tx, adoption, *, now):
    """Publish only a supported decision after revalidating its exact live binding."""
    fence = adoption.entry_fence or entry_fence(tx, (adoption.snapshot.confirmation, adoption.snapshot.assistant))
    if paused_since(tx, fence):
        raise ContractError("ACCESS_DENIED", "entry_paused")
    if adoption.selection is None:
        raise ContractError("DERIVATION_INVALID", "adoption_selection")
    if owner_selection_reason(tx, adoption.snapshot, adoption.selection) is not None:
        return None
    reason = adoption.reason or binding_reason(tx, adoption.snapshot, adoption.selection)
    if reason is not None:
        record_binding(tx, adoption, reason=reason, now=now)
        return None
    if adoption.support is None or adoption.support.state != "supported":
        return None
    proposal = adoption.proposal
    contents = {
        (s.ref, s.revision): s.event["content"] for s in (adoption.snapshot.confirmation, adoption.snapshot.assistant)
    }
    if adoption.support.fingerprint != support_fingerprint(proposal, contents):
        raise ContractError("DERIVATION_INVALID", "adoption_support")
    source_refs = list(dict.fromkeys(f"{s['source_ref']}@{s['source_revision']}" for s in proposal["evidence_spans"]))
    envelope = dict(
        protocol_version="1.1",
        source_refs=source_refs,
        claim_proposals=[proposal],
        resume_proposals=[],
        reference_proposals=[],
    )
    with using_support((adoption.support,)):
        # The receipt is checked again by qualification against live source contents.
        record_binding(tx, adoption, reason=None, now=now)
        validated = validate_claims(tx, envelope, adoption.snapshot.confirmation.scope_id)
        item = apply_claim_frames(tx, validated["claim_proposals"][0], adoption.snapshot.confirmation.scope_id, now)[0]
    tx._check(write=True).execute(
        "UPDATE adoption_relations SET claim_ref=?,claim_revision=? "
        "WHERE confirmation_ref=? AND confirmation_revision=?",
        (item.ref, item.revision, adoption.snapshot.confirmation.ref, adoption.snapshot.confirmation.revision),
    )
    from .adoption_dedup import retire_duplicates

    retire_duplicates(tx, adoption, item.ref, now=now)
    from .session_brief import rebuild_scope

    rebuild_scope(tx, adoption.snapshot.confirmation.scope_id, now)
    return item
