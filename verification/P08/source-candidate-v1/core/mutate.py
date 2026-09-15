"""Claim mutations inside one storage transaction; models only submit proposals."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
import re

from ..contracts import (ClaimProposal, ContractError, SourceSnapshot, TrustedContext,
                         validate_model_request, validate_payload, validate_proposal_references)
from ..secret_patterns import contains_secret_like_text
from .claim_storage import parse_source_ref
from .aliases import validate_alias_source, validate_alias_target
from .source_qualification import AUTHORITY_QUESTION
from .claims import (ClaimVersion, Qualification, canonical_time, effective_origin,
                     evidence_context, grounded_time, qualify, same_assertion, select_effective)


@dataclass(frozen=True)
class Mutation:
    ref: str
    revision: int
    disposition: str
    state: str


@dataclass(frozen=True)
class MutationReceipt:
    items: tuple[Mutation, ...]
    memory_epoch: int
    durability: str = "persisted"


def evidence_refs(proposal: ClaimProposal) -> tuple[str, ...]:
    refs = [f"{s['source_ref']}@{s['source_revision']}" for s in proposal["evidence_spans"]]
    refs.extend(proposal.get("intention", {}).get("state_evidence_refs", []))
    refs.extend(proposal.get("procedure", {}).get("counterexample_refs", []))
    return tuple(dict.fromkeys(refs))


def validate_claims(tx, value, scope_id: str):
    tx._scope(scope_id)
    result = validate_payload("consolidation_result", value)
    if contains_secret_like_text(json.dumps(result, ensure_ascii=False)):
        raise ContractError("INPUT_INVALID", "plaintext_secret_rejected")
    snapshots = []
    for ref in result["source_refs"]:
        key = parse_source_ref(ref)
        tx.claims.require_live_source(*key)
        source = tx.source(*key)
        if source.scope_id != scope_id:
            raise ContractError("ACCESS_DENIED", "evidence_scope")
        snapshots.append(SourceSnapshot(source.ref,source.revision,source.event["content"],source.scope_id,source.event["origin"],
                                        verified_original_origin=source.event.get('source_original_origin') if source.import_provenance_sha256 else None))
    return validate_proposal_references(result, tuple(snapshots), tx.context)


def apply_claim(tx, proposal: ClaimProposal, scope_id: str, now: str) -> Mutation:
    roots = tx.claims.roots(evidence_refs(proposal))
    qualification = qualify(proposal, roots, project_id=tx.context.project_id)
    if proposal["kind"] == "alias":
        alias = proposal["alias"]
        # A sourced naming claim supplies stable identity; aliases preserve the
        # old name. Artifact/display bindings have their own versioned repository.
        target_versions = tx.claims.versions(alias["target_ref"])
        target = select_effective(target_versions, now)
        try:
            validate_alias_target(target, scope_id=scope_id, project_id=tx.context.project_id,
                                  branch_id=tx.context.branch_id)
            if proposal["subject"] != target.payload["subject"]:
                raise ContractError("ACCESS_DENIED", "alias_subject_identity")
            supported = False
            for root in roots:
                if effective_origin(root) != "human_direct" or root.capture_state != "complete" or root.capture_gaps:
                    continue
                for span in proposal["evidence_spans"]:
                    if (span["source_ref"], span["source_revision"]) != (root.ref, root.revision):
                        continue
                    try:
                        validate_alias_source(
                            span["quote"],
                            alias["name"],
                            target,
                            source_text=root.content,
                        )
                        supported = True
                    except ContractError:
                        continue
            if not supported:
                raise ContractError("ACCESS_DENIED", "alias_relation_not_bound")
        except ContractError:
            qualification = Qualification("proposed","inferred_suggestion","alias_target_or_relation_unverified")
    history = tx.claims.slot(scope_id, proposal)
    previous = next((v for v in history if v.revision == v.current_revision), None)
    # Repeated extraction cannot alter the live identity, validity, head,
    # lifecycle, epoch or work queue of the same assertion.
    identical = next((v for v in reversed(history) if same_assertion(v.payload, proposal)
                      and (v.state == qualification.state or v.state == "active")), None)
    if identical is not None:
        return Mutation(identical.ref,identical.revision,"duplicate",identical.state)
    advance = True
    conflicts = ()
    if previous is not None and previous.state != "proposed":
        if qualification.state == "proposed":
            advance = False
        else:
            old_roots = tx.claims.roots(evidence_refs(previous.payload))
            new_start, old_start = canonical_time(proposal["valid_from"]), canonical_time(previous.valid_from)
            new_times = [canonical_time(r.occurred_at) for r in roots if r.occurred_at is not None]
            old_times = [canonical_time(r.occurred_at) for r in old_roots if r.occurred_at is not None]
            late = (new_start is not None and old_start is not None and new_start < old_start) or (
                new_times and old_times and max(new_times) < max(old_times) and not (new_start and old_start and new_start > old_start))
            if late:
                advance = False
                qualification = Qualification("active",qualification.basis,"late_historical_evidence")
            else:
                try:
                    tx.claims.current_human(evidence_refs(proposal),scope_id)
                    live_human = True
                except ContractError:
                    live_human = False
                ordered = bool(new_start and old_start and new_start > old_start)
                if not live_human and not ordered:
                    qualification = Qualification("disputed",qualification.basis,"conflicting_evidence_order_unknown")
                    conflicts = tuple(dict.fromkeys((*previous.conflict_revisions,previous.revision)))
    saved = tx.claims.append(scope_id, proposal, qualification, recorded_at=now, previous=previous,
                             advance_head=advance, conflicts=conflicts)
    return Mutation(saved.ref,saved.revision,"inserted" if advance else "historical",saved.state)


def accept_claim_proposals(storage, clock, context: TrustedContext, value, *, scope_id: str,
                           remaining_seconds: float = 1.0) -> MutationReceipt:
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        result = validate_claims(tx, value, scope_id)
        if result["resume_proposals"] or result["reference_proposals"]:
            raise ContractError("INPUT_INVALID", "use_consolidation_dispatch_for_nonclaims")
        items = tuple(apply_claim(tx, proposal, scope_id, clock.utc_now()) for proposal in result["claim_proposals"])
        epoch = tx.status().memory_epoch
    return MutationReceipt(items,epoch)


_CORRECTION = re.compile(r"写错|说错|不对|改为|改成|改用|更正|纠正|替代|\b(?:correct|replace|change to)\b", re.I)
_RETRACT = re.compile(r"撤回|撤销|作废|删除这个事实|\b(?:retract|withdraw|revoke)\b", re.I)
_AMBIGUOUS = re.compile(r"那个不对|改一下|\b(?:change that|that is wrong)\b", re.I)
_NO_FAST_PATH = re.compile(r"假设|假如|如果|除非|仅限|只限|今天|明天|以后|日起|不要改|别改|\b(?:suppose|unless|only today|tomorrow|do not change)\b", re.I)
_NEGATED_ACTION = re.compile(r"(?:不要|别|不能|不必|暂不)[^。！？!?;；\n]{0,4096}(?:改|更正|撤回|撤销|作废)|\b(?:do not|don't|never)\s+(?:change|correct|retract|withdraw|replace)\b", re.I)
_QUESTION = AUTHORITY_QUESTION


def _revise_in_transaction(tx, clock, request, *, source=None) -> Mutation:
    history = tx.claims.versions(request["target_ref"])
    if not history:
        raise ContractError("SOURCE_MISSING")
    head = next(v for v in history if v.revision == v.current_revision)
    tx.claims.require_target(head)
    if head.current_revision != request["expected_revision"]:
        raise ContractError("VERSION_CONFLICT")
    refs = tuple(request["source_evidence_refs"])
    source = tx.claims.current_human(refs,head.scope_id)
    raw = source.event["content"]
    if _NEGATED_ACTION.search(raw) or _QUESTION.search(raw):
        raise ContractError("ACCESS_DENIED", "revision_not_asserted")
    new_value = request["new_value"]
    retraction = new_value is None or new_value == {"state":"retracted"}
    if retraction:
        if not _RETRACT.search(raw):
            raise ContractError("ACCESS_DENIED", "retraction_not_authorized")
    elif not _CORRECTION.search(raw):
        raise ContractError("ACCESS_DENIED", "correction_not_authorized")
    if not any(marker in raw for marker in (head.ref,head.payload["subject"],head.payload["value_text"])):
        raise ContractError("ACCESS_DENIED", "target_not_bound")
    updated = deepcopy(head.payload)
    if isinstance(new_value, str):
        updated["value_text"] = new_value
    elif not retraction:
        if type(new_value) is not dict or set(new_value)-{"value_text","valid_to","procedure","intention","alias"}:
            raise ContractError("INPUT_INVALID", "new_value")
        updated.update(deepcopy(new_value))
    updated["conditions"] = request["conditions"]
    updated["valid_from"] = request["valid_from"]
    updated["statement_kind"] = "assertion"
    # Explicit revisions bind their spans to the authorizing current utterance.
    # They do not silently reuse an old quotation as permission for a new action.
    anchor = next((marker for marker in (head.ref,head.payload["subject"],head.payload["value_text"]) if marker in raw),"") if retraction else updated["value_text"]
    proof = evidence_context(raw,anchor) if anchor else ""
    if not proof or proof not in raw or not any(marker in proof for marker in (head.ref,head.payload["subject"],head.payload["value_text"])):
        raise ContractError("DERIVATION_INVALID", "revision_proof_ambiguous")
    if not (_RETRACT.search(proof) if retraction else _CORRECTION.search(proof)):
        raise ContractError("ACCESS_DENIED", "revision_not_bound_to_assertion")
    if len(proof) > 4096:
        raise ContractError("INPUT_INVALID", "explicit_revision_quote_budget")
    updated["evidence_spans"] = [dict(source_ref=source.ref,source_revision=source.revision,quote=proof)]
    if "intention" in updated:
        updated["intention"]["state_evidence_refs"] = [f"{source.ref}@{source.revision}"]
    result = dict(protocol_version="1.1", source_refs=list(refs),claim_proposals=[updated],resume_proposals=[],reference_proposals=[])
    validate_claims(tx,result,head.scope_id)
    roots = tx.claims.roots(evidence_refs(updated))
    roots = tuple(replace(root,content=evidence_context(root.content,proof)) for root in roots)
    if not all(grounded_time(updated[field],roots) for field in ("valid_from","valid_to")):
        raise ContractError("DERIVATION_INVALID", "time_not_grounded")
    if retraction:
        qualification = Qualification("retracted","direct_report","explicit_retraction")
    else:
        if updated["value_text"] not in raw:
            raise ContractError("DERIVATION_INVALID", "new_value_not_supported")
        qualification = qualify(updated,roots,project_id=tx.context.project_id)
        # An explicit, scoped attribute correction may omit the old predicate
        # while naming the subject/old value and replacement literally.
        if qualification.reason == "fact_entailment_unproved" and _CORRECTION.search(raw):
            qualification = Qualification("active","direct_report","explicit_attribute_correction")
        if qualification.state != "active":
            raise ContractError("DERIVATION_INVALID", qualification.reason)
    if same_assertion(head.payload,updated) and head.state == qualification.state:
        return Mutation(head.ref,head.revision,"duplicate",head.state)
    same_conditions = sorted(set(head.payload["conditions"])) == sorted(set(updated["conditions"]))
    if not same_conditions:
        if retraction:
            raise ContractError("INPUT_INVALID", "retraction_conditions")
        return apply_claim(tx,updated,head.scope_id,clock.utc_now())
    saved = tx.claims.append(head.scope_id,updated,qualification,recorded_at=clock.utc_now(),previous=head,
                            expected_revision=request["expected_revision"])
    return Mutation(saved.ref,saved.revision,"retracted" if retraction else "revised",saved.state)


def revise(storage, clock, context: TrustedContext, value, *, remaining_seconds: float = 1.0) -> MutationReceipt:
    request = validate_model_request("revise_request",value,context)
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        item = _revise_in_transaction(tx,clock,request)
        epoch = tx.status().memory_epoch
    return MutationReceipt((item,),epoch)


def capture_correction(tx, source, clock) -> Mutation | None:
    """Bounded model-free explicit target path. Ambiguity preserves the raw update."""
    raw = source.event["content"]
    if source.event["origin"] != "human_direct" or source.capture_gaps or source.event["capture_state"] != "complete":
        return None
    if not (_CORRECTION.search(raw) or _RETRACT.search(raw) or _AMBIGUOUS.search(raw)):
        return None
    heads = [next(v for v in tx.claims.versions(ref) if v.revision == v.current_revision) for ref in tx.claims.list_refs()]
    heads = [v for v in heads if v.scope_id == source.scope_id and (v.project_id,v.branch_id) == (source.project_id,source.branch_id) and v.state in {"active","disputed"}]
    matches = [v for v in heads if any(s in raw for s in (v.ref,v.payload["subject"],v.payload["value_text"]))]
    exact = [v for v in matches if v.ref in raw or v.payload["predicate"] in raw]
    if exact:
        matches = exact
    tokens = re.findall(r"(?:改为|改成|改用|用|是|change to)\s*([A-Za-z0-9\u4e00-\u9fff_.-]{1,120})(?=[。！？；，,;.!?\s]|$)",raw,re.I)
    if len(matches) == 1 and (tokens or _RETRACT.search(raw)) and not (_NO_FAST_PATH.search(raw) or _NEGATED_ACTION.search(raw) or _QUESTION.search(raw)):
        target = matches[0]
        if source.event["occurred_at"] and target.valid_from and canonical_time(source.event["occurred_at"]) < canonical_time(target.valid_from):
            tx.claims.unresolved(source.ref,source.revision,(target.ref,),recorded_at=clock.utc_now())
            return None
        request = dict(protocol_version="1.1",target_ref=target.ref,expected_revision=target.current_revision,
                       new_value=None if _RETRACT.search(raw) else tokens[-1],conditions=target.payload["conditions"],
                       source_evidence_refs=[f"{source.ref}@{source.revision}"],valid_from=source.event["occurred_at"])
        try:
            with tx.savepoint():
                return _revise_in_transaction(tx,clock,request)
        except ContractError:
            # Rejected semantics do not discard the user's captured occurrence.
            pass
    tx.claims.unresolved(source.ref,source.revision,tuple(v.ref for v in (matches or heads)),recorded_at=clock.utc_now())
    return None


def current_claim(storage, clock, context: TrustedContext, ref: str, *, as_of: str | None = None,
                  known_at: str | None = None) -> ClaimVersion | None:
    with storage.read(context) as tx:
        return select_effective(tx.claims.versions(ref),as_of or clock.utc_now(),as_of=as_of is not None,known_at=known_at)
