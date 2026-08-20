"""Digest leave-state and durable-quality port.

Owns the journal-digest contract that used to be scattered across
``journal.py`` / ``journal_store.py``: every loaded entry leaves as
processed, deferred, quarantined, or retryable-pending, and obvious tool
noise cannot consume the extractor.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

from .gating import clean_text
from .journal_llm import JournalDigestLLMError

LEAVE_PROCESSED = "processed"
LEAVE_DEFERRED = "deferred"
LEAVE_QUARANTINED = "quarantined"
LEAVE_PENDING = "retryable-pending"


def _coerce_int(value: object, default: int = 0) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default

HIGH_VALUE_DURABLE_SIGNAL_RE = re.compile(
    r"\b(?:preference|prefers|constraint|policy|api\s+boundary|environment\s+fact|"
    r"root\s+cause|fix|workaround|verification|verified|reusable|workflow|procedure|"
    r"runbook|pitfall|design\s+decision|stable|rollback|guardrail)\b|"
    r"(?:偏好|约束|边界|环境事实|根因|修复|验证|可复用|流程|步骤|规程|坑|设计决策|稳定|回滚|防护)",
    re.IGNORECASE,
)
# Bare modal verbs (should/must/requires) appear in tool-noise fixtures and
# must not promote a row into the extractor. Only verified/rollback/guardrail
# (and the matching Chinese tokens) may lift a tool trace.
TOOL_PROMOTION_SIGNAL_RE = re.compile(
    r"\b(?:verified|rollback|guardrail)\b|(?:验证|回滚|防护)",
    re.IGNORECASE,
)
TOOL_NOISE_RE = re.compile(
    r"tool execution (?:summary|trace)|output_preview=omitted|execute_code|skill_view|"
    r"search_files|read_file|tool execute_",
    re.IGNORECASE,
)
COMPACTION_MARKERS = (
    "[CONTEXT COMPACTION",
    "Historical Task Snapshot",
    "[Recent Telegram chat history",
)


@dataclass(frozen=True)
class LeavePlan:
    skipped_ids: list[int] = field(default_factory=list)
    quarantined_ids: list[int] = field(default_factory=list)
    attempts_quarantined_ids: list[int] = field(default_factory=list)
    retryable_quarantined_ids: list[int] = field(default_factory=list)
    deferred_ids: list[int] = field(default_factory=list)
    pending_ids: list[int] = field(default_factory=list)
    applied_ids: list[int] = field(default_factory=list)

    @property
    def terminal_ids(self) -> set[int]:
        return set(self.applied_ids) | set(self.skipped_ids) | set(self.quarantined_ids)


def clamp_dynamic_digest_limit(
    *,
    backlog: int,
    configured_limit: int,
    configured_threshold: object = None,
    configured_ceiling: object = None,
) -> int:
    """Scale a digest window without honoring stale 2000/1200 defaults."""

    window = max(1, int(configured_limit or 1))
    pending = max(0, int(backlog or 0))
    raw_threshold = _coerce_int(configured_threshold)
    auto_threshold = max(1, window * 4)
    if raw_threshold <= 0 or raw_threshold >= 2000:
        threshold = auto_threshold
    else:
        threshold = max(1, raw_threshold)
    if pending <= threshold:
        return window
    raw_ceiling = _coerce_int(configured_ceiling)
    auto_ceiling = max(window * 8, window, 8)
    if raw_ceiling <= 0 or raw_ceiling >= 1200:
        ceiling = auto_ceiling
    else:
        ceiling = max(window, raw_ceiling)
    return min(pending, max(window, ceiling))


def _as_id_set(values: Iterable[object]) -> set[int]:
    ids: set[int] = set()
    for value in values:
        if isinstance(value, bool):
            ids.add(int(value))
            continue
        if isinstance(value, int):
            ids.add(value)
            continue
        if isinstance(value, float):
            ids.add(int(value))
            continue
        if isinstance(value, (str, bytes, bytearray)):
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                continue
    return ids


def plan_loaded_leave(
    *,
    loaded_ids: Iterable[object],
    candidate_ids: Iterable[object],
    reviewed_ids: Iterable[object],
    unresolved_ids: Iterable[object],
    retryable_unresolved_ids: Iterable[object],
    deferred_ids: Iterable[object],
    applied_ids: Iterable[object],
    attempts_after: dict[int, int] | None = None,
    quarantine_threshold: int = 3,
    retryable_failures_after: dict[int, int] | None = None,
    retryable_failures_threshold: int = 3,
    pollution_ids: Iterable[object] = (),
) -> LeavePlan:
    """Turn one loaded window into the four exclusive leave states.

    Deterministic-attempt quarantine, retryable-budget quarantine, and digest
    pollution share this plan so a row cannot be double-marked. A candidate
    that was not checkpointed/applied, including dry-run, stays pending rather
    than disappearing from the partition. Pollution is exactly quarantined.
    """

    loaded = _as_id_set(loaded_ids)
    pollution = _as_id_set(pollution_ids) & loaded
    candidates = _as_id_set(candidate_ids) & loaded
    reviewed = (_as_id_set(reviewed_ids) & loaded) - pollution
    unresolved = (_as_id_set(unresolved_ids) & loaded) - pollution
    retryable = (_as_id_set(retryable_unresolved_ids) & loaded) - pollution
    deferred = (_as_id_set(deferred_ids) & loaded) - pollution
    applied = (_as_id_set(applied_ids) & loaded) - pollution
    attempts = attempts_after or {}
    retryable_after = retryable_failures_after or {}
    threshold = max(1, int(quarantine_threshold or 3))
    retryable_threshold = max(1, int(retryable_failures_threshold or 3))

    unresolved_without_candidate = sorted((unresolved - candidates) - applied)
    reviewed_without_candidate = sorted((reviewed - unresolved) - candidates - applied)
    countable = [entry_id for entry_id in unresolved_without_candidate if entry_id not in retryable]
    attempts_quarantined = sorted(
        entry_id
        for entry_id in countable
        if int(attempts.get(entry_id, 0) or 0) >= threshold
    )
    retryable_quarantined = sorted(
        entry_id
        for entry_id in unresolved_without_candidate
        if entry_id in retryable
        and int(retryable_after.get(entry_id, 0) or 0) >= retryable_threshold
    )
    quarantined = sorted(set(attempts_quarantined) | set(retryable_quarantined) | set(pollution))
    pending = [
        entry_id
        for entry_id in unresolved_without_candidate
        if entry_id not in set(quarantined)
    ]
    leftover = loaded - candidates - reviewed - unresolved - deferred - applied - pollution
    # Leftover is a successful parse that never cited the row. That is
    # retryable-pending, not budget-defer and not a provider retry failure.
    pending.extend(sorted(leftover))
    unapplied_candidates = sorted((candidates - applied) - set(quarantined) - set(pending))
    pending.extend(unapplied_candidates)
    return LeavePlan(
        skipped_ids=reviewed_without_candidate,
        quarantined_ids=quarantined,
        attempts_quarantined_ids=attempts_quarantined,
        retryable_quarantined_ids=retryable_quarantined,
        deferred_ids=sorted(deferred - candidates - reviewed - unresolved - applied),
        pending_ids=pending,
        applied_ids=sorted(applied),
    )


def next_session_resume_after_id(
    *,
    covered_ids: Iterable[object],
    loaded_ids: Iterable[object],
) -> int:
    """Advance a per-session digest cursor past the last covered entry.

    Covered rows were in an extractor chunk this run. Budget-deferred overflow
    is excluded so the next load starts at that overflow instead of the same
    prefix. If nothing was covered, the cursor jumps to the last loaded id so
    the following run wraps instead of spinning.
    """

    covered = _as_id_set(covered_ids)
    if covered:
        return max(covered)
    loaded = _as_id_set(loaded_ids)
    return max(loaded) if loaded else 0


def leave_plan_receipt_actions(
    plan: LeavePlan,
    *,
    unresolved_ids: Iterable[object],
    retryable_unresolved_ids: Iterable[object],
    quarantine_threshold: int,
    retryable_failures_threshold: int = 3,
) -> list[dict[str, Any]]:
    """Build bounded, non-content leave-state actions for one loaded window."""

    unresolved = _as_id_set(unresolved_ids)
    retryable = _as_id_set(retryable_unresolved_ids)
    pending_unresolved = [entry_id for entry_id in plan.pending_ids if entry_id in unresolved]
    pending_leftover = [entry_id for entry_id in plan.pending_ids if entry_id not in unresolved]
    actions: list[dict[str, Any]] = []
    if pending_unresolved:
        actions.append(
            {
                "action": "pending",
                "reason": "chunk extraction unresolved",
                "entry_count": len(pending_unresolved),
                "entry_ids": pending_unresolved[:20],
                "retryable_count": len(
                    [entry_id for entry_id in pending_unresolved if entry_id in retryable]
                ),
            }
        )
    if pending_leftover:
        actions.append(
            {
                "action": "pending",
                "reason": "parsed chunk did not cite this loaded entry",
                "entry_count": len(pending_leftover),
                "entry_ids": pending_leftover[:20],
                "retryable_count": len(pending_leftover),
            }
        )
    if plan.deferred_ids:
        actions.append(
            {
                "action": "deferred",
                "reason": "per-session chunk budget reached",
                "entry_count": len(plan.deferred_ids),
                "entry_ids": plan.deferred_ids[:20],
            }
        )
    if plan.attempts_quarantined_ids:
        actions.append(
            {
                "action": "quarantine",
                "reason": "chunk extraction unresolved after bounded attempts",
                "entry_count": len(plan.attempts_quarantined_ids),
                "entry_ids": plan.attempts_quarantined_ids[:20],
                "attempts_threshold": max(1, int(quarantine_threshold or 3)),
            }
        )
    if plan.retryable_quarantined_ids:
        actions.append(
            {
                "action": "quarantine",
                "reason": "persistent retryable extractor failure after durable budget",
                "entry_count": len(plan.retryable_quarantined_ids),
                "entry_ids": plan.retryable_quarantined_ids[:20],
                "retryable_failures_threshold": max(
                    1, int(retryable_failures_threshold or 3)
                ),
            }
        )
    if plan.skipped_ids:
        actions.append(
            {
                "action": "skip",
                "reason": "no durable memory candidate",
                "entry_count": len(plan.skipped_ids),
                "entry_ids": plan.skipped_ids[:20],
            }
        )
    return actions


def loaded_leave_sets(
    plan: LeavePlan,
    *,
    admission_ids: Iterable[object] = (),
) -> dict[str, list[int]]:
    """Partition one window into the four exclusive leave states."""

    admission = _as_id_set(admission_ids)
    processed = (set(plan.applied_ids) | set(plan.skipped_ids) | admission) - set(
        plan.quarantined_ids
    )
    return {
        "processed_ids": sorted(processed),
        "retryable_pending_ids": list(plan.pending_ids),
        "deferred_ids": list(plan.deferred_ids),
        "quarantined_ids": list(plan.quarantined_ids),
    }


def effective_per_session_limit(
    configured: object, run_limit: int, session_count: int = 1
) -> int:
    """Cap each session only when more than one session is waiting."""

    bound = max(1, int(run_limit or 1))
    sessions = max(1, int(session_count or 1))
    raw = _coerce_int(configured)
    if sessions <= 1:
        return bound if raw <= 0 or raw >= bound else min(raw, bound)
    fair = max(1, bound // min(sessions, 8))
    if raw <= 0 or raw >= bound:
        return fair
    return max(1, min(raw, bound - 1))


def has_high_value_durable_signal(text: str) -> bool:
    return bool(HIGH_VALUE_DURABLE_SIGNAL_RE.search(clean_text(text or "")))


def has_tool_promotion_signal(text: str) -> bool:
    return bool(TOOL_PROMOTION_SIGNAL_RE.search(clean_text(text or "")))


def is_compaction_or_wrapper(text: str) -> bool:
    raw = text or ""
    return any(marker in raw for marker in COMPACTION_MARKERS)


def is_tool_noise(text: str) -> bool:
    raw = text or ""
    if not TOOL_NOISE_RE.search(raw):
        return False
    # Noise markers are classified first. A tool dump may still enter the
    # extractor when it also carries verified/rollback/guardrail evidence.
    return not has_tool_promotion_signal(raw)


def split_digestible_entries(
    entries: list[Any],
) -> tuple[list[Any], list[Any]]:
    """Keep user/assistant facts for extraction; park tool/compaction noise."""

    digestible: list[Any] = []
    evidence_only: list[Any] = []
    for entry in entries:
        role = str(entry.role or "").strip().lower()
        content = str(entry.content or "")
        if is_compaction_or_wrapper(content):
            evidence_only.append(entry)
            continue
        if role == "tool" and (
            is_tool_noise(content) or not has_tool_promotion_signal(content)
        ):
            evidence_only.append(entry)
            continue
        digestible.append(entry)
    return digestible, evidence_only


def admission_leave_reason(entry: Any) -> str:
    content = str(getattr(entry, "content", "") or "")
    if is_compaction_or_wrapper(content):
        return "admission:compaction"
    return "admission:tool_noise"


def journal_scope_session_identity(entry: Any) -> tuple[str, str] | None:
    """Return the stored ``(scope_id, session_id)`` pair used by journal cursors.

    Either part missing is ambiguous and must not authorize tool provenance.
    The pair is the same composite key ``journal_store`` already groups by;
    callers must not concatenate the fields into a new identity string.
    """

    scope_id = str(getattr(entry, "scope_id", "") or "").strip()
    session_id = str(getattr(entry, "session_id", "") or "").strip()
    if not scope_id or not session_id:
        return None
    return (scope_id, session_id)


def _identity_from_entry_ids(
    entry_ids: Iterable[object],
    digestible_by_id: dict[int, Any],
) -> tuple[str, str] | None:
    """Resolve one composite identity from a candidate's non-tool source rows."""

    identities: set[tuple[str, str]] = set()
    seen = False
    for entry_id in _as_id_set(entry_ids):
        entry = digestible_by_id.get(entry_id)
        if entry is None:
            return None
        if str(getattr(entry, "role", "") or "").strip().lower() == "tool":
            continue
        identity = journal_scope_session_identity(entry)
        if identity is None:
            return None
        identities.add(identity)
        seen = True
    if not seen or len(identities) != 1:
        return None
    return next(iter(identities))


def attach_digestible_tool_provenance(candidates: list[Any], digestible: list[Any]) -> list[Any]:
    """Keep high-value tool journal ids covered by each candidate's own chunk.

    A digestible tool row is not a coverage proof. Only exact
    ``covered_tool_ids`` carried from the per-chunk extractor may authorize
    a tool id, and only when that tool shares the candidate's stored
    ``(scope_id, session_id)`` pair and is not budget-deferred. Missing
    attempted metadata, missing exact coverage, or an ambiguous identity
    fails closed. Never reconstruct a global min/max ID window, and never
    dump every tool onto the first candidate.
    """

    if not candidates or not hasattr(candidates, "attempted_entry_ids"):
        return candidates
    attempted = _as_id_set(getattr(candidates, "attempted_entry_ids", ()))
    if not attempted:
        return candidates
    deferred = _as_id_set(getattr(candidates, "deferred_entry_ids", ()))
    digestible_by_id: dict[int, Any] = {}
    for entry in digestible:
        try:
            digestible_by_id[int(entry.id)] = entry
        except (TypeError, ValueError, AttributeError):
            continue
    for candidate in candidates:
        exact = getattr(candidate, "covered_tool_ids", None)
        if exact is None:
            continue
        candidate_identity = _identity_from_entry_ids(
            getattr(candidate, "entry_ids", ()) or (),
            digestible_by_id,
        )
        if candidate_identity is None:
            continue
        extra: list[int] = []
        for tool_id in _as_id_set(exact):
            if tool_id in deferred:
                continue
            entry = digestible_by_id.get(tool_id)
            if entry is None:
                continue
            if str(getattr(entry, "role", "") or "").strip().lower() != "tool":
                continue
            tool_identity = journal_scope_session_identity(entry)
            if tool_identity is None or tool_identity != candidate_identity:
                continue
            extra.append(tool_id)
        if not extra:
            continue
        existing: list[int] = []
        for item in getattr(candidate, "entry_ids", None) or []:
            try:
                existing.append(int(item))
            except (TypeError, ValueError):
                continue
        candidate.entry_ids = list(dict.fromkeys([*existing, *extra]))
    return candidates


def active_journal_digest_llm_error() -> type[JournalDigestLLMError]:
    """Return the live exception class, honoring monkeypatches on this module.

    Old imports expect ``scope_recall.digest_state.JournalDigestLLMError``.
    Production raise/except sites must look up through this function so a
    test or operator patch on this module is the class that is actually used.
    """

    module = sys.modules.get(__name__)
    current = getattr(module, "JournalDigestLLMError", None) if module is not None else None
    return current if isinstance(current, type) else JournalDigestLLMError
