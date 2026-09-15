"""Pure Claim qualification and temporal selection; no adapters, SQL or models."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re

from ..contracts import Basis, ClaimProposal, ContractError, Origin
from ..fact_actions import ClaimDraft, EvidenceReference
from ..fact_evidence import evidence_supports_claim
from ..fact_temporal_semantics import classify_durable_state_clause
from .source_qualification import AUTHORITY_QUESTION,asserted_marker


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


def effective_origin(root: RootEvidence) -> str:
    return (root.original_origin or "origin_unknown") if root.origin == "imported" and root.import_verified else ("origin_unknown" if root.origin == "imported" else root.origin)


def grounded_time(value: str | None, roots: tuple[RootEvidence, ...]) -> bool:
    if value is None:
        return True
    stamp = datetime.fromisoformat(canonical_time(value))
    for root in roots:
        if root.occurred_at is not None and canonical_time(root.occurred_at) == canonical_time(value):
            return True
        text = root.content
        dates = (stamp.strftime("%Y-%m-%d"), f"{stamp.year}年{stamp.month}月{stamp.day}日")
        if any(date in text for date in dates):
            return True
        if root.occurred_at is not None:
            source_time = datetime.fromisoformat(canonical_time(root.occurred_at))
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


def qualify(proposal: ClaimProposal, roots: tuple[RootEvidence, ...], *, project_id: str | None = None) -> Qualification:
    """Source roles are necessary, never sufficient, to promote a proposal.

    P01 has already checked exact evidence spans against these source versions.
    This rule retains unsupported interpretation as proposed. It neither invents
    dates nor converts the model's requested state into governance authority.
    """
    # A summary may link to a root for lineage, but cannot lend its own wording
    # that root's authority. Promotion requires a span on the actual root.
    roots = tuple(root for root in roots if any(span["source_ref"] == root.ref and span["source_revision"] == root.revision for span in proposal["evidence_spans"]))
    quoted = "\n".join(span["quote"] for span in proposal["evidence_spans"])
    complete_roots = tuple(r for r in roots if r.capture_state == "complete" and not r.capture_gaps)
    if not complete_roots:
        return Qualification("proposed", "inferred_suggestion", "no_complete_root_span")
    roots = complete_roots
    roots = tuple(replace(r,content="\n".join(evidence_context(r.content,span["quote"]) for span in proposal["evidence_spans"]
                             if span["source_ref"] == r.ref and span["source_revision"] == r.revision)) for r in roots)
    source_text = "\n".join(r.content for r in roots)
    kind = proposal["kind"]
    if proposal["statement_kind"] in {"proposal", "hypothetical", "quotation", "fictional", "unknown"}:
        return Qualification("proposed", "inferred_suggestion", "statement_not_asserted")
    if AUTHORITY_QUESTION.search(source_text):
        return Qualification("proposed", "inferred_suggestion", "question_not_asserted")
    if _HYPOTHETICAL.search(source_text) or _UNDECIDED.search(source_text):
        return Qualification("proposed", "inferred_suggestion", "hypothetical_or_undecided")
    if kind in {"preference", "constraint", "decision"} and _QUOTED_OTHER.search(source_text):
        return Qualification("proposed", "inferred_suggestion", "other_speaker")
    if _LIMIT.search(source_text) and not proposal["conditions"] and proposal["valid_to"] is None:
        return Qualification("proposed", "inferred_suggestion", "missing_limitation")
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
    if subject not in quoted and subject != project_id and not (
        subject.casefold() in {"user", "current_user", "用户", "我"} and re.search(r"我|\b(?:I|my|we|our)\b", quoted, re.I)
    ):
        return Qualification("proposed", "inferred_suggestion", "subject_not_bound")
    if kind == "intention":
        intention = proposal["intention"]
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
        verification = proposal["procedure"]["verification_basis"]
        if any(step not in source_text for step in proposal["procedure"]["method"]):
            return Qualification("proposed", "inferred_suggestion", "method_steps_unproved")
        if any(condition not in source_text for condition in proposal["procedure"]["non_applicable"]):
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
            supported = any(evidence_supports_claim(EvidenceReference(
                "direct_user" if effective_origin(root) == "human_direct" else "external_record",
                root.ref, evidence_context(root.content,span["quote"]), proposal["subject"] if effective_origin(root) == "human_direct" and proposal["subject"].casefold() in {"user", "current_user", "用户", "我"} else ""), draft)
                for root in (*human, *observed, *documents) for span in proposal["evidence_spans"]
                if span["source_ref"] == root.ref and span["source_revision"] == root.revision)
        except ValueError:
            supported = False
        bounded_literal = not _NEGATION.search(source_text) and all(
            part in quoted for part in (proposal["subject"], proposal["predicate"], proposal["value_text"]))
        if not supported and not bounded_literal:
            return Qualification("proposed", "inferred_suggestion", "fact_entailment_unproved")
    if kind not in {"procedure", "intention", "alias"} and proposal["value_text"] not in quoted:
        return Qualification("proposed", "inferred_suggestion", "value_not_supported_by_quote")
    if kind in {"preference", "decision"} and _NEGATION.search(source_text) and not _NEGATION.search(proposal["value_text"]):
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
    return (start is None or start <= when) and (end is None or when < end)


def select_effective(versions: tuple[ClaimVersion, ...], instant: str, *, as_of: bool = False, known_at: str | None = None) -> ClaimVersion | None:
    if known_at is not None:
        versions = tuple(v for v in versions if canonical_time(v.recorded_from) <= canonical_time(known_at))
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
