from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from _scope_recall_public_memory_port import attach_public_truth_ports
from scope_recall.experience_store import create_playbook, experience_stats
from scope_recall.fact_repository import fact_ownership_for_memories
from scope_recall.lifecycle_service import hard_delete_memories
from scope_recall.memory_ops import archive_memories, merge_memories
from scope_recall.schemas import (
    MAX_MEMORY_ID_LENGTH,
    MAX_MEMORY_IDS_PER_REQUEST,
    SCOPE_RECALL_FORGET_SCHEMA,
    SCOPE_RECALL_MEMORY_SCHEMA,
    SCOPE_RECALL_MERGE_SCHEMA,
)
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.sqlite_params import chunked_sql_parameters
from scope_recall.tooling import ScopeRecallToolService


def _insert_memories(conn: sqlite3.Connection, count: int) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        memory_id = f"memory-{index:04d}"
        store_row(
            conn,
            memory_id=memory_id,
            scope_id="scope-a",
            platform="test",
            user_id="user-a",
            chat_id="chat-a",
            thread_id="",
            gateway_session_key="gateway-a",
            agent_identity="agent-a",
            agent_workspace="workspace-a",
            session_id="session-a",
            source="tool",
            target="memory",
            content=f"Durable memory row {index} for SQLite parameter-boundary testing.",
            allow_duplicate=True,
            commit=False,
            enqueue_vector_intent=False,
        )
        ids.append(memory_id)
    conn.commit()
    return ids


def _memory_connection(tmp_path: Path, name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / name)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def test_forget_schemas_bound_id_count_and_length() -> None:
    compact = SCOPE_RECALL_MEMORY_SCHEMA["parameters"]["properties"]
    legacy_forget = SCOPE_RECALL_FORGET_SCHEMA["parameters"]["properties"]
    legacy_merge = SCOPE_RECALL_MERGE_SCHEMA["parameters"]["properties"]

    for properties in (compact, legacy_forget):
        assert properties["id"]["maxLength"] == MAX_MEMORY_ID_LENGTH
        assert properties["ids"]["maxItems"] == MAX_MEMORY_IDS_PER_REQUEST
        assert properties["ids"]["items"]["maxLength"] == MAX_MEMORY_ID_LENGTH

    assert compact["source_ids"]["maxItems"] == MAX_MEMORY_IDS_PER_REQUEST
    assert compact["source_ids"]["items"]["maxLength"] == MAX_MEMORY_ID_LENGTH
    assert compact["target_id"]["maxLength"] == MAX_MEMORY_ID_LENGTH

    assert legacy_merge["source_ids"]["maxItems"] == MAX_MEMORY_IDS_PER_REQUEST
    assert legacy_merge["source_ids"]["items"]["maxLength"] == MAX_MEMORY_ID_LENGTH
    assert legacy_merge["target_id"]["maxLength"] == MAX_MEMORY_ID_LENGTH

def test_chunked_sql_parameters_honors_runtime_variable_budget() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 7)
        chunks = list(
            chunked_sql_parameters(
                conn,
                list(range(10)),
                reserved=1,
                variables_per_item=2,
            )
        )
    finally:
        conn.close()

    assert chunks == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]


def test_runtime_bypass_returns_structured_error_for_oversized_ids() -> None:
    class Provider:
        @staticmethod
        def _clean_text(value: str) -> str:
            return value

    service = ScopeRecallToolService(Provider())
    payload = json.loads(
        service._handle_forget(
            {"ids": [f"memory-{index}" for index in range(MAX_MEMORY_IDS_PER_REQUEST + 1)]}
        )
    )

    assert payload["invalid_arguments"] is True
    assert payload["field"] == "ids"
    assert payload["constraint"] == f"maxItems={MAX_MEMORY_IDS_PER_REQUEST}"


def test_runtime_bypass_returns_structured_error_for_long_id() -> None:
    class Provider:
        @staticmethod
        def _clean_text(value: str) -> str:
            return value

    service = ScopeRecallToolService(Provider())
    payload = json.loads(
        service._handle_forget({"id": "x" * (MAX_MEMORY_ID_LENGTH + 1)})
    )

    assert payload["invalid_arguments"] is True
    assert payload["field"] == "id"
    assert payload["constraint"] == f"maxLength={MAX_MEMORY_ID_LENGTH}"


def test_merge_runtime_bypass_rejects_oversized_source_ids() -> None:
    class Provider:
        @staticmethod
        def _clean_text(value: str) -> str:
            return value

    service = ScopeRecallToolService(Provider())
    payload = json.loads(
        service._handle_merge(
            {
                "target_id": "memory-target",
                "source_ids": [
                    f"memory-{index}" for index in range(MAX_MEMORY_IDS_PER_REQUEST + 1)
                ],
            }
        )
    )

    assert payload["invalid_arguments"] is True
    assert payload["field"] == "source_ids"
    assert payload["constraint"] == f"maxItems={MAX_MEMORY_IDS_PER_REQUEST}"


def test_fact_ownership_chunks_more_ids_than_connection_limit(tmp_path: Path) -> None:
    conn = _memory_connection(tmp_path, "facts.sqlite3")
    try:
        ids = _insert_memories(conn, 75)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 40)

        assert fact_ownership_for_memories(conn, ids) == {}
    finally:
        conn.close()


def test_soft_archive_chunks_selection_and_fact_guard(tmp_path: Path) -> None:
    conn = _memory_connection(tmp_path, "archive.sqlite3")
    ids = _insert_memories(conn, 75)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 40)

    class Provider:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._conn = connection
            self._lock = threading.RLock()
            self._writable_scope_ids = ["scope-a"]
            self._accessible_scope_ids = ["scope-a"]
            self._config = {"vector": {"enabled": False}}
            self._vector_enabled = False
            self._vector_ready = False
            self._vector_status = "disabled"
            self._vector_message = ""
            attach_public_truth_ports(self)

        def _require_conn(self) -> sqlite3.Connection:
            return self._conn

        def _rollback_conn_after_error(self, _context: str) -> None:
            self._conn.rollback()

    try:
        result = archive_memories(
            Provider(conn),
            ids,
            reason="parameter boundary test",
            actor="test",
        )

        assert result["archived"] == 75
        assert result["ids"] == ids
        archived = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE json_extract(metadata, '$.lifecycle') = 'archived'"
        ).fetchone()[0]
        assert archived == 75
    finally:
        conn.close()


def test_hard_delete_chunks_inside_one_atomic_transaction(tmp_path: Path) -> None:
    conn = _memory_connection(tmp_path, "delete.sqlite3")
    ids = _insert_memories(conn, 75)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 40)
    try:
        result = hard_delete_memories(
            conn,
            memory_ids=ids,
            scope_ids=["scope-a"],
            require_vector_delete=False,
            actor="test",
            reason="parameter boundary test",
        )

        assert result["deleted"] == 75
        assert result["ids"] == ids
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE action = 'hard_delete'"
        ).fetchone()[0]
        assert audit_count == 75
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_merge_chunks_source_lookup_and_atomic_delete(tmp_path: Path) -> None:
    conn = _memory_connection(tmp_path, "merge.sqlite3")
    all_ids = _insert_memories(conn, 76)
    target_id, source_ids = all_ids[0], all_ids[1:]
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 40)

    class Provider:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._conn = connection
            self._lock = threading.RLock()
            self._scope_id = "scope-a"
            self._shared_scope_id = "scope-a"
            self._shared_pool_scope_id = "scope-pool"
            self._writable_scope_ids = ["scope-a"]
            self._accessible_scope_ids = ["scope-a"]
            self._config = {
                "relation_extraction_enabled": False,
                "vector": {"enabled": False},
            }
            self._vector_enabled = False
            self._vector_ready = False
            self._vector_status = "disabled"
            self._vector_message = ""
            attach_public_truth_ports(self)

        def _require_conn(self) -> sqlite3.Connection:
            return self._conn

        @staticmethod
        def _clean_text(value: str) -> str:
            return value.strip()

        def _rollback_conn_after_error(self, _context: str) -> None:
            self._conn.rollback()

    try:
        result = merge_memories(Provider(conn), target_id, source_ids)

        assert result["merged"] is True
        assert result["deleted"] == 75
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        remaining = conn.execute("SELECT id FROM memories").fetchone()[0]
        assert remaining == target_id
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_hard_delete_late_chunk_failure_rolls_back_every_chunk(tmp_path: Path) -> None:
    conn = _memory_connection(tmp_path, "delete-rollback.sqlite3")
    ids = _insert_memories(conn, 75)
    conn.execute(
        """
        CREATE TRIGGER fail_late_memory_delete
        BEFORE DELETE ON memories
        WHEN OLD.id = 'memory-0060'
        BEGIN
            SELECT RAISE(ABORT, 'forced late chunk failure');
        END
        """
    )
    conn.commit()
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 40)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="forced late chunk failure"):
            hard_delete_memories(
                conn,
                memory_ids=ids,
                scope_ids=["scope-a"],
                require_vector_delete=False,
                actor="test",
                reason="forced rollback test",
            )

        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 75
        assert conn.execute(
            "SELECT COUNT(*) FROM governance_audit_events WHERE action = 'hard_delete'"
        ).fetchone()[0] == 0
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_experience_stats_aggregates_without_expanding_accessible_playbook_ids(
    tmp_path: Path,
) -> None:
    conn = _memory_connection(tmp_path, "experience-stats.sqlite3")
    payload = {
        "schema_version": "procedural_playbook.v1",
        "task_class": "parameter_limit_probe",
        "title": "Parameter limit probe",
        "trigger": "Exercise statistics with many accessible playbooks.",
        "goal": "Aggregate outcomes without a host-parameter overflow.",
        "preconditions": [
            {"id": "p1", "check": "Create playbooks.", "evidence_required": "rows"}
        ],
        "steps": [
            {
                "number": 1,
                "capability_class": "read_only",
                "action": "Read aggregate statistics.",
                "evidence_required": "counts",
            }
        ],
        "pitfalls": [
            {
                "signal": "too many SQL variables",
                "mistake": "Expand every playbook ID into an IN clause.",
                "correction": "Aggregate with a scoped relational query.",
            }
        ],
        "verification": ["Outcome counts match accessible runs."],
        "cleanup": ["Close the test connection."],
        "reuse_policy": {"default_decision": "guided_reuse"},
    }
    try:
        for index in range(50):
            create_playbook(
                conn,
                playbook_id=f"playbook-{index:03d}",
                scope_id="scope-a",
                payload=payload,
            )
        conn.executemany(
            """
            INSERT INTO experience_runs(
                id, playbook_id, scope_id, decision, outcome, started_at
            ) VALUES (?, ?, ?, 'guided_reuse', ?, '2026-08-05T00:00:00Z')
            """,
            [
                (
                    f"run-{index:03d}",
                    f"playbook-{index:03d}",
                    "scope-a",
                    "success" if index % 2 == 0 else "failed",
                )
                for index in range(50)
            ]
            + [("run-inaccessible", "playbook-000", "scope-b", "failed")],
        )
        conn.execute(
            "UPDATE procedural_playbooks SET status = 'api_key=secret-one' "
            "WHERE id = 'playbook-000'"
        )
        conn.execute(
            "UPDATE procedural_playbooks SET status = 'token=secret-two' "
            "WHERE id = 'playbook-001'"
        )
        conn.execute(
            "UPDATE experience_runs SET outcome = 'api_key=secret-one' "
            "WHERE id = 'run-000'"
        )
        conn.execute(
            "UPDATE experience_runs SET outcome = 'token=secret-two' "
            "WHERE id = 'run-001'"
        )
        conn.commit()
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 40)

        stats = experience_stats(conn, accessible_scope_ids=["scope-a"])

        assert stats["playbooks"] == {
            "total": 50,
            "by_status": {"[REDACTED_SECRET]": 2, "candidate": 48},
        }
        assert stats["runs"] == {
            "total": 50,
            "by_outcome": {"[REDACTED_SECRET]": 2, "failed": 24, "success": 24},
        }
    finally:
        conn.close()
