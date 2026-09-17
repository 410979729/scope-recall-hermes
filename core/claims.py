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
    escaped JSON body has no real punctuation to cut on: measured on alpha,
    **all 16** claims rejected as ``question_not_asserted`` came from sources
    with zero real newlines and 14 to 46 escaped ones, so the context ran 155 to
    1,581 characters and one ``?`` anywhere inside vetoed the claim.  Twelve of
    the sixteen had no question marker in the quoted text at all -- the rejected
    statements were lines like ``Version: 3.1.0.dev2+alpha.2``.

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
        # correct assertions on alpha were vetoed by a question standing in
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


_UNASSERTED_STATEMENTS = frozenset({"request", "proposal", "hypothetical", "quotation", "fictional", "unknown"})
_HUMAN_ONLY_KINDS = frozenset({"preference", "constraint", "decision", "intention", "alias"})
#: Origins a cited root may lend authority from; ``_authority`` needs one of them.
_AUTHORITY_ORIGINS = ("human_direct", "tool_observation", "external_document")
#: Kinds proved by something other than the value inside a quote (``_value_preserved``).
_VALUE_FREE_KINDS = frozenset({"procedure", "intention", "alias"})
_POLARITY_KINDS = frozenset({"fact", "preference", "constraint", "decision"})


@dataclass(frozen=True)
class _Cited:
    """One proposal against the complete root spans it actually cites.

    ``roots`` are narrowed to the sentence radius around each quote; ``quoted``
    is the quotes themselves; ``asserted`` is the clause each quote was lifted
    from, taken before narrowing because the negation and polarity rules keep
    the wider radius.  ``human``/``observed``/``documents`` partition the roots
    by effective origin.
    """

    proposal: ClaimProposal
    roots: tuple[RootEvidence, ...]
    quoted: str
    asserted: str
    cited_spans: tuple[tuple[str, str], ...]
    source_text: str
    human: tuple[RootEvidence, ...]
    observed: tuple[RootEvidence, ...]
    documents: tuple[RootEvidence, ...]
    subject_bound: bool
    explicit_attribute_correction: bool

    @property
    def kind(self) -> str:
        return self.proposal["kind"]

    @property
    def subject(self) -> str:
        return self.proposal["subject"]


def _cite(proposal: ClaimProposal, roots: tuple[RootEvidence, ...], *, subject_bound: bool,
          explicit_attribute_correction: bool) -> _Cited | None:
    """Pair every cited span with its complete root; None when no root qualifies."""
    spans = proposal["evidence_spans"]

    def cited(root: RootEvidence):
        return [span for span in spans if span["source_ref"] == root.ref and span["source_revision"] == root.revision]

    # A summary may link to a root for lineage, but cannot lend its own wording
    # that root's authority.  Promotion requires a span on the actual root.
    roots = tuple(r for r in roots if cited(r) and r.capture_state == "complete" and not r.capture_gaps)
    if not roots:
        return None
    narrowed = tuple(replace(r, content="\n".join(evidence_context(r.content, span["quote"]) for span in cited(r))) for r in roots)
    by_origin = {origin: tuple(r for r in narrowed if effective_origin(r) == origin)
                 for origin in _AUTHORITY_ORIGINS}
    return _Cited(
        proposal, narrowed,
        quoted="\n".join(span["quote"] for span in spans),
        asserted="\n".join(assertion_clause(r.content, span["quote"]) for r in roots for span in cited(r)),
        cited_spans=tuple((span["quote"], r.content) for r in roots for span in cited(r)),
        source_text="\n".join(r.content for r in narrowed),
        human=by_origin["human_direct"], observed=by_origin["tool_observation"], documents=by_origin["external_document"],
        subject_bound=subject_bound, explicit_attribute_correction=explicit_attribute_correction,
    )


# Each rule answers with the refusal reason, or None.  Order matters: the first
# refusal wins, and later rules assume the earlier ones passed.
def _not_asserted(c: _Cited) -> str | None:
    if c.proposal["statement_kind"] in _UNASSERTED_STATEMENTS:
        return "statement_not_asserted"
    if AUTHORITY_QUESTION.search(c.asserted or c.source_text):
        return "question_not_asserted"
    if _HYPOTHETICAL.search(c.source_text) or _UNDECIDED.search(c.source_text) or UNASSERTED_UNCERTAINTY.search(c.source_text):
        return "hypothetical_or_undecided"
    if is_transient_request(c.source_text):
        return "transient_request_not_durable"
    if (c.kind in {"preference", "constraint", "decision"} and _QUOTED_OTHER.search(c.source_text)) or REPORTED_SPEECH.search(c.source_text):
        return "other_speaker"
    return None


def _scope_preserved(c: _Cited) -> str | None:
    conditions, valid_to = c.proposal["conditions"], c.proposal["valid_to"]
    if any(not condition_supports_value(c.source_text, condition, c.proposal["value_text"]) for condition in conditions):
        return "condition_not_supported"
    if _LIMIT.search(c.source_text) and not conditions and valid_to is None:
        return "missing_limitation"
    for marker in RELATIVE_SCOPE.finditer(c.source_text):
        if valid_to is None and not any(bound_literal(condition, marker.group()) for condition in conditions):
            return "relative_scope_not_preserved"
    return None


def _authority(c: _Cited) -> str | None:
    if not (c.human or c.observed or c.documents):
        return "no_independent_authority"
    if c.kind in _HUMAN_ONLY_KINDS and not c.human:
        return "requires_human_source"
    if any(not grounded_time(c.proposal[field], c.roots) for field in ("valid_from", "valid_to")):
        return "time_not_grounded"
    return None


def _subject_binds(c: _Cited) -> str | None:
    # Aliases have a separate, exact old-name -> target proof in apply_claim;
    # that proof may name the sourced old label instead of its internal subject.
    if c.subject_bound or c.kind == "alias":
        return None
    subject, kind = c.subject, c.kind
    self_bound = subject.casefold() in _SELF_SUBJECTS and self_report_bound(c.source_text, c.proposal["value_text"], kind=kind)
    if subject in c.quoted and not bound_literal(c.quoted, subject):
        return "subject_not_bound"
    if subject == "我" and not self_bound:
        return "subject_not_bound"
    # The subject may also be stated verbatim in the sentence next to the quote,
    # in the same source; see core/subject_binding.py.  Nothing is inferred: only
    # literal text from the cited source counts, and a subject that merely
    # points ("you", "the agent") is refused at any distance.
    if not bound_literal(c.quoted, subject) and not neighbourhood_binds(subject, c.cited_spans) and not self_bound:
        return "subject_not_bound"
    return None


def _intention_proved(c: _Cited) -> str | None:
    if c.kind != "intention":
        return None
    intention = c.proposal.get("intention")
    if intention is None:
        return "intention_missing"
    if intention["target"] not in c.source_text and c.proposal["predicate"] not in c.source_text:
        return "intention_target_unproved"
    state = intention["state"]
    if state == "pending" and intention["cue"] not in c.source_text:
        return "intention_cue_unproved"
    state_sources = set(intention["state_evidence_refs"])
    authorized = "\n".join(r.content for r in c.roots if f"{r.ref}@{r.revision}" in state_sources
                           and effective_origin(r) in {"human_direct", "tool_observation"})
    if state == "completed" and (not asserted_marker(_COMPLETION, authorized) or _NEGATED_COMPLETION.search(authorized)):
        return "completion_unproved"
    if state == "cancelled" and not asserted_marker(_CANCELLATION, authorized):
        return "cancellation_unproved"
    if state == "expired" and c.proposal["valid_to"] is None:
        return "expiry_unproved"
    return None


def _procedure_proved(c: _Cited) -> str | None:
    if c.kind != "procedure":
        return None
    procedure = c.proposal.get("procedure")
    if procedure is None:
        return "procedure_missing"
    if any(step not in c.source_text for step in procedure["method"]):
        return "method_steps_unproved"
    if any(condition not in c.source_text for condition in procedure["non_applicable"]):
        return "method_exception_unproved"
    verification = procedure["verification_basis"]
    if verification == "user_accepted" and not any(asserted_marker(_ACCEPTANCE, r.content) for r in c.human):
        return "method_acceptance_unproved"
    if verification == "observed_once" and not c.observed:
        return "method_observation_unproved"
    if verification in {"inferred_suggestion", "unknown"}:
        return "method_is_suggestion"
    return None


def _fact_entailed(c: _Cited) -> str | None:
    """Ordinary durable facts keep the claim-frame support check."""
    if c.kind != "fact" or c.explicit_attribute_correction:
        return None
    try:
        draft = ClaimDraft.from_parts(subject=c.subject, predicate=c.proposal["predicate"], value=c.proposal["value_text"], scope_id="qualification-only")
        evidence = tuple(
            EvidenceReference(
                "direct_user" if effective_origin(root) == "human_direct" else "external_record",
                root.ref, evidence_context(root.content, span["quote"]),
                c.subject if c.subject_bound and _principal_ref(root) == c.subject else "",
            )
            for root in (*c.human, *c.observed, *c.documents) for span in c.proposal["evidence_spans"]
            if span["source_ref"] == root.ref and span["source_revision"] == root.revision)
    except ValueError:
        return "fact_entailment_unproved"
    if any(evidence_supports_claim(item, draft) for item in evidence) or any(evidence_supports_relation(item, draft) for item in evidence):
        return None
    return "fact_entailment_unproved"


def _value_preserved(c: _Cited) -> str | None:
    value, conditions = c.proposal["value_text"], c.proposal["conditions"]
    if c.kind not in _VALUE_FREE_KINDS and value not in c.quoted:
        return "value_not_supported_by_quote"
    if c.kind in _POLARITY_KINDS and not preserves_qualifiers(c.source_text, value, conditions=conditions, polarity_only=True):
        return "value_polarity_not_preserved"
    if c.kind in {"preference", "decision"} and _NEGATION.search(c.source_text) and not _NEGATION.search(value):
        from .claim_normalization import rejects_other_value
        if not rejects_other_value(c.source_text, c.proposal):
            return "negation_not_preserved"
    return None


def _time_scope_known(c: _Cited) -> str | None:
    temporal = classify_durable_state_clause(c.source_text)
    valid_from, valid_to = c.proposal["valid_from"], c.proposal["valid_to"]
    if temporal == "past" and valid_from is None:
        return "historical_start_unknown"
    if temporal == "future" and valid_from is None and c.kind != "intention":
        return "future_start_unknown"
    if temporal == "temporary" and valid_to is None and not c.proposal["conditions"]:
        return "temporary_scope_unknown"
    return None


_REFUSALS = (_not_asserted, _scope_preserved, _authority, _subject_binds, _intention_proved,
             _procedure_proved, _fact_entailed, _value_preserved, _time_scope_known)


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
            return qualify(bound_proposal, roots, project_id=project_id,
                           explicit_attribute_correction=explicit_attribute_correction, _subject_bound=True)
    cited = _cite(proposal, roots, subject_bound=_subject_bound, explicit_attribute_correction=explicit_attribute_correction)
    if cited is None:
        return Qualification("proposed", "inferred_suggestion", "no_complete_root_span")
    for rule in _REFUSALS:
        reason = rule(cited)
        if reason is not None:
            return Qualification("proposed", "inferred_suggestion", reason)
    if cited.human:
        return Qualification("active", "direct_report", "explicit_scoped_source")
    return Qualification("active", "observed", "observation_at_source_time")


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
    against 11 active on alpha — so 95% of what consolidation produced was
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
