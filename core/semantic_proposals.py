"""Prepare source support and same-slot merge judgements outside publication transactions."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from ..contracts import ContractError
from . import private
from .candidate_lifecycle import model_subject
from .evidence_support import SupportVerdict, positioned, support_fingerprint, validate_positions
from .failure_policy import failure_category
from .language_resources import prompt_text


def prepare_support(value, sources, judge, *, candidates=(), remaining_seconds=1.0, adoption=False, observation=False):
    """Check cheap literal support first; ask only for normalized, positioned evidence.

    Missing or undecided model capability produces a candidate. Provider failures
    propagate to the worker's shared failure policy instead of being retried here.
    """
    from .claims import RootEvidence, qualify

    contents = {(s.ref, s.revision): s.event["content"] for s in sources}
    roots = tuple(
        RootEvidence(
            s.ref,
            s.revision,
            s.event["origin"],
            s.event.get("source_original_origin"),
            s.event["content"],
            s.event["occurred_at"],
            s.event["capture_state"],
            s.session_id,
            bool(s.import_provenance_sha256),
            s.capture_gaps,
            s.event.get("source_principal"),
        )
        for s in sources
    )
    verdicts = []
    deadline = time.monotonic() + remaining_seconds
    candidate_map = candidates if isinstance(candidates, dict) else {}
    failure_code = None
    for proposal in value["claim_proposals"]:
        validate_positions(proposal, contents)
        if not positioned(proposal):
            continue
        eligible = candidate_map.get(id(proposal), candidates if not candidate_map else ())
        fingerprint = support_fingerprint(proposal, contents)
        literal = qualify(proposal, roots, _legacy=True).state == "active"
        if not adoption and not observation and literal and (not eligible or judge is None):
            verdicts.append(SupportVerdict(fingerprint, "supported"))
            continue
        if judge is None or failure_code is not None or time.monotonic() >= deadline:
            verdicts.append(SupportVerdict(fingerprint, "unavailable", failure_code=failure_code))
            continue
        try:
            private.require_outbound()
            raw = judge(
                support_messages(proposal, sources, eligible, adoption=adoption, observation=observation),
                remaining_seconds=max(0.001, deadline - time.monotonic()),
            )
        except (RuntimeError, OSError) as exc:
            failure_code = _support_failure_code(exc)
            state = "unavailable_" + failure_category(failure_code)
            verdicts.append(SupportVerdict(fingerprint, state, failure_code=failure_code))
            continue
        result = judgement_result(raw, eligible)
        state, action = result["support"], result["action"]
        verdicts.append(SupportVerdict(fingerprint, state, result["target_ref"], result["target_revision"], action))
    return tuple(verdicts)


def judgement_result(raw, candidates):
    """Validate the shared support/action decision against only the supplied versions."""
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) != {"support", "action", "target_ref", "target_revision"}:
        raise ContractError("DERIVATION_INVALID", "support_verdict")
    if result["support"] not in {"supported", "unsupported", "uncertain"} or result["action"] not in {
        "new",
        "update",
        "duplicate",
    }:
        raise ContractError("DERIVATION_INVALID", "support_verdict")
    if result["action"] != "new" and (
        type(result["target_ref"]) is not str or type(result["target_revision"]) is not int
    ):
        raise ContractError("DERIVATION_INVALID", "merge_target")
    target = next(
        (c for c in candidates if (c["ref"], c["revision"]) == (result["target_ref"], result["target_revision"])), None
    )
    if (result["action"] == "new" and (result["target_ref"] is not None or result["target_revision"] is not None)) or (
        result["action"] != "new" and target is None
    ):
        raise ContractError("DERIVATION_INVALID", "merge_target")
    return result


def _merge_alias_live(tx, alias, now):
    """A semantic alias cannot nominate slots after either identity endpoint changes."""
    from .aliases import entity_alias_proof

    proof = (
        tx._check()
        .execute(
            "SELECT 1 FROM entity_alias_proofs WHERE alias_ref=? AND alias_revision=?", (alias.ref, alias.revision)
        )
        .fetchone()
    )
    if proof is None:
        return True
    versions = tx.claims.versions(alias.payload["alias"]["target_ref"])
    target = next((v for v in versions if v.revision == v.current_revision), None)
    return target is not None and entity_alias_proof(tx, alias, target, now)


def merge_candidates(tx, proposal, scope_id, *, now, entity=None, vector_hits=()):
    """Find same-slot candidates by normalized components and verified aliases in this audience."""
    from .claims import bind_claim_subject, evidence_refs, valid_at
    from .fact_identity import normalize_fact_component

    if entity is not None:
        from .entity_candidates import entity_candidates

        return entity_candidates(tx, entity, now=now, vector_hits=vector_hits)
    roots = tx.claims.roots(evidence_refs(proposal))
    bound, _, issue = bind_claim_subject(proposal, roots, semantic_support=True)
    if issue:
        return ()
    subject = normalize_fact_component(bound["subject"])
    predicate = normalize_fact_component(bound["predicate"])
    subjects = {subject}
    for ref in tx.claims.merge_lookup(scope_id, alias_name=proposal["subject"], instant=now):
        alias = next((v for v in tx.claims.versions(ref) if v.revision == v.current_revision), None)
        if (
            alias
            and alias.state == "active"
            and alias.scope_id == scope_id
            and valid_at(alias, now, historical=False)
            and _merge_alias_live(tx, alias, now)
        ):
            subjects.add(normalize_fact_component(alias.payload["subject"]))
    found = []
    refs = tx.claims.merge_lookup(
        scope_id, subjects=subjects, predicate=predicate, kind=proposal["kind"], conditions=proposal["conditions"]
    )
    for ref in refs:
        versions = tx.claims.versions(ref)
        head = next((v for v in versions if v.revision == v.current_revision), None)
        if head is None or head.scope_id != scope_id or head.state not in {"active", "proposed", "disputed"}:
            continue
        if (
            normalize_fact_component(head.payload["subject"]) in subjects
            and normalize_fact_component(head.payload["predicate"]) == predicate
            and sorted(head.payload["conditions"]) == sorted(proposal["conditions"])
        ):
            found.append(dict(ref=head.ref, revision=head.revision, payload=head.payload))
    return tuple(found[:16])


def prepare_publication(storage, context, value, sources, judge, *, remaining_seconds=1.0, clock=None):
    """Read candidate slots before judgement, without retaining a transaction across the request."""
    if not any(positioned(proposal) for proposal in value["claim_proposals"]):
        return ()
    candidates = {}
    now = clock.utc_now() if clock is not None else datetime.now(UTC).isoformat()
    full_sources = []
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        for source in sources:
            current = tx.source(source.ref, source.revision)
            if current is None:
                raise ContractError("SOURCE_MISSING")
            full_sources.append(current)
        for proposal in value["claim_proposals"]:
            if positioned(proposal):
                scope_id = next(
                    (s.scope_id for s in sources if s.ref == proposal["evidence_spans"][0]["source_ref"]), None
                )
                if scope_id is not None:
                    candidates[id(proposal)] = merge_candidates(tx, proposal, scope_id, now=now)
    targets = tuple((item["ref"], item["revision"]) for entries in candidates.values() for item in entries)
    with private.using_outbound_check(
        lambda: _publication_blocked(storage, context, tuple(full_sources), targets, clock)
    ):
        return prepare_support(
            value, tuple(full_sources), judge, candidates=candidates, remaining_seconds=remaining_seconds
        )


def support_messages(proposal, sources, candidates=(), *, adoption=False, observation=False):
    """Format a judgement using already authorized source windows and merge candidates."""
    identities = {}
    safe_proposal = dict(proposal, subject=model_subject(proposal["subject"], sources, identities=identities))
    safe_candidates = [
        dict(
            item,
            payload=dict(
                item["payload"], subject=model_subject(item["payload"]["subject"], sources, identities=identities)
            ),
        )
        for item in candidates
    ]
    adoption_rule = (
        (
            " The human explicitly adopted only the selected assistant slices. Judge the resulting decision, "
            "not the truth of assistant claims. Require all owner exclusions and prerequisites to remain. "
            "Adoption supports an intention only: completed actions or successful execution are unsupported. "
            "The attribution prefix is code-owned metadata, not additional evidence."
        )
        if adoption
        else ""
    )
    observation_rule = (
        " This is an inductive observation, not a quotation or authorization. Judge its complete body, "
        "subject and qualifications against every cited session. Require a useful recurring pattern or "
        "cross-session synthesis supported by at least two different sessions. Contradictions, stronger "
        "quantifiers and invented habits are unsupported. Only action new is allowed."
        if observation
        else ""
    )
    messages = [
        {
            "role": "system",
            "content": prompt_text("support") + " " + prompt_text("merge") + adoption_rule + observation_rule,
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "proposal": safe_proposal,
                    "sources": [
                        {
                            "source_ref": s.ref,
                            "source_revision": s.revision,
                            "content": (
                                "\n".join(
                                    span["quote"]
                                    for span in proposal["evidence_spans"]
                                    if (span["source_ref"], span["source_revision"]) == (s.ref, s.revision)
                                )
                                if adoption
                                else _support_window(proposal, s)
                            ),
                        }
                        for s in sources
                    ],
                    "candidates": safe_candidates,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
    if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > 64000:
        raise ContractError("INPUT_INVALID", "support_input_budget")
    return messages


def _support_window(proposal, source):
    """Include complete cited sentences while bounding the independent judgement request."""
    from .claims import evidence_span_context

    spans = [
        span
        for span in proposal["evidence_spans"]
        if (span["source_ref"], span["source_revision"]) == (source.ref, source.revision)
    ]
    content = "\n".join(evidence_span_context(source.event["content"], span) for span in spans)
    if len(content.encode("utf-8")) > 32000:
        raise ContractError("INPUT_INVALID", "support_input_budget")
    return content


def _support_failure_code(exc):
    """Keep bounded machine error codes, never exception text or provider response bodies."""
    code = getattr(exc, "error_type", "model_unavailable")
    detail = getattr(exc, "detail", None)
    if code == "http_status" and isinstance(detail, str) and len(detail) == 3 and detail.isdigit():
        return "http_" + detail
    return (
        code if isinstance(code, str) and len(code) <= 80 and code.replace("_", "").isalnum() else "model_unavailable"
    )


def _publication_blocked(storage, context, sources, targets, clock):
    """Recheck source authority, selected target versions and private policy before every judgement request."""
    versions = []
    with storage.read(context, remaining_seconds=1.0) as tx:
        for source in sources:
            tx.claims.require_live_source(source.ref, source.revision)
            current = tx.source(source.ref, source.revision)
            if current is None or current.suppressed:
                raise ContractError("ACCESS_DENIED", "authority_revoked")
        for ref, revision in targets:
            version = tx.claims.version(ref, revision)
            if version is None or version.suppressed:
                raise ContractError("ACCESS_DENIED", "authority_revoked")
            if version.current_revision != revision:
                raise ContractError("VERSION_CONFLICT", "memory_epoch")
            tx.claims.require_target(version)
            versions.append(version)
    return clock is not None and private.outbound_blocked(storage, clock, context, sources=sources, claims=versions)
