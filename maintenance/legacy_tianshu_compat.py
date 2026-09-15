"""Narrow, opt-in Tianshu 2.0.1 migration compatibility; no live writes.

Reader evidence: old scope.py accessible_scope_ids selects explicit local/shared
identities; memory_queries.py inspect_memory and recall queries authorize with
memories.scope_id IN accessible_scope_params. Metadata is descriptive, not an
additional memory ACL. This contract DOES NOT cover episodes/history: some old
readers also authorize through a physical shared_scope_id column.

Integration (migrate_v2 remains unchanged by this module):
* Recognize BRIDGE_TABLE using bridge_table_contract() in _KNOWN/_REQUIRED/
  _LEGACY_COLUMNS and _classify_table_disposition (audit_completed_transport).
  Call build_completed_bridge_archive on the read-only snapshot BEFORE any
  destination write; its exception must block cutover. Persist its complete JSON
  in a restricted, non-searchable audit sidecar; deserialize and call
  verify_completed_bridge_archive before declaring preservation complete. Never
  feed it to add(), source_events, indexing, a bridge queue or transport worker.
* After _resolve_scope_mapping succeeds, prepare_memory_storage_authority with
  the resolved explicit map and manifest-bound target scopes. At ONLY the
  memory_rows add(..., _scope(row), ...) call, substitute
  resolve_memory_scope(row, _scope(row), authority=authority). Leave journal,
  episode/history and every unrelated _scope call unchanged. The returned
  scope_authorization includes the original descriptive gap as evidence.
* Do not replace row metadata or lifecycle with this return value. Existing
  legacy_metadata remains required; retain original snapshot/metadata bytes as
  well if the existing sanitization path is not byte-preserving. This helper
  changes neither rows nor the existing migration lifecycle/redaction rules.

Schema verification is necessary but does not prove deployed reader semantics.
The named reader contract must be explicitly attested by the integrator after
source review. A one-to-one map preserves partitions, not runtime audience ACLs:
manifest audience binding must separately preserve the old read boundary.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import sqlite3
from types import MappingProxyType
from typing import Any

BRIDGE_TABLE = "shared_bridge_outbox"
BRIDGE_COLUMNS = tuple(
    "id event_key memory_id operation source_updated_at status attempts worker_id "
    "last_error_type available_at created_at updated_at completed_at".split()
)
_BRIDGE_SCHEMA = tuple(
    (name, "INTEGER" if name in {"id", "attempts"} else "TEXT", int(name != "id"), int(name == "id"))
    for name in BRIDGE_COLUMNS
)
_MEMORY_COLUMNS = tuple(
    "id scope_id platform user_id chat_id thread_id gateway_session_key agent_identity "
    "agent_workspace session_id source target content summary created_at updated_at "
    "last_recalled_turn dedup_key metadata".split()
)
_MEMORY_REQUIRED = frozenset(
    "scope_id source target content summary created_at updated_at last_recalled_turn".split()
)
_MEMORY_SCHEMA = tuple(
    (name, "INTEGER" if name == "last_recalled_turn" else "TEXT", int(name in _MEMORY_REQUIRED), int(name == "id"))
    for name in _MEMORY_COLUMNS
)
MEMORY_READER_CONTRACT = "tianshu-2.0.1/memories-physical-scope-in-accessible-scopes"
_ARCHIVE_FORMAT = "scope-recall-completed-bridge-audit/1"
IMPORT_LEDGER_TABLE = "import_ledger"
IMPORT_LEDGER_COLUMNS = tuple(
    "import_fingerprint source_kind source_scope source_path memory_id imported_at".split()
)
_IMPORT_LEDGER_SCHEMA = tuple(
    (name, "TEXT", int(name != "import_fingerprint"), int(name == "import_fingerprint"))
    for name in IMPORT_LEDGER_COLUMNS
)


class LegacyCompatibilityError(ValueError):
    """Fail-closed compatibility rejection; messages never include row bodies."""


def _schema(conn: sqlite3.Connection, table: str) -> tuple[tuple[Any, ...], ...]:
    # Only module-owned constant table names reach this query.
    kind = conn.execute("SELECT type FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    if kind is None or kind[0] != "table":
        raise LegacyCompatibilityError("legacy table missing or not a table")
    return tuple((r[1], r[2], r[3], r[5]) for r in conn.execute(f'PRAGMA table_info("{table}")'))


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def bridge_table_contract() -> dict[str, Any]:
    """Return fresh recognition data, not permission to ignore pending work."""
    return {
        "table": BRIDGE_TABLE,
        "columns": list(BRIDGE_COLUMNS),
        "required_columns": list(BRIDGE_COLUMNS),
        "disposition": "audit_completed_transport",
        "replay": False,
    }


def build_completed_bridge_archive(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read all completed transport records losslessly into an inert JSON envelope.

    The caller owns the read-only frozen snapshot and durable audit persistence.
    An absent table, schema drift, duplicate identities, non-completed status or
    absent completion timestamp blocks; no partial/truncated archive is returned.
    No source or destination SQL mutation, transport, promotion or deletion occurs.
    Treat the envelope as restricted data, not a public migration report.
    """
    if _schema(conn, BRIDGE_TABLE) != _BRIDGE_SCHEMA:
        raise LegacyCompatibilityError("unverified shared_bridge_outbox schema")
    columns = ",".join(f'"{name}"' for name in BRIDGE_COLUMNS)
    rows = [dict(zip(BRIDGE_COLUMNS, tuple(row))) for row in conn.execute(
        f'SELECT {columns} FROM "{BRIDGE_TABLE}" ORDER BY id'
    )]
    ids: set[int] = set()
    keys: set[str] = set()
    for row in rows:
        if row["status"] != "completed":
            raise LegacyCompatibilityError("non_completed_bridge_outbox_blocks_cutover")
        if not isinstance(row["completed_at"], str) or not row["completed_at"].strip():
            raise LegacyCompatibilityError("bridge_completion_evidence_missing")
        if type(row["id"]) is not int or row["id"] in ids:
            raise LegacyCompatibilityError("invalid_bridge_identity")
        if not isinstance(row["event_key"], str) or not row["event_key"] or row["event_key"] in keys:
            raise LegacyCompatibilityError("invalid_bridge_event_identity")
        ids.add(row["id"])
        keys.add(row["event_key"])
    payload = {
        "format": _ARCHIVE_FORMAT,
        "table": BRIDGE_TABLE,
        "disposition": "audit_completed_transport",
        "replay": False,
        "columns": list(BRIDGE_COLUMNS),
        "schema_sql": conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (BRIDGE_TABLE,)).fetchone()[0],
        "row_count": len(rows),
        "rows": rows,
    }
    return {**payload, "sha256": _digest(payload)}


def verify_completed_bridge_archive(conn: sqlite3.Connection, persisted: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a deserialized durable archive against the exact source snapshot.

    Count alone is insufficient: equality includes every field and the payload
    digest. Return only safe aggregate evidence, never the transport record data.
    """
    expected = build_completed_bridge_archive(conn)
    if dict(persisted) != expected:
        raise LegacyCompatibilityError("bridge_audit_archive_not_lossless")
    return {"row_count": expected["row_count"], "sha256": expected["sha256"], "replay": False}


def build_import_ledger_archive(conn: sqlite3.Connection) -> dict[str, Any]:
    """Preserve the 2.0.1 OpenClaw import deduplication ledger as inert audit.

    migration_openclaw.ensure_import_ledger_schema owns this exact schema. These
    are import receipts, including possible deleted-memory references, not facts
    or grants. Never replay them or interpret source_scope as a runtime audience.
    """
    if _schema(conn, IMPORT_LEDGER_TABLE) != _IMPORT_LEDGER_SCHEMA:
        raise LegacyCompatibilityError("unverified_import_ledger_schema")
    columns = ",".join(f'"{name}"' for name in IMPORT_LEDGER_COLUMNS)
    rows = [dict(zip(IMPORT_LEDGER_COLUMNS, tuple(row))) for row in conn.execute(
        f'SELECT {columns} FROM "{IMPORT_LEDGER_TABLE}" ORDER BY import_fingerprint'
    )]
    fingerprints: set[str] = set()
    memory_ids: set[str] = set()
    for row in rows:
        if any(type(value) is not str or not value for value in row.values()):
            raise LegacyCompatibilityError("invalid_import_ledger_record")
        if row["import_fingerprint"] in fingerprints or row["memory_id"] in memory_ids:
            raise LegacyCompatibilityError("duplicate_import_ledger_identity")
        fingerprints.add(row["import_fingerprint"])
        memory_ids.add(row["memory_id"])
    payload = {
        "format": "scope-recall-import-ledger-audit/1",
        "table": IMPORT_LEDGER_TABLE,
        "disposition": "audit_import_provenance",
        "replay": False,
        "columns": list(IMPORT_LEDGER_COLUMNS),
        "schema_sql": conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (IMPORT_LEDGER_TABLE,)).fetchone()[0],
        "row_count": len(rows),
        "rows": rows,
    }
    return {**payload, "sha256": _digest(payload)}


def verify_import_ledger_archive(conn: sqlite3.Connection, persisted: Mapping[str, Any]) -> dict[str, Any]:
    expected = build_import_ledger_archive(conn)
    if dict(persisted) != expected:
        raise LegacyCompatibilityError("import_ledger_archive_not_lossless")
    return {"row_count": expected["row_count"], "sha256": expected["sha256"], "replay": False}


@dataclass(frozen=True)
class MemoryStorageAuthority:
    """Snapshot-bound original scope/metadata records and an explicit injective map.

    Construct with prepare_memory_storage_authority, not from arbitrary row data.
    The map is already resolved against a trusted installation manifest; this
    object creates no runtime grants and must not outlive its frozen snapshot.
    """
    reader_contract: str
    source_scope_map: Mapping[str, str]
    original_rows: Mapping[str, tuple[str, Any]]


def prepare_memory_storage_authority(
    conn: sqlite3.Connection,
    *,
    source_scope_map: Mapping[str, str],
    bound_target_scopes: frozenset[str],
    verified_reader_contract: str,
) -> MemoryStorageAuthority:
    """Verify this known memory schema, row provenance and one-to-one mapping.

    Extra schema columns are rejected because they could introduce new ACLs.
    No fallback/default source, trimming, length-token repair, alias merging or
    private/group inference is performed. Additional map entries for other
    source tables are allowed, but the entire supplied map must be injective.
    """
    if verified_reader_contract != MEMORY_READER_CONTRACT:
        raise LegacyCompatibilityError("memory_storage_reader_contract_not_verified")
    if _schema(conn, "memories") != _MEMORY_SCHEMA:
        raise LegacyCompatibilityError("unverified_memories_schema")
    mapping = dict(source_scope_map)
    for source, target in mapping.items():
        if any(type(value) is not str or not value.strip() or value == "*" for value in (source, target)):
            raise LegacyCompatibilityError("invalid_explicit_scope_mapping")
        if target not in bound_target_scopes:
            raise LegacyCompatibilityError("scope_target_not_manifest_bound")
    if len(set(mapping.values())) != len(mapping):
        raise LegacyCompatibilityError("source_scope_mapping_collision")
    originals: dict[str, tuple[str, Any]] = {}
    for identity, source, metadata in conn.execute("SELECT id, scope_id, metadata FROM memories"):
        if type(identity) is not str or not identity or identity in originals:
            raise LegacyCompatibilityError("invalid_memory_identity")
        if type(source) is not str or source not in mapping:
            raise LegacyCompatibilityError("physical_memory_scope_not_explicitly_mapped")
        originals[identity] = (source, metadata)
    return MemoryStorageAuthority(MEMORY_READER_CONTRACT, MappingProxyType(mapping), MappingProxyType(originals))


def resolve_memory_scope(
    row: Mapping[str, Any],
    strict_scope: Mapping[str, Any],
    *,
    authority: MemoryStorageAuthority,
) -> dict[str, Any]:
    """Resolve only proven missing/stale shared descriptors, retaining evidence.

    Feed a memory row after _map_scope_rows and the unmodified _scope(row) result.
    Exact original id/scope/metadata and resolved target are checked before any
    override. Invalid metadata, changed provenance, unsupported modes and local
    conflicts stay blocked. The input row (including lifecycle) is never edited.
    """
    result = dict(strict_scope)
    if authority.reader_contract != MEMORY_READER_CONTRACT:
        raise LegacyCompatibilityError("memory_storage_reader_contract_not_verified")
    original = authority.original_rows.get(row.get("id"))
    if original is None:
        raise LegacyCompatibilityError("memory_original_row_unverified")
    source, raw_metadata = original
    target = authority.source_scope_map[source]
    if (row.get("__legacy_source_scope_id", row.get("scope_id")) != source
            or row.get("scope_id") != target
            or row.get("metadata") != raw_metadata
            or result.get("source_scope_id") != source
            or result.get("row_scope_id") != target
            or row.get("__scope_mapping_gap")
            or "__legacy_default_scope" in row):
        raise LegacyCompatibilityError("memory_scope_or_metadata_provenance_changed")
    try:
        metadata = json.loads(raw_metadata) if raw_metadata not in (None, "") else {}
    except (TypeError, ValueError):
        result["gap"] = "legacy_metadata_not_json_object"
        return result
    if not isinstance(metadata, dict):
        result["gap"] = "legacy_metadata_not_json_object"
        return result
    mode = metadata.get("scope_mode", "")
    if not isinstance(mode, str):
        result["gap"] = "unsupported_scope_mode"
        return result
    mode = mode.strip().lower()
    field = {"shared": "shared_scope_id", "shared_pool": "shared_pool_scope_id"}.get(mode)
    allowed_gap = {"shared": "explicit_shared_scope_mismatch", "shared_pool": "explicit_shared_pool_scope_mismatch"}.get(mode)
    if field is None or not allowed_gap or result.get("gap") != allowed_gap:
        return result
    # Only missing or string-valued old descriptors are established compatible.
    # Structured values may encode an unknown policy and must not be discarded.
    if any(metadata.get(key) is not None and not isinstance(metadata[key], str)
           for key in ("runtime_scope_id", "shared_scope_id", "shared_pool_scope_id")):
        return result
    result["gap"] = ""
    result["legacy_storage_authority"] = {
        "contract": MEMORY_READER_CONTRACT,
        "basis": "original_memories.scope_id",
        "original_gap": allowed_gap,
        "descriptor_field": field,
        "descriptor_state": "missing" if not metadata.get(field) else "stale",
        "original_descriptor": metadata.get(field),
        "source_scope_id": source,
        "target_scope_id": target,
        "metadata_preserved": True,
        "permission_promoted": False,
    }
    return result
