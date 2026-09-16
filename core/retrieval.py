"""Immutable retrieval values for the single core recall pipeline.

The objects in this module deliberately contain no host, Provider, or vector
database implementation.  A request is copied into :class:`SearchContext`
once at the trusted boundary; downstream stages receive that frozen snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import base64
import json
import math
from typing import Literal

from ..contracts import ContractError, RecallRequest, TrustedContext, validate_model_request


ObjectKind = Literal["event", "claim", "episode", "artifact", "reference"]
CandidateChannel = Literal["lexical", "exact_ref", "vector", "recent_raw", "relation", "background"]
RecallMode = Literal["auto", "current", "history", "as_of", "method"]
Coverage = Literal["complete_for_query", "partial", "unknown"]
Answerability = Literal["supported", "partial", "ambiguous", "unknown"]

# Character-calibrated whole-packet units from recall_budget.estimate_tokens.
# This is not a measured provider tokenizer count; bytes are diagnostic only.
AUTOMATIC_PACKET_BUDGET_UNITS = 4096


def optional_json(text: object) -> object:
    """Decode JSON carried in metadata; absent or malformed text reads as ``None``."""
    if type(text) is not str:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _utc(value: str) -> str:
    if type(value) is not str:
        raise ContractError("INPUT_INVALID", "timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("INPUT_INVALID", "timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("INPUT_INVALID", "timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SearchLimits:
    """Fixed per-query bounds shared by every retrieval channel."""

    max_items: int = 6
    budget_tokens: int = AUTOMATIC_PACKET_BUDGET_UNITS
    candidate_pool: int = 48
    recent_items: int = 8
    relation_hops: int = 2
    relation_objects: int = 24
    vector_limit: int = 48
    followups: int = 1

    def __post_init__(self) -> None:
        checks = {
            "max_items": (self.max_items, 1, 30),
            "budget_tokens": (self.budget_tokens, 64, 8000),
            "candidate_pool": (self.candidate_pool, 1, 200),
            "recent_items": (self.recent_items, 0, 24),
            "relation_hops": (self.relation_hops, 0, 2),
            # The ceiling used to equal the default, so an operator who noticed
            # relation_bound_reached could not raise it. Relation expansion is
            # the only path by which a claim reaches automatic recall — the
            # lexical, vector and recent channels all yield events — so hitting
            # this bound is exactly what makes the derived layer unreachable.
            "relation_objects": (self.relation_objects, 0, 200),
            "vector_limit": (self.vector_limit, 0, 200),
            "followups": (self.followups, 0, 1),
        }
        for name, (value, low, high) in checks.items():
            if type(value) is not int or not low <= value <= high:
                raise ContractError("INPUT_INVALID", name)


@dataclass(frozen=True)
class SearchContext:
    """Trusted, immutable snapshot used by all stages of one retrieval."""

    query: str
    mode: RecallMode
    as_of: str | None
    focus_refs: tuple[str, ...]
    limits: SearchLimits
    deadline: float
    now: str
    trusted_context: TrustedContext
    current_source_refs: tuple[str, ...] = ()
    request_id: str = ""

    def __post_init__(self) -> None:
        if type(self.query) is not str or not self.query.strip() or len(self.query) > 8192:
            raise ContractError("INPUT_INVALID", "query")
        if type(self.request_id) is not str or len(self.request_id) > 100:
            raise ContractError("INPUT_INVALID", "request_id")
        if self.mode not in {"auto", "current", "history", "as_of", "method"}:
            raise ContractError("INPUT_INVALID", "mode")
        if self.mode == "as_of" and self.as_of is None:
            raise ContractError("INPUT_INVALID", "as_of")
        if self.as_of is not None:
            object.__setattr__(self, "as_of", _utc(self.as_of))
        object.__setattr__(self, "now", _utc(self.now))
        if type(self.deadline) not in (int, float) or not math.isfinite(self.deadline):
            raise ContractError("INPUT_INVALID", "deadline")
        if not isinstance(self.limits, SearchLimits):
            raise ContractError("INPUT_INVALID", "limits")
        if not isinstance(self.trusted_context, TrustedContext):
            raise ContractError("IDENTITY_UNBOUND")
        if type(self.focus_refs) is not tuple or len(self.focus_refs) > 4:
            raise ContractError("INPUT_INVALID", "focus_refs")
        if any(type(ref) is not str or not 1 <= len(ref) <= 240 for ref in self.focus_refs):
            raise ContractError("INPUT_INVALID", "focus_refs")
        if len(set(self.focus_refs)) != len(self.focus_refs):
            raise ContractError("INPUT_INVALID", "focus_refs")
        if type(self.current_source_refs) is not tuple or len(self.current_source_refs) > 16:
            raise ContractError("INPUT_INVALID", "current_source_refs")
        if any(type(ref) is not str or not 1 <= len(ref) <= 300 for ref in self.current_source_refs):
            raise ContractError("INPUT_INVALID", "current_source_refs")
        if len(set(self.current_source_refs)) != len(self.current_source_refs):
            raise ContractError("INPUT_INVALID", "current_source_refs")

    @classmethod
    def from_request(
        cls,
        request: RecallRequest | dict | str | bytes,
        trusted_context: TrustedContext,
        *,
        now: str,
        deadline: float,
        current_source_refs: tuple[str, ...] = (),
    ) -> "SearchContext":
        payload = validate_model_request(
            "recall_request", dict(request) if isinstance(request, dict) else request, trusted_context
        )
        limits = SearchLimits(max_items=payload["max_items"], budget_tokens=payload["budget_tokens"])
        return cls(
            query=payload["query"],
            mode=payload["mode"],
            as_of=payload.get("as_of"),
            focus_refs=tuple(payload.get("focus_refs", ())),
            limits=limits,
            deadline=deadline,
            now=now,
            trusted_context=trusted_context,
            current_source_refs=tuple(current_source_refs),
            request_id=payload["request_id"],
        )


def effective_limits(context: SearchContext) -> SearchLimits:
    """Automatic recall delivers at most six items within the automatic packet
    budget, whatever the request asked for; explicit modes keep their limits."""
    if context.mode != "auto":
        return context.limits
    return replace(
        context.limits,
        max_items=min(context.limits.max_items, 6),
        budget_tokens=min(context.limits.budget_tokens, AUTOMATIC_PACKET_BUDGET_UNITS),
    )


#: Episode gaps that make a resume unsafe to act on from live modes.
STALE_RESUME_GAPS = ("resume_requires_rebuild", "source_version_changed", "environment_needs_revalidation")


@dataclass(frozen=True)
class CandidateRef:
    kind: ObjectKind
    ref: str
    revision: int
    source: CandidateChannel
    rank: int = 1
    lexical_score: float | None = None
    vector_score: float | None = None
    vector_id: str | None = None
    embedding_space: str | None = None
    fusion_score: float = 0.0
    matched_query_terms: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"event", "claim", "episode", "artifact", "reference"}:
            raise ContractError("INPUT_INVALID", "candidate_kind")
        if type(self.ref) is not str or not 1 <= len(self.ref) <= 240:
            raise ContractError("INPUT_INVALID", "candidate_ref")
        if type(self.revision) is not int or self.revision < 1:
            raise ContractError("INPUT_INVALID", "candidate_revision")
        if self.source not in {"lexical", "exact_ref", "vector", "recent_raw", "relation", "background"}:
            raise ContractError("INPUT_INVALID", "candidate_source")
        if type(self.rank) is not int or self.rank < 1:
            raise ContractError("INPUT_INVALID", "candidate_rank")
        for name, value in (("lexical_score", self.lexical_score), ("vector_score", self.vector_score)):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ContractError("INPUT_INVALID", name)
        if type(self.fusion_score) not in (int, float) or not math.isfinite(float(self.fusion_score)):
            raise ContractError("INPUT_INVALID", "fusion_score")
        if self.matched_query_terms is not None and (
            type(self.matched_query_terms) is not tuple
            or any(type(term) is not str or not 1 <= len(term) <= 240 for term in self.matched_query_terms)
        ):
            raise ContractError("INPUT_INVALID", "matched_query_terms")

    @property
    def key(self) -> tuple[str, str, int]:
        return self.kind, self.ref, self.revision


@dataclass(frozen=True)
class RetrievedObject:
    ref: str
    revision: int
    kind: str
    content: str
    origin: str
    temporal_status: Literal["current", "historical", "disputed", "unknown"]
    applicability: str
    evidence_refs: tuple[str, ...]
    basis: str
    expandable: bool
    source_kinds: tuple[str, ...] = ()
    relation_refs: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.ref) is not str or not self.ref or type(self.revision) is not int or self.revision < 1:
            raise ContractError("INPUT_INVALID", "retrieved_identity")
        if type(self.content) is not str or not self.content:
            raise ContractError("INPUT_INVALID", "retrieved_content")
        if self.temporal_status not in {"current", "historical", "disputed", "unknown"}:
            raise ContractError("INPUT_INVALID", "temporal_status")
        if type(self.evidence_refs) is not tuple or any(type(ref) is not str for ref in self.evidence_refs):
            raise ContractError("INPUT_INVALID", "evidence_refs")
        if type(self.expandable) is not bool:
            raise ContractError("INPUT_INVALID", "expandable")


@dataclass(frozen=True)
class RetrievalResult:
    candidates: tuple[CandidateRef, ...]
    items: tuple[RetrievedObject, ...]
    memory_epoch: int | None
    gaps: tuple[str, ...] = ()
    coverage: Coverage = "unknown"
    answerability_hint: Answerability = "unknown"
    candidate_count: int = 0
    admitted_count: int = 0
    diagnostics: tuple[tuple[str, str], ...] = ()
    request_id: str = ""
    unmet_needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollectionQuery:
    object_kind: Literal["event", "claim", "episode", "artifact", "reference"]
    where: tuple[tuple[str, str], ...] = ()
    page_size: int = 30
    memory_epoch: int = 0
    scope_digest: str = ""
    mode: RecallMode = "current"
    as_of: str | None = None

    def __post_init__(self) -> None:
        if self.object_kind not in {"event", "claim", "episode", "artifact", "reference"}:
            raise ContractError("INPUT_INVALID", "collection_kind")
        if type(self.where) is not tuple or len(self.where) > 8:
            raise ContractError("INPUT_INVALID", "collection_where")
        if any(type(pair) is not tuple or len(pair) != 2 or any(type(v) is not str for v in pair) for pair in self.where):
            raise ContractError("INPUT_INVALID", "collection_where")
        if type(self.page_size) is not int or not 1 <= self.page_size <= 100:
            raise ContractError("INPUT_INVALID", "collection_page_size")
        if type(self.memory_epoch) is not int or self.memory_epoch < 0:
            raise ContractError("INPUT_INVALID", "collection_epoch")


@dataclass(frozen=True)
class PageCursor:
    memory_epoch: int
    scope_digest: str
    project_id: str | None
    branch_id: str | None
    mode: RecallMode
    as_of: str | None
    filters: tuple[tuple[str, str], ...]
    last_sort_key: tuple[str, str, int]
    object_kind: str = ""

    def __post_init__(self) -> None:
        if type(self.memory_epoch) is not int or self.memory_epoch < 0:
            raise ContractError("INPUT_INVALID", "cursor_epoch")
        if type(self.scope_digest) is not str or len(self.scope_digest) != 64:
            raise ContractError("INPUT_INVALID", "cursor_scope")
        if self.mode not in {"auto", "current", "history", "as_of", "method"}:
            raise ContractError("INPUT_INVALID", "cursor_mode")
        if type(self.last_sort_key) is not tuple or len(self.last_sort_key) != 3:
            raise ContractError("INPUT_INVALID", "cursor_sort")
        if self.object_kind not in {"event", "claim", "episode", "artifact", "reference"}:
            raise ContractError("INPUT_INVALID", "cursor_kind")

    def encode(self) -> str:
        payload = {
            "memory_epoch": self.memory_epoch,
            "scope_digest": self.scope_digest,
            "project_id": self.project_id,
            "branch_id": self.branch_id,
            "mode": self.mode,
            "as_of": self.as_of,
            "filters": list(self.filters),
            "last_sort_key": list(self.last_sort_key),
            "object_kind": self.object_kind,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "PageCursor":
        if type(value) is not str or not value or len(value) > 4096:
            raise ContractError("INPUT_INVALID", "cursor")
        try:
            padded = value + "=" * (-len(value) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            return cls(
                int(payload["memory_epoch"]),
                payload["scope_digest"],
                payload.get("project_id"),
                payload.get("branch_id"),
                payload["mode"],
                payload.get("as_of"),
                tuple(tuple(item) for item in payload["filters"]),
                tuple(payload["last_sort_key"]),
                payload["object_kind"],
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise ContractError("INPUT_INVALID", "cursor") from exc
