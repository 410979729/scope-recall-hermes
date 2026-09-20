"""Public C3 candidate-lifecycle values and model-input formatter.

Candidate processing is metadata beside a claim version.  It never replaces
the fact state and it never grants source, identity or write authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import unicodedata
from typing import TYPE_CHECKING, Protocol

from ..contracts import ContractError

if TYPE_CHECKING:
    from .storage import StoredSource

RULE_VERSION = "r1-candidate-v1"
SOURCE_MATCH_LIMIT = 16
PROCESS_BATCH_LIMIT = 8
DORMANCY_DAYS = 30
_SELF_SUBJECTS = frozenset({"user", "current_user", "用户", "我"})
#: Everything a name may be written with that does not change which name it is.
_NOT_NAME = re.compile(r"[\s\"'`*_（）()【】\[\]「」“”‘’]+")


@dataclass(frozen=True)
class CandidateSnapshot:
    ref: str
    revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None
    fact_state: str
    payload: dict
    processing_state: str
    reason: str
    rule_version: str


@dataclass(frozen=True)
class CandidateRegistration:
    ref: str
    revision: int
    processing_state: str
    reason: str
    disposition: str
    evaluation_id: int | None = None
    work_queued: bool = False


@dataclass(frozen=True)
class CandidateSourceTrigger:
    source_ref: str
    source_revision: int
    disposition: str
    matched: int = 0
    scheduled: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class CandidateEvaluationSnapshot:
    evaluation_id: int
    candidate: CandidateSnapshot
    evidence_refs: tuple[tuple[str, int], ...]
    evidence_fingerprint: str
    memory_epoch: int
    state: str
    model_attempted_at: str | None


@dataclass(frozen=True)
class CandidateSummary:
    pending_evaluation: int = 0
    waiting_evidence: int = 0
    dormant: int = 0
    blocked: int = 0
    resolved: int = 0
    archived_other: int = 0
    failed: int = 0
    budget_paused: int = 0
    capability_unavailable: int = 0
    oldest_waiting_at: str | None = None


class CandidateEvaluator(Protocol):
    """Optional model port used by the existing bounded worker.

    The returned text uses the existing ``consolidation_result`` envelope.
    Core performs C2 validation and application after re-reading every source,
    the candidate head, the work lease and ``memory_epoch``.
    """

    def evaluate_candidate(
        self,
        candidate: CandidateSnapshot,
        sources: tuple[StoredSource, ...],
        *,
        remaining_seconds: float,
        validation_feedback: dict[str, str] | None = None,
    ) -> str: ...


def _verified_human_principal_refs(sources: tuple[StoredSource, ...]) -> frozenset[str]:
    refs: set[str] = set()
    for source in sources:
        principal = source.event.get("source_principal")
        if not isinstance(principal, dict):
            continue
        ref = principal.get("principal_ref")
        if (
            principal.get("kind") == "human"
            and principal.get("resolution") == "verified"
            and isinstance(ref, str)
            and ref
        ):
            refs.add(ref)
    return frozenset(refs)


def candidate_model_subject(
    candidate: CandidateSnapshot,
    sources: tuple[StoredSource, ...],
) -> str | None:
    """Return a model-safe subject label without exposing a C1 authority key."""
    subject = candidate.payload.get("subject")
    if not isinstance(subject, str):
        return None
    if subject in _verified_human_principal_refs(sources):
        return "current_user"
    return subject


def candidate_subject_matches(
    candidate: CandidateSnapshot,
    sources: tuple[StoredSource, ...],
    proposed_subject: object,
) -> bool:
    """Accept a natural self label only when C1 evidence can rebind it.

    The model is never allowed to repeat a verified ``principal_ref``. C2's
    normal ``apply_claim`` path performs the authoritative binding, and the
    worker verifies that persisted result before committing the transaction.
    """
    expected = candidate.payload.get("subject")
    if not isinstance(expected, str) or not isinstance(proposed_subject, str):
        return False
    principal_refs = _verified_human_principal_refs(sources)
    if expected in principal_refs:
        return proposed_subject.casefold() in _SELF_SUBJECTS
    return proposed_subject == expected


def _plain(text: object) -> str:
    """One name with nothing that changes how it reads: escapes, spacing, width, case."""
    if not isinstance(text, str) or not text:
        return ""
    unescaped = text
    for _round in range(2):  # a candidate stored through two encodings carries \\"
        try:
            decoded = json.loads(f'"{unescaped}"')
        except ValueError:
            break
        if not isinstance(decoded, str) or decoded == unescaped:
            break
        unescaped = decoded
    return _NOT_NAME.sub("", unicodedata.normalize("NFKC", unescaped)).casefold()


def candidate_name_matches(expected: object, proposed: object) -> bool:
    """Whether a proposed subject or predicate is the candidate's own, written differently.

    Replayed against the real model on one instance's terminally failed evaluations,
    every rejected name was the candidate's: ``embedding_retry.py`` came back as
    ``embedding_retry.py 全文`` from the document's heading, a subject holding
    ``\\"看图\\"`` came back with plain quotes, and a predicate of a whole clause
    came back as its first word with the rest moved into ``value_text``.  So one
    name containing the other, once nothing that changes how it reads is left, is
    the same name -- and ``kimi`` against ``ollama`` still is not.
    """
    left, right = _plain(expected), _plain(proposed)
    if not left or not right:
        return False
    return left in right or right in left


def candidate_identity_restored(
    candidate: CandidateSnapshot,
    sources: tuple[StoredSource, ...],
    proposal: dict,
) -> dict:
    """The proposal carrying the candidate's own identity, or a rejection.

    What the candidate is -- its kind, subject and predicate -- is already
    recorded; an evaluation decides whether the evidence supports it, with what
    value and on which quote.  A name written differently is restored rather than
    rejected, because re-asking cost a second model call and usually came back
    written differently again: one of one instance's candidates was refused four times
    over its predicate.  A name that is not the candidate's is still refused, and
    a kind never is: it is one of a fixed set, so there is nothing to write
    differently.  A verified human principal keeps the existing rule -- the model
    must say a self label and ``apply_claim`` performs the binding.
    """
    for field in ("kind", "predicate"):
        expected = candidate.payload.get(field)
        if proposal.get(field) == expected:
            continue
        if field == "kind" or not candidate_name_matches(expected, proposal.get(field)):
            raise ContractError("DERIVATION_INVALID", f"candidate_{field}")
        proposal = {**proposal, field: expected}
    if candidate_subject_matches(candidate, sources, proposal.get("subject")):
        return proposal
    subject = candidate.payload.get("subject")
    if subject in _verified_human_principal_refs(sources) or not candidate_name_matches(subject, proposal.get("subject")):
        raise ContractError("DERIVATION_INVALID", "candidate_subject")
    return {**proposal, "subject": subject}


#: Candidate re-evaluation carries the whole consolidation prompt (about 11.5 KB
#: of instruction prose and inlined schema) plus a candidate block of its own,
#: so the shared 16 KB consolidation ceiling left roughly 3.4 KB for evidence.
#: Measured against one instance's live database that admitted none of the 115
#: oversized evaluations: the smallest was already 2,892 characters of evidence.
#: This ceiling is the request's real bound — the candidate block is counted
#: inside it, not appended past it — and stays far below the auxiliary model's
#: context window.
CANDIDATE_EVALUATION_INPUT_BUDGET = 64000


#: A source longer than this reaches a candidate evaluation as a window around
#: the candidate's first verified saved quote, falling back to its value or
#: subject when no saved quote matches this source version.
#: Tool output dominated the evidence: replayed over one instance's evaluations, the
#: calls still made after the verdict limit carried 28 M characters, and windows
#: of this size keep 44% of them.  Qualification reads the complete stored source
#: around each quote, so a window changes what the model reads, never what a
#: quote has to satisfy.
EVIDENCE_WINDOW_THRESHOLD = 3000
EVIDENCE_WINDOW_RADIUS = 1500


def _needle_pattern(text: object) -> re.Pattern[str] | None:
    """Match ``text`` whatever spacing or punctuation sits between its letters and digits."""
    characters = [character for character in str(text or "") if character.isalnum()]
    if not characters:
        return None
    return re.compile(r"[\W_]*".join(re.escape(character) for character in characters), re.IGNORECASE)


def evidence_window(source: StoredSource, needles, evidence_spans=()) -> StoredSource:
    """Prefer the first exact saved quote for this source version, then a needle.

    Several distant quotes cannot all fit one bounded contiguous window. Use
    the first valid span in payload order, retaining context on both sides;
    never concatenate fragments or treat a saved span as admission authority.
    """
    from .consolidation_chunks import ChunkedSource, ConsolidationChunk

    content = str(source.event.get("content") or "")
    total = len(content)
    if total <= EVIDENCE_WINDOW_THRESHOLD or getattr(source, "consolidation_window", None) is not None:
        return source
    start, end = 0, EVIDENCE_WINDOW_THRESHOLD
    anchor = None
    for span in evidence_spans:
        if not isinstance(span, dict) or (span.get("source_ref"), span.get("source_revision")) != (source.ref, source.revision):
            continue
        quote = span.get("quote")
        if not isinstance(quote, str) or not quote:
            continue
        position = content.find(quote)
        if position >= 0:
            anchor = (position, position + len(quote))
            break
    if anchor is None:
        for needle in needles:
            pattern = _needle_pattern(needle)
            match = pattern.search(content) if pattern is not None else None
            if match is not None:
                anchor = (match.start(), match.end())
                break
    if anchor is not None:
        start = max(0, anchor[0] - EVIDENCE_WINDOW_RADIUS)
        end = min(total, anchor[1] + EVIDENCE_WINDOW_RADIUS)
    return ChunkedSource(**dict(source.__dict__, event=dict(source.event, content=content[start:end])),
                         consolidation_window=ConsolidationChunk(start, end, total), consolidation_seed=())


def candidate_evaluation_messages(
    candidate: CandidateSnapshot,
    sources: tuple[StoredSource, ...],
    *,
    budget: int = CANDIDATE_EVALUATION_INPUT_BUDGET,
    validation_feedback: dict[str, str] | None = None,
) -> list[dict]:
    """Build a bounded candidate-specific request for the shared model port."""
    from .consolidate import consolidation_messages

    needles = (candidate.payload.get("value_text"), candidate.payload.get("subject"))
    spans = candidate.payload.get("evidence_spans") or ()
    sources = tuple(evidence_window(source, needles, spans) for source in sources)
    messages = consolidation_messages(sources, episode_ref=None, budget=budget,
                                      validation_feedback=validation_feedback)
    candidate_json = json.dumps(
        {
            "candidate_ref": candidate.ref,
            "candidate_revision": candidate.revision,
            "kind": candidate.payload.get("kind"),
            "subject": candidate_model_subject(candidate, sources),
            "predicate": candidate.payload.get("predicate"),
            "value_text": candidate.payload.get("value_text"),
            "conditions": candidate.payload.get("conditions", []),
            "rule_version": candidate.rule_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(candidate_json.encode("utf-8")) > 32768:
        raise ContractError("INPUT_INVALID", "candidate_input_budget")
    messages.insert(0, {
        "role": "system",
        "content": (
            "Re-evaluate only the supplied candidate against the supplied authorized sources. "
            "Return the existing consolidation_result JSON object. source_refs must list every supplied "
            "source version exactly once. Return zero claim_proposals when evidence is insufficient; "
            "otherwise return at most one proposal with the same kind, subject and predicate. "
            "Use the model-safe candidate subject exactly; never output an internal principal_ref. "
            "Do not choose a fact state: Core qualification owns that decision."
        ),
    })
    messages.append({"role": "user", "content": "candidate=" + candidate_json})
    # The consolidation formatter bounded only its own two messages. The system
    # preamble and candidate block above are appended after that check, so
    # without this the request could reach roughly 49 KB while still claiming to
    # be bounded. Raise the same field the consolidation path raises: callers
    # and the oversize recovery already recognise it.
    if sum(len(message["content"].encode("utf-8")) for message in messages) > budget:
        raise ContractError("INPUT_INVALID", "consolidation_input_budget")
    return messages


__all__ = [
    "RULE_VERSION", "SOURCE_MATCH_LIMIT", "PROCESS_BATCH_LIMIT", "DORMANCY_DAYS",
    "CandidateSnapshot", "CandidateRegistration",
    "CandidateSourceTrigger", "CandidateEvaluationSnapshot", "CandidateSummary",
    "CandidateEvaluator", "candidate_evaluation_messages", "candidate_model_subject",
    "candidate_identity_restored", "candidate_name_matches",
    "candidate_subject_matches",
]
