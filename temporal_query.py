"""Read-only semantic-time query views over the temporal fact ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .fact_identity import (
    MAX_FACT_PREDICATE_CHARS,
    MAX_FACT_SUBJECT_CHARS,
    FactIdentityError,
    canonical_fact_key,
    normalize_fact_component,
)
from .fact_repository import (
    FactClaim,
    claim_history,
    claims_as_of,
    current_claims_for_scopes,
    get_claims_by_ids,
    predecessor_claim_ids_by_successor,
)
from .gating import build_fts_query, lexical_overlap_details, semantic_query_tokens
from .lifecycle_policy import ordinary_recall_lifecycle_visible_sql


MAX_QUERY_INSTANT_CHARS = 64
MAX_QUERY_SCOPE_ID_CHARS = 240
MAX_QUERY_SCOPES = 64
MAX_PRECEDENCE_MEMORY_IDS = 1_000
MAX_CURRENT_FACT_CANDIDATES = 1_000
MAX_FTS_ROUTE_TOKENS = 12
MAX_FTS_TOKEN_ROUTES = 20
MAX_SLOT_CLAIM_SCAN = 1_000
MAX_CURRENT_QUERY_CHARS = 1_000
SQL_ID_BATCH = 400


class TemporalQueryError(ValueError):
    """Raised when a temporal read request is ambiguous or out of bounds."""


@dataclass(frozen=True, slots=True)
class TemporalMemoryPrecedence:
    """Memory-level precedence derived from the ledger at one semantic instant."""

    semantic_at: str
    current_memory_ids: frozenset[str]
    suppressed_memory_ids: frozenset[str]
    current_fact_keys: frozenset[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "semantic_at": self.semantic_at,
            "current_memory_ids": sorted(self.current_memory_ids),
            "suppressed_memory_ids": sorted(self.suppressed_memory_ids),
            "current_fact_keys": sorted(self.current_fact_keys),
        }


@dataclass(frozen=True, slots=True)
class TemporalFactView:
    """Structured current/as-of/history projection with source and evidence."""

    mode: str
    claim: FactClaim
    content: str
    summary: str
    source: str
    target: str
    updated_at: str
    evidence: tuple[dict[str, Any], ...]
    semantic_at: str | None
    known_at: str | None
    uncertain: bool
    predecessor_claim_id: str | None

    def as_dict(self) -> dict[str, Any]:
        explanation = _fact_explanation(self)
        return {
            "mode": self.mode,
            "claim": self.claim.as_dict(),
            "memory": {
                "id": self.claim.memory_id,
                "content": self.content,
                "summary": self.summary,
                "source": self.source,
                "target": self.target,
                "updated_at": self.updated_at,
            },
            "evidence": [dict(item) for item in self.evidence],
            "interval": {
                "valid_from": self.claim.valid_from,
                "valid_to": self.claim.valid_to,
                "recorded_at": self.claim.recorded_at,
                "retired_at": self.claim.retired_at,
            },
            "confidence": self.claim.confidence,
            "uncertain": self.uncertain,
            "semantic_at": self.semantic_at,
            "known_at": self.known_at,
            "transition": {
                "predecessor_claim_id": self.predecessor_claim_id,
                "superseded_by_claim_id": self.claim.superseded_by_claim_id,
            },
            "explanation": explanation,
        }


@dataclass(frozen=True, slots=True)
class CurrentFactView:
    """Recall-ready read projection for one semantically current claim."""

    claim_id: str
    memory_id: str
    scope_id: str
    fact_key: str
    subject_key: str
    predicate_key: str
    value: str
    cardinality: str
    status: str
    valid_from: str | None
    valid_to: str | None
    recorded_at: str
    confidence: float
    source_type: str
    source_ref: str
    content: str
    summary: str
    source: str
    target: str
    updated_at: str
    evidence_count: int
    semantic_at: str
    score: float
    score_explain: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in (
                "claim_id",
                "memory_id",
                "scope_id",
                "fact_key",
                "subject_key",
                "predicate_key",
                "value",
                "cardinality",
                "status",
                "valid_from",
                "valid_to",
                "recorded_at",
                "confidence",
                "source_type",
                "source_ref",
                "content",
                "summary",
                "source",
                "target",
                "updated_at",
                "evidence_count",
                "semantic_at",
                "score",
                "score_explain",
            )
        }


def _fact_explanation(view: TemporalFactView) -> str:
    claim = view.claim
    if view.mode == "current":
        return f"current at {view.semantic_at}"
    if view.mode == "as_of":
        suffix = f"; known at {view.known_at}" if view.known_at else ""
        return f"valid at {view.semantic_at}{suffix}"
    if claim.superseded_by_claim_id:
        return f"superseded by {claim.superseded_by_claim_id}"
    if claim.status == "retracted":
        return f"retracted at {claim.retired_at or 'unknown time'}"
    if view.predecessor_claim_id:
        return f"replaced {view.predecessor_claim_id}"
    if claim.status == "uncertain":
        return "uncertain claim"
    return "current adopted claim"


def _zone(timezone_name: str) -> ZoneInfo:
    name = str(timezone_name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise TemporalQueryError(f"unknown timezone: {name}") from exc


def _localize_naive(value: datetime, zone: ZoneInfo) -> datetime:
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise TemporalQueryError(
            "local timestamp is ambiguous in the configured timezone; include an offset"
        )
    round_trip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if round_trip != value:
        raise TemporalQueryError(
            "local timestamp does not exist in the configured timezone; include an offset"
        )
    return first


def normalize_query_instant(
    value: Any = None,
    *,
    timezone_name: str = "UTC",
    now: datetime | None = None,
) -> str:
    """Normalize an explicit/local query instant to an aware UTC ISO timestamp."""

    zone = _zone(timezone_name)
    raw = now if value in (None, "") else value
    if raw is None:
        raw = datetime.now(timezone.utc)
    if isinstance(raw, datetime):
        parsed = raw
    else:
        text = str(raw).strip()
        if len(text) > MAX_QUERY_INSTANT_CHARS:
            raise TemporalQueryError(
                f"query instant exceeds {MAX_QUERY_INSTANT_CHARS} characters"
            )
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise TemporalQueryError(
                "query instant must be an ISO-8601 timestamp"
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = _localize_naive(parsed, zone)
    return parsed.astimezone(timezone.utc).isoformat()


def _query_score_details(
    query: str,
    claim: FactClaim,
    content: str,
    summary: str,
) -> dict[str, Any]:
    return lexical_overlap_details(
        query,
        claim.subject_key,
        claim.predicate_key,
        claim.value,
        content,
        summary,
    )


def _query_score(query: str, claim: FactClaim, content: str, summary: str) -> float:
    return float(_query_score_details(query, claim, content, summary)["score"])


def _bounded_limit(value: Any, *, maximum: int) -> int:
    if type(value) is not int:
        raise TemporalQueryError("limit must be an integer")
    parsed = value
    if parsed < 1 or parsed > maximum:
        raise TemporalQueryError(f"limit must be between 1 and {maximum}")
    return parsed


def _normalize_scope_ids(
    scope_ids: list[str] | tuple[str, ...],
) -> list[str]:
    scopes = list(dict.fromkeys(str(item).strip() for item in scope_ids))
    if not scopes or any(not item for item in scopes):
        raise TemporalQueryError("scope_ids must contain at least one non-empty scope")
    if len(scopes) > MAX_QUERY_SCOPES:
        raise TemporalQueryError(f"scope_ids exceeds {MAX_QUERY_SCOPES} entries")
    if any(len(scope_id) > MAX_QUERY_SCOPE_ID_CHARS for scope_id in scopes):
        raise TemporalQueryError(
            f"scope_id exceeds {MAX_QUERY_SCOPE_ID_CHARS} characters"
        )
    return scopes


def _normalize_memory_ids(
    memory_ids: list[str] | tuple[str, ...],
    *,
    maximum: int = MAX_PRECEDENCE_MEMORY_IDS,
) -> list[str]:
    values = list(dict.fromkeys(str(item).strip() for item in memory_ids))
    if any(not item for item in values):
        raise TemporalQueryError("memory_ids must not contain empty entries")
    if len(values) > maximum:
        raise TemporalQueryError(f"memory_ids exceeds {maximum} entries")
    if any(len(memory_id) > 200 for memory_id in values):
        raise TemporalQueryError("memory_id exceeds 200 characters")
    return values


def _batches(values: list[str], size: int = SQL_ID_BATCH) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _memory_rows(
    conn: sqlite3.Connection,
    *,
    memory_ids: list[str],
    scope_ids: list[str],
    include_hidden: bool = False,
) -> dict[str, sqlite3.Row]:
    if not memory_ids:
        return {}
    scope_placeholders = ",".join("?" for _ in scope_ids)
    lifecycle_clause = (
        "1 = 1"
        if include_hidden
        else ordinary_recall_lifecycle_visible_sql("m")
    )
    output: dict[str, sqlite3.Row] = {}
    for batch in _batches(list(dict.fromkeys(memory_ids))):
        memory_placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT m.id, m.scope_id, m.content, m.summary, m.source, m.target,
                   m.updated_at, m.metadata
            FROM memories AS m
            WHERE m.id IN ({memory_placeholders})
              AND m.scope_id IN ({scope_placeholders})
              AND {lifecycle_clause}
            """,
            [*batch, *scope_ids],
        ).fetchall()
        output.update({str(row["id"]): row for row in rows})
    return output


def _evidence_counts(
    conn: sqlite3.Connection,
    claim_ids: list[str],
) -> dict[str, int]:
    if not claim_ids:
        return {}
    output: dict[str, int] = {}
    for batch in _batches(list(dict.fromkeys(claim_ids))):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT claim_id, COUNT(*) AS evidence_count
            FROM fact_claim_evidence
            WHERE claim_id IN ({placeholders})
            GROUP BY claim_id
            """,
            batch,
        ).fetchall()
        output.update(
            {
                str(row["claim_id"]): int(row["evidence_count"])
                for row in rows
            }
        )
    return output


def _evidence_details(
    conn: sqlite3.Connection,
    claim_ids: list[str],
    *,
    known_at: str | None = None,
) -> dict[str, tuple[dict[str, Any], ...]]:
    if not claim_ids:
        return {}
    output: dict[str, list[dict[str, Any]]] = {}
    for batch in _batches(list(dict.fromkeys(claim_ids))):
        placeholders = ",".join("?" for _ in batch)
        cutoff_clause = "AND recorded_at <= ?" if known_at is not None else ""
        params: tuple[Any, ...] = (
            (*batch, known_at) if known_at is not None else tuple(batch)
        )
        rows = conn.execute(
            f"""
            SELECT evidence_id, claim_id, source_type, source_ref, evidence_hash,
                   excerpt, recorded_at, metadata
            FROM (
                SELECT evidence_id, claim_id, source_type, source_ref,
                       evidence_hash, excerpt, recorded_at, metadata,
                       ROW_NUMBER() OVER (
                           PARTITION BY claim_id
                           ORDER BY recorded_at ASC, evidence_id ASC
                       ) AS evidence_rank
                FROM fact_claim_evidence
                WHERE claim_id IN ({placeholders})
                  {cutoff_clause}
            )
            WHERE evidence_rank <= 20
            ORDER BY claim_id ASC, recorded_at ASC, evidence_id ASC
            """,
            params,
        ).fetchall()
        for row in rows:
            claim_id = str(row["claim_id"])
            bucket = output.setdefault(claim_id, [])
            if len(bucket) >= 20:
                continue
            try:
                metadata = json.loads(str(row["metadata"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TemporalQueryError(
                    f"evidence metadata is invalid for {row['evidence_id']}"
                ) from exc
            if not isinstance(metadata, dict):
                raise TemporalQueryError(
                    f"evidence metadata must be an object for {row['evidence_id']}"
                )
            bucket.append(
                {
                    "evidence_id": str(row["evidence_id"]),
                    "source_type": str(row["source_type"]),
                    "source_ref": str(row["source_ref"]),
                    "evidence_hash": str(row["evidence_hash"]),
                    "excerpt": str(row["excerpt"]),
                    "recorded_at": str(row["recorded_at"]),
                    "metadata": metadata,
                }
            )
    return {claim_id: tuple(items) for claim_id, items in output.items()}


def _current_visible_slot_claims(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    subject: str,
    predicate: str,
    semantic_at: str,
    limit: int,
) -> list[FactClaim]:
    """Select visible current slot claims before applying the caller cap."""

    fact_key = canonical_fact_key(subject, predicate)
    lifecycle_clause = ordinary_recall_lifecycle_visible_sql("m")
    rows = conn.execute(
        f"""
        SELECT fc.claim_id
        FROM fact_claims AS fc
        JOIN memories AS m
          ON m.id = fc.memory_id AND m.scope_id = fc.scope_id
        WHERE fc.scope_id = ? AND fc.fact_key = ?
          AND fc.status = 'current' AND fc.retired_at IS NULL
          AND (fc.valid_from IS NULL OR fc.valid_from <= ?)
          AND (fc.valid_to IS NULL OR fc.valid_to > ?)
          AND {lifecycle_clause}
        ORDER BY COALESCE(fc.valid_from, ''), fc.recorded_at, fc.claim_id
        LIMIT ?
        """,
        (scope_id, fact_key, semantic_at, semantic_at, limit),
    ).fetchall()
    return get_claims_by_ids(
        conn,
        claim_ids=[str(row["claim_id"]) for row in rows],
        scope_ids=[scope_id],
    )


def query_fact_views(
    conn: sqlite3.Connection,
    *,
    scope_ids: list[str] | tuple[str, ...],
    action: str,
    subject: Any,
    predicate: Any,
    at: Any = None,
    known_at: Any = None,
    timezone_name: str = "UTC",
    now: datetime | None = None,
    limit: int = 20,
) -> list[TemporalFactView]:
    """Query one scoped fact slot in current, as-of, or history mode."""

    mode = str(action or "current").strip().lower().replace("-", "_")
    if mode == "asof":
        mode = "as_of"
    if mode not in {"current", "as_of", "history"}:
        raise TemporalQueryError("action must be current, as_of, or history")
    bounded_limit = _bounded_limit(limit, maximum=100)
    try:
        normalized_subject = normalize_fact_component(
            subject,
            field_name="subject",
            max_chars=MAX_FACT_SUBJECT_CHARS,
        )
        normalized_predicate = normalize_fact_component(
            predicate,
            field_name="predicate",
            max_chars=MAX_FACT_PREDICATE_CHARS,
        )
    except FactIdentityError as exc:
        raise TemporalQueryError(str(exc)) from exc
    scopes = _normalize_scope_ids(scope_ids)
    if mode != "as_of" and known_at not in (None, ""):
        raise TemporalQueryError("known_at is only valid for as_of queries")
    if mode == "history" and at not in (None, ""):
        raise TemporalQueryError("at is not used for history queries")

    semantic_at: str | None = None
    normalized_known_at: str | None = None
    claims: list[FactClaim] = []
    if mode == "current":
        semantic_at = normalize_query_instant(
            at,
            timezone_name=timezone_name,
            now=now,
        )
        for scope_id in sorted(scopes):
            remaining = bounded_limit - len(claims)
            if remaining <= 0:
                break
            claims.extend(
                _current_visible_slot_claims(
                    conn,
                    scope_id=scope_id,
                    subject=normalized_subject,
                    predicate=normalized_predicate,
                    semantic_at=semantic_at,
                    limit=remaining,
                )
            )
    elif mode == "as_of":
        if at in (None, ""):
            raise TemporalQueryError("at is required for as_of queries")
        semantic_at = normalize_query_instant(at, timezone_name=timezone_name)
        if known_at not in (None, ""):
            normalized_known_at = normalize_query_instant(
                known_at,
                timezone_name=timezone_name,
            )
        for scope_id in sorted(scopes):
            remaining = bounded_limit - len(claims)
            if remaining <= 0:
                break
            claims.extend(
                claims_as_of(
                    conn,
                    scope_id=scope_id,
                    subject=normalized_subject,
                    predicate=normalized_predicate,
                    valid_at=semantic_at,
                    known_at=normalized_known_at,
                    limit=remaining,
                    scan_limit=MAX_SLOT_CLAIM_SCAN,
                )
            )
    else:
        for scope_id in sorted(scopes):
            remaining = bounded_limit - len(claims)
            if remaining <= 0:
                break
            claims.extend(
                claim_history(
                    conn,
                    scope_id=scope_id,
                    subject=normalized_subject,
                    predicate=normalized_predicate,
                    limit=remaining,
                    reject_overflow=False,
                )
            )

    claims.sort(
        key=lambda claim: (
            claim.scope_id,
            claim.fact_key,
            claim.valid_from or "",
            claim.recorded_at,
            claim.claim_id,
        )
    )
    predecessor_by_claim = {
        str(claim.superseded_by_claim_id): claim.claim_id
        for claim in claims
        if claim.superseded_by_claim_id
    }
    selected_claims: list[FactClaim] = []
    rows: dict[str, sqlite3.Row] = {}
    for index in range(0, len(claims), SQL_ID_BATCH):
        batch = claims[index : index + SQL_ID_BATCH]
        batch_rows = _memory_rows(
            conn,
            memory_ids=list(dict.fromkeys(claim.memory_id for claim in batch)),
            scope_ids=scopes,
            include_hidden=mode != "current",
        )
        for claim in batch:
            row = batch_rows.get(claim.memory_id)
            if row is None:
                continue
            selected_claims.append(claim)
            rows[claim.memory_id] = row
            if len(selected_claims) >= bounded_limit:
                break
        if len(selected_claims) >= bounded_limit:
            break
    claims = selected_claims
    evidence = _evidence_details(
        conn,
        [claim.claim_id for claim in claims],
        known_at=normalized_known_at,
    )
    unresolved_successors = [
        claim.claim_id
        for claim in claims
        if claim.claim_id not in predecessor_by_claim
    ]
    predecessor_by_claim.update(
        predecessor_claim_ids_by_successor(
            conn,
            successor_claim_ids=unresolved_successors,
            scope_ids=scopes,
            known_at=normalized_known_at,
        )
    )
    views: list[TemporalFactView] = []
    for claim in claims:
        row = rows.get(claim.memory_id)
        if row is None:
            continue
        views.append(
            TemporalFactView(
                mode=mode,
                claim=claim,
                content=str(row["content"] or ""),
                summary=str(row["summary"] or row["content"] or ""),
                source=str(row["source"] or ""),
                target=str(row["target"] or "memory"),
                updated_at=str(row["updated_at"] or claim.recorded_at),
                evidence=evidence.get(claim.claim_id, ()),
                semantic_at=semantic_at,
                known_at=normalized_known_at,
                uncertain=(
                    claim.status == "uncertain"
                    or claim.assertion_kind == "inferred"
                    or claim.confidence < 0.75
                ),
                predecessor_claim_id=predecessor_by_claim.get(claim.claim_id),
            )
        )
    return views


def query_temporal_memory_precedence(
    conn: sqlite3.Connection,
    *,
    scope_ids: list[str] | tuple[str, ...],
    memory_ids: list[str] | tuple[str, ...] | None = None,
    valid_at: Any = None,
    timezone_name: str = "UTC",
    now: datetime | None = None,
) -> TemporalMemoryPrecedence:
    """Derive current-vs-stale precedence for a bounded memory candidate set."""

    normalized_scopes = _normalize_scope_ids(scope_ids)
    semantic_at = normalize_query_instant(
        valid_at,
        timezone_name=timezone_name,
        now=now,
    )
    if memory_ids is None:
        scope_placeholders = ",".join("?" for _ in normalized_scopes)
        discovered = conn.execute(
            f"""
            SELECT DISTINCT memory_id
            FROM fact_claims
            WHERE scope_id IN ({scope_placeholders})
            ORDER BY memory_id ASC
            LIMIT ?
            """,
            [*normalized_scopes, MAX_PRECEDENCE_MEMORY_IDS + 1],
        ).fetchall()
        if len(discovered) > MAX_PRECEDENCE_MEMORY_IDS:
            raise TemporalQueryError(
                "memory_ids are required when temporal precedence exceeds "
                f"{MAX_PRECEDENCE_MEMORY_IDS} ledger memories"
            )
        bounded_memory_ids = [str(row["memory_id"]) for row in discovered]
    else:
        bounded_memory_ids = _normalize_memory_ids(memory_ids)
    if not bounded_memory_ids:
        return TemporalMemoryPrecedence(
            semantic_at=semantic_at,
            current_memory_ids=frozenset(),
            suppressed_memory_ids=frozenset(),
            current_fact_keys=frozenset(),
        )
    current: list[FactClaim] = []
    for batch in _batches(bounded_memory_ids):
        current.extend(
            current_claims_for_scopes(
                conn,
                scope_ids=normalized_scopes,
                valid_at=semantic_at,
                memory_ids=batch,
            )
        )
    scope_placeholders = ",".join("?" for _ in normalized_scopes)
    ledger_memory_ids: set[str] = set()
    for batch in _batches(bounded_memory_ids):
        memory_placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT DISTINCT memory_id
            FROM fact_claims
            WHERE scope_id IN ({scope_placeholders})
              AND memory_id IN ({memory_placeholders})
            """,
            [*normalized_scopes, *batch],
        ).fetchall()
        ledger_memory_ids.update(str(row["memory_id"]) for row in rows)
    current_memory_ids = frozenset(claim.memory_id for claim in current)
    return TemporalMemoryPrecedence(
        semantic_at=semantic_at,
        current_memory_ids=current_memory_ids,
        suppressed_memory_ids=frozenset(ledger_memory_ids - current_memory_ids),
        current_fact_keys=frozenset(claim.fact_key for claim in current),
    )


def _fts_token_routes(tokens: list[str]) -> list[list[str]]:
    """Return bounded routes while preserving complete coverage through 200 tokens.

    Head/tail/spread routes preserve high-value positions. Remaining tokens are
    chunked deterministically until the explicit route budget is exhausted; the
    caller reports any residual coverage gap instead of claiming completeness.
    """

    unique = list(dict.fromkeys(str(token) for token in tokens if str(token)))
    if len(unique) <= MAX_FTS_ROUTE_TOKENS:
        return [unique] if unique else []
    head = unique[:MAX_FTS_ROUTE_TOKENS]
    tail = unique[-MAX_FTS_ROUTE_TOKENS:]
    # Even sampling prevents a long middle clause from disappearing while the
    # independent tail route preserves late decisive terms such as an identifier.
    last = len(unique) - 1
    spread_denominator = MAX_FTS_ROUTE_TOKENS - 1
    spread = [
        unique[round(index * last / spread_denominator)]
        for index in range(MAX_FTS_ROUTE_TOKENS)
    ]
    routes: list[list[str]] = []
    for route in (head, tail, spread):
        deduped = list(dict.fromkeys(route))
        if deduped and deduped not in routes:
            routes.append(deduped)
    covered = {token for route in routes for token in route}
    remaining = [token for token in unique if token not in covered]
    for index in range(0, len(remaining), MAX_FTS_ROUTE_TOKENS):
        if len(routes) >= MAX_FTS_TOKEN_ROUTES:
            break
        routes.append(remaining[index : index + MAX_FTS_ROUTE_TOKENS])
    return routes


def _current_recall_claim_ids(
    conn: sqlite3.Connection,
    *,
    scope_ids: list[str],
    semantic_at: str,
    query: str,
) -> tuple[list[str], dict[str, Any]]:
    """Generate an indexed, relevance-ordered candidate pool before scoring."""

    normalized_query = str(query or "").strip().casefold()
    if len(normalized_query) > MAX_CURRENT_QUERY_CHARS:
        raise TemporalQueryError(
            f"query exceeds {MAX_CURRENT_QUERY_CHARS} characters"
        )
    semantic_tokens = semantic_query_tokens(normalized_query)
    token_routes = _fts_token_routes(semantic_tokens)
    unique_semantic_tokens = list(dict.fromkeys(semantic_tokens))
    covered_tokens = {token for route in token_routes for token in route}
    token_count = len(unique_semantic_tokens)
    covered_token_count = sum(
        1 for token in unique_semantic_tokens if token in covered_tokens
    )
    token_coverage_complete = covered_token_count == token_count
    scope_placeholders = ",".join("?" for _ in scope_ids)
    lifecycle_clause = ordinary_recall_lifecycle_visible_sql("m")
    candidate_window = MAX_CURRENT_FACT_CANDIDATES + 1
    route_candidate_counts: list[int] = []
    raw_unique_candidate_count = 0

    if token_routes:
        route_claim_ids: list[list[str]] = []
        for route in token_routes:
            fts_query = build_fts_query(route)
            route_rows = conn.execute(
                f"""
                WITH ranked_candidates AS (
                    SELECT claim_id, bm25(fact_claims_fts) AS relevance_rank
                    FROM fact_claims_fts
                    WHERE fact_claims_fts MATCH ?
                )
                SELECT fc.claim_id
                FROM ranked_candidates AS ranked
                JOIN fact_claims AS fc ON fc.claim_id = ranked.claim_id
                JOIN memories AS m
                  ON m.id = fc.memory_id AND m.scope_id = fc.scope_id
                WHERE fc.scope_id IN ({scope_placeholders})
                  AND fc.status = 'current'
                  AND fc.retired_at IS NULL
                  AND (fc.valid_from IS NULL OR fc.valid_from <= ?)
                  AND (fc.valid_to IS NULL OR fc.valid_to > ?)
                  AND {lifecycle_clause}
                ORDER BY ranked.relevance_rank ASC,
                         fc.confidence DESC,
                         fc.recorded_at DESC,
                         fc.claim_id ASC
                LIMIT ?
                """,
                [
                    fts_query,
                    *scope_ids,
                    semantic_at,
                    semantic_at,
                    candidate_window,
                ],
            ).fetchall()
            ids = [str(row["claim_id"]) for row in route_rows]
            route_claim_ids.append(ids)
            route_candidate_counts.append(len(ids))

        raw_unique_candidate_count = len(
            {claim_id for route in route_claim_ids for claim_id in route}
        )
        selected: list[str] = []
        seen: set[str] = set()
        max_route_length = max((len(route) for route in route_claim_ids), default=0)
        for rank in range(max_route_length):
            for route in route_claim_ids:
                if rank >= len(route):
                    continue
                claim_id = route[rank]
                if claim_id in seen:
                    continue
                seen.add(claim_id)
                selected.append(claim_id)
                if len(selected) >= MAX_CURRENT_FACT_CANDIDATES:
                    break
            if len(selected) >= MAX_CURRENT_FACT_CANDIDATES:
                break
        bounded_claim_ids = selected
        truncated = (
            raw_unique_candidate_count > MAX_CURRENT_FACT_CANDIDATES
            or any(count >= candidate_window for count in route_candidate_counts)
        )
        strategy = (
            "fts5_bm25"
            if len(token_routes) == 1
            else "fts5_bm25_multi_route"
        )
    else:
        rows = conn.execute(
            f"""
            SELECT fc.claim_id
            FROM fact_claims AS fc
            JOIN memories AS m
              ON m.id = fc.memory_id AND m.scope_id = fc.scope_id
            WHERE fc.scope_id IN ({scope_placeholders})
              AND fc.status = 'current'
              AND fc.retired_at IS NULL
              AND (fc.valid_from IS NULL OR fc.valid_from <= ?)
              AND (fc.valid_to IS NULL OR fc.valid_to > ?)
              AND {lifecycle_clause}
            ORDER BY fc.confidence DESC, fc.recorded_at DESC, fc.claim_id ASC
            LIMIT ?
            """,
            [*scope_ids, semantic_at, semantic_at, candidate_window],
        ).fetchall()
        truncated = len(rows) > MAX_CURRENT_FACT_CANDIDATES
        bounded_claim_ids = [
            str(row["claim_id"])
            for row in rows[:MAX_CURRENT_FACT_CANDIDATES]
        ]
        raw_unique_candidate_count = len(rows)
        strategy = "bounded_current_listing"

    diagnostics = {
        "strategy": strategy,
        "semantic_tokens": semantic_tokens,
        "token_routes": token_routes,
        "route_candidate_counts": route_candidate_counts,
        "token_count": token_count,
        "covered_token_count": covered_token_count,
        "token_coverage_complete": token_coverage_complete,
        "candidate_limit": MAX_CURRENT_FACT_CANDIDATES,
        "candidate_count": len(bounded_claim_ids),
        "raw_unique_candidate_count": raw_unique_candidate_count,
        "truncated": truncated,
        "complete": not truncated and token_coverage_complete,
    }
    return bounded_claim_ids, diagnostics


def _get_claims_by_ids_batched(
    conn: sqlite3.Connection,
    *,
    claim_ids: list[str],
    scope_ids: list[str],
) -> list[FactClaim]:
    claims: list[FactClaim] = []
    for index in range(0, len(claim_ids), 1_000):
        claims.extend(
            get_claims_by_ids(
                conn,
                claim_ids=claim_ids[index : index + 1_000],
                scope_ids=scope_ids,
            )
        )
    return claims


def query_current_fact_views(
    conn: sqlite3.Connection,
    *,
    scope_ids: list[str] | tuple[str, ...],
    query: str = "",
    valid_at: Any = None,
    timezone_name: str = "UTC",
    now: datetime | None = None,
    limit: int = 50,
    diagnostics: dict[str, Any] | None = None,
) -> list[CurrentFactView]:
    """Return current claims joined to recall-visible memory rows, without writes."""

    bounded_limit = _bounded_limit(limit, maximum=200)
    normalized_scopes = _normalize_scope_ids(scope_ids)
    semantic_at = normalize_query_instant(
        valid_at,
        timezone_name=timezone_name,
        now=now,
    )
    claim_ids, candidate_diagnostics = _current_recall_claim_ids(
        conn,
        scope_ids=normalized_scopes,
        semantic_at=semantic_at,
        query=query,
    )
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(candidate_diagnostics)
    claims = _get_claims_by_ids_batched(
        conn,
        claim_ids=claim_ids,
        scope_ids=normalized_scopes,
    )
    rows = _memory_rows(
        conn,
        memory_ids=list(dict.fromkeys(claim.memory_id for claim in claims)),
        scope_ids=normalized_scopes,
    )
    evidence = _evidence_counts(conn, [claim.claim_id for claim in claims])
    views: list[CurrentFactView] = []
    for claim in claims:
        row = rows.get(claim.memory_id)
        if row is None:
            continue
        content = str(row["content"] or "")
        summary = str(row["summary"] or content)
        score_explain = _query_score_details(query, claim, content, summary)
        score = float(score_explain["score"])
        if query and score <= 0.0:
            continue
        views.append(
            CurrentFactView(
                claim_id=claim.claim_id,
                memory_id=claim.memory_id,
                scope_id=claim.scope_id,
                fact_key=claim.fact_key,
                subject_key=claim.subject_key,
                predicate_key=claim.predicate_key,
                value=claim.value,
                cardinality=claim.cardinality,
                status=claim.status,
                valid_from=claim.valid_from,
                valid_to=claim.valid_to,
                recorded_at=claim.recorded_at,
                confidence=claim.confidence,
                source_type=claim.source_type,
                source_ref=claim.source_ref,
                content=content,
                summary=summary,
                source=str(row["source"] or ""),
                target=str(row["target"] or "memory"),
                updated_at=str(row["updated_at"] or claim.recorded_at),
                evidence_count=evidence.get(claim.claim_id, 0),
                semantic_at=semantic_at,
                score=score,
                score_explain=score_explain,
            )
        )
    views.sort(
        key=lambda item: (
            -item.score,
            -item.confidence,
            item.fact_key,
            item.claim_id,
        )
    )
    return views[:bounded_limit]


__all__ = [
    "CurrentFactView",
    "TemporalFactView",
    "TemporalMemoryPrecedence",
    "TemporalQueryError",
    "normalize_query_instant",
    "query_current_fact_views",
    "query_fact_views",
    "query_temporal_memory_precedence",
]
