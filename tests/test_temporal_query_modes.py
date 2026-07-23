"""Structured current/as-of/history temporal query modes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

import pytest

from scope_recall.fact_identity import canonical_fact_key
from scope_recall.fact_repository import (
    close_claim_interval,
    insert_claim,
    link_claim_evidence,
    predecessor_claim_ids_by_successor,
)
from scope_recall.sql_store import ensure_schema
from scope_recall.temporal_query import (
    TemporalQueryError,
    query_current_fact_views,
    query_fact_views,
    query_temporal_memory_precedence,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def _memory(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    *,
    lifecycle: str = "promoted",
    scope_id: str = "scope-a",
) -> None:
    metadata = json.dumps(
        {"lifecycle": lifecycle, "memory_type": "factual"},
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, ?, 'fact-executor', 'user', ?, ?,
                  '2026-01-01T00:00:00+00:00',
                  '2026-03-02T00:00:00+00:00', ?)
        """,
        (memory_id, scope_id, content, content, metadata),
    )


def _insert(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    memory_id: str,
    value: str,
    valid_from: str,
    recorded_at: str,
    scope_id: str = "scope-a",
    assertion_kind: str = "direct",
    confidence: float = 0.95,
    subject: str = "Asha",
    predicate: str = "lives in",
    cardinality: str = "single",
) -> None:
    insert_claim(
        conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id=scope_id,
        subject=subject,
        predicate=predicate,
        value=value,
        cardinality=cardinality,
        assertion_kind=assertion_kind,
        valid_from=valid_from,
        recorded_at=recorded_at,
        confidence=confidence,
        source_type="user_message",
        source_ref=f"message:{claim_id}",
    )


def _seed_chain(conn: sqlite3.Connection) -> None:
    _memory(conn, "memory-old", "Asha lived in Mumbai.", lifecycle="archived")
    _memory(conn, "memory-new", "Asha now lives in Bangalore.")
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
    link_claim_evidence(
        conn,
        claim_id="claim-old",
        source_type="user_message",
        source_ref="message:old-evidence",
        excerpt="I lived in Mumbai until February.",
        recorded_at="2026-03-01T00:00:01+00:00",
        metadata={"direct": True},
    )
    link_claim_evidence(
        conn,
        claim_id="claim-new",
        source_type="user_message",
        source_ref="message:new-evidence",
        excerpt="I moved to Bangalore in February.",
        recorded_at="2026-03-02T00:00:01+00:00",
    )
    conn.commit()


def test_current_as_of_and_history_return_expected_bitemporal_views():
    conn = _conn()
    _seed_chain(conn)

    current = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="current",
        subject="Asha",
        predicate="lives in",
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    january = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="as_of",
        subject="Asha",
        predicate="lives in",
        at="2026-01-15T00:00:00+00:00",
    )
    history = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="history",
        subject="Asha",
        predicate="lives in",
    )

    assert [view.claim.value for view in current] == ["Bangalore"]
    assert current[0].as_dict()["transition"] == {
        "predecessor_claim_id": "claim-old",
        "superseded_by_claim_id": None,
    }
    assert [view.claim.value for view in january] == ["Mumbai"]
    assert [view.claim.value for view in history] == ["Mumbai", "Bangalore"]
    assert history[0].content == "Asha lived in Mumbai."
    assert history[0].evidence[0]["excerpt"] == "I lived in Mumbai until February."
    assert history[0].as_dict()["transition"] == {
        "predecessor_claim_id": None,
        "superseded_by_claim_id": "claim-new",
    }
    assert history[1].as_dict()["transition"] == {
        "predecessor_claim_id": "claim-old",
        "superseded_by_claim_id": None,
    }
    assert history[0].as_dict()["explanation"] == "superseded by claim-new"
    assert history[1].as_dict()["explanation"] == "replaced claim-old"
    conn.close()


def test_evidence_details_are_limited_per_claim_inside_sql():
    conn = _conn()
    _memory(conn, "memory-many-evidence", "Asha uses a bounded evidence set.")
    _insert(
        conn,
        claim_id="claim-many-evidence",
        memory_id="memory-many-evidence",
        value="bounded",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-01T00:00:00+00:00",
        predicate="evidence policy",
    )
    for index in range(100):
        link_claim_evidence(
            conn,
            claim_id="claim-many-evidence",
            source_type="audit",
            source_ref=f"audit:evidence-{index:03d}",
            excerpt=f"Evidence {index:03d}",
            recorded_at=(
                f"2026-01-02T00:{index // 60:02d}:{index % 60:02d}+00:00"
            ),
        )
    conn.commit()

    views = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="current",
        subject="Asha",
        predicate="evidence policy",
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
        limit=1,
    )

    assert len(views[0].evidence) == 20
    assert views[0].evidence[0]["source_ref"] == "audit:evidence-000"
    assert views[0].evidence[-1]["source_ref"] == "audit:evidence-019"

    for metadata, expected in (
        ("{bad", "metadata is invalid"),
        ("[]", "metadata must be an object"),
        ('"text"', "metadata must be an object"),
        ("null", "metadata must be an object"),
    ):
        conn.execute(
            """
            UPDATE fact_claim_evidence SET metadata = ?
            WHERE source_ref = 'audit:evidence-000'
            """,
            (metadata,),
        )
        with pytest.raises(TemporalQueryError, match=expected):
            query_fact_views(
                conn,
                scope_ids=["scope-a"],
                action="current",
                subject="Asha",
                predicate="evidence policy",
                now=datetime(2026, 4, 1, tzinfo=timezone.utc),
                limit=1,
            )
    conn.close()


def test_as_of_known_at_models_delayed_ingestion_without_scope_leak():
    conn = _conn()
    _seed_chain(conn)
    _memory(
        conn,
        "memory-other",
        "Asha lived in Tokyo in another scope.",
        scope_id="scope-b",
    )
    _insert(
        conn,
        claim_id="claim-other",
        memory_id="memory-other",
        value="Tokyo",
        valid_from="2020-01-01T00:00:00+00:00",
        recorded_at="2020-01-02T00:00:00+00:00",
        scope_id="scope-b",
    )
    conn.commit()

    not_yet_known = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="as_of",
        subject="Asha",
        predicate="lives in",
        at="2026-01-15T00:00:00+00:00",
        known_at="2026-02-01T00:00:00+00:00",
    )
    later_known = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="as_of",
        subject="Asha",
        predicate="lives in",
        at="2026-01-15T00:00:00+00:00",
        known_at="2026-03-01T12:00:00+00:00",
    )

    assert not_yet_known == []
    assert [view.claim.value for view in later_known] == ["Mumbai"]
    assert all(view.claim.scope_id == "scope-a" for view in later_known)
    assert later_known[0].known_at == "2026-03-01T12:00:00+00:00"
    conn.close()


def test_known_at_reconstructs_interval_closure_as_it_was_then_known():
    conn = _conn()
    _seed_chain(conn)
    link_claim_evidence(
        conn,
        claim_id="claim-old",
        source_type="audit",
        source_ref="audit:late",
        excerpt="Evidence learned after the requested known_at cutoff.",
        recorded_at="2026-03-10T00:00:00+00:00",
    )
    conn.commit()

    before_closure_known = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="as_of",
        subject="Asha",
        predicate="lives in",
        at="2026-02-15T00:00:00+00:00",
        known_at="2026-03-01T12:00:00+00:00",
    )
    historical_after_closure = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="as_of",
        subject="Asha",
        predicate="lives in",
        at="2026-01-15T00:00:00+00:00",
        known_at="2026-04-01T00:00:00+00:00",
    )
    successor_after_closure = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="as_of",
        subject="Asha",
        predicate="lives in",
        at="2026-02-15T00:00:00+00:00",
        known_at="2026-04-01T00:00:00+00:00",
    )

    assert [view.claim.value for view in before_closure_known] == ["Mumbai"]
    reconstructed = before_closure_known[0].claim
    assert reconstructed.valid_to is None
    assert reconstructed.retired_at is None
    assert reconstructed.status == "current"
    assert reconstructed.superseded_by_claim_id is None
    assert "_valid_to_recorded_at" not in reconstructed.metadata
    assert "_valid_to_provenance" not in reconstructed.metadata
    assert {item["source_ref"] for item in before_closure_known[0].evidence} == {
        "message:old-evidence"
    }
    assert [view.claim.value for view in historical_after_closure] == ["Mumbai"]
    assert [view.claim.value for view in successor_after_closure] == ["Bangalore"]
    conn.close()


def test_known_at_bounds_predecessor_relationships_by_closure_time():
    conn = _conn()
    _memory(conn, "memory-predecessor", "Asha lived in Mumbai.")
    _memory(conn, "memory-successor", "Asha lives in Bangalore.")
    _insert(
        conn,
        claim_id="claim-predecessor",
        memory_id="memory-predecessor",
        value="Mumbai",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    close_claim_interval(
        conn,
        claim_id="claim-predecessor",
        valid_to="2026-02-01T00:00:00+00:00",
        retired_at="2026-03-02T00:00:00+00:00",
        status="superseded",
        superseded_by_claim_id="claim-successor",
    )
    _insert(
        conn,
        claim_id="claim-successor",
        memory_id="memory-successor",
        value="Bangalore",
        valid_from="2026-02-01T00:00:00+00:00",
        recorded_at="2026-03-01T00:00:00+00:00",
    )
    conn.commit()

    before_link_known = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="as_of",
        subject="Asha",
        predicate="lives in",
        at="2026-02-15T00:00:00+00:00",
        known_at="2026-03-01T12:00:00+00:00",
    )
    after_link_known = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="as_of",
        subject="Asha",
        predicate="lives in",
        at="2026-02-15T00:00:00+00:00",
        known_at="2026-03-03T00:00:00+00:00",
    )

    early_successor = next(
        view for view in before_link_known if view.claim.claim_id == "claim-successor"
    )
    late_successor = next(
        view for view in after_link_known if view.claim.claim_id == "claim-successor"
    )
    assert early_successor.predecessor_claim_id is None
    assert late_successor.predecessor_claim_id == "claim-predecessor"
    conn.close()


def test_predecessor_cutoff_requires_successor_recorded_by_known_at():
    conn = _conn()
    _memory(conn, "memory-predecessor-late-successor", "Old")
    _memory(conn, "memory-late-successor", "New")
    _insert(
        conn,
        claim_id="claim-predecessor-late-successor",
        memory_id="memory-predecessor-late-successor",
        value="Old",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    close_claim_interval(
        conn,
        claim_id="claim-predecessor-late-successor",
        retired_at="2026-03-01T00:00:00+00:00",
        valid_to="2026-02-01T00:00:00+00:00",
        superseded_by_claim_id="claim-late-successor",
    )
    _insert(
        conn,
        claim_id="claim-late-successor",
        memory_id="memory-late-successor",
        value="New",
        valid_from="2026-02-01T00:00:00+00:00",
        recorded_at="2026-03-03T00:00:00+00:00",
    )
    conn.commit()

    before_successor = predecessor_claim_ids_by_successor(
        conn,
        successor_claim_ids=["claim-late-successor"],
        scope_ids=["scope-a"],
        known_at="2026-03-02T00:00:00+00:00",
    )
    after_successor = predecessor_claim_ids_by_successor(
        conn,
        successor_claim_ids=["claim-late-successor"],
        scope_ids=["scope-a"],
        known_at="2026-03-04T00:00:00+00:00",
    )

    assert before_successor == {}
    assert after_successor == {
        "claim-late-successor": "claim-predecessor-late-successor"
    }
    conn.close()


def test_inferred_or_low_confidence_claim_is_marked_uncertain():
    conn = _conn()
    _memory(conn, "memory-inferred", "Asha may live in Pune.")
    _insert(
        conn,
        claim_id="claim-inferred",
        memory_id="memory-inferred",
        value="Pune",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-02T00:00:00+00:00",
        assertion_kind="inferred",
        confidence=0.6,
    )
    conn.commit()

    views = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="history",
        subject="Asha",
        predicate="lives in",
    )

    assert len(views) == 1
    assert views[0].uncertain is True
    assert views[0].as_dict()["uncertain"] is True
    conn.close()


def test_current_limit_is_applied_after_hidden_memory_filtering():
    conn = _conn()
    _memory(conn, "memory-hidden", "Hidden current value", lifecycle="archived")
    _memory(conn, "memory-visible", "Visible current value")
    _insert(
        conn,
        claim_id="claim-hidden-first",
        memory_id="memory-hidden",
        value="Hidden",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-01T00:00:00+00:00",
        cardinality="multi",
    )
    _insert(
        conn,
        claim_id="claim-visible-second",
        memory_id="memory-visible",
        value="Visible",
        valid_from="2026-02-01T00:00:00+00:00",
        recorded_at="2026-02-01T00:00:00+00:00",
        cardinality="multi",
    )
    conn.commit()

    views = query_fact_views(
        conn,
        scope_ids=["scope-a"],
        action="current",
        subject="Asha",
        predicate="lives in",
        at="2026-04-01T00:00:00+00:00",
        limit=1,
    )

    assert [view.claim.value for view in views] == ["Visible"]
    conn.close()


def test_current_views_and_precedence_batch_large_candidate_sets():
    conn = _conn()
    memory_ids: list[str] = []
    for index in range(450):
        memory_id = f"memory-batch-{index:04d}"
        memory_ids.append(memory_id)
        _memory(conn, memory_id, f"Batch fact {index}")
        _insert(
            conn,
            claim_id=f"claim-batch-{index:04d}",
            memory_id=memory_id,
            value=f"Value {index}",
            valid_from="2026-01-01T00:00:00+00:00",
            recorded_at=f"2026-01-02T00:{index // 60:02d}:{index % 60:02d}+00:00",
            predicate=f"batch predicate {index:04d}",
        )
    conn.commit()

    views = query_current_fact_views(
        conn,
        scope_ids=["scope-a"],
        valid_at="2026-04-01T00:00:00+00:00",
        limit=200,
    )
    precedence = query_temporal_memory_precedence(
        conn,
        scope_ids=["scope-a"],
        memory_ids=memory_ids,
        valid_at="2026-04-01T00:00:00+00:00",
    )

    assert len(views) == 200
    assert len(precedence.current_memory_ids) == 450
    assert precedence.suppressed_memory_ids == frozenset()
    conn.close()


def test_current_sql_prefilter_matches_python_unicode_casefold_semantics():
    conn = _conn()
    _memory(conn, "memory-unicode", "STRASSE")
    _insert(
        conn,
        claim_id="claim-unicode",
        memory_id="memory-unicode",
        value="Route",
        valid_from="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-02T00:00:00+00:00",
        predicate="route label",
    )
    conn.commit()

    views = query_current_fact_views(
        conn,
        scope_ids=["scope-a"],
        query="straße",
        valid_at="2026-04-01T00:00:00+00:00",
        limit=1,
    )

    assert [view.memory_id for view in views] == ["memory-unicode"]
    conn.close()


def test_current_query_prefilters_before_the_thousand_claim_scan_cap():
    conn = _conn()
    predicates = [f"scan predicate {index:04d}" for index in range(1002)]
    relevant_predicate = max(
        predicates,
        key=lambda predicate: canonical_fact_key("Asha", predicate),
    )
    relevant_memory_id = ""
    for index, predicate in enumerate(predicates):
        memory_id = f"memory-scan-{index:04d}"
        is_relevant = predicate == relevant_predicate
        if is_relevant:
            relevant_memory_id = memory_id
        content = "needle target fact" if is_relevant else f"unrelated fact {index}"
        _memory(conn, memory_id, content)
        _insert(
            conn,
            claim_id=f"claim-scan-{index:04d}",
            memory_id=memory_id,
            value=f"Value {index}",
            valid_from="2026-01-01T00:00:00+00:00",
            recorded_at="2026-01-02T00:00:00+00:00",
            predicate=predicate,
        )
    conn.commit()

    views = query_current_fact_views(
        conn,
        scope_ids=["scope-a"],
        query="needle",
        valid_at="2026-04-01T00:00:00+00:00",
        limit=1,
    )

    assert [view.memory_id for view in views] == [relevant_memory_id]
    conn.close()


def test_action_specific_temporal_arguments_fail_closed():
    conn = _conn()
    with pytest.raises(TemporalQueryError, match="required"):
        query_fact_views(
            conn,
            scope_ids=["scope-a"],
            action="as_of",
            subject="Asha",
            predicate="lives in",
        )
    with pytest.raises(TemporalQueryError, match="not used"):
        query_fact_views(
            conn,
            scope_ids=["scope-a"],
            action="history",
            subject="Asha",
            predicate="lives in",
            at="2026-01-01T00:00:00+00:00",
        )
    conn.close()
