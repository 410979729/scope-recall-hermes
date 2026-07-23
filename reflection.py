"""Bounded, scoped, read-only evidence collection for cross-memory reflection.

The collector deliberately stops before synthesis: it gathers ordinary recall rows and
one temporal fact view, then emits stable citation anchors for ``reflection_llm``.
It never writes memory state and accepts at most one caller-supplied follow-up query.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field, replace
import json
import re
import sqlite3
from typing import Any, ContextManager

from .models import RecallItem
from .recall import RecallService
from .temporal_query import CurrentFactView, query_current_fact_views


MAX_REFLECTION_QUERY_CHARS = 2_000
MAX_REFLECTION_SCOPES = 64
MAX_REFLECTION_SCOPE_ID_CHARS = 240
MAX_REFLECTION_EVIDENCE = 64
MAX_REFLECTION_CHARS = 50_000
MAX_REFLECTION_ITEM_CHARS = 4_000
MAX_REFLECTION_RECALL_LIMIT = 100
MAX_REFLECTION_FACT_LIMIT = 100
_HISTORY_HINTS = (
    "history",
    "historical",
    "previous",
    "previously",
    "formerly",
    "before",
    "used to",
    "changed",
    "change over time",
    "timeline",
    "过去",
    "历史",
    "之前",
    "曾经",
    "原来",
    "变化",
    "演变",
    "何时",
)


class ReflectionEvidenceError(ValueError):
    """Raised when a reflection evidence request violates a safety bound."""


@dataclass(frozen=True, slots=True)
class ReflectionBudget:
    """Hard limits for one reflection evidence pass."""

    max_evidence: int = 24
    max_chars: int = 12_000
    max_item_chars: int = 2_000
    recall_limit: int = 24
    fact_limit: int = 24

    def validated(self) -> "ReflectionBudget":
        _strict_int("max_evidence", self.max_evidence, minimum=1, maximum=MAX_REFLECTION_EVIDENCE)
        _strict_int("max_chars", self.max_chars, minimum=128, maximum=MAX_REFLECTION_CHARS)
        _strict_int("max_item_chars", self.max_item_chars, minimum=40, maximum=MAX_REFLECTION_ITEM_CHARS)
        _strict_int("recall_limit", self.recall_limit, minimum=1, maximum=MAX_REFLECTION_RECALL_LIMIT)
        _strict_int("fact_limit", self.fact_limit, minimum=1, maximum=MAX_REFLECTION_FACT_LIMIT)
        return self


@dataclass(frozen=True, slots=True)
class ReflectionEvidence:
    """One citation-safe memory or temporal-claim projection."""

    evidence_id: str
    kind: str
    memory_id: str
    claim_id: str | None
    scope_id: str
    content: str
    summary: str
    source: str
    target: str
    score: float
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "memory_id": self.memory_id,
            "claim_id": self.claim_id,
            "scope_id": self.scope_id,
            "content": self.content,
            "summary": self.summary,
            "source": self.source,
            "target": self.target,
            "score": self.score,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ReflectionEvidencePack:
    """Read-only evidence bundle consumed by the reflection synthesizer."""

    query: str
    intent: str
    evidence: tuple[ReflectionEvidence, ...]
    char_count: int
    trace: dict[str, Any]

    @property
    def citation_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.evidence)

    def as_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": self.query,
            "intent": self.intent,
            "evidence": [item.as_dict() for item in self.evidence],
            "char_count": self.char_count,
        }
        if include_trace:
            payload["trace"] = dict(self.trace)
        return payload


def _strict_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise ReflectionEvidenceError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ReflectionEvidenceError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_query(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReflectionEvidenceError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ReflectionEvidenceError(f"{field_name} must not be empty")
    if len(text) > MAX_REFLECTION_QUERY_CHARS:
        raise ReflectionEvidenceError(
            f"{field_name} exceeds {MAX_REFLECTION_QUERY_CHARS} characters"
        )
    return text


def classify_reflection_intent(query: str) -> str:
    """Choose current or history ledger semantics from explicit query hints."""

    normalized = str(query or "").strip().casefold()
    return "history" if any(hint in normalized for hint in _HISTORY_HINTS) else "current"


def _accessible_scopes(provider: Any) -> list[str]:
    raw = getattr(provider, "_accessible_scope_ids", None)
    if not isinstance(raw, (list, tuple)):
        raise ReflectionEvidenceError("provider accessible scopes are unavailable")
    if len(raw) > MAX_REFLECTION_SCOPES:
        raise ReflectionEvidenceError(
            f"accessible scopes exceed {MAX_REFLECTION_SCOPES} entries"
        )
    scopes = list(dict.fromkeys(str(item).strip() for item in raw))
    if not scopes or any(not item for item in scopes):
        raise ReflectionEvidenceError("provider accessible scopes are invalid")
    if any(len(item) > MAX_REFLECTION_SCOPE_ID_CHARS for item in scopes):
        raise ReflectionEvidenceError(
            f"scope_id exceeds {MAX_REFLECTION_SCOPE_ID_CHARS} characters"
        )
    return scopes


def _provider_lock(provider: Any) -> ContextManager[Any]:
    lock = getattr(provider, "_lock", None)
    return lock if lock is not None else nullcontext()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        is not None
    )


def _recall_scope(item: RecallItem, *, scopes: list[str], provider: Any) -> str | None:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    scope_id = str(metadata.get("scope_id") or "").strip()
    if scope_id:
        return scope_id if scope_id in scopes else None
    # Curated USER.md/MEMORY.md rows are loaded by the provider after its own
    # curated-memory access gate and do not carry a SQLite scope_id.
    if item.source == "builtin-curated":
        provider_scope = str(getattr(provider, "_scope_id", "") or "").strip()
        if provider_scope in scopes:
            return provider_scope
    return None


def _memory_evidence(item: RecallItem, *, scope_id: str) -> ReflectionEvidence:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    return ReflectionEvidence(
        evidence_id=f"memory:{item.id}",
        kind="memory",
        memory_id=str(item.id),
        claim_id=None,
        scope_id=scope_id,
        content=str(item.content or ""),
        summary=str(item.summary or item.content or ""),
        source=str(item.source or ""),
        target=str(item.target or "memory"),
        score=max(0.0, min(1.0, float(item.score or 0.0))),
        updated_at=str(item.updated_at or ""),
        metadata={
            key: metadata[key]
            for key in (
                "memory_type",
                "lifecycle",
                "importance",
                "entities",
                "provenance_root",
                "source_type",
                "source_ref",
                "origin_message_id",
                "digest_session_id",
                "journal_entry_ids",
                "journal_session_ids",
            )
            if key in metadata
        },
    )


def _current_fact_evidence(view: CurrentFactView) -> ReflectionEvidence:
    return ReflectionEvidence(
        evidence_id=f"claim:{view.claim_id}",
        kind="fact_current",
        memory_id=view.memory_id,
        claim_id=view.claim_id,
        scope_id=view.scope_id,
        content=view.content,
        summary=f"{view.subject_key} {view.predicate_key}: {view.value}",
        source=view.source,
        target=view.target,
        score=max(0.0, min(1.0, max(view.score, view.confidence))),
        updated_at=view.updated_at,
        metadata={
            "fact_key": view.fact_key,
            "status": view.status,
            "value": view.value,
            "cardinality": view.cardinality,
            "valid_from": view.valid_from,
            "valid_to": view.valid_to,
            "recorded_at": view.recorded_at,
            "confidence": view.confidence,
            "source_type": view.source_type,
            "source_ref": view.source_ref,
            "evidence_count": view.evidence_count,
            "semantic_at": view.semantic_at,
        },
    )


def _query_terms(query: str) -> list[str]:
    normalized = str(query or "").strip().casefold()
    terms = re.findall(r"[a-z0-9\u4e00-\u9fff_-]+", normalized)
    # One-character Latin tokens add noise; retain CJK tokens where a single
    # character can still be semantically useful.
    return list(
        dict.fromkeys(
            term
            for term in terms
            if len(term) >= 2 or re.search(r"[\u4e00-\u9fff]", term)
        )
    )[:32]


def _history_fact_evidence(
    conn: sqlite3.Connection,
    *,
    scopes: list[str],
    queries: list[str],
    limit: int,
) -> list[ReflectionEvidence]:
    """Return bounded ledger history, including lifecycle-hidden source rows."""

    terms = list(dict.fromkeys(term for query in queries for term in _query_terms(query)))
    if not terms:
        return []
    conn.create_function(
        "scope_recall_reflection_casefold",
        1,
        lambda value: str(value or "").casefold(),
        deterministic=True,
    )
    term_rows = ",".join("(?)" for _ in terms)
    scope_placeholders = ",".join("?" for _ in scopes)
    search_text = (
        "scope_recall_reflection_casefold(COALESCE(fc.subject_key, '') || ' ' || "
        "COALESCE(fc.predicate_key, '') || ' ' || COALESCE(fc.value, '') || ' ' || "
        "COALESCE(m.content, '') || ' ' || COALESCE(m.summary, ''))"
    )
    rows = conn.execute(
        f"""
        WITH search_terms(term) AS (VALUES {term_rows})
        SELECT fc.claim_id, fc.memory_id, fc.scope_id, fc.fact_key,
               fc.subject_key, fc.predicate_key, fc.value, fc.cardinality,
               fc.status, fc.valid_from, fc.valid_to, fc.recorded_at,
               fc.retired_at, fc.confidence, fc.assertion_kind,
               fc.source_type, fc.source_ref, fc.superseded_by_claim_id,
               m.content, m.summary, m.source, m.target, m.updated_at
        FROM fact_claims AS fc
        JOIN memories AS m
          ON m.id = fc.memory_id AND m.scope_id = fc.scope_id
        WHERE fc.scope_id IN ({scope_placeholders})
          AND EXISTS (
              SELECT 1 FROM search_terms
              WHERE INSTR({search_text}, search_terms.term) > 0
          )
        ORDER BY fc.fact_key ASC,
                 COALESCE(fc.valid_from, '') ASC,
                 fc.recorded_at ASC,
                 fc.claim_id ASC
        LIMIT ?
        """,
        [*terms, *scopes, limit],
    ).fetchall()
    output: list[ReflectionEvidence] = []
    for row in rows:
        claim_id = str(row["claim_id"])
        confidence = max(0.0, min(1.0, float(row["confidence"] or 0.0)))
        output.append(
            ReflectionEvidence(
                evidence_id=f"claim:{claim_id}",
                kind="fact_history",
                memory_id=str(row["memory_id"]),
                claim_id=claim_id,
                scope_id=str(row["scope_id"]),
                content=str(row["content"] or ""),
                summary=(
                    f"{row['subject_key']} {row['predicate_key']}: {row['value']}"
                ),
                source=str(row["source"] or ""),
                target=str(row["target"] or "memory"),
                score=confidence,
                updated_at=str(row["updated_at"] or row["recorded_at"]),
                metadata={
                    "fact_key": str(row["fact_key"]),
                    "status": str(row["status"]),
                    "value": str(row["value"]),
                    "cardinality": str(row["cardinality"]),
                    "valid_from": row["valid_from"],
                    "valid_to": row["valid_to"],
                    "recorded_at": str(row["recorded_at"]),
                    "retired_at": row["retired_at"],
                    "confidence": confidence,
                    "assertion_kind": str(row["assertion_kind"]),
                    "source_type": str(row["source_type"]),
                    "source_ref": str(row["source_ref"]),
                    "superseded_by_claim_id": row["superseded_by_claim_id"],
                },
            )
        )
    return output


def _serialized_chars(item: ReflectionEvidence) -> int:
    return len(
        json.dumps(
            item.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _clip_text(value: str, maximum: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= maximum:
        return text, False
    if maximum <= 1:
        return text[:maximum], True
    return f"{text[: maximum - 1]}…", True


def _deduplicate_evidence(
    items: list[ReflectionEvidence],
) -> tuple[list[ReflectionEvidence], int]:
    fact_items = [item for item in items if item.kind.startswith("fact_")]
    memory_items = [item for item in items if not item.kind.startswith("fact_")]
    claim_memory_ids = {item.memory_id for item in fact_items}
    ordered = [
        *sorted(fact_items, key=lambda item: (-item.score, item.evidence_id)),
        *sorted(
            (item for item in memory_items if item.memory_id not in claim_memory_ids),
            key=lambda item: (-item.score, item.evidence_id),
        ),
    ]
    deduped: list[ReflectionEvidence] = []
    seen: set[str] = set()
    for item in ordered:
        if item.evidence_id in seen:
            continue
        seen.add(item.evidence_id)
        deduped.append(item)
    return deduped, max(0, len(items) - len(deduped))


def _apply_budget(
    items: list[ReflectionEvidence],
    *,
    budget: ReflectionBudget,
) -> tuple[tuple[ReflectionEvidence, ...], int, bool]:
    selected: list[ReflectionEvidence] = []
    used = 0
    truncated = False
    for item in items:
        if len(selected) >= budget.max_evidence:
            truncated = True
            break
        content, content_clipped = _clip_text(item.content, budget.max_item_chars)
        summary, summary_clipped = _clip_text(item.summary, budget.max_item_chars)
        candidate = replace(item, content=content, summary=summary)
        size = _serialized_chars(candidate)
        if used + size > budget.max_chars:
            remaining = budget.max_chars - used
            # Shrink the two free-text fields before dropping an otherwise useful
            # citation. Structured identity/provenance fields are never clipped.
            overhead_candidate = replace(candidate, content="", summary="")
            overhead = _serialized_chars(overhead_candidate)
            if remaining <= overhead:
                truncated = True
                break
            text_budget = remaining - overhead
            content_budget = max(0, min(len(content), text_budget * 2 // 3))
            summary_budget = max(0, text_budget - content_budget)
            candidate = replace(
                candidate,
                content=_clip_text(content, content_budget)[0],
                summary=_clip_text(summary, summary_budget)[0],
            )
            size = _serialized_chars(candidate)
            while size > remaining and (candidate.content or candidate.summary):
                if len(candidate.content) >= len(candidate.summary) and candidate.content:
                    candidate = replace(candidate, content=candidate.content[:-1])
                elif candidate.summary:
                    candidate = replace(candidate, summary=candidate.summary[:-1])
                size = _serialized_chars(candidate)
            if size > remaining:
                truncated = True
                break
            truncated = True
        truncated = truncated or content_clipped or summary_clipped
        selected.append(candidate)
        used += size
    if len(selected) < len(items):
        truncated = True
    return tuple(selected), used, truncated


def merge_reflection_evidence_packs(
    primary: ReflectionEvidencePack,
    supplemental: ReflectionEvidencePack,
    *,
    budget: ReflectionBudget | None = None,
) -> ReflectionEvidencePack:
    """Merge one supplemental retrieval without rerunning the initial query.

    Both inputs must already satisfy the collector's read-only contract. The
    merged pack is re-deduplicated and re-budgeted so a follow-up cannot exceed
    the original prompt envelope.
    """

    if not isinstance(primary, ReflectionEvidencePack) or not isinstance(
        supplemental, ReflectionEvidencePack
    ):
        raise ReflectionEvidenceError("reflection evidence packs are required")
    primary_delta = int(primary.trace.get("write_delta") or 0)
    supplemental_delta = int(supplemental.trace.get("write_delta") or 0)
    if primary_delta or supplemental_delta:
        raise ReflectionEvidenceError("cannot merge a reflection pack with writes")
    if not str(supplemental.query or "").strip():
        raise ReflectionEvidenceError("supplemental query is required")

    validated_budget = (budget or ReflectionBudget()).validated()
    candidates, merge_duplicates = _deduplicate_evidence(
        [*primary.evidence, *supplemental.evidence]
    )
    bounded, char_count, budget_truncated = _apply_budget(
        candidates,
        budget=validated_budget,
    )

    def _trace_int(pack: ReflectionEvidencePack, key: str) -> int:
        return int(pack.trace.get(key) or 0)

    trace = {
        "intent": primary.intent,
        "retrieval_count": _trace_int(primary, "retrieval_count")
        + _trace_int(supplemental, "retrieval_count"),
        "followup_used": True,
        "accessible_scope_count": max(
            _trace_int(primary, "accessible_scope_count"),
            _trace_int(supplemental, "accessible_scope_count"),
        ),
        "raw_recall_count": _trace_int(primary, "raw_recall_count")
        + _trace_int(supplemental, "raw_recall_count"),
        "fact_count": _trace_int(primary, "fact_count")
        + _trace_int(supplemental, "fact_count"),
        "scope_filtered": _trace_int(primary, "scope_filtered")
        + _trace_int(supplemental, "scope_filtered"),
        "deduplicated": _trace_int(primary, "deduplicated")
        + _trace_int(supplemental, "deduplicated")
        + merge_duplicates,
        "pre_budget_count": len(candidates),
        "returned_count": len(bounded),
        "truncated": bool(
            primary.trace.get("truncated")
            or supplemental.trace.get("truncated")
            or budget_truncated
        ),
        "write_delta": 0,
        "followup_query": supplemental.query,
    }
    return ReflectionEvidencePack(
        query=primary.query,
        intent=primary.intent,
        evidence=bounded,
        char_count=char_count,
        trace=trace,
    )


def build_reflection_evidence_pack(
    provider: Any,
    *,
    query: str,
    budget: ReflectionBudget | None = None,
    followup_query: str | None = None,
    query_intent: str | None = None,
) -> ReflectionEvidencePack:
    """Collect one initial and at most one supplementary evidence retrieval.

    Scope ids come only from the provider's trusted runtime context. A caller cannot
    supply or widen them. The provider lock binds ``total_changes`` evidence to the
    same source epoch and prevents concurrent plugin writes from looking like a
    reflection mutation.
    """

    normalized_query = _bounded_query(query, field_name="query")
    normalized_followup: str | None = None
    if followup_query is not None:
        normalized_followup = _bounded_query(
            followup_query,
            field_name="followup_query",
        )
        if normalized_followup == normalized_query:
            normalized_followup = None
    validated_budget = (budget or ReflectionBudget()).validated()
    scopes = _accessible_scopes(provider)
    if query_intent is None:
        intent = classify_reflection_intent(normalized_query)
    else:
        intent = str(query_intent or "").strip().lower()
        if intent not in {"current", "history"}:
            raise ReflectionEvidenceError("query_intent must be current or history")
    queries = [normalized_query]
    if normalized_followup:
        queries.append(normalized_followup)

    with _provider_lock(provider):
        conn = provider._require_conn()
        before_changes = int(conn.total_changes)
        service = RecallService(provider)
        raw_recall: list[RecallItem] = []
        for retrieval_query in queries:
            raw_recall.extend(
                service.search_memories(
                    retrieval_query,
                    limit=validated_budget.recall_limit,
                )
            )

        fact_items: list[ReflectionEvidence] = []
        if _table_exists(conn, "fact_claims"):
            if intent == "history":
                fact_items = _history_fact_evidence(
                    conn,
                    scopes=scopes,
                    queries=queries,
                    limit=validated_budget.fact_limit,
                )
            else:
                by_claim: dict[str, ReflectionEvidence] = {}
                for retrieval_query in queries:
                    for view in query_current_fact_views(
                        conn,
                        scope_ids=scopes,
                        query=retrieval_query,
                        limit=validated_budget.fact_limit,
                    ):
                        by_claim.setdefault(
                            view.claim_id,
                            _current_fact_evidence(view),
                        )
                fact_items = list(by_claim.values())

        scope_filtered = 0
        memory_items: list[ReflectionEvidence] = []
        for item in raw_recall:
            scope_id = _recall_scope(item, scopes=scopes, provider=provider)
            if scope_id is None:
                scope_filtered += 1
                continue
            memory_items.append(_memory_evidence(item, scope_id=scope_id))

        after_changes = int(conn.total_changes)
    write_delta = after_changes - before_changes
    if write_delta != 0:
        raise ReflectionEvidenceError(
            "reflection evidence collection mutated SQLite state"
        )

    # Temporal claims are the authoritative representation of the same source
    # memory. Keep distinct claims, but do not spend budget on a duplicate plain
    # memory projection for any memory already represented by a claim.
    deduped, duplicate_count = _deduplicate_evidence([*fact_items, *memory_items])
    evidence, char_count, truncated = _apply_budget(
        deduped,
        budget=validated_budget,
    )
    trace = {
        "intent": intent,
        "retrieval_count": len(queries),
        "followup_used": normalized_followup is not None,
        "accessible_scope_count": len(scopes),
        "raw_recall_count": len(raw_recall),
        "fact_count": len(fact_items),
        "scope_filtered": scope_filtered,
        "deduplicated": max(0, duplicate_count),
        "pre_budget_count": len(deduped),
        "returned_count": len(evidence),
        "truncated": truncated,
        "write_delta": write_delta,
    }
    return ReflectionEvidencePack(
        query=normalized_query,
        intent=intent,
        evidence=evidence,
        char_count=char_count,
        trace=trace,
    )


__all__ = [
    "MAX_REFLECTION_CHARS",
    "MAX_REFLECTION_EVIDENCE",
    "MAX_REFLECTION_ITEM_CHARS",
    "MAX_REFLECTION_QUERY_CHARS",
    "ReflectionBudget",
    "ReflectionEvidence",
    "ReflectionEvidenceError",
    "ReflectionEvidencePack",
    "build_reflection_evidence_pack",
    "classify_reflection_intent",
    "merge_reflection_evidence_packs",
]
