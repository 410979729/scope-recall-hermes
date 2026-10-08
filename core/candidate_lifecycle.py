"""Public C3 candidate-lifecycle values and model-input formatter.

Candidate processing is metadata beside a claim version.  It never replaces
the fact state and it never grants source, identity or write authority.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..contracts import ContractError, verified_human_principal_ref

if TYPE_CHECKING:
    from .source_records import StoredSource

RULE_VERSION = "r1-candidate-v1"
SOURCE_MATCH_LIMIT = 16
PROCESS_BATCH_LIMIT = 8
DORMANCY_DAYS = 30
#: The words a proposal names its own speaker with.
SELF_SUBJECTS = frozenset({"user", "current_user", "用户", "我"})
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
    refs = (verified_human_principal_ref(source.event.get("source_principal")) for source in sources)
    return frozenset(ref for ref in refs if ref is not None)


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
        return proposed_subject.casefold() in SELF_SUBJECTS
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

    Replayed against the real model on one store's terminally failed evaluations,
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
    if subject in _verified_human_principal_refs(sources) or not candidate_name_matches(
        subject, proposal.get("subject")
    ):
        raise ContractError("DERIVATION_INVALID", "candidate_subject")
    return {**proposal, "subject": subject}


__all__ = [
    "RULE_VERSION",
    "SOURCE_MATCH_LIMIT",
    "PROCESS_BATCH_LIMIT",
    "DORMANCY_DAYS",
    "CandidateSnapshot",
    "CandidateRegistration",
    "CandidateSourceTrigger",
    "CandidateEvaluationSnapshot",
    "CandidateSummary",
    "CandidateEvaluator",
    "candidate_model_subject",
    "candidate_identity_restored",
    "candidate_name_matches",
    "candidate_subject_matches",
]
