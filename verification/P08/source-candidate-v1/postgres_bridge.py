"""Optional PostgreSQL adapter for the external shared-memory bridge.

This adapter publishes v1 external bridge payloads into a PostgreSQL table. It is
an optional integration: importing this module does not require psycopg, and a
DSN is only required when opening the bridge.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from .external_bridge import EXPORT_SCHEMA_VERSION

DEFAULT_POSTGRES_BRIDGE_DSN_ENV = "SCOPE_RECALL_POSTGRES_BRIDGE_DSN"
DEFAULT_POSTGRES_BRIDGE_TABLE = "scope_recall_shared_memories"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_identifier(value: str) -> str:
    name = str(value or DEFAULT_POSTGRES_BRIDGE_TABLE).strip()
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError("postgres bridge table_name must be a simple SQL identifier")
    return '"' + name.replace('"', '""') + '"'


def build_postgres_schema_sql(*, table_name: str = DEFAULT_POSTGRES_BRIDGE_TABLE) -> str:
    """Return the PostgreSQL schema used by the shared-memory bridge."""
    table = _quote_identifier(table_name)
    index_prefix = str(table_name or DEFAULT_POSTGRES_BRIDGE_TABLE).strip()
    if not _IDENTIFIER_RE.fullmatch(index_prefix):
        raise ValueError("postgres bridge table_name must be a simple SQL identifier")
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    target TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    conflict_policy TEXT NOT NULL,
    source_scope_id TEXT NOT NULL DEFAULT '',
    source_updated_at TEXT NOT NULL DEFAULT '',
    source_trust DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS {index_prefix}_target_idx ON {table}(target);
CREATE INDEX IF NOT EXISTS {index_prefix}_source_scope_idx ON {table}(source_scope_id);
CREATE INDEX IF NOT EXISTS {index_prefix}_source_updated_idx ON {table}(source_updated_at);
""".strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


class PostgresSharedMemoryBridge:
    """Publish external shared-memory records into PostgreSQL."""

    def __init__(self, *, dsn_env: str = DEFAULT_POSTGRES_BRIDGE_DSN_ENV, table_name: str = DEFAULT_POSTGRES_BRIDGE_TABLE) -> None:
        self._dsn_env = str(dsn_env or DEFAULT_POSTGRES_BRIDGE_DSN_ENV)
        self._table_name = str(table_name or DEFAULT_POSTGRES_BRIDGE_TABLE)
        self._quoted_table = _quote_identifier(self._table_name)
        self._conn: Any = None

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
        except Exception:
            return False
        return True

    def open(self) -> None:
        dsn = os.environ.get(self._dsn_env)
        if not dsn:
            raise RuntimeError(f"postgres bridge DSN environment variable is not set: {self._dsn_env}")
        psycopg = importlib.import_module("psycopg")
        self._conn = psycopg.connect(dsn)
        self.ensure_schema()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _require_conn(self) -> Any:
        if self._conn is None:
            raise RuntimeError("postgres shared-memory bridge is not open")
        return self._conn

    def ensure_schema(self) -> None:
        conn = self._require_conn()
        with conn.cursor() as cur:
            cur.execute(build_postgres_schema_sql(table_name=self._table_name))
        conn.commit()

    def publish_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Publish a contract-v1 export payload into PostgreSQL."""
        if str(payload.get("schema_version") or "") != EXPORT_SCHEMA_VERSION:
            raise ValueError(f"unsupported external export schema_version: {payload.get('schema_version')}")
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError("payload.records must be a list")
        now = datetime.now(timezone.utc).isoformat()
        inserted = 0
        conn = self._require_conn()
        with conn.cursor() as cur:
            for raw_record in records:
                if not isinstance(raw_record, dict):
                    continue
                record_id = str(raw_record.get("id") or "").strip()
                if not record_id:
                    continue
                raw_provenance = raw_record.get("provenance")
                provenance: dict[str, Any] = raw_provenance if isinstance(raw_provenance, dict) else {}
                raw_metadata = raw_record.get("metadata")
                metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
                try:
                    source_trust = max(0.0, min(1.0, float(provenance.get("source_trust", 0.5))))
                except (TypeError, ValueError):
                    source_trust = 0.5
                cur.execute(
                    f"""
                    INSERT INTO {self._quoted_table}(
                        id, schema_version, target, content, summary, metadata, provenance,
                        conflict_policy, source_scope_id, source_updated_at, source_trust, imported_at
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        schema_version = EXCLUDED.schema_version,
                        target = EXCLUDED.target,
                        content = EXCLUDED.content,
                        summary = EXCLUDED.summary,
                        metadata = EXCLUDED.metadata,
                        provenance = EXCLUDED.provenance,
                        conflict_policy = EXCLUDED.conflict_policy,
                        source_scope_id = EXCLUDED.source_scope_id,
                        source_updated_at = EXCLUDED.source_updated_at,
                        source_trust = EXCLUDED.source_trust,
                        imported_at = EXCLUDED.imported_at
                    """,
                    (
                        record_id,
                        str(raw_record.get("schema_version") or EXPORT_SCHEMA_VERSION),
                        str(raw_record.get("target") or ""),
                        str(raw_record.get("content") or ""),
                        str(raw_record.get("summary") or ""),
                        _json_dumps(metadata),
                        _json_dumps(provenance),
                        str(raw_record.get("conflict_policy") or payload.get("conflict_policy") or ""),
                        str(provenance.get("scope_id") or ""),
                        str(provenance.get("updated_at") or ""),
                        source_trust,
                        now,
                    ),
                )
                inserted += 1
        conn.commit()
        return {"ok": True, "schema_version": EXPORT_SCHEMA_VERSION, "table_name": self._table_name, "published": inserted}
