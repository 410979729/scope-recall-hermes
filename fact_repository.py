"""Transaction-neutral repository for structured bitemporal fact claims.

All write helpers accept an explicit SQLite connection and never commit. Query
helpers are read-only and preserve scope isolation and dual valid/recorded time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3
from typing import Any, Mapping

from .capture_filters import (
    contains_secret_like_text,
    sanitize_report_text,
    sanitize_structured_value,
)
from .fact_identity import FactIdentityError, build_fact_identity, canonical_fact_key

class TemporalFactError(ValueError):
    """Base error for structured temporal-fact operations."""


class TemporalValidationError(TemporalFactError):
    """Raised when a claim cannot satisfy repository input contracts."""


class TemporalConflictError(TemporalFactError):
    """Raised when a write would violate identity, CAS, or interval rules."""


FACT_EXECUTOR_MUTATION_AUTHORITY = "fact_executor"


class FactMutationAuthorityError(RuntimeError):
    """A legacy mutation attempted to change a fact-owned memory."""

    def __init__(
        self,
        operation: str,
        ownership: Mapping[str, tuple[str, ...]],
    ) -> None:
        self.operation = str(operation or "legacy mutation")
        self.ownership = {
            str(memory_id): tuple(str(status) for status in statuses)
            for memory_id, statuses in ownership.items()
        }
        self.memory_ids = tuple(sorted(self.ownership))
        joined = ", ".join(self.memory_ids)
        super().__init__(
            f"{self.operation} is blocked for fact-owned memory ids [{joined}]; "
            "use structured Fact Evolution/review"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "blocked_fact_ids": list(self.memory_ids),
            "fact_claim_statuses": {
                memory_id: list(statuses)
                for memory_id, statuses in self.ownership.items()
            },
        }


def fact_ownership_for_memories(
    conn: sqlite3.Connection,
    memory_ids: list[str] | tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """Return every structured-claim status owned by each requested memory.

    History is ownership too: mutating or deleting a terminal fact memory through
    legacy CRUD would corrupt valid/recorded-as-of auditability even when no
    current claim remains. The helper is read-only and safe before schema v1.8.
    """

    clean_ids = sorted(
        {str(memory_id).strip() for memory_id in memory_ids if str(memory_id).strip()}
    )
    if not clean_ids:
        return {}
    table_exists = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'fact_claims'
        """
    ).fetchone()
    if table_exists is None:
        return {}
    placeholders = ",".join("?" for _ in clean_ids)
    rows = conn.execute(
        f"""
        SELECT memory_id, status
        FROM fact_claims
        WHERE memory_id IN ({placeholders})
        ORDER BY memory_id, status, claim_id
        """,
        clean_ids,
    ).fetchall()
    collected: dict[str, list[str]] = {}
    for row in rows:
        if isinstance(row, sqlite3.Row):
            memory_id = str(row["memory_id"])
            status = str(row["status"] or "unknown")
        else:
            memory_id = str(row[0])
            status = str(row[1] or "unknown")
        statuses = collected.setdefault(memory_id, [])
        if status not in statuses:
            statuses.append(status)
    return {memory_id: tuple(statuses) for memory_id, statuses in collected.items()}


def require_fact_mutation_authority(
    conn: sqlite3.Connection,
    memory_ids: list[str] | tuple[str, ...],
    *,
    operation: str,
    authority: str = "",
) -> None:
    """Fail closed unless the sole fact executor explicitly owns the mutation."""

    if str(authority or "") == FACT_EXECUTOR_MUTATION_AUTHORITY:
        return
    ownership = fact_ownership_for_memories(conn, memory_ids)
    if ownership:
        raise FactMutationAuthorityError(operation, ownership)


@dataclass(frozen=True, slots=True)
class FactClaim:
    claim_id: str
    memory_id: str
    scope_id: str
    subject_key: str
    predicate_key: str
    fact_key: str
    value: str
    normalized_value: str
    value_fingerprint: str
    cardinality: str
    assertion_kind: str
    valid_from: str | None
    valid_to: str | None
    recorded_at: str
    retired_at: str | None
    status: str
    confidence: float
    superseded_by_claim_id: str | None
    source_type: str
    source_ref: str
    evidence_hash: str
    metadata: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = {
            field: getattr(self, field)
            for field in (
                "claim_id",
                "memory_id",
                "scope_id",
                "subject_key",
                "predicate_key",
                "fact_key",
                "value",
                "normalized_value",
                "value_fingerprint",
                "cardinality",
                "assertion_kind",
                "valid_from",
                "valid_to",
                "recorded_at",
                "retired_at",
                "status",
                "confidence",
                "superseded_by_claim_id",
                "source_type",
                "source_ref",
                "evidence_hash",
            )
        }
        payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True, slots=True)
class ClaimEvidence:
    evidence_id: str
    claim_id: str
    source_type: str
    source_ref: str
    evidence_hash: str
    excerpt: str
    recorded_at: str
    metadata: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "claim_id": self.claim_id,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "evidence_hash": self.evidence_hash,
            "excerpt": self.excerpt,
            "recorded_at": self.recorded_at,
            "metadata": dict(self.metadata),
        }


_CARDINALITY_ALIASES = {
    "single": "single",
    "multi": "multi",
    "multiple": "multi",
    "many": "multi",
}
_ASSERTION_KINDS = frozenset({"direct", "inferred", "validated"})
_CLAIM_STATUSES = frozenset({"current", "superseded", "retracted", "uncertain"})
_INSERTABLE_CLAIM_STATUSES = frozenset({"current", "uncertain"})
_AUTHORITATIVE_INTERVAL_STATUSES = frozenset({"current", "superseded", "retracted"})
_VALID_TO_RECORDED_AT_META = "_valid_to_recorded_at"
_VALID_TO_PROVENANCE_META = "_valid_to_provenance"
_VALID_TO_PROVENANCE_RECORDED = "recorded"
_VALID_TO_PROVENANCE_CLOSURE = "closure"
_CLAIM_SELECT = """
    SELECT claim_id, memory_id, scope_id, subject_key, predicate_key, fact_key,
           value, normalized_value, value_fingerprint, cardinality,
           assertion_kind, valid_from, valid_to, recorded_at, retired_at,
           status, confidence, superseded_by_claim_id, source_type, source_ref,
           evidence_hash, metadata
    FROM fact_claims
"""


def _claim_select_indexed_by(index_name: str) -> str:
    """Return the stable claim projection with one allowlisted SQLite index hint."""

    if index_name != "idx_fact_claims_memory":
        raise TemporalValidationError("unsupported fact claim index hint")
    return _CLAIM_SELECT.replace(
        "FROM fact_claims",
        f"FROM fact_claims INDEXED BY {index_name}",
        1,
    )


def _required_text(value: Any, *, field: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise TemporalValidationError(f"{field} is required")
    if len(text) > max_chars:
        raise TemporalValidationError(f"{field} exceeds {max_chars} characters")
    if any(ord(char) < 32 for char in text):
        raise TemporalValidationError(f"{field} contains control characters")
    return text


def _optional_report_text(value: Any, *, max_chars: int) -> str:
    if value in (None, ""):
        return ""
    raw = str(value)
    if contains_secret_like_text(raw):
        raise TemporalValidationError("secret-like text is not allowed")
    return sanitize_report_text(raw)[:max_chars].strip()


def _display_value(value: Any) -> str:
    raw = str(value or "")
    if contains_secret_like_text(raw):
        raise TemporalValidationError("secret-like value is not allowed")
    display = sanitize_report_text(raw).strip()
    if not display:
        raise TemporalValidationError("value is required")
    if len(display) > 2000:
        raise TemporalValidationError("value exceeds 2000 characters")
    return display


def _normalize_cardinality(value: Any) -> str:
    normalized = _CARDINALITY_ALIASES.get(str(value or "single").strip().lower())
    if normalized is None:
        raise TemporalValidationError("cardinality must be single or multi")
    return normalized


def _normalize_choice(value: Any, *, field: str, allowed: frozenset[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise TemporalValidationError(
            f"{field} must be one of {', '.join(sorted(allowed))}"
        )
    return normalized


def _normalize_timestamp(
    value: Any,
    *,
    field: str,
    required: bool,
) -> str | None:
    if value in (None, ""):
        if required:
            raise TemporalValidationError(f"{field} is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise TemporalValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TemporalValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _as_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _validated_valid_to_provenance(
    claim: FactClaim,
) -> tuple[str, datetime] | None:
    raw_timestamp = claim.metadata.get(_VALID_TO_RECORDED_AT_META)
    raw_kind = claim.metadata.get(_VALID_TO_PROVENANCE_META)
    if claim.valid_to is None:
        if raw_timestamp is not None or raw_kind is not None:
            raise TemporalValidationError(
                "open interval cannot carry valid_to transaction-time provenance"
            )
        return None
    if not raw_timestamp:
        raise TemporalValidationError(
            "valid_to transaction-time provenance is missing"
        )
    try:
        normalized_timestamp = _normalize_timestamp(
            raw_timestamp,
            field="valid_to provenance",
            required=True,
        )
    except TemporalValidationError as exc:
        raise TemporalValidationError(
            "valid_to transaction-time provenance is invalid"
        ) from exc
    if raw_kind not in {
        _VALID_TO_PROVENANCE_RECORDED,
        _VALID_TO_PROVENANCE_CLOSURE,
    }:
        raise TemporalValidationError(
            "valid_to transaction-time provenance kind is invalid"
        )
    assert normalized_timestamp is not None
    expected_timestamp = (
        claim.recorded_at
        if raw_kind == _VALID_TO_PROVENANCE_RECORDED
        else claim.retired_at
    )
    if expected_timestamp is None or normalized_timestamp != expected_timestamp:
        raise TemporalValidationError(
            "valid_to transaction-time provenance is inconsistent"
        )
    parsed = _as_datetime(normalized_timestamp)
    assert parsed is not None
    return str(raw_kind), parsed


def _metadata_json(metadata: Mapping[str, Any] | None) -> str:
    raw_mapping = dict(metadata or {})
    raw_json = json.dumps(raw_mapping, ensure_ascii=False, default=str)
    if contains_secret_like_text(raw_json):
        raise TemporalValidationError("secret-like metadata is not allowed")
    sanitized, _ = sanitize_structured_value(raw_mapping)
    if not isinstance(sanitized, Mapping):
        sanitized = {}
    return json.dumps(
        dict(sanitized),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _metadata_from_json(value: Any, *, field: str = "metadata") -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise TemporalValidationError(f"{field} is invalid") from exc
    if not isinstance(parsed, Mapping):
        raise TemporalValidationError(f"{field} must be an object")
    return dict(parsed)


def _row_mapping(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {str(key): row[key] for key in row.keys()}
    columns = [str(item[0]) for item in cursor.description or []]
    return {column: row[index] for index, column in enumerate(columns)}


def _claim_from_mapping(row: Mapping[str, Any]) -> FactClaim:
    claim = FactClaim(
        claim_id=str(row["claim_id"]),
        memory_id=str(row["memory_id"]),
        scope_id=str(row["scope_id"]),
        subject_key=str(row["subject_key"]),
        predicate_key=str(row["predicate_key"]),
        fact_key=str(row["fact_key"]),
        value=str(row["value"]),
        normalized_value=str(row["normalized_value"]),
        value_fingerprint=str(row["value_fingerprint"]),
        cardinality=str(row["cardinality"]),
        assertion_kind=str(row["assertion_kind"]),
        valid_from=str(row["valid_from"]) if row["valid_from"] is not None else None,
        valid_to=str(row["valid_to"]) if row["valid_to"] is not None else None,
        recorded_at=str(row["recorded_at"]),
        retired_at=str(row["retired_at"]) if row["retired_at"] is not None else None,
        status=str(row["status"]),
        confidence=float(row["confidence"]),
        superseded_by_claim_id=(
            str(row["superseded_by_claim_id"])
            if row["superseded_by_claim_id"] is not None
            else None
        ),
        source_type=str(row["source_type"]),
        source_ref=str(row["source_ref"]),
        evidence_hash=str(row["evidence_hash"]),
        metadata=_metadata_from_json(row["metadata"], field="claim metadata"),
    )
    _validated_valid_to_provenance(claim)
    return claim


def _fetch_claims(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> list[FactClaim]:
    cursor = conn.execute(sql, params)
    return [
        _claim_from_mapping(_row_mapping(cursor, row))
        for row in cursor.fetchall()
    ]


def _get_claim_unscoped(
    conn: sqlite3.Connection,
    claim_id: str,
) -> FactClaim | None:
    normalized_id = _required_text(claim_id, field="claim_id", max_chars=160)
    claims = _fetch_claims(
        conn,
        f"{_CLAIM_SELECT} WHERE claim_id = ?",
        (normalized_id,),
    )
    return claims[0] if claims else None


def get_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    scope_ids: list[str] | tuple[str, ...],
) -> FactClaim | None:
    """Return one claim only when it belongs to an explicitly allowed scope."""

    normalized_id = _required_text(claim_id, field="claim_id", max_chars=160)
    if not isinstance(scope_ids, (list, tuple)) or not scope_ids:
        raise TemporalValidationError("scope_ids must contain at least one scope")
    if len(scope_ids) > 64:
        raise TemporalValidationError("scope_ids exceeds 64 entries")
    normalized_scopes = tuple(
        dict.fromkeys(
            _required_text(scope_id, field="scope_id", max_chars=240)
            for scope_id in scope_ids
        )
    )
    if not normalized_scopes:
        raise TemporalValidationError("scope_ids must contain at least one scope")
    if len(normalized_scopes) > 64:
        raise TemporalValidationError("scope_ids exceeds 64 entries")
    placeholders = ",".join("?" for _ in normalized_scopes)
    claims = _fetch_claims(
        conn,
        f"{_CLAIM_SELECT} WHERE claim_id = ? "
        f"AND scope_id IN ({placeholders})",
        (normalized_id, *normalized_scopes),
    )
    return claims[0] if claims else None


def get_claims_by_ids(
    conn: sqlite3.Connection,
    *,
    claim_ids: list[str] | tuple[str, ...],
    scope_ids: list[str] | tuple[str, ...],
) -> list[FactClaim]:
    """Return a bounded scoped claim set while preserving caller order."""

    if not isinstance(claim_ids, (list, tuple)):
        raise TemporalValidationError("claim_ids must be a list or tuple")
    if not isinstance(scope_ids, (list, tuple)) or not scope_ids:
        raise TemporalValidationError("scope_ids must contain at least one scope")
    if len(claim_ids) > 1000:
        raise TemporalValidationError("claim_ids exceeds 1000 entries")
    if len(scope_ids) > 64:
        raise TemporalValidationError("scope_ids exceeds 64 entries")
    normalized_claim_ids = tuple(
        dict.fromkeys(
            _required_text(claim_id, field="claim_id", max_chars=160)
            for claim_id in claim_ids
        )
    )
    normalized_scopes = tuple(
        dict.fromkeys(
            _required_text(scope_id, field="scope_id", max_chars=240)
            for scope_id in scope_ids
        )
    )
    if not normalized_claim_ids:
        return []
    if not normalized_scopes:
        raise TemporalValidationError("scope_ids must contain at least one scope")
    if len(normalized_claim_ids) > 1000:
        raise TemporalValidationError("claim_ids exceeds 1000 entries")
    if len(normalized_scopes) > 64:
        raise TemporalValidationError("scope_ids exceeds 64 entries")
    scope_placeholders = ",".join("?" for _ in normalized_scopes)
    by_id: dict[str, FactClaim] = {}
    for index in range(0, len(normalized_claim_ids), 400):
        batch = normalized_claim_ids[index : index + 400]
        claim_placeholders = ",".join("?" for _ in batch)
        claims = _fetch_claims(
            conn,
            f"{_CLAIM_SELECT} "
            f"WHERE claim_id IN ({claim_placeholders}) "
            f"AND scope_id IN ({scope_placeholders})",
            (*batch, *normalized_scopes),
        )
        by_id.update({claim.claim_id: claim for claim in claims})
    return [by_id[claim_id] for claim_id in normalized_claim_ids if claim_id in by_id]


def predecessor_claim_ids_by_successor(
    conn: sqlite3.Connection,
    *,
    successor_claim_ids: list[str] | tuple[str, ...],
    scope_ids: list[str] | tuple[str, ...],
    known_at: str | None = None,
) -> dict[str, str]:
    """Resolve one deterministic predecessor for bounded successor claim IDs."""

    if not isinstance(successor_claim_ids, (list, tuple)):
        raise TemporalValidationError("successor_claim_ids must be a list or tuple")
    if not isinstance(scope_ids, (list, tuple)) or not scope_ids:
        raise TemporalValidationError("scope_ids must contain at least one scope")
    if len(successor_claim_ids) > 1000:
        raise TemporalValidationError("successor_claim_ids exceeds 1000 entries")
    if len(scope_ids) > 64:
        raise TemporalValidationError("scope_ids exceeds 64 entries")
    normalized_successors = tuple(
        dict.fromkeys(
            _required_text(claim_id, field="claim_id", max_chars=160)
            for claim_id in successor_claim_ids
        )
    )
    normalized_scopes = tuple(
        dict.fromkeys(
            _required_text(scope_id, field="scope_id", max_chars=240)
            for scope_id in scope_ids
        )
    )
    if not normalized_successors:
        return {}
    if not normalized_scopes:
        raise TemporalValidationError("scope_ids must contain at least one scope")
    if len(normalized_successors) > 1000:
        raise TemporalValidationError("successor_claim_ids exceeds 1000 entries")
    if len(normalized_scopes) > 64:
        raise TemporalValidationError("scope_ids exceeds 64 entries")
    normalized_known_at = (
        _normalize_timestamp(known_at, field="known_at", required=True)
        if known_at not in (None, "")
        else None
    )
    scope_placeholders = ",".join("?" for _ in normalized_scopes)
    output: dict[str, str] = {}
    for index in range(0, len(normalized_successors), 400):
        batch = normalized_successors[index : index + 400]
        successor_placeholders = ",".join("?" for _ in batch)
        predecessor_cutoff = (
            "AND predecessor.retired_at <= ?" if normalized_known_at else ""
        )
        successor_cutoff = (
            "AND successor.recorded_at <= ?" if normalized_known_at else ""
        )
        params: tuple[Any, ...]
        if normalized_known_at:
            params = (
                normalized_known_at,
                normalized_known_at,
                *batch,
                *normalized_scopes,
                normalized_known_at,
            )
        else:
            params = (*batch, *normalized_scopes)
        rows = conn.execute(
            f"""
            SELECT successor.claim_id AS successor_id,
                   (
                       SELECT predecessor.claim_id
                       FROM fact_claims AS predecessor
                       WHERE predecessor.superseded_by_claim_id = successor.claim_id
                         AND predecessor.scope_id = successor.scope_id
                         AND predecessor.fact_key = successor.fact_key
                         {predecessor_cutoff}
                       ORDER BY predecessor.recorded_at ASC,
                                predecessor.claim_id ASC
                       LIMIT 1
                   ) AS predecessor_id,
                   (
                       SELECT predecessor.claim_id
                       FROM fact_claims AS predecessor
                       WHERE predecessor.superseded_by_claim_id = successor.claim_id
                         AND predecessor.scope_id = successor.scope_id
                         AND predecessor.fact_key = successor.fact_key
                         {predecessor_cutoff}
                       ORDER BY predecessor.recorded_at ASC,
                                predecessor.claim_id ASC
                       LIMIT 1 OFFSET 1
                   ) AS second_predecessor_id
            FROM fact_claims AS successor
            WHERE successor.claim_id IN ({successor_placeholders})
              AND successor.scope_id IN ({scope_placeholders})
              {successor_cutoff}
            ORDER BY successor.claim_id ASC
            """,
            params,
        ).fetchall()
        for row in rows:
            successor_id = str(row["successor_id"] or "")
            predecessor_id = str(row["predecessor_id"] or "")
            if not successor_id or not predecessor_id:
                continue
            if row["second_predecessor_id"] is not None:
                raise TemporalConflictError(
                    f"successor {successor_id} has multiple predecessors"
                )
            output[successor_id] = predecessor_id
    return output


def _intervals_overlap(
    first_start: str | None,
    first_end: str | None,
    second_start: str | None,
    second_end: str | None,
) -> bool:
    first_start_dt = _as_datetime(first_start)
    first_end_dt = _as_datetime(first_end)
    second_start_dt = _as_datetime(second_start)
    second_end_dt = _as_datetime(second_end)
    if first_end_dt is not None and second_start_dt is not None:
        if first_end_dt <= second_start_dt:
            return False
    if second_end_dt is not None and first_start_dt is not None:
        if second_end_dt <= first_start_dt:
            return False
    return True


def _validate_interval(valid_from: str | None, valid_to: str | None) -> None:
    if valid_from is not None and valid_to is not None:
        if _as_datetime(valid_to) <= _as_datetime(valid_from):  # type: ignore[operator]
            raise TemporalValidationError("valid_to must be later than valid_from")


def _assert_no_single_overlap(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    fact_key: str,
    valid_from: str | None,
    valid_to: str | None,
) -> None:
    rows = _fetch_claims(
        conn,
        f"{_CLAIM_SELECT} "
        "WHERE scope_id = ? AND fact_key = ? AND cardinality = 'single' "
        "AND status IN ('current', 'superseded', 'retracted')",
        (scope_id, fact_key),
    )
    for existing in rows:
        if _intervals_overlap(
            existing.valid_from,
            existing.valid_to,
            valid_from,
            valid_to,
        ):
            raise TemporalConflictError(
                f"single-value interval overlaps claim {existing.claim_id}"
            )


def insert_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    memory_id: str,
    scope_id: str,
    subject: Any,
    predicate: Any,
    value: Any,
    cardinality: str = "single",
    assertion_kind: str = "direct",
    valid_from: Any = None,
    valid_to: Any = None,
    recorded_at: Any,
    status: str = "current",
    confidence: float = 0.5,
    source_type: str,
    source_ref: Any = "",
    evidence_hash: Any = "",
    metadata: Mapping[str, Any] | None = None,
) -> FactClaim:
    """Insert one claim without committing the caller's transaction."""

    normalized_claim_id = _required_text(claim_id, field="claim_id", max_chars=160)
    normalized_memory_id = _required_text(memory_id, field="memory_id", max_chars=160)
    normalized_scope_id = _required_text(scope_id, field="scope_id", max_chars=240)
    normalized_cardinality = _normalize_cardinality(cardinality)
    normalized_assertion = _normalize_choice(
        assertion_kind,
        field="assertion_kind",
        allowed=_ASSERTION_KINDS,
    )
    normalized_status = _normalize_choice(
        status,
        field="status",
        allowed=_INSERTABLE_CLAIM_STATUSES,
    )
    if not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0:
        raise TemporalValidationError("confidence must be between 0 and 1")
    normalized_valid_from = _normalize_timestamp(
        valid_from,
        field="valid_from",
        required=False,
    )
    normalized_valid_to = _normalize_timestamp(
        valid_to,
        field="valid_to",
        required=False,
    )
    _validate_interval(normalized_valid_from, normalized_valid_to)
    normalized_recorded_at = _normalize_timestamp(
        recorded_at,
        field="recorded_at",
        required=True,
    )
    assert normalized_recorded_at is not None
    normalized_source_type = _required_text(
        source_type,
        field="source_type",
        max_chars=80,
    )
    normalized_source_ref = _optional_report_text(source_ref, max_chars=500)
    normalized_evidence_hash = _optional_report_text(evidence_hash, max_chars=160)
    display_value = _display_value(value)
    try:
        identity = build_fact_identity(subject, predicate, display_value)
    except FactIdentityError as exc:
        raise TemporalValidationError(str(exc)) from exc
    claim_metadata = dict(metadata or {})
    if normalized_valid_to is not None:
        claim_metadata[_VALID_TO_RECORDED_AT_META] = normalized_recorded_at
        claim_metadata[_VALID_TO_PROVENANCE_META] = (
            _VALID_TO_PROVENANCE_RECORDED
        )
    else:
        claim_metadata.pop(_VALID_TO_RECORDED_AT_META, None)
        claim_metadata.pop(_VALID_TO_PROVENANCE_META, None)

    memory_row = conn.execute(
        "SELECT scope_id FROM memories WHERE id = ?",
        (normalized_memory_id,),
    ).fetchone()
    if memory_row is None:
        raise TemporalValidationError("memory_id does not exist")
    memory_scope = str(memory_row[0])
    if memory_scope != normalized_scope_id:
        raise TemporalValidationError("memory scope does not match claim scope")

    if normalized_cardinality == "single" and normalized_status in _AUTHORITATIVE_INTERVAL_STATUSES:
        _assert_no_single_overlap(
            conn,
            scope_id=normalized_scope_id,
            fact_key=identity.fact_key,
            valid_from=normalized_valid_from,
            valid_to=normalized_valid_to,
        )

    try:
        conn.execute(
            """
            INSERT INTO fact_claims(
                claim_id, memory_id, scope_id, subject_key, predicate_key,
                fact_key, value, normalized_value, value_fingerprint,
                cardinality, assertion_kind, valid_from, valid_to, recorded_at,
                retired_at, status, confidence, superseded_by_claim_id,
                source_type, source_ref, evidence_hash, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?,
                      NULL, ?, ?, ?, ?)
            """,
            (
                normalized_claim_id,
                normalized_memory_id,
                normalized_scope_id,
                identity.subject,
                identity.predicate,
                identity.fact_key,
                display_value,
                identity.value,
                identity.value_fingerprint,
                normalized_cardinality,
                normalized_assertion,
                normalized_valid_from,
                normalized_valid_to,
                normalized_recorded_at,
                normalized_status,
                float(confidence),
                normalized_source_type,
                normalized_source_ref,
                normalized_evidence_hash,
                _metadata_json(claim_metadata),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise TemporalConflictError(f"claim insert conflict: {exc}") from exc
    inserted = _get_claim_unscoped(conn, normalized_claim_id)
    if inserted is None:  # pragma: no cover - SQLite insert/read invariant
        raise TemporalConflictError("claim insert did not produce a row")
    return inserted


def _evidence_from_mapping(row: Mapping[str, Any]) -> ClaimEvidence:
    return ClaimEvidence(
        evidence_id=str(row["evidence_id"]),
        claim_id=str(row["claim_id"]),
        source_type=str(row["source_type"]),
        source_ref=str(row["source_ref"]),
        evidence_hash=str(row["evidence_hash"]),
        excerpt=str(row["excerpt"]),
        recorded_at=str(row["recorded_at"]),
        metadata=_metadata_from_json(row["metadata"], field="evidence metadata"),
    )


def link_claim_evidence(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    source_type: str,
    source_ref: str,
    excerpt: Any = "",
    evidence_hash: str = "",
    recorded_at: Any,
    metadata: Mapping[str, Any] | None = None,
    evidence_id: str = "",
) -> ClaimEvidence:
    """Idempotently link bounded evidence without committing."""

    normalized_claim_id = _required_text(claim_id, field="claim_id", max_chars=160)
    if _get_claim_unscoped(conn, normalized_claim_id) is None:
        raise TemporalValidationError("claim_id does not exist")
    normalized_source_type = _required_text(
        source_type,
        field="source_type",
        max_chars=80,
    )
    normalized_source_ref = _required_text(
        source_ref,
        field="source_ref",
        max_chars=500,
    )
    normalized_source_ref = _optional_report_text(
        normalized_source_ref,
        max_chars=500,
    )
    normalized_excerpt = _optional_report_text(excerpt, max_chars=800)
    normalized_recorded_at = _normalize_timestamp(
        recorded_at,
        field="recorded_at",
        required=True,
    )
    assert normalized_recorded_at is not None
    if evidence_hash:
        normalized_hash = _required_text(
            evidence_hash,
            field="evidence_hash",
            max_chars=160,
        )
    else:
        material = "\0".join(
            (normalized_source_type, normalized_source_ref, normalized_excerpt)
        )
        normalized_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    if evidence_id:
        normalized_evidence_id = _required_text(
            evidence_id,
            field="evidence_id",
            max_chars=160,
        )
    else:
        material = "\0".join(
            (
                normalized_claim_id,
                normalized_source_type,
                normalized_source_ref,
                normalized_hash,
            )
        )
        normalized_evidence_id = (
            f"evidence:v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"
        )
    metadata_json = _metadata_json(metadata)
    try:
        conn.execute(
            """
            INSERT INTO fact_claim_evidence(
                evidence_id, claim_id, source_type, source_ref, evidence_hash,
                excerpt, recorded_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_evidence_id,
                normalized_claim_id,
                normalized_source_type,
                normalized_source_ref,
                normalized_hash,
                normalized_excerpt,
                normalized_recorded_at,
                metadata_json,
            ),
        )
    except sqlite3.IntegrityError:
        cursor = conn.execute(
            """
            SELECT evidence_id, claim_id, source_type, source_ref,
                   evidence_hash, excerpt, recorded_at, metadata
            FROM fact_claim_evidence
            WHERE claim_id = ? AND source_type = ? AND source_ref = ?
              AND evidence_hash = ?
            """,
            (
                normalized_claim_id,
                normalized_source_type,
                normalized_source_ref,
                normalized_hash,
            ),
        )
        existing = cursor.fetchone()
        if existing is None:
            raise TemporalConflictError("evidence insert conflict")
        existing_evidence = _evidence_from_mapping(_row_mapping(cursor, existing))
        expected_evidence = ClaimEvidence(
            evidence_id=normalized_evidence_id,
            claim_id=normalized_claim_id,
            source_type=normalized_source_type,
            source_ref=normalized_source_ref,
            evidence_hash=normalized_hash,
            excerpt=normalized_excerpt,
            recorded_at=normalized_recorded_at,
            metadata=_metadata_from_json(
                metadata_json,
                field="evidence metadata",
            ),
        )
        if existing_evidence != expected_evidence:
            raise TemporalConflictError(
                "evidence retry conflicts with persisted payload"
            )
        return existing_evidence
    cursor = conn.execute(
        """
        SELECT evidence_id, claim_id, source_type, source_ref,
               evidence_hash, excerpt, recorded_at, metadata
        FROM fact_claim_evidence WHERE evidence_id = ?
        """,
        (normalized_evidence_id,),
    )
    row = cursor.fetchone()
    if row is None:  # pragma: no cover - SQLite insert/read invariant
        raise TemporalConflictError("evidence insert did not produce a row")
    return _evidence_from_mapping(_row_mapping(cursor, row))


def close_claim_interval(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    retired_at: Any,
    valid_to: Any = None,
    status: str = "superseded",
    superseded_by_claim_id: str = "",
    expected_status: str = "current",
) -> FactClaim:
    """CAS-close one adopted claim without deleting it or committing."""

    normalized_status = _normalize_choice(
        status,
        field="status",
        allowed=frozenset({"superseded", "retracted"}),
    )
    normalized_expected = _normalize_choice(
        expected_status,
        field="expected_status",
        allowed=_CLAIM_STATUSES,
    )
    existing = _get_claim_unscoped(conn, claim_id)
    if existing is None:
        raise TemporalValidationError("claim_id does not exist")
    normalized_retired_at = _normalize_timestamp(
        retired_at,
        field="retired_at",
        required=True,
    )
    assert normalized_retired_at is not None
    if _as_datetime(normalized_retired_at) < _as_datetime(existing.recorded_at):  # type: ignore[operator]
        raise TemporalValidationError("retired_at cannot be earlier than recorded_at")
    normalized_valid_to = (
        _normalize_timestamp(valid_to, field="valid_to", required=False)
        if valid_to not in (None, "")
        else existing.valid_to
    )
    _validate_interval(existing.valid_from, normalized_valid_to)
    successor = (
        _required_text(
            superseded_by_claim_id,
            field="superseded_by_claim_id",
            max_chars=160,
        )
        if superseded_by_claim_id
        else None
    )
    if successor == existing.claim_id:
        raise TemporalValidationError("claim cannot supersede itself")
    if normalized_status == "retracted" and successor is not None:
        raise TemporalValidationError("retracted claim cannot have a successor")
    if normalized_status == "superseded" and successor is None:
        raise TemporalValidationError("superseded claim requires a successor")
    if successor is not None:
        successor_claim = _get_claim_unscoped(conn, successor)
        if successor_claim is not None:
            if (
                successor_claim.scope_id != existing.scope_id
                or successor_claim.fact_key != existing.fact_key
            ):
                raise TemporalValidationError(
                    "successor must share predecessor scope and fact key"
                )
            if (
                successor_claim.status not in _INSERTABLE_CLAIM_STATUSES
                or successor_claim.retired_at is not None
            ):
                raise TemporalValidationError("successor claim must be active")

    _validated_valid_to_provenance(existing)
    closure_metadata = dict(existing.metadata)
    if normalized_valid_to is None:
        closure_metadata.pop(_VALID_TO_RECORDED_AT_META, None)
        closure_metadata.pop(_VALID_TO_PROVENANCE_META, None)
    elif normalized_valid_to != existing.valid_to:
        closure_metadata[_VALID_TO_RECORDED_AT_META] = normalized_retired_at
        closure_metadata[_VALID_TO_PROVENANCE_META] = (
            _VALID_TO_PROVENANCE_CLOSURE
        )
    else:
        closure_metadata.setdefault(
            _VALID_TO_RECORDED_AT_META,
            existing.recorded_at,
        )
        closure_metadata.setdefault(
            _VALID_TO_PROVENANCE_META,
            _VALID_TO_PROVENANCE_RECORDED,
        )

    cursor = conn.execute(
        """
        UPDATE fact_claims
        SET valid_to = ?, retired_at = ?, status = ?,
            superseded_by_claim_id = ?, metadata = ?
        WHERE claim_id = ? AND status = ? AND retired_at IS NULL
        """,
        (
            normalized_valid_to,
            normalized_retired_at,
            normalized_status,
            successor,
            _metadata_json(closure_metadata),
            existing.claim_id,
            normalized_expected,
        ),
    )
    if cursor.rowcount != 1:
        raise TemporalConflictError("claim close CAS conflict")
    closed = _get_claim_unscoped(conn, existing.claim_id)
    if closed is None:  # pragma: no cover - UPDATE cannot remove the row
        raise TemporalConflictError("closed claim disappeared")
    return closed


def retract_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    retired_at: Any,
    valid_to: Any = None,
    expected_status: str = "current",
) -> FactClaim:
    return close_claim_interval(
        conn,
        claim_id=claim_id,
        retired_at=retired_at,
        valid_to=valid_to,
        status="retracted",
        expected_status=expected_status,
    )


def _query_fact_key(subject: Any, predicate: Any) -> str:
    try:
        return canonical_fact_key(subject, predicate)
    except FactIdentityError as exc:
        raise TemporalValidationError(str(exc)) from exc


def _interval_valid_at(
    *,
    valid_from: str | None,
    valid_to: str | None,
    instant: str,
) -> bool:
    instant_dt = _as_datetime(instant)
    start = _as_datetime(valid_from)
    end = _as_datetime(valid_to)
    if start is not None and instant_dt < start:  # type: ignore[operator]
        return False
    return not (end is not None and instant_dt >= end)  # type: ignore[operator]


def current_claims(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    subject: Any,
    predicate: Any,
    valid_at: Any = None,
    limit: int = 1000,
) -> list[FactClaim]:
    """Return a bounded adopted claim set for one scoped slot."""

    normalized_scope = _required_text(scope_id, field="scope_id", max_chars=240)
    fact_key = _query_fact_key(subject, predicate)
    if type(limit) is not int:
        raise TemporalValidationError("limit must be an integer")
    if limit < 1 or limit > 1000:
        raise TemporalValidationError("limit must be between 1 and 1000")
    clauses = [
        "scope_id = ?",
        "fact_key = ?",
        "status = 'current'",
        "retired_at IS NULL",
    ]
    params: list[Any] = [normalized_scope, fact_key]
    if valid_at not in (None, ""):
        normalized_valid_at = _normalize_timestamp(
            valid_at,
            field="valid_at",
            required=True,
        )
        assert normalized_valid_at is not None
        clauses.extend(
            [
                "(valid_from IS NULL OR valid_from <= ?)",
                "(valid_to IS NULL OR valid_to > ?)",
            ]
        )
        params.extend([normalized_valid_at, normalized_valid_at])
    params.append(limit + 1)
    rows = _fetch_claims(
        conn,
        f"{_CLAIM_SELECT} WHERE {' AND '.join(clauses)} "
        "ORDER BY recorded_at ASC, claim_id ASC LIMIT ?",
        tuple(params),
    )
    if len(rows) > limit:
        raise TemporalValidationError(
            f"current claim slot exceeds bounded limit of {limit} claims"
        )
    return rows


def current_claims_for_scopes(
    conn: sqlite3.Connection,
    *,
    scope_ids: list[str] | tuple[str, ...],
    valid_at: Any,
    memory_ids: list[str] | tuple[str, ...] | None = None,
    limit: int = 1000,
) -> list[FactClaim]:
    """Return semantically current claims across bounded scopes and memories."""

    if not isinstance(scope_ids, (list, tuple)) or not scope_ids:
        raise TemporalValidationError("scope_ids must contain at least one scope")
    if len(scope_ids) > 64:
        raise TemporalValidationError("scope_ids exceeds 64 entries")
    normalized_scopes = tuple(
        dict.fromkeys(
            _required_text(scope_id, field="scope_id", max_chars=240)
            for scope_id in scope_ids
        )
    )
    if len(normalized_scopes) > 64:
        raise TemporalValidationError("scope_ids exceeds 64 entries")
    normalized_memory_ids: tuple[str, ...] | None = None
    if memory_ids is not None:
        if not isinstance(memory_ids, (list, tuple)):
            raise TemporalValidationError("memory_ids must be a list or tuple")
        if len(memory_ids) > 512:
            raise TemporalValidationError("memory_ids exceeds 512 entries")
        normalized_memory_ids = tuple(
            dict.fromkeys(
                _required_text(memory_id, field="memory_id", max_chars=200)
                for memory_id in memory_ids
            )
        )
        if len(normalized_memory_ids) > 512:
            raise TemporalValidationError("memory_ids exceeds 512 entries")
        if not normalized_memory_ids:
            return []
    if type(limit) is not int:
        raise TemporalValidationError("limit must be an integer")
    bounded_limit = limit
    if bounded_limit < 1 or bounded_limit > 1000:
        raise TemporalValidationError("limit must be between 1 and 1000")
    normalized_valid_at = _normalize_timestamp(
        valid_at,
        field="valid_at",
        required=True,
    )
    assert normalized_valid_at is not None
    scope_placeholders = ",".join("?" for _ in normalized_scopes)
    clauses = [
        f"scope_id IN ({scope_placeholders})",
        "status = 'current'",
        "retired_at IS NULL",
        "(valid_from IS NULL OR valid_from <= ?)",
        "(valid_to IS NULL OR valid_to > ?)",
    ]
    params: list[Any] = [
        *normalized_scopes,
        normalized_valid_at,
        normalized_valid_at,
    ]
    claim_select = _CLAIM_SELECT
    if normalized_memory_ids is not None:
        memory_placeholders = ",".join("?" for _ in normalized_memory_ids)
        clauses.append(f"memory_id IN ({memory_placeholders})")
        params.extend(normalized_memory_ids)
        # The ORDER BY otherwise makes SQLite prefer the scope/fact index and
        # linearly filter large ledgers. Memory-precedence lookups are bounded
        # by explicit memory IDs, so force the dedicated selective index.
        claim_select = _claim_select_indexed_by("idx_fact_claims_memory")
    sql = (
        f"{claim_select} WHERE {' AND '.join(clauses)} "
        "ORDER BY fact_key ASC, recorded_at ASC, claim_id ASC LIMIT ?"
    )
    params.append(bounded_limit + 1)
    rows = _fetch_claims(conn, sql, tuple(params))
    if len(rows) > bounded_limit:
        raise TemporalValidationError(
            f"current claim query exceeds bounded limit of {bounded_limit} claims"
        )
    return rows


def claims_as_of(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    subject: Any,
    predicate: Any,
    valid_at: Any,
    known_at: Any = None,
    limit: int | None = None,
    scan_limit: int = 1000,
) -> list[FactClaim]:
    """Return bounded facts valid at a real-world instant, optionally as then known."""

    normalized_scope = _required_text(scope_id, field="scope_id", max_chars=240)
    fact_key = _query_fact_key(subject, predicate)
    normalized_valid_at = _normalize_timestamp(
        valid_at,
        field="valid_at",
        required=True,
    )
    assert normalized_valid_at is not None
    normalized_known_at = (
        _normalize_timestamp(known_at, field="known_at", required=True)
        if known_at not in (None, "")
        else None
    )
    if limit is not None:
        if type(limit) is not int:
            raise TemporalValidationError("limit must be an integer")
        if limit < 1 or limit > 100:
            raise TemporalValidationError("limit must be between 1 and 100")
    if type(scan_limit) is not int:
        raise TemporalValidationError("scan_limit must be an integer")
    if scan_limit < 1 or scan_limit > 1000:
        raise TemporalValidationError("scan_limit must be between 1 and 1000")
    rows = _fetch_claims(
        conn,
        f"{_CLAIM_SELECT} "
        "WHERE scope_id = ? AND fact_key = ? "
        "ORDER BY COALESCE(valid_from, ''), recorded_at, claim_id "
        "LIMIT ?",
        (normalized_scope, fact_key, scan_limit + 1),
    )
    if len(rows) > scan_limit:
        raise TemporalValidationError(
            f"fact slot exceeds bounded scan limit of {scan_limit} claims"
        )
    output: list[FactClaim] = []
    known_dt = _as_datetime(normalized_known_at)
    for claim in rows:
        reconstructed = claim
        effective_valid_to = claim.valid_to
        if known_dt is not None:
            if _as_datetime(claim.recorded_at) > known_dt:  # type: ignore[operator]
                continue
            provenance = _validated_valid_to_provenance(claim)
            valid_to_recorded_dt = provenance[1] if provenance else None
            retired_dt = _as_datetime(claim.retired_at)
            if retired_dt is not None and retired_dt > known_dt:
                # Retirement state was not yet knowable. Only erase the
                # interval end when its own provenance also post-dates the
                # transaction-time cutoff; original finite intervals remain.
                if (
                    valid_to_recorded_dt is not None
                    and valid_to_recorded_dt > known_dt
                ):
                    effective_valid_to = None
                reconstructed_metadata = dict(claim.metadata)
                if effective_valid_to is None:
                    reconstructed_metadata.pop(
                        _VALID_TO_RECORDED_AT_META,
                        None,
                    )
                    reconstructed_metadata.pop(
                        _VALID_TO_PROVENANCE_META,
                        None,
                    )
                reconstructed = replace(
                    claim,
                    valid_to=effective_valid_to,
                    retired_at=None,
                    status="current",
                    superseded_by_claim_id=None,
                    metadata=reconstructed_metadata,
                )
        if not _interval_valid_at(
            valid_from=reconstructed.valid_from,
            valid_to=effective_valid_to,
            instant=normalized_valid_at,
        ):
            continue
        if limit is None or len(output) < limit:
            output.append(reconstructed)
    return output


def claim_history(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    subject: Any,
    predicate: Any,
    limit: int = 1000,
    reject_overflow: bool = True,
) -> list[FactClaim]:
    """Return a scoped evolution chain in deterministic order."""

    normalized_scope = _required_text(scope_id, field="scope_id", max_chars=240)
    fact_key = _query_fact_key(subject, predicate)
    if type(limit) is not int:
        raise TemporalValidationError("limit must be an integer")
    bounded_limit = limit
    if bounded_limit < 1 or bounded_limit > 1000:
        raise TemporalValidationError("limit must be between 1 and 1000")
    if type(reject_overflow) is not bool:
        raise TemporalValidationError("reject_overflow must be a boolean")
    sql = (
        f"{_CLAIM_SELECT} "
        "WHERE scope_id = ? AND fact_key = ? "
        "ORDER BY COALESCE(valid_from, ''), recorded_at, claim_id LIMIT ?"
    )
    params: tuple[Any, ...] = (
        normalized_scope,
        fact_key,
        bounded_limit + 1,
    )
    rows = _fetch_claims(conn, sql, params)
    if len(rows) > bounded_limit and reject_overflow:
        raise TemporalValidationError(
            f"claim history exceeds bounded limit of {bounded_limit} claims"
        )
    return rows[:bounded_limit]


__all__ = [
    "ClaimEvidence",
    "FactClaim",
    "TemporalConflictError",
    "TemporalFactError",
    "TemporalValidationError",
    "claim_history",
    "claims_as_of",
    "close_claim_interval",
    "current_claims",
    "current_claims_for_scopes",
    "get_claim",
    "get_claims_by_ids",
    "insert_claim",
    "link_claim_evidence",
    "predecessor_claim_ids_by_successor",
    "retract_claim",
]
