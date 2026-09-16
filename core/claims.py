"""Pure Claim qualification and temporal selection; no adapters, SQL or models."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re

from ..contracts import Basis, ClaimProposal, ContractError, Origin, SourcePrincipal
from .fact_actions import ClaimDraft, EvidenceReference
from .fact_evidence import evidence_supports_claim, evidence_supports_relation
from .fact_temporal_semantics import classify_durable_state_clause
from .subject_binding import neighbourhood_binds
from .source_qualification import (AUTHORITY_QUESTION, RELATIVE_SCOPE,
                                   REPORTED_SPEECH, UNASSERTED_UNCERTAINTY,
                                   asserted_marker, bound_literal,
                                   condition_supports_value,
                                   is_transient_request, preserves_qualifiers,
                                   self_report_bound)


@dataclass(frozen=True)
class RootEvidence:
    ref: str
    revision: int
    origin: Origin
    original_origin: str | None
    content: str
    occurred_at: str | None
    capture_state: str
    session_id: str
    import_verified: bool = False
    capture_gaps: tuple[str, ...] = ()
    source_principal: SourcePrincipal | None = None


@dataclass(frozen=True)
class Qualification:
    state: str
    basis: Basis
    reason: str


@dataclass(frozen=True)
class ClaimVersion:
    ref: str
    revision: int
    current_revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None
    payload: ClaimProposal
    state: str
    basis: Basis
    reason: str
    valid_from: str | None
    valid_to: str | None
    recorded_from: str
    recorded_to: str | None
    replaces_revision: int | None
    conflict_revisions: tuple[int, ...]
    suppressed: bool = False


def canonical_time(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ContractError("INPUT_INVALID", "timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("INPUT_INVALID", "timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("INPUT_INVALID", "timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def claim_slot(scope_id: str, project_id: str | None, branch_id: str | None, proposal: ClaimProposal) -> str:
    """Only identical explicit slots merge; aliases/semantic similarity confer no identity."""
    identity = [scope_id, project_id, branch_id, proposal["kind"], proposal["subject"],
                proposal["predicate"], sorted(set(proposal["conditions"]))]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def same_assertion(left: ClaimProposal, right: ClaimProposal) -> bool:
    fields = ("kind", "subject", "predicate", "value_text", "conditions", "statement_kind",
              "valid_from", "valid_to", "procedure", "intention", "alias")
    return all((sorted(set(left[k])) == sorted(set(right[k]))) if k == "conditions" else left.get(k) == right.get(k) for k in fields)


_HYPOTHETICAL = re.compile(r"假设|假如|设想|虚构|假定|\b(?:suppose|hypothetic|fictional|what if)\b", re.I)
_UNDECIDED = re.compile(r"先不要定|还[没未]决定|尚未确定|没有决定|待定|\b(?:not decided|undecided)\b", re.I)
_QUOTED_OTHER = re.compile(r"不是我的|不是我本人|客户原文|\b(?:not my preference|customer said)\b", re.I)
_LIMIT = re.compile(r"只限|仅限|本次|这次|今天|未授权|除非|\b(?:only this|this time|unless|without permission)\b", re.I)
_ACCEPTANCE = re.compile(r"认可|验收通过|按这个方法|就按这样|这个方法[很好对可]|\b(?:accept|approved|use this method)\b", re.I)
_COMPLETION = re.compile(r"已经完成|已完成|检查完成|完成了|\b(?:completed|finished|done)\b", re.I)
_CANCELLATION = re.compile(r"取消|撤销|作废|\b(?:cancel|revoke|withdraw)\b", re.I)
_NEGATED_COMPLETION = re.compile(r"未完成|没[有]?完成|尚未完成|提醒|\b(?:not done|not completed|remind)\b", re.I)
_NEGATION = re.compile(r"不是|并非|没有|未曾|不喜欢|讨厌|\b(?:not|never|dislike)\b", re.I)
_SELF_SUBJECTS = frozenset({"user", "current_user", "用户", "我"})


def _principal_ref(root: RootEvidence) -> str | None:
    principal = root.source_principal
    if not isinstance(principal, dict):
        return None
    if principal.get("kind") != "human" or principal.get("resolution") != "verified":
        return None
    value = principal.get("principal_ref")
    return value if isinstance(value, str) and value else None


def _unresolved_subject(proposal: ClaimProposal, roots: tuple[RootEvidence, ...]) -> str:
    refs = sorted(
        f"{root.ref}@{root.revision}"
        for root in roots
        if any(span["source_ref"] == root.ref and span["source_revision"] == root.revision
               for span in proposal["evidence_spans"])
    )
    digest = hashlib.sha256(json.dumps(refs, separators=(",", ":")).encode()).hexdigest()
    return f"unresolved-source:{digest}"


def bind_claim_subject(
    proposal: ClaimProposal,
    roots: tuple[RootEvidence, ...],
) -> tuple[ClaimProposal, bool, str | None]:
    """Bind a first-person model label to C1 evidence, or isolate it safely.

    The model may repeat ``我`` or a legacy self label, but it cannot mint an
    authority key. Unresolved sources receive a source-specific non-authority
    slot so different speakers in one shared scope cannot collapse together.
    """
    if proposal["subject"].casefold() not in _SELF_SUBJECTS:
        return proposal, False, None
    relevant = tuple(
        root for root in roots
        if effective_origin(root) == "human_direct"
        and any(span["source_ref"] == root.ref and span["source_revision"] == root.revision
                for span in proposal["evidence_spans"])
    )
    bound = deepcopy(proposal)
    if not relevant or not any(
        self_report_bound(
            "\n".join(evidence_context(root.content, span["quote"])
                      for span in proposal["evidence_spans"]
                      if span["source_ref"] == root.ref and span["source_revision"] == root.revision),
            proposal["value_text"],
            kind=proposal["kind"],
        )
        for root in relevant
    ):
        bound["subject"] = _unresolved_subject(proposal, roots)
        return bound, False, "subject_not_bound"
    principals = tuple(_principal_ref(root) for root in relevant)
    if any(principal is None for principal in principals):
        bound["subject"] = _unresolved_subject(proposal, roots)
        return bound, False, "source_identity_unresolved"
    unique = set(principals)
    if len(unique) != 1:
        bound["subject"] = _unresolved_subject(proposal, roots)
        return bound, False, "source_identity_conflict"
    bound["subject"] = next(iter(unique))
    return bound, True, None


def effective_origin(root: RootEvidence) -> str:
    return (root.original_origin or "origin_unknown") if root.origin == "imported" and root.import_verified else ("origin_unknown" if root.origin == "imported" else root.origin)


def grounded_time(value: str | None, roots: tuple[RootEvidence, ...]) -> bool:
    if value is None:
        return True
    canonical_value = canonical_time(value)
    if canonical_value is None:
        return False
    stamp = datetime.fromisoformat(canonical_value)
    for root in roots:
        if root.occurred_at is not None and canonical_time(root.occurred_at) == canonical_time(value):
            return True
        text = root.content
        dates = (stamp.strftime("%Y-%m-%d"), f"{stamp.year}年{stamp.month}月{stamp.day}日")
        if any(date in text for date in dates):
            return True
        if root.occurred_at is not None:
            canonical_source_time = canonical_time(root.occurred_at)
            if canonical_source_time is None:
                continue
            source_time = datetime.fromisoformat(canonical_source_time)
            if source_time.year == stamp.year and f"{stamp.month}月{stamp.day}日" in text:
                return True
            if "今天" in text and source_time.date() == stamp.date() and stamp.hour == stamp.minute == stamp.second == stamp.microsecond == 0:
                return True
            # End of the explicitly stated one-day exception is the following
            # UTC boundary only when the source itself supplies a UTC timestamp.
            if "只限今天" in text and (stamp.date()-source_time.date()).days == 1 and stamp.hour == stamp.minute == stamp.second == stamp.microsecond == 0:
                return True
    return False


def evidence_context(content: str, quote: str) -> str:
    """Include the quote's sentence so trimming cannot discard its negation.

    Unrelated sentences are not evidence for this assertion. A preceding label
    ending in a colon belongs to the quote (for example a fictional scenario).
    Repeated indistinguishable quotations retain all their possible contexts.
    """
    contexts = []
    start = 0
    for _ in range(128):
        at = content.find(quote,start)
        if at < 0:
            break
        before = list(re.finditer(r"[。！？!?;；\n]",content[:at]))
        left = before[-1].end() if before else 0
        quote_end = at+len(quote)
        if quote.rstrip(" \t\r\"“”’'").endswith(tuple("。！？!?;；\n")):
            right = quote_end
        else:
            following = re.search(r"[。！？!?;；\n]",content[quote_end:])
            right = quote_end+following.end() if following else len(content)
        prefix = content[:left].rstrip()
        if prefix.endswith((":","：")):
            header = re.split(r"[。！？!?;；\n]",prefix)[-1]
            left = max(0,left-len(header)-1)
        contexts.append(content[left:right])
        start = at+max(1,len(quote))
    if content.find(quote,start) >= 0:
        raise ContractError("DERIVATION_INVALID", "ambiguous_quote_locations")
    return "\n".join(dict.fromkeys(contexts))


#: Real sentence punctuation *plus* the two-character escapes that stand in for
#: it when a tool envelope is stored as a JSON string body.
_ASSERTION_BREAK = re.compile(r"[。！？!?;；\n]|\\[nrt]")


def _ends_clause(quote: str) -> bool:
    """Whether the quote's own final character already closes its clause."""
    trimmed = quote.rstrip(" \t\r\"\u201c\u201d\u2019'")
    if not trimmed:
        return False
    breaks = list(_ASSERTION_BREAK.finditer(trimmed))
    return bool(breaks) and breaks[-1].end() == len(trimmed)


def assertion_clause(content: str, quote: str) -> str:
    """Return the clause the quote sits in, treating escaped breaks as breaks.

    ``evidence_context`` cuts on real punctuation only, because its job is to
    keep a quote's negation attached to it.  A tool envelope stored as an
    escaped JSON body has no real punctuation to cut on: measured on tianshu,
    **all 16** claims rejected as ``question_not_asserted`` came from sources
    with zero real newlines and 14 to 46 escaped ones, so the context ran 155 to
    1,581 characters and one ``?`` anywhere inside vetoed the claim.  Twelve of
    the sixteen had no question marker in the quoted text at all -- the rejected
    statements were lines like ``Version: 3.1.0.dev2+tianshu.2``.

    This is for tests that must judge *this* assertion rather than everything
    the source happens to mention.  Splitting on ``\\n`` also splits a literal
    Windows path such as ``C:\\new``, which can only ever narrow what such a
    test reads -- acceptable here, and the reason this is not used for the
    negation, polarity or condition checks, which keep the wider context.
    """
    clauses = []
    start = 0
    closes = _ends_clause(quote)
    for _ in range(128):
        at = content.find(quote,start)
        if at < 0:
            break
        before = list(_ASSERTION_BREAK.finditer(content,0,at))
        left = before[-1].end() if before else 0
        end = at+len(quote)
        # A quote whose own last character is the break already ends its
        # clause.  Searching onwards from there finds the *next* sentence's
        # terminator and pulls that whole sentence in -- which is how three
        # correct assertions on tianshu were vetoed by a question standing in
        # the sentence after them, in a message that said in so many words
        # "我只是问…这不是确认".  ``evidence_context`` has always had this
        # guard; this is the same one.
        if closes:
            right = end
        else:
            after = _ASSERTION_BREAK.search(content,end)
            right = after.end() if after else len(content)
        clauses.append(content[left:right])
        start = at+max(1,len(quote))
    return "\n".join(dict.fromkeys(clauses)) if clauses else quote


def qualify(proposal: ClaimProposal, roots: tuple[RootEvidence, ...], *, project_id: str | None = None,
            explicit_attribute_correction: bool = False,
            _subject_bound: bool = False) -> Qualification:
    """Source roles are necessary, never sufficient, to promote a proposal.

    P01 has already checked exact evidence spans against these source versions.
    This rule retains unsupported interpretation as proposed. It neither invents
    dates nor converts the model's requested state into governance authority.
    """
    if not _subject_bound:
        bound_proposal, subject_bound, binding_issue = bind_claim_subject(proposal, roots)
        if binding_issue is not None:
            return Qualification("proposed", "inferred_suggestion", binding_issue)
        if subject_bound:
            return qualify(
                bound_proposal,
                roots,
                project_id=project_id,
                explicit_attribute_correction=explicit_attribute_correction,
                _subject_bound=True,
            )
    # A summary may link to a root for lineage, but cannot lend its own wording
    # that root's authority. Promotion requires a span on the actual root.
    roots = tuple(root for root in roots if any(span["source_ref"] == root.ref and span["source_revision"] == root.revision for span in proposal["evidence_spans"]))
    quoted = "\n".join(span["quote"] for span in proposal["evidence_spans"])
    complete_roots = tuple(r for r in roots if r.capture_state == "complete" and not r.capture_gaps)
    if not complete_roots:
        return Qualification("proposed", "inferred_suggestion", "no_complete_root_span")
    roots = complete_roots
    # Whether this proposal is a question is a property of the sentence it was
    # lifted from, not of everything else the source happens to say.  Taken
    # before the contexts below are computed, because those keep the wider
    # radius the negation and polarity checks depend on.
    asserted_text = "\n".join(assertion_clause(r.content,span["quote"]) for r in roots
                              for span in proposal["evidence_spans"]
                              if span["source_ref"] == r.ref and span["source_revision"] == r.revision)
    # The stored text each cited span came from, kept before roots collapse to
    # per-quote contexts.  Only the subject check below uses it, and only to
    # look one sentence wider than the quote; see core/subject_binding.py.
    cited_spans = tuple(
        (span["quote"], r.content)
        for r in roots
        for span in proposal["evidence_spans"]
        if span["source_ref"] == r.ref and span["source_revision"] == r.revision
    )
    roots = tuple(replace(r,content="\n".join(evidence_context(r.content,span["quote"]) for span in proposal["evidence_spans"]
                             if span["source_ref"] == r.ref and span["source_revision"] == r.revision)) for r in roots)
    source_text = "\n".join(r.content for r in roots)
    kind = proposal["kind"]
    if proposal["statement_kind"] in {"request", "proposal", "hypothetical", "quotation", "fictional", "unknown"}:
        return Qualification("proposed", "inferred_suggestion", "statement_not_asserted")
    if AUTHORITY_QUESTION.search(asserted_text or source_text):
        return Qualification("proposed", "inferred_suggestion", "question_not_asserted")
    if (_HYPOTHETICAL.search(source_text) or _UNDECIDED.search(source_text)
            or UNASSERTED_UNCERTAINTY.search(source_text)):
        return Qualification("proposed", "inferred_suggestion", "hypothetical_or_undecided")
    if is_transient_request(source_text):
        return Qualification("proposed", "inferred_suggestion", "transient_request_not_durable")
    if ((kind in {"preference", "constraint", "decision"} and _QUOTED_OTHER.search(source_text))
            or REPORTED_SPEECH.search(source_text)):
        return Qualification("proposed", "inferred_suggestion", "other_speaker")
    if any(not condition_supports_value(source_text, condition, proposal["value_text"])
           for condition in proposal["conditions"]):
        return Qualification("proposed", "inferred_suggestion", "condition_not_supported")
    if _LIMIT.search(source_text) and not proposal["conditions"] and proposal["valid_to"] is None:
        return Qualification("proposed", "inferred_suggestion", "missing_limitation")
    for marker in RELATIVE_SCOPE.finditer(source_text):
        if not any(bound_literal(condition, marker.group()) for condition in proposal["conditions"]) and proposal["valid_to"] is None:
            return Qualification("proposed", "inferred_suggestion", "relative_scope_not_preserved")
    human = tuple(r for r in roots if effective_origin(r) == "human_direct")
    observed = tuple(r for r in roots if effective_origin(r) == "tool_observation")
    documents = tuple(r for r in roots if effective_origin(r) == "external_document")
    if not human and not observed and not documents:
        return Qualification("proposed", "inferred_suggestion", "no_independent_authority")
    if kind in {"preference", "constraint", "decision", "intention", "alias"} and not human:
        return Qualification("proposed", "inferred_suggestion", "requires_human_source")
    if any(not grounded_time(proposal[field], roots) for field in ("valid_from", "valid_to")):
        return Qualification("proposed", "inferred_suggestion", "time_not_grounded")
    subject = proposal["subject"]
    # Aliases have a separate, exact old-name -> target proof in apply_claim;
    # that proof may name the sourced old label instead of its internal subject.
    if not _subject_bound and kind != "alias" and subject in quoted and not bound_literal(quoted, subject):
        return Qualification("proposed", "inferred_suggestion", "subject_not_bound")
    is_self_subject = subject.casefold() in _SELF_SUBJECTS
    if not _subject_bound and kind != "alias" and subject == "我" and not self_report_bound(source_text, proposal["value_text"], kind=kind):
        return Qualification("proposed", "inferred_suggestion", "subject_not_bound")
    # Second rung: the subject may also be stated verbatim in the sentence next
    # to the quote, in the same source.  Measured on tianshu, 48 of the 92
    # refusals here were that -- "此测试项目的名称为 SRLIVE-…。…不要写业务文件"
    # states the subject one sentence before the clause that was quoted.
    # Nothing is inferred: the rung returns only literal text from the cited
    # source, and a subject that merely points ("you", "the agent") is refused
    # at any distance.
    if (not _subject_bound and kind != "alias" and not bound_literal(quoted, subject)
            and not neighbourhood_binds(subject, cited_spans)
            and not (is_self_subject and self_report_bound(source_text, proposal["value_text"], kind=kind))):
        return Qualification("proposed", "inferred_suggestion", "subject_not_bound")
    if kind == "intention":
        intention = proposal.get("intention")
        if intention is None:
            return Qualification("proposed", "inferred_suggestion", "intention_missing")
        if intention["target"] not in source_text and proposal["predicate"] not in source_text:
            return Qualification("proposed", "inferred_suggestion", "intention_target_unproved")
        if intention["state"] == "pending" and intention["cue"] not in source_text:
            return Qualification("proposed", "inferred_suggestion", "intention_cue_unproved")
        state_sources = set(intention["state_evidence_refs"])
        state_roots = tuple(r for r in roots if f"{r.ref}@{r.revision}" in state_sources)
        authorized_state = "\n".join(r.content for r in state_roots if effective_origin(r) in {"human_direct", "tool_observation"})
        if intention["state"] == "completed" and (not asserted_marker(_COMPLETION,authorized_state) or _NEGATED_COMPLETION.search(authorized_state)):
            return Qualification("proposed", "inferred_suggestion", "completion_unproved")
        if intention["state"] == "cancelled" and not asserted_marker(_CANCELLATION,authorized_state):
            return Qualification("proposed", "inferred_suggestion", "cancellation_unproved")
        if intention["state"] == "expired" and proposal["valid_to"] is None:
            return Qualification("proposed", "inferred_suggestion", "expiry_unproved")
    if kind == "procedure":
        procedure = proposal.get("procedure")
        if procedure is None:
            return Qualification("proposed", "inferred_suggestion", "procedure_missing")
        verification = procedure["verification_basis"]
        if any(step not in source_text for step in procedure["method"]):
            return Qualification("proposed", "inferred_suggestion", "method_steps_unproved")
        if any(condition not in source_text for condition in procedure["non_applicable"]):
            return Qualification("proposed", "inferred_suggestion", "method_exception_unproved")
        if verification == "user_accepted" and not any(asserted_marker(_ACCEPTANCE,r.content) for r in human):
            return Qualification("proposed", "inferred_suggestion", "method_acceptance_unproved")
        if verification == "observed_once" and not observed:
            return Qualification("proposed", "inferred_suggestion", "method_observation_unproved")
        if verification in {"inferred_suggestion", "unknown"}:
            return Qualification("proposed", "inferred_suggestion", "method_is_suggestion")
    # Keep the established claim-frame support check for ordinary durable facts.
    # Finite/dated statements have their own bounded interval, which the legacy
    # current-only lifecycle could not represent.
    if kind == "fact":
        try:
            draft = ClaimDraft.from_parts(subject=proposal["subject"], predicate=proposal["predicate"], value=proposal["value_text"], scope_id="qualification-only")
            evidence = tuple(EvidenceReference(
                "direct_user" if effective_origin(root) == "human_direct" else "external_record",
                root.ref, evidence_context(root.content, span["quote"]),
                proposal["subject"] if _subject_bound and _principal_ref(root) == proposal["subject"] else "",
            ) for root in (*human, *observed, *documents) for span in proposal["evidence_spans"]
                if span["source_ref"] == root.ref and span["source_revision"] == root.revision)
            supported = any(evidence_supports_claim(item, draft) for item in evidence)
            relation_supported = any(evidence_supports_relation(item, draft) for item in evidence)
        except ValueError:
            supported = relation_supported = False
        if not supported and not relation_supported and not explicit_attribute_correction:
            return Qualification("proposed", "inferred_suggestion", "fact_entailment_unproved")
    if kind not in {"procedure", "intention", "alias"} and proposal["value_text"] not in quoted:
        return Qualification("proposed", "inferred_suggestion", "value_not_supported_by_quote")
    if kind in {"fact", "preference", "constraint", "decision"} and not preserves_qualifiers(
        source_text, proposal["value_text"], conditions=proposal["conditions"], polarity_only=True,
    ):
        return Qualification("proposed", "inferred_suggestion", "value_polarity_not_preserved")
    if kind in {"preference", "decision"} and _NEGATION.search(source_text) and not _NEGATION.search(proposal["value_text"]):
        from .claim_normalization import rejects_other_value
        if not rejects_other_value(source_text, proposal):
            return Qualification("proposed", "inferred_suggestion", "negation_not_preserved")
    temporal = classify_durable_state_clause(source_text)
    if temporal == "past" and proposal["valid_from"] is None:
        return Qualification("proposed", "inferred_suggestion", "historical_start_unknown")
    if temporal == "future" and proposal["valid_from"] is None and kind != "intention":
        return Qualification("proposed", "inferred_suggestion", "future_start_unknown")
    if temporal == "temporary" and proposal["valid_to"] is None and not proposal["conditions"]:
        return Qualification("proposed", "inferred_suggestion", "temporary_scope_unknown")
    basis: Basis = "direct_report" if human else "observed"
    return Qualification("active", basis, "explicit_scoped_source" if human else "observation_at_source_time")


def valid_at(version: ClaimVersion, instant: str, *, historical: bool) -> bool:
    when = canonical_time(instant)
    start, end = canonical_time(version.valid_from), canonical_time(version.valid_to)
    if historical and start is None:
        return False
    return (start is None or (when is not None and start <= when)) and (end is None or (when is not None and when < end))


def select_proposal(versions: tuple[ClaimVersion, ...], instant: str) -> ClaimVersion | None:
    """The head of a claim that qualification has never promoted.

    ``select_effective`` deliberately returns None for these, and it is right to:
    a proposal is not an effective version and must never be answered as if it
    were settled. But that also removed them from recall entirely, and on a real
    instance almost the whole derived layer sits in this state — 231 proposed
    against 11 active on tianshu — so 95% of what consolidation produced was
    invisible to every caller, with no error and no gap to notice it by.

    Returning the head here lets retrieval admit it *labelled*, so the reader can
    weigh it, instead of the system silently having nothing to show.

    Not for historical queries: ``as_of`` asks what was true at a past instant,
    and an unpromoted proposal cannot answer that.
    """
    if not versions:
        return None
    heads = [v for v in versions if v.revision == 1 or v.replaces_revision is not None]
    if not heads:
        return None
    head = max(heads, key=lambda v: v.revision)
    if head.state != "proposed" or not valid_at(head, instant, historical=False):
        return None
    return head


def select_effective(versions: tuple[ClaimVersion, ...], instant: str, *, as_of: bool = False, known_at: str | None = None) -> ClaimVersion | None:
    if known_at is not None:
        known_time = canonical_time(known_at)
        if known_time is None:
            return None
        versions = tuple(
            v for v in versions
            if (recorded := canonical_time(v.recorded_from)) is not None and recorded <= known_time
        )
    if not versions:
        return None
    heads = [v for v in versions if v.revision == 1 or v.replaces_revision is not None]
    head = max(heads, key=lambda v: v.revision)
    # A replacement with an unknown effective date can establish an explicitly
    # current assertion, but supplies no answer about a past date.
    if as_of and head.valid_from is None and head.state != "proposed":
        return None
    if head.state != "proposed" and valid_at(head, instant, historical=as_of):
        return None if head.state == "retracted" else head
    candidates = [v for v in versions if v.state in {"active", "superseded", "disputed", "retracted"} and valid_at(v, instant, historical=as_of or head.state == "disputed")]
    if not candidates:
        return None
    chosen = max(candidates, key=lambda v: (canonical_time(v.valid_from) or "", v.revision))
    return None if chosen.state == "retracted" else chosen
