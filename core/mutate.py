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
from .evidence_quote import resolve_evidence_quotes
from .source_qualification import (AUTHORITY_QUESTION, RELATIVE_SCOPE,
                                   bound_literal, first_person_reference)
from .corroboration import CORROBORATED_REASON, corroboration_promotes
from .claims import (ClaimVersion, Qualification, bind_claim_subject,
                     canonical_time, effective_origin, evidence_context,
                     grounded_time, qualify, same_assertion, select_effective)


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
    intention = proposal.get("intention")
    if intention is not None:
        refs.extend(intention["state_evidence_refs"])
    procedure = proposal.get("procedure")
    if procedure is not None:
        refs.extend(procedure["counterexample_refs"])
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
        if source is None:
            raise ContractError("SOURCE_MISSING")
        if source.scope_id != scope_id:
            raise ContractError("ACCESS_DENIED", "evidence_scope")
        snapshots.append(SourceSnapshot(source.ref,source.revision,source.event["content"],source.scope_id,source.event["origin"],
                                        verified_original_origin=source.event.get('source_original_origin') if source.import_provenance_sha256 else None))
    # Every entry above has been checked live and in scope, so this is the one
    # place where a model's quotes can be mapped back to stored slices for every
    # path that accepts a proposal: consolidation, candidate re-evaluation and
    # the host-facing proposal surface all come through here.  Spans naming
    # anything outside this map are left untouched and the strict checks that
    # follow reject them exactly as before.
    resolve_evidence_quotes(result, {(s.ref, s.revision): s.content for s in snapshots})
    return validate_proposal_references(result, tuple(snapshots), tx.context)


def apply_claim(tx, proposal: ClaimProposal, scope_id: str, now: str, *,
                _subject_bound: bool = False) -> Mutation:
    roots = tx.claims.roots(evidence_refs(proposal))
    from .claim_normalization import normalize_frame, human_owner, source_order
    proposal = normalize_frame(proposal, roots)
    if _subject_bound:
        subject_bound, binding_issue = True, None
    else:
        proposal, subject_bound, binding_issue = bind_claim_subject(proposal, roots)
    qualification = (
        Qualification("proposed", "inferred_suggestion", binding_issue)
        if binding_issue is not None
        else qualify(
            proposal,
            roots,
            project_id=tx.context.project_id,
            _subject_bound=subject_bound,
        )
    )
    if proposal["kind"] == "alias":
        alias = proposal.get("alias")
        if alias is None:
            raise ContractError("INPUT_INVALID", "alias")
        # A sourced naming claim supplies stable identity; aliases preserve the
        # old name. Artifact/display bindings have their own versioned repository.
        target_versions = tx.claims.versions(alias["target_ref"])
        target = select_effective(target_versions, now)
        try:
            if target is None:
                raise ContractError("SOURCE_MISSING", "alias_target")
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
        # The same assertion arriving from a second independent first-hand
        # source is not a duplicate, it is corroboration -- the one dimension
        # this system had no way to accumulate.  Nothing below reconsiders
        # either statement; both were judged normally and both fell short for a
        # reason that another witness can cure.  See core/corroboration.py.
        #
        # Only when the slot's head *is* this assertion.  Defence in depth: the
        # conflict path below already turns a differing value away before it
        # gets here, and this makes sure a future change to that path cannot
        # quietly turn corroboration into a way around ordering, principal and
        # disputed-state handling.
        head_is_this_assertion = previous is None or same_assertion(previous.payload, proposal)
        if head_is_this_assertion and corroboration_promotes(
            existing=identical,
            existing_reason=identical.reason,
            incoming_reason=qualification.reason,
            incoming_roots=roots,
            existing_roots=tx.claims.roots(evidence_refs(identical.payload)),
        ):
            promoted = Qualification("active", "direct_report", CORROBORATED_REASON)
            saved = tx.claims.append(scope_id, proposal, promoted, recorded_at=now,
                                     previous=previous if previous is not None else identical,
                                     advance_head=True)
            return Mutation(saved.ref, saved.revision, "inserted", saved.state)
        return Mutation(identical.ref,identical.revision,"duplicate",identical.state)
    advance = True
    conflicts = ()
    if previous is not None and previous.state != "proposed":
        if qualification.state == "proposed":
            advance = False
        else:
            old_roots = tx.claims.roots(evidence_refs(previous.payload))
            new_owner, old_owner = human_owner(roots), human_owner(old_roots)
            new_order, old_order = source_order(tx, roots), source_order(tx, old_roots)
            ingestion_ordered = (new_owner is not None and new_owner == old_owner
                                 and new_order is not None and old_order is not None
                                 and all(r.occurred_at is None for r in (*roots, *old_roots)))
            new_start, old_start = canonical_time(proposal["valid_from"]), canonical_time(previous.valid_from)
            new_times = [stamp for r in roots if r.occurred_at is not None
                         if (stamp := canonical_time(r.occurred_at)) is not None]
            old_times = [stamp for r in old_roots if r.occurred_at is not None
                         if (stamp := canonical_time(r.occurred_at)) is not None]
            late = (new_start is not None and old_start is not None and new_start < old_start) or (
                new_times and old_times and max(new_times) < max(old_times) and not (new_start and old_start and new_start > old_start))
            late = late or (ingestion_ordered and new_order < old_order)
            if new_owner is not None and old_owner is not None and new_owner != old_owner:
                advance = False
                qualification = Qualification('proposed', 'inferred_suggestion', 'different_source_principal')
            elif late:
                advance = False
                qualification = Qualification("active",qualification.basis,"late_historical_evidence")
            else:
                try:
                    tx.claims.current_human(evidence_refs(proposal),scope_id)
                    live_human = True
                except ContractError:
                    live_human = False
                ordered = (bool(new_start and old_start and new_start > old_start)
                           or bool(new_times and old_times and min(new_times) > max(old_times))
                           or (ingestion_ordered and new_order > old_order))
                if not live_human and not ordered:
                    qualification = Qualification("disputed",qualification.basis,"conflicting_evidence_order_unknown")
                    conflicts = tuple(dict.fromkeys((*previous.conflict_revisions,previous.revision)))
    saved = tx.claims.append(scope_id, proposal, qualification, recorded_at=now, previous=previous,
                             advance_head=advance, conflicts=conflicts)
    return Mutation(saved.ref,saved.revision,"inserted" if advance else "historical",saved.state)


def register_applied_candidate(tx, mutation: Mutation, now: str, *,
                               schedule_initial: bool = True):
    """Register a newly current fact version through C3 in this transaction.

    Candidate-worker publication calls ``apply_claim`` directly and performs
    its own ``schedule_initial=False`` registration.  The ordinary C2
    orchestrators use this helper so a failed registration rolls the fact
    write back instead of leaving a crash window between the two commits.
    """
    if mutation.disposition not in {"inserted", "revised", "retracted"}:
        return None
    return tx.candidates.register(
        mutation.ref,
        mutation.revision,
        observed_at=now,
        schedule_initial=schedule_initial,
    )


def apply_claim_frames(tx, proposal, scope_id, now):
    from .claim_normalization import expand_frames
    roots = tx.claims.roots(evidence_refs(proposal))
    items = []
    for frame in expand_frames(proposal, roots):
        item = apply_claim(tx, frame, scope_id, now)
        register_applied_candidate(tx, item, now)
        items.append(item)
    return items


def accept_claim_proposals(storage, clock, context: TrustedContext, value, *, scope_id: str,
                           remaining_seconds: float = 1.0) -> MutationReceipt:
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        result = validate_claims(tx, value, scope_id)
        if result["resume_proposals"] or result["reference_proposals"]:
            raise ContractError("INPUT_INVALID", "use_consolidation_dispatch_for_nonclaims")
        items = []
        for proposal in result["claim_proposals"]:
            items.extend(apply_claim_frames(tx, proposal, scope_id, clock.utc_now()))
        epoch = tx.status().memory_epoch
    return MutationReceipt(tuple(items),epoch)


_CORRECTION = re.compile(r"写错|说错|不对|改为|改成|改用|换成|换为|调整为|调整成|更正|纠正|替代|替换|更换|\b(?:correct|replace|change to|switch to)\b", re.I)
_RETRACT = re.compile(r"撤回|撤销|作废|删除这个事实|不再使用|不再采用|停止使用|停止采用|弃用|\b(?:retract|withdraw|revoke|stop using|discontinue)\b", re.I)
_AMBIGUOUS = re.compile(
    r"那个不对|改一下|(?:把)?(?:那个|这个|它)(?:改掉|改一下|换掉|删掉|撤回)|"
    r"\b(?:change that|that is wrong|replace it|remove it)\b",
    re.I,
)
_NO_FAST_PATH = re.compile(r"假设|假如|如果|除非|仅限|只限|今天|明天|以后|日起|将停止|将不再|将弃用|不要改|别改|\b(?:suppose|unless|only today|tomorrow|do not change|will stop|will discontinue)\b", re.I)
_NEGATED_ACTION = re.compile(r"(?:不要|别|不能|不必|暂不)[^。！？!?;；\n]{0,4096}(?:改|换成|换为|调整为|调整成|替换|更换|更正|撤回|撤销|作废|停止|不再|弃用)|\b(?:do not|don't|never)\s+(?:change|correct|retract|withdraw|replace|stop|discontinue|switch)\b", re.I)
_QUESTION = AUTHORITY_QUESTION
_REPORTED_ACTION = re.compile(r"举例|示例|客户原文|引用|他说|她说|他们说|文档写|\b(?:for example|example:|quoted|customer said|he said|she said)\b", re.I)


def _source_principal_ref(source) -> str | None:
    principal = source.event.get("source_principal")
    if not isinstance(principal, dict):
        return None
    if principal.get("kind") != "human" or principal.get("resolution") != "verified":
        return None
    value = principal.get("principal_ref")
    return value if isinstance(value, str) and value else None


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
    principal_matches = _source_principal_ref(source) == head.payload["subject"]
    first_person_target = principal_matches and first_person_reference(raw)
    source_time = canonical_time(source.event["occurred_at"])
    head_time = canonical_time(head.valid_from)
    if source_time is not None and head_time is not None and source_time < head_time:
        raise ContractError("DERIVATION_INVALID", "late_historical_correction")
    if _NEGATED_ACTION.search(raw) or _QUESTION.search(raw) or _REPORTED_ACTION.search(raw):
        raise ContractError("ACCESS_DENIED", "revision_not_asserted")
    new_value = request["new_value"]
    retraction = new_value is None or new_value == {"state":"retracted"}
    if retraction:
        if not _RETRACT.search(raw):
            raise ContractError("ACCESS_DENIED", "retraction_not_authorized")
        if _NO_FAST_PATH.search(raw) or RELATIVE_SCOPE.search(raw):
            raise ContractError("ACCESS_DENIED", "conditional_retraction_not_authorized")
    elif not _CORRECTION.search(raw):
        raise ContractError("ACCESS_DENIED", "correction_not_authorized")
    if (not any(bound_literal(raw, marker) for marker in (head.ref,head.payload["subject"],head.payload["value_text"]))
            and not (first_person_target and bound_literal(raw, head.payload["predicate"]))):
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
    anchor = next((marker for marker in (head.ref,head.payload["subject"],head.payload["value_text"]) if bound_literal(raw, marker)),"") if retraction else updated["value_text"]
    proof = evidence_context(raw,anchor) if anchor else ""
    if (not proof or proof not in raw
            or (not any(bound_literal(proof, marker) for marker in (head.ref,head.payload["subject"],head.payload["value_text"]))
                and not (first_person_target and bound_literal(proof, head.payload["predicate"])))):
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
        qualification = qualify(updated,roots,project_id=tx.context.project_id,
                                explicit_attribute_correction=bool(_CORRECTION.search(proof)),
                                _subject_bound=principal_matches)
        # An explicit, scoped attribute correction may omit the old predicate
        # while naming the subject/old value and replacement literally.
        # The exception is applied inside qualification so it cannot skip
        # polarity, conditions, source identity or temporal checks.
        if qualification.state != "active":
            raise ContractError("DERIVATION_INVALID", qualification.reason)
    if same_assertion(head.payload,updated) and head.state == qualification.state:
        return Mutation(head.ref,head.revision,"duplicate",head.state)
    same_conditions = sorted(set(head.payload["conditions"])) == sorted(set(updated["conditions"]))
    if not same_conditions:
        if retraction:
            raise ContractError("INPUT_INVALID", "retraction_conditions")
        now = clock.utc_now()
        mutation = apply_claim(tx,updated,head.scope_id,now,
                               _subject_bound=principal_matches)
        register_applied_candidate(tx, mutation, now)
        tx.claims.resolve_updates(mutation.ref, resolved_at=now)
        return mutation
    now = clock.utc_now()
    saved = tx.claims.append(head.scope_id,updated,qualification,recorded_at=now,previous=head,
                            expected_revision=request["expected_revision"])
    mutation = Mutation(saved.ref,saved.revision,"retracted" if retraction else "revised",saved.state)
    register_applied_candidate(tx, mutation, now)
    # The ambiguity this claim was a candidate for has now been answered by an
    # authorized revision of it.  Without this, ``unresolved_updates`` only ever
    # grew: ``resolved`` had no writer anywhere in the codebase.
    tx.claims.resolve_updates(mutation.ref, resolved_at=now)
    return mutation


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
    if _REPORTED_ACTION.search(raw):
        return None
    if not (_CORRECTION.search(raw) or _RETRACT.search(raw) or _AMBIGUOUS.search(raw)):
        return None
    refs = tx.claims.correction_refs(raw, source.scope_id)
    principal_ref = _source_principal_ref(source)
    self_reference = principal_ref is not None and first_person_reference(raw)
    if self_reference:
        refs = tuple(dict.fromkeys((*refs, *tx.claims.list_refs(subject=principal_ref))))
    fallback_context = False
    if not refs and _AMBIGUOUS.search(raw):
        # Keep the previous bounded disambiguation context, without selecting
        # any of its candidates as an authorized target.
        refs = tx.claims.list_refs()
        fallback_context = True
    heads = [next(v for v in tx.claims.versions(ref) if v.revision == v.current_revision) for ref in refs]
    heads = [v for v in heads if v.scope_id == source.scope_id and (v.project_id,v.branch_id) == (source.project_id,source.branch_id) and v.state in {"active","disputed"}]
    matches = [v for v in heads if any(bound_literal(raw, s) for s in (v.ref,v.payload["subject"],v.payload["value_text"]))
               or (self_reference and v.payload["subject"] == principal_ref)]
    exact = [v for v in matches if bound_literal(raw, v.ref) or bound_literal(raw, v.payload["predicate"])]
    if exact:
        matches = exact
    tokens = re.findall(r"(?:改为|改成|改用|换成|换为|调整为|调整成|替换为|更换为|用|是|change to|switch to)\s*([A-Za-z0-9\u4e00-\u9fff_.-]{1,120})(?=[。！？；，,;.!?\s]|$)",raw,re.I)
    replacements = re.findall(r"(?:改为|改成|改用|换成|换为|调整为|调整成|替换为|更换为|change to|switch to)\s*([A-Za-z0-9\u4e00-\u9fff_.-]{1,120})(?=[。！？；，,;.!?\s]|$)",raw,re.I)
    if len(matches) == 1:
        old = re.escape(matches[0].payload['value_text'])
        english = re.findall(r'(?i:\breplace)\s+' + old + r'\s+(?i:with)\s+([A-Za-z0-9\u4e00-\u9fff_.-]{1,120})(?=[。！？；，,;.!?\s]|$)', raw)
        tokens.extend(english)
        replacements.extend(english)
    # A sentence-final ASCII full stop is punctuation, not part of the value.
    tokens = [token.rstrip('.') for token in tokens]
    replacements = [token.rstrip('.') for token in replacements]
    if not fallback_context and len(matches) == 1 and len(replacements) <= 1 and (tokens or _RETRACT.search(raw)) and not (_NO_FAST_PATH.search(raw) or RELATIVE_SCOPE.search(raw) or _NEGATED_ACTION.search(raw) or _QUESTION.search(raw)):
        target = matches[0]
        source_time = canonical_time(source.event["occurred_at"])
        target_time = canonical_time(target.valid_from)
        if source_time is not None and target_time is not None and source_time < target_time:
            tx.claims.unresolved(source.ref,source.revision,(target.ref,),recorded_at=clock.utc_now())
            return None
        request = dict(protocol_version="1.1",target_ref=target.ref,expected_revision=target.current_revision,
                       new_value=(replacements[-1] if replacements else None) if _RETRACT.search(raw) else tokens[-1],conditions=target.payload["conditions"],
                       source_evidence_refs=[f"{source.ref}@{source.revision}"],valid_from=source.event["occurred_at"])
        try:
            with tx.savepoint():
                return _revise_in_transaction(tx,clock,request)
        except ContractError:
            # Rejected semantics do not discard the user's captured occurrence.
            pass
    tx.claims.unresolved(source.ref,source.revision,tuple(v.ref for v in (matches or heads)),recorded_at=clock.utc_now())
    return None


def capture_confirmation(tx, source, clock) -> Mutation | None:
    """Promote one proposal a person explicitly adopted.  Model-free.

    The gates exist to stop the *model* asserting what the evidence does not
    support.  A person reading a proposal and keeping it is not that, and is
    the highest authority this system has -- so this does not re-run any gate,
    it records who vouched for the claim.

    Everything that makes it safe is in the targeting: the text must literally
    name the claim, and it must name exactly one.  A bare "记住" resolves to
    nothing and is recorded as unresolved rather than guessed at.
    """
    from .confirmation import CONFIRMED_REASON, confirmation_targets, is_confirmation

    raw = source.event["content"]
    if source.event["origin"] != "human_direct" or source.capture_gaps or source.event["capture_state"] != "complete":
        return None
    if not is_confirmation(raw):
        return None
    refs = tx.claims.proposed_refs(raw, source.scope_id)
    heads = [next((v for v in tx.claims.versions(ref) if v.revision == v.current_revision), None)
             for ref in refs]
    heads = [v for v in heads if v is not None and v.state == "proposed"
             and v.scope_id == source.scope_id
             and (v.project_id, v.branch_id) == (source.project_id, source.branch_id)]
    matches = confirmation_targets(raw, heads, bound_literal=bound_literal)
    if len(matches) != 1:
        if heads:
            tx.claims.unresolved(source.ref, source.revision, tuple(v.ref for v in heads),
                                 recorded_at=clock.utc_now())
        return None
    target = matches[0]
    payload = deepcopy(target.payload)
    spans = list(payload.get("evidence_spans") or ())
    # The confirmation is itself evidence: it is the record of who vouched.
    spans.append({"source_ref": source.ref, "source_revision": source.revision,
                  "quote": raw[:2000]})
    payload["evidence_spans"] = spans
    try:
        with tx.savepoint():
            saved = tx.claims.append(
                target.scope_id, payload,
                Qualification("active", "direct_report", CONFIRMED_REASON),
                recorded_at=clock.utc_now(), previous=target, advance_head=True,
            )
    except ContractError:
        tx.claims.unresolved(source.ref, source.revision, (target.ref,),
                             recorded_at=clock.utc_now())
        return None
    return Mutation(saved.ref, saved.revision, "inserted", saved.state)


def current_claim(storage, clock, context: TrustedContext, ref: str, *, as_of: str | None = None,
                  known_at: str | None = None) -> ClaimVersion | None:
    with storage.read(context) as tx:
        return select_effective(tx.claims.versions(ref),as_of or clock.utc_now(),as_of=as_of is not None,known_at=known_at)
