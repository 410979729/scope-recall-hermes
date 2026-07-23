"""Repository behavior for structured bitemporal fact claims."""

from __future__ import annotations

import sqlite3

import pytest

from scope_recall.sql_store import ensure_schema
from scope_recall.fact_repository import (
    TemporalConflictError,
    TemporalValidationError,
    claim_history,
    claims_as_of,
    close_claim_interval,
    current_claims,
    current_claims_for_scopes,
    get_claim,
    get_claims_by_ids,
    insert_claim,
    link_claim_evidence,
    predecessor_claim_ids_by_successor,
    retract_claim,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def _memory(conn: sqlite3.Connection, memory_id: str, scope_id: str = "scope-a") -> None:
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary, created_at, updated_at
        ) VALUES (?, ?, 'test', 'memory', ?, ?,
                  '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
        """,
        (memory_id, scope_id, f"content-{memory_id}", f"summary-{memory_id}"),
    )


def _insert(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    memory_id: str,
    value: str,
    valid_from: str | None = None,
    valid_to: str | None = None,
    recorded_at: str = "2026-03-01T00:00:00+00:00",
    scope_id: str = "scope-a",
    cardinality: str = "single",
    predicate: str = "lives in",
    status: str = "current",
):
    return insert_claim(
        conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id=scope_id,
        subject="Asha",
        predicate=predicate,
        value=value,
        cardinality=cardinality,
        assertion_kind="direct",
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=recorded_at,
        status=status,
        confidence=0.9,
        source_type="message",
        source_ref="message-7",
        metadata={"origin": "test"},
    )


def test_insert_and_evidence_link_use_caller_transaction_without_commit():
    conn = _conn()
    _memory(conn, "memory-1")
    conn.commit()
    conn.execute("BEGIN")

    claim = _insert(
        conn,
        claim_id="claim-1",
        memory_id="memory-1",
        value="Bangalore",
        valid_from="2026-02-01T00:00:00+00:00",
    )
    evidence = link_claim_evidence(
        conn,
        claim_id=claim.claim_id,
        source_type="message",
        source_ref="message-7",
        excerpt="The user directly corrected the city.",
        recorded_at="2026-03-01T00:00:00+00:00",
    )

    assert conn.in_transaction is True
    assert claim.subject_key == "asha"
    assert claim.predicate_key == "lives in"
    assert claim.normalized_value == "bangalore"
    assert claim.metadata["origin"] == "test"
    assert claim.valid_from == "2026-02-01T00:00:00+00:00"
    assert evidence.claim_id == claim.claim_id
    assert len(evidence.evidence_hash) == 64
    exact_retry = link_claim_evidence(
        conn,
        claim_id=claim.claim_id,
        source_type="message",
        source_ref="message-7",
        excerpt="The user directly corrected the city.",
        evidence_hash=evidence.evidence_hash,
        recorded_at="2026-03-01T00:00:00+00:00",
        evidence_id=evidence.evidence_id,
    )
    assert exact_retry == evidence

    conflicting_payloads = (
        {"evidence_id": "different-explicit-id"},
        {
            "evidence_id": evidence.evidence_id,
            "excerpt": "A conflicting correction.",
        },
        {
            "evidence_id": evidence.evidence_id,
            "recorded_at": "2026-03-02T00:00:00+00:00",
        },
        {
            "evidence_id": evidence.evidence_id,
            "metadata": {"quality": "different"},
        },
    )
    for overrides in conflicting_payloads:
        payload = {
            "claim_id": claim.claim_id,
            "source_type": "message",
            "source_ref": "message-7",
            "excerpt": "The user directly corrected the city.",
            "evidence_hash": evidence.evidence_hash,
            "recorded_at": "2026-03-01T00:00:00+00:00",
        }
        payload.update(overrides)
        with pytest.raises(TemporalConflictError, match="evidence"):
            link_claim_evidence(conn, **payload)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_claim_evidence").fetchone()[0] == 0


def test_supersede_chain_supports_current_as_of_history_and_delayed_ingestion():
    conn = _conn()
    _memory(conn, "memory-old")
    _memory(conn, "memory-new")
    conn.commit()
    conn.execute("BEGIN")
    _insert(
        conn,
        claim_id="claim-old",
        memory_id="memory-old",
        value="Mumbai",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at="2026-03-01T00:00:00+00:00",
    )
    close_claim_interval(
        conn,
        claim_id="claim-old",
        valid_to="2026-02-01T00:00:00+00:00",
        retired_at="2026-03-02T00:00:00+00:00",
        status="superseded",
        superseded_by_claim_id="claim-new",
    )
    _insert(
        conn,
        claim_id="claim-new",
        memory_id="memory-new",
        value="Bangalore",
        valid_from="2026-02-01T00:00:00+00:00",
        recorded_at="2026-03-02T00:00:00+00:00",
    )
    conn.commit()

    current = current_claims(conn, scope_id="scope-a", subject="Asha", predicate="lives in")
    january = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2026-01-15T00:00:00+00:00",
    )
    boundary = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2026-02-01T00:00:00+00:00",
    )
    not_yet_known = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2026-01-15T00:00:00+00:00",
        known_at="2026-02-01T00:00:00+00:00",
    )
    later_known = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2026-01-15T00:00:00+00:00",
        known_at="2026-03-01T12:00:00+00:00",
    )
    history = claim_history(conn, scope_id="scope-a", subject="Asha", predicate="lives in")

    assert [item.value for item in current] == ["Bangalore"]
    assert [item.value for item in january] == ["Mumbai"]
    assert [item.value for item in boundary] == ["Bangalore"]
    assert not_yet_known == []
    assert [item.value for item in later_known] == ["Mumbai"]
    assert [item.claim_id for item in history] == ["claim-old", "claim-new"]
    assert history[0].superseded_by_claim_id == "claim-new"


def test_known_at_preserves_original_finite_valid_to_before_later_retirement():
    conn = _conn()
    _memory(conn, "memory-finite")
    _insert(
        conn,
        claim_id="claim-finite",
        memory_id="memory-finite",
        value="Mumbai",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to="2026-01-31T00:00:00+00:00",
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    retract_claim(
        conn,
        claim_id="claim-finite",
        retired_at="2026-03-01T00:00:00+00:00",
    )

    invalid_by_february = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2026-02-15T00:00:00+00:00",
        known_at="2026-02-01T00:00:00+00:00",
    )
    valid_in_january = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2026-01-15T00:00:00+00:00",
        known_at="2026-02-01T00:00:00+00:00",
    )

    assert invalid_by_february == []
    assert len(valid_in_january) == 1
    assert valid_in_january[0].valid_to == "2026-01-31T00:00:00+00:00"
    assert valid_in_january[0].retired_at is None
    assert valid_in_january[0].status == "current"
    assert valid_in_january[0].metadata["_valid_to_recorded_at"] == (
        "2026-01-01T00:00:00+00:00"
    )
    assert valid_in_january[0].metadata["_valid_to_provenance"] == "recorded"

    for metadata, expected in (
        ("{}", "provenance is missing"),
        ('{"_valid_to_recorded_at":"not-a-timestamp"}', "provenance is invalid"),
        ('{"_valid_to_recorded_at":"2026-03-01T00:00:00"}', "provenance is invalid"),
        ('{"_valid_to_recorded_at":"2026-01-01T00:00:00+00:00"}', "provenance kind is invalid"),
        ('{"_valid_to_recorded_at":"2025-01-01T00:00:00+00:00","_valid_to_provenance":"recorded"}', "provenance is inconsistent"),
        ('{"_valid_to_recorded_at":"2027-01-01T00:00:00+00:00","_valid_to_provenance":"recorded"}', "provenance is inconsistent"),
        ('{"_valid_to_recorded_at":"2026-03-01T00:00:00+00:00","_valid_to_provenance":"recorded"}', "provenance is inconsistent"),
        ('{"_valid_to_recorded_at":"2026-01-01T00:00:00+00:00","_valid_to_provenance":"closure"}', "provenance is inconsistent"),
    ):
        conn.execute(
            "UPDATE fact_claims SET metadata = ? WHERE claim_id = 'claim-finite'",
            (metadata,),
        )
        with pytest.raises(TemporalValidationError, match=expected):
            claims_as_of(
                conn,
                scope_id="scope-a",
                subject="Asha",
                predicate="lives in",
                valid_at="2026-01-15T00:00:00+00:00",
                known_at="2026-02-01T00:00:00+00:00",
            )


def test_open_ended_claim_reconstructs_before_later_retirement():
    conn = _conn()
    _memory(conn, "memory-open")
    _insert(
        conn,
        claim_id="claim-open",
        memory_id="memory-open",
        value="Mumbai",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    retract_claim(
        conn,
        claim_id="claim-open",
        retired_at="2026-03-01T00:00:00+00:00",
    )

    reconstructed = claims_as_of(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        valid_at="2026-02-15T00:00:00+00:00",
        known_at="2026-02-01T00:00:00+00:00",
    )

    assert len(reconstructed) == 1
    assert reconstructed[0].valid_to is None
    assert reconstructed[0].retired_at is None
    assert reconstructed[0].status == "current"
    assert reconstructed[0].superseded_by_claim_id is None
    assert "_valid_to_recorded_at" not in reconstructed[0].metadata


def test_as_of_fails_closed_when_slot_exceeds_scan_budget():
    conn = _conn()
    for index in range(2):
        memory_id = f"memory-scan-{index}"
        _memory(conn, memory_id)
        _insert(
            conn,
            claim_id=f"claim-scan-{index}",
            memory_id=memory_id,
            value=f"Value {index}",
            valid_from="2026-01-01T00:00:00+00:00",
            recorded_at=f"2026-01-0{index + 1}T00:00:00+00:00",
            cardinality="multi",
        )

    with pytest.raises(TemporalValidationError, match="current claim slot"):
        current_claims(
            conn,
            scope_id="scope-a",
            subject="Asha",
            predicate="lives in",
            valid_at="2026-02-01T00:00:00+00:00",
            limit=1,
        )
    with pytest.raises(TemporalValidationError, match="current claim query"):
        current_claims_for_scopes(
            conn,
            scope_ids=["scope-a"],
            valid_at="2026-02-01T00:00:00+00:00",
            limit=1,
        )
    with pytest.raises(TemporalValidationError, match="limit must be an integer"):
        current_claims_for_scopes(
            conn,
            scope_ids=["scope-a"],
            valid_at="2026-02-01T00:00:00+00:00",
            limit=None,  # type: ignore[arg-type]
        )
    with pytest.raises(TemporalValidationError, match="claim history"):
        claim_history(
            conn,
            scope_id="scope-a",
            subject="Asha",
            predicate="lives in",
            limit=1,
        )
    partial_history = claim_history(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
        limit=1,
        reject_overflow=False,
    )
    assert len(partial_history) == 1
    with pytest.raises(TemporalValidationError, match="limit must be an integer"):
        claim_history(
            conn,
            scope_id="scope-a",
            subject="Asha",
            predicate="lives in",
            limit=None,  # type: ignore[arg-type]
        )
    with pytest.raises(TemporalValidationError, match="bounded scan limit"):
        claims_as_of(
            conn,
            scope_id="scope-a",
            subject="Asha",
            predicate="lives in",
            valid_at="2026-02-01T00:00:00+00:00",
            scan_limit=1,
        )


def test_retract_closes_current_adoption_without_hard_deleting_history_or_memory():
    conn = _conn()
    _memory(conn, "memory-1")
    _insert(
        conn,
        claim_id="claim-1",
        memory_id="memory-1",
        value="Bangalore",
        valid_from="2026-01-01T00:00:00+00:00",
    )

    retracted = retract_claim(
        conn,
        claim_id="claim-1",
        retired_at="2026-03-03T00:00:00+00:00",
        valid_to="2026-03-01T00:00:00+00:00",
    )

    assert retracted.status == "retracted"
    assert current_claims(
        conn,
        scope_id="scope-a",
        subject="Asha",
        predicate="lives in",
    ) == []
    history = claim_history(conn, scope_id="scope-a", subject="Asha", predicate="lives in")
    assert [item.status for item in history] == ["retracted"]
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='memory-1'").fetchone()[0] == 1


def test_predecessor_relationships_require_same_scope_and_fact_slot():
    conn = _conn()
    for memory_id, scope_id in (
        ("memory-successor-cross", "scope-a"),
        ("memory-predecessor-cross", "scope-b"),
        ("memory-successor-fact", "scope-a"),
        ("memory-predecessor-fact", "scope-a"),
        ("memory-successor-multi", "scope-a"),
        ("memory-predecessor-one", "scope-a"),
        ("memory-predecessor-two", "scope-a"),
        ("memory-invalid-status", "scope-a"),
    ):
        _memory(conn, memory_id, scope_id)

    _insert(
        conn,
        claim_id="successor-cross",
        memory_id="memory-successor-cross",
        value="successor",
        cardinality="multi",
    )
    _insert(
        conn,
        claim_id="predecessor-cross",
        memory_id="memory-predecessor-cross",
        value="predecessor",
        scope_id="scope-b",
        cardinality="multi",
    )
    with pytest.raises(TemporalValidationError, match="share predecessor scope"):
        close_claim_interval(
            conn,
            claim_id="predecessor-cross",
            retired_at="2026-04-01T00:00:00+00:00",
            superseded_by_claim_id="successor-cross",
        )

    _insert(
        conn,
        claim_id="successor-fact",
        memory_id="memory-successor-fact",
        value="successor",
        predicate="lives in",
        cardinality="multi",
    )
    _insert(
        conn,
        claim_id="predecessor-fact",
        memory_id="memory-predecessor-fact",
        value="predecessor",
        predicate="employer",
        cardinality="multi",
    )
    with pytest.raises(TemporalValidationError, match="share predecessor scope"):
        close_claim_interval(
            conn,
            claim_id="predecessor-fact",
            retired_at="2026-04-01T00:00:00+00:00",
            superseded_by_claim_id="successor-fact",
        )

    assert predecessor_claim_ids_by_successor(
        conn,
        successor_claim_ids=["successor-cross", "successor-fact"],
        scope_ids=["scope-a", "scope-b"],
    ) == {}
    assert get_claim(
        conn,
        "predecessor-cross",
        scope_ids=["scope-b"],
    ).status == "current"  # type: ignore[union-attr]
    assert get_claim(
        conn,
        "predecessor-fact",
        scope_ids=["scope-a"],
    ).status == "current"  # type: ignore[union-attr]

    for claim_id, memory_id in (
        ("successor-multi", "memory-successor-multi"),
        ("predecessor-one", "memory-predecessor-one"),
        ("predecessor-two", "memory-predecessor-two"),
    ):
        _insert(
            conn,
            claim_id=claim_id,
            memory_id=memory_id,
            value=claim_id,
            predicate="hobby",
            cardinality="multi",
        )
    for predecessor_id in ("predecessor-one", "predecessor-two"):
        close_claim_interval(
            conn,
            claim_id=predecessor_id,
            retired_at="2026-04-01T00:00:00+00:00",
            superseded_by_claim_id="successor-multi",
        )
    with pytest.raises(TemporalConflictError, match="multiple predecessors"):
        predecessor_claim_ids_by_successor(
            conn,
            successor_claim_ids=["successor-multi"],
            scope_ids=["scope-a"],
        )

    for invalid_status in ("superseded", "retracted"):
        with pytest.raises(TemporalValidationError, match="status must be one of"):
            _insert(
                conn,
                claim_id=f"invalid-{invalid_status}",
                memory_id="memory-invalid-status",
                value="invalid",
                status=invalid_status,
            )


def test_linked_claim_identity_updates_are_rejected_atomically():
    conn = _conn()
    _memory(conn, "memory-linked-predecessor", "scope-a")
    _memory(conn, "memory-linked-successor", "scope-a")
    _insert(
        conn,
        claim_id="linked-successor",
        memory_id="memory-linked-successor",
        value="new",
        cardinality="multi",
    )
    _insert(
        conn,
        claim_id="linked-predecessor",
        memory_id="memory-linked-predecessor",
        value="old",
        cardinality="multi",
    )
    close_claim_interval(
        conn,
        claim_id="linked-predecessor",
        retired_at="2026-04-01T00:00:00+00:00",
        superseded_by_claim_id="linked-successor",
    )
    conn.commit()

    for claim_id, field, value, expected in (
        ("linked-predecessor", "fact_key", "tampered-fact", "outgoing successor"),
        ("linked-predecessor", "scope_id", "scope-b", "outgoing successor"),
        ("linked-successor", "fact_key", "tampered-fact", "predecessor"),
        ("linked-successor", "scope_id", "scope-b", "predecessor"),
    ):
        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match=expected):
            conn.execute(
                f"UPDATE fact_claims SET {field} = ? WHERE claim_id = ?",
                (value, claim_id),
            )
        conn.rollback()

    predecessor = get_claim(
        conn,
        "linked-predecessor",
        scope_ids=["scope-a"],
    )
    successor = get_claim(
        conn,
        "linked-successor",
        scope_ids=["scope-a"],
    )
    assert predecessor is not None and successor is not None
    assert predecessor.fact_key == successor.fact_key
    assert predecessor.scope_id == successor.scope_id == "scope-a"
    assert predecessor.superseded_by_claim_id == successor.claim_id


def test_deferred_successor_invariants_fail_atomically():
    conn = _conn()
    _memory(conn, "memory-predecessor-deferred", "scope-a")
    _memory(conn, "memory-successor-cross-deferred", "scope-b")
    _insert(
        conn,
        claim_id="predecessor-deferred",
        memory_id="memory-predecessor-deferred",
        value="old",
        cardinality="multi",
    )
    conn.commit()

    conn.execute("BEGIN")
    close_claim_interval(
        conn,
        claim_id="predecessor-deferred",
        retired_at="2026-04-01T00:00:00+00:00",
        superseded_by_claim_id="missing-successor",
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.commit()
    conn.rollback()
    restored = get_claim(
        conn,
        "predecessor-deferred",
        scope_ids=["scope-a"],
    )
    assert restored is not None
    assert restored.status == "current"
    assert restored.superseded_by_claim_id is None

    conn.execute("BEGIN")
    close_claim_interval(
        conn,
        claim_id="predecessor-deferred",
        retired_at="2026-04-01T00:00:00+00:00",
        superseded_by_claim_id="future-cross-successor",
    )
    with pytest.raises(TemporalConflictError, match="successor invariant"):
        _insert(
            conn,
            claim_id="future-cross-successor",
            memory_id="memory-successor-cross-deferred",
            value="new",
            scope_id="scope-b",
            cardinality="multi",
        )
    conn.rollback()
    restored = get_claim(
        conn,
        "predecessor-deferred",
        scope_ids=["scope-a"],
    )
    assert restored is not None
    assert restored.status == "current"
    assert restored.superseded_by_claim_id is None


def test_raw_sql_rejects_inactive_existing_and_deferred_successors():
    conn = _conn()
    for memory_id in (
        "memory-raw-predecessor",
        "memory-raw-retired-successor",
        "memory-raw-deferred-successor",
    ):
        _memory(conn, memory_id, "scope-a")
    _insert(
        conn,
        claim_id="raw-predecessor",
        memory_id="memory-raw-predecessor",
        value="old",
        cardinality="multi",
    )
    _insert(
        conn,
        claim_id="raw-retired-successor",
        memory_id="memory-raw-retired-successor",
        value="new",
        cardinality="multi",
    )
    conn.execute(
        "UPDATE fact_claims SET status = 'retracted', retired_at = ? "
        "WHERE claim_id = 'raw-retired-successor'",
        ("2026-04-01T00:00:00+00:00",),
    )
    conn.commit()

    conn.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError, match="successor invariant"):
        conn.execute(
            "UPDATE fact_claims SET status = 'superseded', retired_at = ?, "
            "superseded_by_claim_id = 'raw-retired-successor' "
            "WHERE claim_id = 'raw-predecessor'",
            ("2026-04-01T00:00:00+00:00",),
        )
    conn.rollback()

    conn.execute("BEGIN")
    conn.execute(
        "UPDATE fact_claims SET status = 'superseded', retired_at = ?, "
        "superseded_by_claim_id = 'raw-deferred-successor' "
        "WHERE claim_id = 'raw-predecessor'",
        ("2026-04-01T00:00:00+00:00",),
    )
    with pytest.raises(sqlite3.IntegrityError, match="successor invariant"):
        conn.execute(
            """
            INSERT INTO fact_claims(
                claim_id, memory_id, scope_id, subject_key, predicate_key,
                fact_key, value, normalized_value, value_fingerprint,
                cardinality, assertion_kind, valid_from, valid_to, recorded_at,
                retired_at, status, confidence, superseded_by_claim_id,
                source_type, source_ref, evidence_hash, metadata
            )
            SELECT
                'raw-deferred-successor', 'memory-raw-deferred-successor',
                scope_id, subject_key, predicate_key, fact_key,
                'new', 'new', 'raw-deferred-fingerprint',
                cardinality, assertion_kind, valid_from, valid_to, recorded_at,
                '2026-04-01T00:00:00+00:00', 'retracted', confidence, NULL,
                source_type, source_ref, evidence_hash, metadata
            FROM fact_claims WHERE claim_id = 'raw-predecessor'
            """
        )
    conn.rollback()

    restored = get_claim(
        conn,
        "raw-predecessor",
        scope_ids=["scope-a"],
    )
    assert restored is not None
    assert restored.status == "current"
    assert restored.retired_at is None
    assert restored.superseded_by_claim_id is None


def test_raw_collection_limits_fail_before_deduplication():
    conn = _conn()
    with pytest.raises(TemporalValidationError, match="scope_ids exceeds 64"):
        get_claim(conn, "claim", scope_ids=["scope-a"] * 65)
    with pytest.raises(TemporalValidationError, match="claim_ids exceeds 1000"):
        get_claims_by_ids(
            conn,
            claim_ids=["claim"] * 1001,
            scope_ids=["scope-a"],
        )
    with pytest.raises(
        TemporalValidationError,
        match="successor_claim_ids exceeds 1000",
    ):
        predecessor_claim_ids_by_successor(
            conn,
            successor_claim_ids=["claim"] * 1001,
            scope_ids=["scope-a"],
        )
    with pytest.raises(TemporalValidationError, match="scope_ids exceeds 64"):
        current_claims_for_scopes(
            conn,
            scope_ids=["scope-a"] * 65,
            valid_at="2026-02-01T00:00:00+00:00",
        )
    with pytest.raises(TemporalValidationError, match="memory_ids exceeds 512"):
        current_claims_for_scopes(
            conn,
            scope_ids=["scope-a"],
            memory_ids=["memory"] * 513,
            valid_at="2026-02-01T00:00:00+00:00",
        )


def test_queries_work_on_read_only_connection_and_do_not_cross_scope(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    writer.execute("PRAGMA foreign_keys=ON")
    ensure_schema(writer)
    _memory(writer, "memory-a", "scope-a")
    _memory(writer, "memory-b", "scope-b")
    _insert(writer, claim_id="claim-a", memory_id="memory-a", value="Bangalore")
    _insert(
        writer,
        claim_id="claim-b",
        memory_id="memory-b",
        value="Mumbai",
        scope_id="scope-b",
    )
    writer.commit()
    writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    try:
        before = readonly.total_changes
        scope_a = current_claims(
            readonly,
            scope_id="scope-a",
            subject="Asha",
            predicate="lives in",
        )
        allowed_claim = get_claim(
            readonly,
            "claim-a",
            scope_ids=["scope-a"],
        )
        out_of_scope = get_claim(
            readonly,
            "claim-b",
            scope_ids=["scope-a"],
        )
        unknown = get_claim(
            readonly,
            "claim-does-not-exist",
            scope_ids=["scope-a"],
        )
        after = readonly.total_changes
    finally:
        readonly.close()

    assert [item.claim_id for item in scope_a] == ["claim-a"]
    assert allowed_claim is not None and allowed_claim.claim_id == "claim-a"
    assert out_of_scope is None
    assert unknown is None
    assert before == after == 0
