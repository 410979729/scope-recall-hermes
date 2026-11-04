"""Source-backed entity judgements published as retractable alias facts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from ..contracts import Alias, ClaimProposal, ContractError, validate_payload
from . import private
from .aliases import ENTITY_KINDS, entity_alias_proof, validate_alias_target
from .claims import Qualification, evidence_refs, valid_at
from .entity_names import checked
from .evidence_support import SupportVerdict
from .language_resources import prompt_text
from .review_policy import PENDING, held_proposal, owner_publication, publication_qualification
from .semantic_proposals import support_messages


def live_entity(tx, ref: str, revision: int, now: str):
    """Read a current, unconditional, human-sourced fact in the exact audience."""
    from .claims import effective_origin
    from .visibility import allowed

    version = tx.claims.version(ref, revision)
    if version is None:
        raise ContractError("SOURCE_MISSING", "entity")
    validate_alias_target(
        version,
        scope_id=version.scope_id,
        project_id=tx.context.project_id,
        branch_id=tx.context.branch_id,
        entity=True,
    )
    if version.current_revision != revision or not valid_at(version, now, historical=False):
        raise ContractError("VERSION_CONFLICT", "entity")
    if version.payload["subject"].startswith("principal:") or len(version.payload["subject"]) > 240:
        raise ContractError("ACCESS_DENIED", "entity_identity")
    if version.payload.get("conditions") or not allowed(tx, "claim", ref, automatic=False):
        raise ContractError("ACCESS_DENIED", "entity")
    sources = tx.claims.live_sources(evidence_refs(version.payload), version.scope_id)
    roots = tx.claims.roots(evidence_refs(version.payload))
    if not roots or any(
        effective_origin(root) != "human_direct" or root.capture_state != "complete" or root.capture_gaps
        for root in roots
    ):
        raise ContractError("ACCESS_DENIED", "entity_source")
    if any(
        root.source_principal is not None and root.source_principal.get("principal_ref") == version.payload["subject"]
        for root in roots
    ):
        raise ContractError("ACCESS_DENIED", "entity_identity")
    rules = private.active_intervals(tx, now)
    if any(private.source_blocked(rules, source) for source in sources):
        raise ContractError("ACCESS_DENIED", "private_mode")
    return version, tuple(sources)


def entity_messages(source, sources, candidates, *, names=None):
    """Reuse the fact judgement envelope with explicit entity-identity criteria."""
    payload = deepcopy(source.payload)
    if names is not None:
        payload["entity_names"] = [name for name in names if name != payload["subject"]]
    messages = support_messages(payload, sources, candidates)
    messages[0]["content"] = prompt_text("entity")
    return messages


def publish_entity_merge(tx, source, target, decision: SupportVerdict, *, now: str, name=None, only_unmerged=False):
    """Publish a prepared same-entity decision under the caller's writer lease.

    Endpoints and their evidence are re-read. The alias carries both original
    evidence sets; the proof pins the exact fact versions. Retraction uses the
    ordinary claim revision interface and never moves the original statements.
    """
    audience = (source.scope_id, source.project_id, source.branch_id)
    if audience != (target.scope_id, target.project_id, target.branch_id):
        raise ContractError("ACCESS_DENIED", "entity_audience")
    if decision.state != "supported" or decision.action != "duplicate":
        raise ContractError("DERIVATION_INVALID", "entity_decision")
    if (decision.target_ref, decision.target_revision) != (target.ref, target.revision):
        raise ContractError("DERIVATION_INVALID", "entity_target")
    kinds = {v.payload.get("entity_type", "unknown") for v in (source, target)} - {"unknown"}
    if len(kinds) > 1:
        raise ContractError("DERIVATION_INVALID", "entity_type")
    current, _ = live_entity(tx, source.ref, source.revision, now)
    destination, _ = live_entity(tx, target.ref, target.revision, now)
    if current.payload != source.payload or destination.payload != target.payload:
        raise ContractError("VERSION_CONFLICT", "entity")
    name = source.payload["subject"] if name is None else name
    if name == target.payload["subject"]:
        checked(tx, source, name, target)
        return None
    if name != source.payload["subject"] and name not in source.payload.get("entity_names", []):
        raise ContractError("DERIVATION_INVALID", "entity_name")
    if name != source.payload["subject"] and not any(
        name in span["quote"] for span in source.payload["evidence_spans"]
    ):
        raise ContractError("DERIVATION_INVALID", "entity_name")
    proposal = _alias_proposal(source, target, name)
    # Alias evidence must satisfy the same public contract as extracted claims.
    validate_payload(
        "consolidation_result",
        dict(
            protocol_version="1.1",
            source_refs=list(
                dict.fromkeys(f"{s['source_ref']}@{s['source_revision']}" for s in proposal["evidence_spans"])
            ),
            claim_proposals=[proposal],
            resume_proposals=[],
            reference_proposals=[],
        ),
    )
    history = tx.claims.slot(source.scope_id, proposal)
    previous = next((v for v in history if v.revision == v.current_revision), None)
    if held_proposal(tx, proposal, source.scope_id, history) is not None:
        return None
    if only_unmerged and previous is not None:
        raise ContractError("VERSION_CONFLICT", "entity_alias")
    # Explicit withdrawals are durable; endpoint revisions may refresh a live proof.
    if previous is not None and previous.state == "retracted":
        tx._check(write=True).execute(
            "UPDATE entity_names SET status='withdrawn',alias_ref=?,alias_revision=? "
            "WHERE audience=hex(json_array(?,?,?)) AND name=?",
            (previous.ref, previous.revision, source.scope_id, source.project_id, source.branch_id, name),
        )
        return None
    if previous is not None and entity_alias_proof(tx, previous, target, now):
        proof = (
            tx._check()
            .execute(
                "SELECT source_ref,source_revision FROM entity_alias_proofs WHERE alias_ref=? AND alias_revision=?",
                (previous.ref, previous.revision),
            )
            .fetchone()
        )
        checked(tx, tx.claims.version(proof[0], proof[1]), name, target, previous)
        return None
    qualification = publication_qualification(
        tx, source.scope_id, Qualification("active", "derived_summary", "entity_identity_supported"), previous, proposal
    )
    saved = tx.claims.append(
        source.scope_id,
        proposal,
        qualification,
        recorded_at=now,
        previous=previous,
        advance_head=previous is None or qualification.reason != PENDING,
    )
    material = json.dumps([source.payload, target.payload, decision.__dict__], sort_keys=True, ensure_ascii=False)
    tx._check(write=True).execute(
        "INSERT INTO entity_alias_proofs VALUES (?,?,?,?,?,?,?)",
        (
            saved.ref,
            saved.revision,
            source.ref,
            source.revision,
            target.ref,
            target.revision,
            hashlib.sha256(material.encode()).hexdigest(),
        ),
    )
    checked(tx, source, name, target, saved)
    return saved


def copy_entity_proof(tx, original, saved):
    """Carry an existing entity proof to an owner-approved or restored version."""
    tx._check(write=True).execute(
        "INSERT INTO entity_alias_proofs SELECT ?,?,source_ref,source_revision,target_ref,target_revision,"
        "judgement_sha256 "
        "FROM entity_alias_proofs WHERE alias_ref=? AND alias_revision=?",
        (saved.ref, saved.revision, original.ref, original.revision),
    )


def approve_entity_alias(tx, clock, pending, current):
    """Approve only the exact still-live entity judgement and migrate its proof."""
    proof = (
        tx._check()
        .execute(
            "SELECT source_ref,source_revision,target_ref,target_revision FROM entity_alias_proofs "
            "WHERE alias_ref=? AND alias_revision=?",
            (pending.ref, pending.revision),
        )
        .fetchone()
    )
    if proof is None:
        raise ContractError("VERSION_CONFLICT", "entity_proof")
    source, _ = live_entity(tx, proof[0], proof[1], clock.utc_now())
    target, _ = live_entity(tx, proof[2], proof[3], clock.utc_now())
    if not entity_alias_proof(tx, pending, target, clock.utc_now()):
        raise ContractError("VERSION_CONFLICT", "entity_proof")
    with owner_publication():
        saved = tx.claims.append(
            pending.scope_id,
            pending.payload,
            Qualification("active", pending.basis, "entity_identity_supported"),
            previous=current,
            expected_revision=current.revision,
            recorded_at=clock.utc_now(),
        )
    copy_entity_proof(tx, pending, saved)
    checked(tx, source, pending.payload["alias"]["name"], target, saved)
    return saved


def _alias_proposal(source, target, name) -> ClaimProposal:
    """Give each audience/name relation one stable slot and deduplicated evidence."""
    spans = []
    for span in (*source.payload["evidence_spans"], *target.payload["evidence_spans"]):
        if span not in spans:
            spans.append(deepcopy(span))
    return ClaimProposal(
        kind="alias",
        subject=target.payload["subject"],
        predicate="entity_identity:" + hashlib.sha256(name.encode()).hexdigest(),
        value_text=name,
        conditions=[],
        statement_kind="assertion",
        valid_from=None,
        valid_to=None,
        evidence_spans=spans,
        alias=Alias(name=name, target_ref=target.ref, scope_description="Same entity in this audience"),
    )


def queue_entity_backfill(tx, *, now: str, limit: int = 8, yield_to: frozenset[str] = frozenset()) -> int:
    """Queue a bounded page through the existing projection work lifecycle.

    The progress bit is additive and defaults to unresolved. Existing projection
    jobs are reused; no second queue or retry policy is introduced. Nothing is
    queued while work of a ``yield_to`` type is ready: claims take projection
    work before candidate evaluation, so a page every pass would hold new facts
    back until every old claim was resolved (days on a store upgraded with tens
    of thousands of claims).
    """
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ContractError("INPUT_INVALID", "entity_backfill_limit")
    if yield_to and tx.work.other_work_ready(now=now, kinds=frozenset(yield_to)):
        return 0
    scopes = sorted(tx.context.allowed_scope_ids)
    if not scopes:
        return 0
    rows = (
        tx._check()
        .execute(
            "SELECT c.claim_id,c.current_revision FROM claim_versions v INDEXED BY entity_unresolved "
            "CROSS JOIN claims c ON v.claim_id=c.claim_id AND v.revision=c.current_revision LEFT JOIN work_items w "
            "ON w.work_type='rebuild_projection' AND w.subject_ref=c.claim_id AND w.subject_revision=v.revision "
            "WHERE c.scope_id IN (SELECT value FROM json_each(?)) AND c.project_id IS ? AND c.branch_id IS ? "
            "AND c.kind IN (SELECT value FROM json_each(?)) AND c.read_blocked=0 AND c.suppressed=0 "
            "AND v.state='active' AND v.entity_resolved=0 "
            "AND (w.work_id IS NULL OR w.state IN ('done','obsolete')) ORDER BY v.claim_id,v.revision LIMIT ?",
            (json.dumps(scopes), tx.context.project_id, tx.context.branch_id, json.dumps(ENTITY_KINDS), limit),
        )
        .fetchall()
    )
    for ref, revision in rows:
        tx.work.enqueue("rebuild_projection", ref, revision, available_at=now)
        tx._check(write=True).execute(
            "UPDATE work_items SET state='pending',attempt=0,available_at=?,last_error_code=NULL "
            "WHERE work_type='rebuild_projection' AND subject_ref=? AND subject_revision=? AND state "
            "IN ('done','obsolete')",
            (now, ref, revision),
        )
    return len(rows)


def entity_progress(tx, *, include_all_projects: bool = False) -> dict[str, int]:
    """Count current visible facts and completed identity checks without reading their bodies."""
    row = (
        tx._check()
        .execute(
            "SELECT count(*),coalesce(sum(v.entity_resolved),0) FROM claims c JOIN claim_versions v "
            "ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.kind IN (SELECT value"
            " FROM json_each(?)) AND v.state='active' "
            "AND c.read_blocked=0 AND c.suppressed=0 AND c.scope_id IN (SELECT value FROM json_each(?)) "
            "AND (? OR (c.project_id IS ? AND c.branch_id IS ?))",
            (
                json.dumps(ENTITY_KINDS),
                json.dumps(sorted(tx.context.allowed_scope_ids)),
                include_all_projects,
                tx.context.project_id,
                tx.context.branch_id,
            ),
        )
        .fetchone()
    )
    return dict(total=row[0], completed=row[1], pending=row[0] - row[1])
