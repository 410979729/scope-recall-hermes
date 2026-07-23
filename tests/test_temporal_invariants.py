"""Temporal invariants for structured factual assertions."""

from __future__ import annotations

import sqlite3

import pytest

from scope_recall.sql_store import ensure_schema
from scope_recall.fact_repository import (
    TemporalConflictError,
    TemporalValidationError,
    claim_history,
    current_claims,
    insert_claim,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    for memory_id in ("m1", "m2", "m3", "m4"):
        conn.execute(
            """
            INSERT INTO memories(
                id, scope_id, source, target, content, summary, created_at, updated_at
            ) VALUES (?, 'scope-a', 'test', 'memory', ?, ?,
                      '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (memory_id, memory_id, memory_id),
        )
    return conn


def _insert(
    conn: sqlite3.Connection,
    claim_id: str,
    memory_id: str,
    *,
    value: str,
    cardinality: str = "single",
    valid_from: str | None = None,
    valid_to: str | None = None,
    recorded_at: str = "2026-04-01T00:00:00+00:00",
):
    return insert_claim(
        conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id="scope-a",
        subject="Asha",
        predicate="likes",
        value=value,
        cardinality=cardinality,
        assertion_kind="direct",
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=recorded_at,
        confidence=0.8,
        source_type="message",
        source_ref=claim_id,
    )


def test_overlapping_single_value_intervals_are_rejected():
    conn = _conn()
    _insert(
        conn,
        "c1",
        "m1",
        value="tea",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to="2026-03-01T00:00:00+00:00",
    )

    with pytest.raises(TemporalConflictError, match="overlaps"):
        _insert(
            conn,
            "c2",
            "m2",
            value="coffee",
            valid_from="2026-02-01T00:00:00+00:00",
            valid_to="2026-04-01T00:00:00+00:00",
        )

    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 1


def test_adjacent_single_intervals_are_allowed_with_left_closed_right_open_semantics():
    conn = _conn()
    _insert(
        conn,
        "c1",
        "m1",
        value="tea",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to="2026-02-01T00:00:00+00:00",
    )
    # Historical rows are not system-current; direct status transition models a
    # fixture that T08 close_claim_interval covers behaviorally.
    conn.execute(
        "UPDATE fact_claims SET status='superseded', retired_at='2026-04-01T00:00:00+00:00' WHERE claim_id='c1'"
    )

    second = _insert(
        conn,
        "c2",
        "m2",
        value="coffee",
        valid_from="2026-02-01T00:00:00+00:00",
        valid_to="2026-03-01T00:00:00+00:00",
    )

    assert second.claim_id == "c2"


def test_multi_value_slots_can_overlap():
    conn = _conn()
    first = _insert(
        conn,
        "c1",
        "m1",
        value="tea",
        cardinality="multi",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    second = _insert(
        conn,
        "c2",
        "m2",
        value="coffee",
        cardinality="multiple",
        valid_from="2026-01-01T00:00:00+00:00",
    )

    assert first.cardinality == second.cardinality == "multi"
    assert len(current_claims(conn, scope_id="scope-a", subject="Asha", predicate="likes")) == 2


def test_timestamps_normalize_to_utc_and_missing_valid_time_stays_unknown():
    conn = _conn()
    converted = _insert(
        conn,
        "c1",
        "m1",
        value="tea",
        cardinality="multi",
        valid_from="2026-01-01T08:00:00+08:00",
        recorded_at="2026-04-01T08:00:00+08:00",
    )
    unknown = _insert(
        conn,
        "c2",
        "m2",
        value="coffee",
        cardinality="multi",
        valid_from=None,
        valid_to=None,
    )

    assert converted.valid_from == "2026-01-01T00:00:00+00:00"
    assert converted.recorded_at == "2026-04-01T00:00:00+00:00"
    assert unknown.valid_from is None
    assert unknown.valid_to is None


def test_naive_or_inverted_timestamps_fail_validation_without_writes():
    conn = _conn()
    before = conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0]

    with pytest.raises(TemporalValidationError, match="timezone"):
        _insert(
            conn,
            "c1",
            "m1",
            value="tea",
            valid_from="2026-01-01T00:00:00",
        )
    with pytest.raises(TemporalValidationError, match="valid_to"):
        _insert(
            conn,
            "c2",
            "m2",
            value="coffee",
            valid_from="2026-03-01T00:00:00+00:00",
            valid_to="2026-02-01T00:00:00+00:00",
        )

    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == before


def test_memory_scope_mismatch_fails_closed():
    conn = _conn()
    conn.execute("UPDATE memories SET scope_id='scope-b' WHERE id='m1'")

    with pytest.raises(TemporalValidationError, match="memory scope"):
        _insert(conn, "c1", "m1", value="tea")

    assert claim_history(conn, scope_id="scope-a", subject="Asha", predicate="likes") == []
