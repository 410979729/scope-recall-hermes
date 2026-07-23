"""Read-only current temporal-fact query service contracts."""

from __future__ import annotations

import sqlite3
from typing import cast

import pytest

from scope_recall.config import DEFAULT_CONFIG
from scope_recall.fact_repository import (
    current_claims_for_scopes,
    insert_claim,
    link_claim_evidence,
)
from scope_recall.sql_store import ensure_schema
from scope_recall.temporal_query import (
    TemporalQueryError,
    normalize_query_instant,
    query_current_fact_views,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


class _RecordingConnection(sqlite3.Connection):
    fact_select: tuple[str, tuple[object, ...]] | None = None

    def execute(self, sql, parameters=(), /):  # type: ignore[no-untyped-def]
        if sql.lstrip().startswith("SELECT") and "FROM fact_claims" in sql:
            self.fact_select = (sql, tuple(parameters))  # type: ignore[arg-type]
        return super().execute(sql, parameters)


def _recording_conn() -> _RecordingConnection:
    conn = sqlite3.connect(":memory:", factory=_RecordingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def _memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    scope_id: str = "scope-a",
    content: str,
) -> None:
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary,
            created_at, updated_at, metadata
        ) VALUES (?, ?, 'fact-executor', 'user', ?, ?,
                  '2026-07-14T09:00:00+00:00',
                  '2026-07-14T09:00:00+00:00',
                  '{"lifecycle":"promoted","memory_type":"factual"}')
        """,
        (memory_id, scope_id, content, content),
    )


def _claim(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    memory_id: str,
    predicate: str,
    value: str,
    scope_id: str = "scope-a",
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> None:
    insert_claim(
        conn,
        claim_id=claim_id,
        memory_id=memory_id,
        scope_id=scope_id,
        subject="Joy",
        predicate=predicate,
        value=value,
        cardinality="single",
        assertion_kind="direct",
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at="2026-07-14T09:00:00+00:00",
        confidence=0.97,
        source_type="user_message",
        source_ref=f"message:{claim_id}",
    )


def _seed_boundaries(conn: sqlite3.Connection) -> None:
    _memory(
        conn,
        "memory-city",
        content="Joy currently lives in Tokyo.",
    )
    _claim(
        conn,
        claim_id="claim-city",
        memory_id="memory-city",
        predicate="lives in",
        value="Tokyo",
        valid_from="2026-07-14T10:00:00+00:00",
        valid_to="2026-07-14T11:00:00+00:00",
    )
    _memory(
        conn,
        "memory-employer",
        content="Joy works at Northstar Labs.",
    )
    _claim(
        conn,
        claim_id="claim-employer",
        memory_id="memory-employer",
        predicate="works at",
        value="Northstar Labs",
        valid_from="2026-07-14T12:00:00+00:00",
    )
    _memory(
        conn,
        "memory-other-scope",
        scope_id="scope-b",
        content="Joy currently lives in Mumbai in another scope.",
    )
    _claim(
        conn,
        claim_id="claim-other-scope",
        memory_id="memory-other-scope",
        predicate="lives in",
        value="Mumbai",
        scope_id="scope-b",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    conn.commit()


def test_memory_filtered_current_query_forces_memory_index() -> None:
    conn = _recording_conn()
    _memory(conn, "memory-city", content="Joy currently lives in Tokyo.")
    _claim(
        conn,
        claim_id="claim-city",
        memory_id="memory-city",
        predicate="lives in",
        value="Tokyo",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    _memory(conn, "memory-employer", content="Joy works at Northstar Labs.")
    _claim(
        conn,
        claim_id="claim-employer",
        memory_id="memory-employer",
        predicate="works at",
        value="Northstar Labs",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    conn.commit()
    conn.fact_select = None

    claims = current_claims_for_scopes(
        conn,
        scope_ids=["scope-a"],
        valid_at="2026-07-15T00:00:00+00:00",
        memory_ids=["memory-city"],
        limit=10,
    )

    assert [claim.claim_id for claim in claims] == ["claim-city"]
    assert conn.fact_select is not None
    sql, params = cast(tuple[str, tuple[object, ...]], conn.fact_select)
    assert "INDEXED BY idx_fact_claims_memory" in sql
    plan = sqlite3.Connection.execute(
        conn,
        f"EXPLAIN QUERY PLAN {sql}",
        params,
    ).fetchall()
    assert any("idx_fact_claims_memory" in str(row[3]) for row in plan)
    conn.close()


def test_repository_current_scope_query_uses_inclusive_start_exclusive_end():
    conn = _conn()
    _seed_boundaries(conn)

    at_start = current_claims_for_scopes(
        conn,
        scope_ids=["scope-a"],
        valid_at="2026-07-14T10:00:00+00:00",
    )
    before_end = current_claims_for_scopes(
        conn,
        scope_ids=["scope-a"],
        valid_at="2026-07-14T10:59:59+00:00",
    )
    at_end = current_claims_for_scopes(
        conn,
        scope_ids=["scope-a"],
        valid_at="2026-07-14T11:00:00+00:00",
    )

    assert [claim.claim_id for claim in at_start] == ["claim-city"]
    assert [claim.claim_id for claim in before_end] == ["claim-city"]
    assert at_end == []
    conn.close()


def test_query_instant_normalizes_offsets_and_configured_local_timezone():
    explicit = normalize_query_instant(
        "2026-07-14T18:30:00+08:00",
        timezone_name="UTC",
    )
    configured_local = normalize_query_instant(
        "2026-07-14T18:30:00",
        timezone_name="Asia/Shanghai",
    )

    assert explicit == "2026-07-14T10:30:00+00:00"
    assert configured_local == explicit
    with pytest.raises(TemporalQueryError, match="timezone"):
        normalize_query_instant(
            "2026-07-14T18:30:00",
            timezone_name="Mars/Olympus",
        )


def test_temporal_query_gate_defaults_off_and_dst_ambiguity_fails_closed():
    assert DEFAULT_CONFIG["temporal_queries"] == {
        "enabled": False,
        "timezone": "UTC",
        "current_limit": 50,
    }
    with pytest.raises(TemporalQueryError, match="offset"):
        normalize_query_instant(
            "2026-11-01T01:30:00",
            timezone_name="America/New_York",
        )
    with pytest.raises(TemporalQueryError, match="offset"):
        normalize_query_instant(
            "2026-03-08T02:30:00",
            timezone_name="America/New_York",
        )


def test_current_view_joins_memory_evidence_and_filters_query_and_scope():
    conn = _conn()
    _seed_boundaries(conn)
    link_claim_evidence(
        conn,
        claim_id="claim-city",
        source_type="user_message",
        source_ref="message:city",
        excerpt="I now live in Tokyo.",
        recorded_at="2026-07-14T09:00:01+00:00",
    )
    conn.commit()
    before = conn.total_changes

    views = query_current_fact_views(
        conn,
        scope_ids=["scope-a"],
        query="Where does Joy live now?",
        valid_at="2026-07-14T18:30:00",
        timezone_name="Asia/Shanghai",
        limit=10,
    )

    assert conn.total_changes == before
    assert len(views) == 1
    view = views[0]
    assert view.claim_id == "claim-city"
    assert view.memory_id == "memory-city"
    assert view.value == "Tokyo"
    assert view.evidence_count == 1
    assert view.target == "user"
    assert view.semantic_at == "2026-07-14T10:30:00+00:00"
    assert view.score > 0.0
    assert view.as_dict()["status"] == "current"
    conn.close()


def test_current_view_excludes_future_claim_and_inaccessible_scope():
    conn = _conn()
    _seed_boundaries(conn)

    views = query_current_fact_views(
        conn,
        scope_ids=["scope-a"],
        query="Joy",
        valid_at="2026-07-14T10:30:00+00:00",
        limit=20,
    )

    assert [view.claim_id for view in views] == ["claim-city"]
    assert all(view.scope_id == "scope-a" for view in views)
    conn.close()


def test_current_view_is_safe_on_read_only_connection(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    writer = sqlite3.connect(db_path)
    writer.row_factory = sqlite3.Row
    writer.execute("PRAGMA foreign_keys=ON")
    ensure_schema(writer)
    _memory(writer, "memory-city", content="Joy currently lives in Tokyo.")
    _claim(
        writer,
        claim_id="claim-city",
        memory_id="memory-city",
        predicate="lives in",
        value="Tokyo",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    writer.commit()
    writer.close()

    readonly = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    readonly.execute("PRAGMA query_only=ON")
    before = readonly.total_changes
    views = query_current_fact_views(
        readonly,
        scope_ids=["scope-a"],
        query="Tokyo",
        valid_at="2026-07-14T10:30:00+00:00",
    )

    assert [view.value for view in views] == ["Tokyo"]
    assert readonly.total_changes == before == 0
    readonly.close()
