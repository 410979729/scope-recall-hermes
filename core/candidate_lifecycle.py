"""Public C3 candidate-lifecycle values and model-input formatter.

Candidate processing is metadata beside a claim version.  It never replaces
the fact state and it never grants source, identity or write authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .storage import StoredSource

RULE_VERSION = "r1-candidate-v1"
SOURCE_MATCH_LIMIT = 16
PROCESS_BATCH_LIMIT = 8
DORMANCY_DAYS = 30
PROCESSING_STATES = frozenset({
    "pending_evaluation", "waiting_evidence", "resolved", "archived", "blocked",
})
_SELF_SUBJECTS = frozenset({"user", "current_user", "用户", "我"})


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


#: Candidate re-evaluation carries the whole consolidation prompt (about 11.5 KB
#: of instruction prose and inlined schema) plus a candidate block of its own,
#: so the shared 16 KB consolidation ceiling left roughly 3.4 KB for evidence.
#: Measured against tianshu's live database that admitted none of the 115
#: oversized evaluations: the smallest was already 2,892 characters of evidence.
#: This ceiling is the request's real bound — the candidate block is counted
#: inside it, not appended past it — and stays far below the auxiliary model's
#: context window.
CANDIDATE_EVALUATION_INPUT_BUDGET = 64000


def candidate_evaluation_messages(
    candidate: CandidateSnapshot,
    sources: tuple[StoredSource, ...],
    *,
    budget: int = CANDIDATE_EVALUATION_INPUT_BUDGET,
) -> list[dict]:
    """Build a bounded candidate-specific request for the shared model port."""
    from .consolidate import consolidation_messages

    messages = consolidation_messages(sources, episode_ref=None, budget=budget)
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
        from ..contracts import ContractError
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
        from ..contracts import ContractError
        raise ContractError("INPUT_INVALID", "consolidation_input_budget")
    return messages


__all__ = [
    "RULE_VERSION", "SOURCE_MATCH_LIMIT", "PROCESS_BATCH_LIMIT", "DORMANCY_DAYS",
    "PROCESSING_STATES", "CandidateSnapshot", "CandidateRegistration",
    "CandidateSourceTrigger", "CandidateEvaluationSnapshot", "CandidateSummary",
    "CandidateEvaluator", "candidate_evaluation_messages", "candidate_model_subject",
    "candidate_subject_matches",
]
