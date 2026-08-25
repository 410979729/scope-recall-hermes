"""Optional PGVector vector companion backend.

PGVector is an optional backend for operators who already run PostgreSQL with the
pgvector extension. SQLite memory rows remain the source of truth; this store is
only a rebuildable vector companion.
"""

from __future__ import annotations

import importlib
import os
import re
from collections import Counter
from typing import Any, Iterable, Mapping

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_identifier(value: str) -> str:
    name = str(value or "scope_recall_vectors").strip()
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError("pgvector table_name must be a simple SQL identifier")
    return '"' + name.replace('"', '""') + '"'


def _record_to_dict(record: Mapping[str, Any] | Any) -> dict[str, Any]:
    try:
        return dict(record)
    except TypeError:
        return dict(vars(record))


class PGVectorStore:
    """PostgreSQL/pgvector companion store.

    The store intentionally requires an explicit DSN environment variable so
    package installs do not attempt network/database connections by default.
    """

    def __init__(
        self,
        *,
        dsn_env: str = "SCOPE_RECALL_PGVECTOR_DSN",
        table_name: str = "scope_recall_vectors",
        dimensions: int,
        metric: str = "cosine",
        connect_timeout_seconds: int = 10,
        statement_timeout_ms: int = 30_000,
        lock_timeout_ms: int = 5_000,
    ) -> None:
        self._dsn_env = str(dsn_env or "SCOPE_RECALL_PGVECTOR_DSN")
        self._table_name = str(table_name or "scope_recall_vectors")
        self._quoted_table = _quote_identifier(self._table_name)
        self._dimensions = int(dimensions)
        self._metric = str(metric or "cosine").strip().lower()
        self._connect_timeout_seconds = max(
            1, min(int(connect_timeout_seconds), 300)
        )
        self._statement_timeout_ms = max(
            100, min(int(statement_timeout_ms), 600_000)
        )
        self._lock_timeout_ms = max(100, min(int(lock_timeout_ms), 600_000))
        self._conn: Any = None

    @property
    def backend(self) -> str:
        return "pgvector"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def id_lookup_indexed(self) -> bool:
        """Companion ``id`` is the PostgreSQL primary key."""

        return True

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def dsn_env(self) -> str:
        return self._dsn_env

    def is_available(self) -> bool:
        if not os.environ.get(self._dsn_env):
            return False
        try:
            importlib.import_module("psycopg")
            importlib.import_module("pgvector.psycopg")
        except Exception:
            return False
        return True

    def open(self) -> None:
        dsn = os.environ.get(self._dsn_env)
        if not dsn:
            raise RuntimeError(f"pgvector DSN environment variable is not set: {self._dsn_env}")
        psycopg = importlib.import_module("psycopg")
        pgvector_psycopg = importlib.import_module("pgvector.psycopg")
        self._conn = psycopg.connect(
            dsn,
            connect_timeout=self._connect_timeout_seconds,
        )
        try:
            register_vector = getattr(pgvector_psycopg, "register_vector", None)
            if callable(register_vector):
                register_vector(self._conn)
            self._configure_session_timeouts()
            self._ensure_schema()
        except Exception:
            self.close()
            raise

    def open_existing(self) -> None:
        """Open an existing PGVector table in a read-only transaction."""

        self._open_existing(read_only=True)

    def open_existing_for_update(self) -> None:
        """Open an existing PGVector table for writes without creating schema."""

        self._open_existing(read_only=False)

    def _open_existing(self, *, read_only: bool) -> None:
        dsn = os.environ.get(self._dsn_env)
        if not dsn:
            raise RuntimeError(f"pgvector DSN environment variable is not set: {self._dsn_env}")
        psycopg = importlib.import_module("psycopg")
        pgvector_psycopg = importlib.import_module("pgvector.psycopg")
        self._conn = psycopg.connect(
            dsn,
            connect_timeout=self._connect_timeout_seconds,
        )
        register_vector = getattr(pgvector_psycopg, "register_vector", None)
        if callable(register_vector):
            register_vector(self._conn)
        try:
            self._configure_session_timeouts()
            with self._conn.cursor() as cur:
                if read_only:
                    cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SELECT to_regclass(%s)", (self._table_name,))
                if cur.fetchone()[0] is None:
                    raise RuntimeError(f"PGVector physical storage is missing table {self._table_name!r}")
                cur.execute(
                    """
                    SELECT format_type(attribute.atttypid, attribute.atttypmod)
                    FROM pg_attribute AS attribute
                    WHERE attribute.attrelid = to_regclass(%s)
                      AND attribute.attname = 'vector'
                      AND NOT attribute.attisdropped
                    """,
                    (self._table_name,),
                )
                row = cur.fetchone()
                vector_type = str(row[0] or "") if row else ""
                match = re.fullmatch(r"vector\((\d+)\)", vector_type)
                if match is None or int(match.group(1)) != self._dimensions:
                    raise RuntimeError(
                        "PGVector physical identity mismatch: "
                        f"vector_type={vector_type!r}, expected_dimensions={self._dimensions}"
                    )
            if not read_only:
                self._conn.commit()
        except Exception:
            self.close()
            raise

    def _require_conn(self) -> Any:
        if self._conn is None:
            raise RuntimeError("pgvector store is not open")
        return self._conn

    def _configure_session_timeouts(self) -> None:
        """Bound every PGVector session before schema or companion I/O."""

        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{self._statement_timeout_ms}ms",),
            )
            cur.execute(
                "SELECT set_config('lock_timeout', %s, false)",
                (f"{self._lock_timeout_ms}ms",),
            )
        conn.commit()

    def _ensure_schema(self) -> None:
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._quoted_table} (
                    id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    target TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    vector vector({self._dimensions}) NOT NULL
                )
                """
            )
            cur.execute(f"CREATE INDEX IF NOT EXISTS {self._table_name}_scope_idx ON {self._quoted_table}(scope_id)")
        conn.commit()

    def upsert(self, record: Mapping[str, Any] | Any) -> None:
        self.upsert_records([_record_to_dict(record)])

    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None:
        payload = [dict(row) for row in rows]
        if not payload:
            return
        conn = self._require_conn()
        with conn.cursor() as cur:
            for row in payload:
                memory_id = str(row.get("id") or "")
                vector = [float(item) for item in (row.get("vector") or [])]
                if not memory_id:
                    continue
                if len(vector) != self._dimensions:
                    raise ValueError(f"vector dimension mismatch: expected {self._dimensions}, got {len(vector)}")
                cur.execute(
                    f"""
                    INSERT INTO {self._quoted_table}(id, scope_id, source, target, content, summary, updated_at, vector)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        scope_id = excluded.scope_id,
                        source = excluded.source,
                        target = excluded.target,
                        content = excluded.content,
                        summary = excluded.summary,
                        updated_at = excluded.updated_at,
                        vector = excluded.vector
                    """,
                    (
                        memory_id,
                        str(row.get("scope_id") or ""),
                        str(row.get("source") or ""),
                        str(row.get("target") or ""),
                        str(row.get("content") or ""),
                        str(row.get("summary") or ""),
                        str(row.get("updated_at") or ""),
                        vector,
                    ),
                )
        conn.commit()

    def delete_by_ids(self, ids: list[str]) -> None:
        ids = [str(item) for item in ids if str(item)]
        if not ids:
            return
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self._quoted_table} WHERE id = ANY(%s)", (ids,))
        conn.commit()

    def contains_id(self, memory_id: str) -> bool:
        """Indexed primary-key existence probe; never counts or lists the corpus."""

        resolved = str(memory_id or "")
        if not resolved:
            return False
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {self._quoted_table} WHERE id = %s LIMIT 1",
                (resolved,),
            )
            row = cur.fetchone()
        return row is not None

    def delete(self, ids: list[str]) -> int:
        existing = [str(item) for item in ids if str(item) and self.contains_id(str(item))]
        if not existing:
            return 0
        self.delete_by_ids(existing)
        return len(existing)

    def list_ids(self) -> list[str]:
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {self._quoted_table} ORDER BY id")
            return [str(row[0]) for row in cur.fetchall()]

    def list_records(self) -> dict[str, dict[str, Any]]:
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, scope_id, source, target, content, summary, updated_at, vector FROM {self._quoted_table}"
            )
            rows = cur.fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            output[str(row[0])] = {
                "id": str(row[0]),
                "scope_id": str(row[1]),
                "source": str(row[2]),
                "target": str(row[3]),
                "content": str(row[4]),
                "summary": str(row[5]),
                "updated_at": str(row[6]),
                "vector": [float(value) for value in row[7]],
            }
        return output

    def sample_metadata(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        """Return a bounded metadata page without selecting the vector column."""

        from .vector_store import clamp_vector_sample_limit

        bounded = clamp_vector_sample_limit(limit)
        start = max(0, int(offset or 0))
        if bounded <= 0:
            return []
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, scope_id, source, target, content, summary, updated_at
                FROM {self._quoted_table}
                ORDER BY id
                LIMIT %s OFFSET %s
                """,
                (bounded, start),
            )
            rows = cur.fetchall()
        return [
            {
                "id": str(row[0]),
                "scope_id": str(row[1] or ""),
                "source": str(row[2] or ""),
                "target": str(row[3] or ""),
                "content": str(row[4] or ""),
                "summary": str(row[5] or ""),
                "updated_at": str(row[6] or ""),
            }
            for row in rows
        ]

    def audit_counts(self) -> dict[str, int]:
        ids = self.list_ids()
        counts = Counter(ids)
        return {
            "physical_rows": len(ids),
            "unique_ids": len(counts),
            "duplicate_rows": sum(count - 1 for count in counts.values() if count > 1),
            "duplicate_ids": sum(1 for count in counts.values() if count > 1),
        }

    def repair_records(self, desired_records: dict[str, dict[str, Any]]) -> int:
        """Prune rows absent from, or stale against, desired SQLite truth.

        This mirrors the cleanup-only SQLite companion contract. Missing or
        changed rows are rebuilt by the explicit embedding/sync path; repair
        must not fabricate vectors from metadata-only desired records.
        """

        existing = self.list_records()
        keep_ids = {
            str(memory_id)
            for memory_id, desired in desired_records.items()
            if str(memory_id) in existing
            and str(existing[str(memory_id)].get("updated_at") or "")
            == str(desired.get("updated_at") or "")
        }
        remove_ids = sorted(set(existing) - keep_ids)
        if remove_ids:
            self.delete_by_ids(remove_ids)
        return len(keep_ids)

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]:
        query_vector = [float(item) for item in vector]
        if len(query_vector) != self._dimensions:
            raise ValueError(f"vector dimension mismatch: expected {self._dimensions}, got {len(query_vector)}")
        operator = "<=>" if self._metric == "cosine" else "<->"
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, scope_id, source, target, content, summary, updated_at, vector {operator} %s AS distance
                FROM {self._quoted_table}
                WHERE scope_id = %s
                ORDER BY vector {operator} %s
                LIMIT %s
                """,
                (query_vector, str(scope_id), query_vector, max(0, int(limit))),
            )
            rows = cur.fetchall()
        return [
            {
                "id": str(row[0]),
                "scope_id": str(row[1]),
                "source": str(row[2]),
                "target": str(row[3]),
                "content": str(row[4]),
                "summary": str(row[5]),
                "updated_at": str(row[6]),
                "_distance": float(row[7]),
            }
            for row in rows
        ]

    def count_rows(self) -> int:
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {self._quoted_table}")
            return int(cur.fetchone()[0])

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
