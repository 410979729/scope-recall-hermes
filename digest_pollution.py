"""Deterministic anti-pollution checks for digest candidate batches.

The model prompt is advisory.  These checks are the final fail-closed barrier for
one-off task snapshots, audit/test status, invented claim values, and mutually
inconsistent single-value facts from the same evidence batch.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any

from .capture_filters import sanitize_report_text
from .fact_actions import EvolutionAction, EvolutionProposal
from .fact_evidence import (
    AUTHORITATIVE_EVIDENCE_SOURCE_TYPES,
    evidence_supports_claim,
)
from .transcript_overlap import is_source_transcript_copy


@dataclass(frozen=True, slots=True)
class DigestPollutionAssessment:
    """Bounded reason codes for a candidate that must stay out of recall."""

    reason_codes: tuple[str, ...] = ()

    @property
    def quarantined(self) -> bool:
        return bool(self.reason_codes)

    def with_reason(self, reason: str) -> "DigestPollutionAssessment":
        return DigestPollutionAssessment(
            tuple(dict.fromkeys((*self.reason_codes, str(reason))))
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "quarantined": self.quarantined,
            "reason_codes": list(self.reason_codes),
        }


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "task_state_snapshot",
        re.compile(
            r"(?:\bPLAN_STATE\b|\blast_completed_task\b|\bcurrent_(?:task|phase)\b|"
            r"\bnext_action\b|\bT\d{1,3}[-_:][^\n]{0,80}\b(?:complete|completed|"
            r"pending|in[ _-]?progress)\b|(?:当前任务|当前阶段|下一步|待办|任务进度)\s*[:：])",
            re.IGNORECASE,
        ),
    ),
    (
        "test_run_snapshot",
        re.compile(
            r"(?:\b\d+\s+(?:tests?\s+)?(?:passed|failed|skipped|warnings?)\b|"
            r"\b(?:pytest|test suite|tests?)\b[^\n]{0,40}\b(?:passed|failed|green|red)\b|"
            r"(?:测试|用例)[^\n]{0,24}(?:\d+\s*(?:个|项)?\s*)?(?:通过|失败|全绿))",
            re.IGNORECASE,
        ),
    ),
    (
        "repository_audit_snapshot",
        re.compile(
            r"(?:\b(?:commit(?:\s+sha)?|HEAD)\s*[:=]?\s*[0-9a-f]{7,40}\b|"
            r"\b(?:git status|worktree)\b[^\n]{0,40}\b(?:clean|dirty)\b|"
            r"\b(?:branch|tag)\s*[:=]\s*[^\s,;]+|"
            r"\b(?:PR|issue|release)\s*#?\d+\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "historical_snapshot",
        re.compile(
            r"(?:historical task snapshot|context compaction|prior context|"
            r"conversation summary for continuation|历史任务快照|上下文压缩|旧快照|"
            r"截至\s*\d{4}[-/年]\d{1,2})",
            re.IGNORECASE,
        ),
    ),
)

_SUCCESS_RE = re.compile(r"\b(?:passed|success|succeeded|green|fixed)\b|通过|成功|已修复", re.IGNORECASE)
_FAILURE_RE = re.compile(r"\b(?:failed|failure|error|blocked|red)\b|失败|报错|阻塞", re.IGNORECASE)
_FACT_SLOT_TYPES = {"factual", "preference", "project", "resource", "constraint"}
_NORMATIVE_TEST_GATE_RE = re.compile(
    r"(?:\b(?:require|requires|required|must|minimum|at\s+least|gate)\b|"
    r"(?:要求|必须|至少|门槛|准入))[^\n]{0,80}"
    r"(?:\b\d+\s+(?:tests?\s+)?passed\b|(?:测试|用例)[^\n]{0,20}(?:通过|全绿))",
    re.IGNORECASE,
)


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _proposal(candidate: Any) -> EvolutionProposal | None:
    value = getattr(candidate, "evolution", None)
    return value if isinstance(value, EvolutionProposal) else None


def _candidate_evidence(
    candidate: Any,
    batch_evidence: Mapping[str, Sequence[str]],
) -> list[str]:
    session_ids = [str(getattr(candidate, "session_id", "") or "")]
    raw_session_ids = getattr(candidate, "session_ids", ())
    if isinstance(raw_session_ids, Sequence) and not isinstance(
        raw_session_ids, (str, bytes)
    ):
        session_ids.extend(str(item or "") for item in raw_session_ids)
    output: list[str] = []
    for session_id in dict.fromkeys(session_ids):
        output.extend(
            str(item) for item in batch_evidence.get(session_id, ()) if str(item)
        )
    proposal = _proposal(candidate)
    if proposal is not None:
        output.extend(reference.quote for reference in proposal.evidence_refs if reference.quote)
    return list(dict.fromkeys(output))


def _text_reasons(text: str, *, memory_type: str) -> list[str]:
    safe = sanitize_report_text(str(text or ""))
    reasons = [reason for reason, pattern in _RULES if pattern.search(safe)]
    normalized_type = str(memory_type or "").strip().lower()
    if (
        "test_run_snapshot" in reasons
        and normalized_type in {"procedure", "workflow", "constraint"}
        and _NORMATIVE_TEST_GATE_RE.search(safe)
    ):
        reasons.remove("test_run_snapshot")
    return reasons


def _evidence_reasons(candidate: Any, evidence: Sequence[str]) -> list[str]:
    if not evidence:
        return []
    content = str(getattr(candidate, "content", "") or "")
    reasons: list[str] = []
    if is_source_transcript_copy(content, evidence):
        reasons.append("source_transcript_overlap")
    proposal = _proposal(candidate)
    if proposal is None:
        return reasons
    combined = _normalized_text("\n".join(evidence))
    claim = proposal.claim
    memory_type = str(getattr(candidate, "memory_type", "") or "").strip().lower()
    if (
        memory_type in _FACT_SLOT_TYPES
        and claim is not None
        and proposal.action
        in {
            EvolutionAction.ADD,
            EvolutionAction.ENRICH,
            EvolutionAction.SUPERSEDE,
            EvolutionAction.RETRACT,
        }
    ):
        authoritative_refs = [
            reference
            for reference in proposal.evidence_refs
            if reference.source_type.strip().lower()
            in AUTHORITATIVE_EVIDENCE_SOURCE_TYPES
        ]
        if authoritative_refs and any(
            not evidence_supports_claim(reference, claim)
            for reference in authoritative_refs
        ):
            reasons.append("claim_not_supported_by_authoritative_evidence")
        if _normalized_text(claim.display_value) not in combined:
            reasons.append("claim_value_not_in_batch_evidence")

    content_success = bool(_SUCCESS_RE.search(content))
    content_failure = bool(_FAILURE_RE.search(content))
    evidence_success = bool(_SUCCESS_RE.search(combined))
    evidence_failure = bool(_FAILURE_RE.search(combined))
    if content_success and not content_failure and evidence_failure and not evidence_success:
        reasons.append("candidate_contradicts_batch_outcome")
    if content_failure and not content_success and evidence_success and not evidence_failure:
        reasons.append("candidate_contradicts_batch_outcome")
    return reasons


def assess_digest_batch(
    candidates: Sequence[Any],
    *,
    batch_evidence: Mapping[str, Sequence[str]] | None = None,
) -> list[DigestPollutionAssessment]:
    """Assess an ordered candidate batch without mutating candidates or storage."""

    evidence_map = batch_evidence if isinstance(batch_evidence, Mapping) else {}
    assessments: list[DigestPollutionAssessment] = []
    for candidate in candidates:
        reasons = _text_reasons(
            str(getattr(candidate, "content", "") or ""),
            memory_type=str(getattr(candidate, "memory_type", "") or ""),
        )
        reasons.extend(
            _evidence_reasons(candidate, _candidate_evidence(candidate, evidence_map))
        )
        assessments.append(
            DigestPollutionAssessment(tuple(dict.fromkeys(reasons)))
        )

    single_slots: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for index, candidate in enumerate(candidates):
        proposal = _proposal(candidate)
        if proposal is None:
            continue
        claim = proposal.claim
        memory_type = str(
            getattr(candidate, "memory_type", "") or ""
        ).strip().lower()
        if memory_type not in _FACT_SLOT_TYPES:
            continue
        if claim is None or claim.cardinality != "single":
            continue
        if proposal.action not in {
            EvolutionAction.ADD,
            EvolutionAction.SUPERSEDE,
        }:
            continue
        single_slots[claim.fact_key][claim.value_fingerprint].append(index)
    for values in single_slots.values():
        if len(values) <= 1:
            continue
        for indexes in values.values():
            for index in indexes:
                assessments[index] = assessments[index].with_reason(
                    "same_batch_single_value_conflict"
                )
    return assessments


__all__ = ["DigestPollutionAssessment", "assess_digest_batch"]
