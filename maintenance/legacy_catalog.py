"""Read-only legacy schema/catalog discovery; never opens a target.

Supported legacy tables and their dispositions have one owner here.
"""
from __future__ import annotations
import hashlib

import sqlite3
from pathlib import Path
from typing import Any
from .backup import _safe_path
from .legacy_v2_compat import (
    BRIDGE_TABLE, BRIDGE_COLUMNS, IMPORT_LEDGER_TABLE, IMPORT_LEDGER_COLUMNS,
)
from .migration_records import MigrationError, _canon, _columns, _open_immutable, _tables

_HISTORY = {
    "fact_action_receipts",
    "experience_runs",
    "reflection_events",
    "fact_freshness",
    "skill_anchors",
    "skill_conflicts",
}
_COMPAT = {
    "facts",
    "fact_versions",
    "claims",
    "claim_versions",
    "aliases",
    "references",
    "artifacts",
    "artifact_versions",
}
_DIGEST_TABLES = {
    "memory_digest_sources",
    "nightly_digest_quarantine",
    "nightly_digest_runs",
}
_KNOWN = {
    "schema_migrations",
    "memories",
    "memories_fts",
    "journal_entries",
    "journal_digest_runs",
    "memory_journal_sources",
    "journal_rejections",
    "journal_session_digest_state",
    "task_episodes",
    "privacy_purge_operations",
    "privacy_purge_tombstones",
    "privacy_purge_source_tombstones",
    "privacy_purge_vector_intents",
    "fact_claims",
    "fact_claim_evidence",
    "fact_claims_fts",
    "fact_claims_fts_membership",
    "procedural_playbooks",
    "playbook_versions",
    BRIDGE_TABLE,
    IMPORT_LEDGER_TABLE,
    *_HISTORY,
    *_COMPAT,
    *_DIGEST_TABLES,
}

# Columns a legacy table must have before any of its rows can be converted.
_REQUIRED = {
    table: frozenset(columns.split())
    for table, columns in {
        "journal_entries": "id scope_id shared_scope_id session_id role content created_at",
        "memories": "id scope_id session_id source target content summary created_at updated_at",
        "memory_journal_sources": "memory_id journal_entry_id",
        "task_episodes": "id scope_id session_id task_goal status started_at",
        "fact_claims": "claim_id memory_id scope_id subject_key predicate_key fact_key value cardinality assertion_kind recorded_at status",
        "fact_claim_evidence": "evidence_id claim_id source_type source_ref evidence_hash excerpt recorded_at",
        "fact_action_receipts": "action_id idempotency_key request_hash scope_id requested_action effective_action status applied receipt_json created_at updated_at",
        "procedural_playbooks": "id scope_id task_class title goal status created_at updated_at",
        "playbook_versions": "id playbook_id version change_type snapshot created_at",
        "experience_runs": "id playbook_id scope_id decision started_at",
        "reflection_events": "id episode_id scope_id event_type outcome created_at",
        "fact_freshness": "id subject_type subject_id fact_key truth_type created_at updated_at",
        "skill_anchors": "id playbook_id skill_name created_at",
        "skill_conflicts": "id playbook_id conflict_summary created_at",
        "memory_digest_sources": "memory_id run_id session_id message_ids source_hash created_at",
        "nightly_digest_quarantine": "id run_id session_id candidate_hash reason_codes created_at",
        "nightly_digest_runs": "id digest_date source_db started_at extractor status",
        BRIDGE_TABLE: " ".join(BRIDGE_COLUMNS),
        IMPORT_LEDGER_TABLE: " ".join(IMPORT_LEDGER_COLUMNS),
    }.items()
}

# Frozen 578b columns for tables whose rows become readable Core content or
# deletion authority. Extra columns may carry an ACL we cannot interpret.
# project_id/branch_id on scope-owning rows are the only extra columns mapped.
_LEGACY_COLUMNS = {
    "journal_entries": "id scope_id shared_scope_id platform user_id chat_id thread_id gateway_session_key agent_identity agent_workspace session_id turn_number role content content_hash created_at processed_run_id processed_at metadata extraction_attempts deferred_run_id deferred_at defer_count retryable_failures",
    "memories": "id scope_id platform user_id chat_id thread_id gateway_session_key agent_identity agent_workspace session_id source target content summary created_at updated_at last_recalled_turn dedup_key metadata",
    "memory_journal_sources": "memory_id journal_entry_id run_id created_at",
    "task_episodes": "id scope_id shared_scope_id session_id task_class task_goal user_intent status outcome started_at ended_at message_ids journal_entry_ids tool_names evidence verification environment metadata",
    "fact_claims": "claim_id memory_id scope_id subject_key predicate_key fact_key value normalized_value value_fingerprint cardinality assertion_kind valid_from valid_to recorded_at retired_at status confidence superseded_by_claim_id source_type source_ref evidence_hash metadata",
    "fact_claim_evidence": "evidence_id claim_id source_type source_ref evidence_hash excerpt recorded_at metadata",
    "fact_action_receipts": "action_id idempotency_key request_hash scope_id requested_action effective_action status applied policy_json receipt_json error created_at updated_at",
    "procedural_playbooks": "id scope_id shared_scope_id task_class title trigger goal preconditions steps pitfalls verification cleanup evidence_anchors related_skills environment_constraints reuse_policy status confidence success_count failure_count stale_count created_from_episode_id superseded_by last_used_at last_verified_at created_at updated_at metadata",
    "playbook_versions": "id playbook_id version change_type change_reason snapshot created_at",
    "experience_runs": "id playbook_id episode_id scope_id decision confidence_at_use preconditions_checked steps_completed evidence outcome outcome_reason model_name tool_call_count token_estimate started_at finished_at metadata",
    "reflection_events": "id episode_id playbook_id scope_id event_type outcome evidence mistakes root_causes corrections proposed_updates applied_updates created_at metadata",
    "fact_freshness": "id subject_type subject_id fact_key truth_type validator_kind validator_spec ttl_days last_checked_at valid_until status stale_reason superseded_by created_at updated_at",
    "skill_anchors": "id playbook_id skill_name load_policy reason created_at",
    "skill_conflicts": "id playbook_id skill_name conflicting_source conflict_summary resolution status created_at resolved_at metadata",
    "privacy_purge_operations": "operation_id request_fingerprint scope_set_hash target_count source_count vector_intent_count status created_at updated_at denied_at erased_at",
    "privacy_purge_tombstones": "operation_id target_hash content_hash created_at",
    "privacy_purge_source_tombstones": "operation_id journal_entry_id source_hash created_at",
    "privacy_purge_vector_intents": "operation_id event_key target_hash completed completed_at created_at",
    "memory_digest_sources": "memory_id run_id session_id message_ids source_hash created_at",
    "nightly_digest_quarantine": "id run_id session_id candidate_hash reason_codes metadata created_at",
    "nightly_digest_runs": "id digest_date source_db started_at finished_at extractor model dry_run status inserted updated skipped deleted error metadata",
    BRIDGE_TABLE: " ".join(BRIDGE_COLUMNS),
    IMPORT_LEDGER_TABLE: " ".join(IMPORT_LEDGER_COLUMNS),
}
_SUPPORTED_COLUMNS = {
    table: frozenset(columns.split())
    | (frozenset({"project_id", "branch_id"}) if "scope_id" in columns.split() else frozenset())
    for table, columns in _LEGACY_COLUMNS.items()
}

_CONTENT_TABLES = {
    "journal_entries",
    "memories",
    "task_episodes",
    "fact_claims",
    "fact_claim_evidence",
    "fact_action_receipts",
    "procedural_playbooks",
    "playbook_versions",
    *_HISTORY,
}
_LINEAGE_TABLES = {"memory_journal_sources", "memory_digest_sources"}
_PURGE_TABLES = {
    "privacy_purge_operations",
    "privacy_purge_tombstones",
    "privacy_purge_source_tombstones",
    "privacy_purge_vector_intents",
}
_DISPOSITIONS = {
    BRIDGE_TABLE: "audit_completed_transport",
    IMPORT_LEDGER_TABLE: "audit_import_provenance",
    **{table: "content" for table in _CONTENT_TABLES},
    **{table: "lineage" for table in _LINEAGE_TABLES},
    "nightly_digest_quarantine": "audit_quarantine",
    "nightly_digest_runs": "audit_run",
    "governance_audit_events": "audit_governance",
    **{table: "purge_authority" for table in _PURGE_TABLES},
}

# Rebuildable indexes and bookkeeping never block a cutover. Matched on the
# lower-cased name because SQLite table names are case-insensitive.
_DERIVED_PREFIXES = ("vector_", "embedding_", "relation_", "lexical_")
_DERIVED_NAMES = frozenset({
    "memory_entities", "memory_relations", "memory_feedback", "operator_operations",
})
_CATALOG_DERIVED_NAMES = _DERIVED_NAMES | {
    "schema_migrations",
    "journal_digest_runs",
    "journal_rejections",
    "journal_session_digest_state",
    "sqlite_sequence",
    *_COMPAT,
}


def _is_derived_index(table: str, names: frozenset[str] = _DERIVED_NAMES) -> bool:
    lower = table.lower()
    return (
        lower.endswith("_fts")
        or "_fts_" in lower
        or lower.startswith(_DERIVED_PREFIXES)
        or lower in names
    )


def _classify_table_disposition(table: str) -> str:
    if table in _DISPOSITIONS:
        return _DISPOSITIONS[table]
    return "derived_index" if _is_derived_index(table, _CATALOG_DERIVED_NAMES) else "unknown"


def _offline_source_path(source: str | Path) -> Path:
    path = _safe_path(source, must_exist=True, error_type=MigrationError)
    if not path.is_file():
        raise MigrationError("source must be a regular offline SQLite file")
    # immutable=1 deliberately ignores journals; refusing a live snapshot is
    # safer than silently omitting committed WAL rows or a rollback journal.
    for suffix in ("-wal", "-journal"):
        sidecar = _safe_path(path.with_name(path.name + suffix), error_type=MigrationError)
        if sidecar.exists() and (not sidecar.is_file() or sidecar.stat().st_size > 0):
            raise MigrationError("offline source has a nonempty WAL or journal; use a consistent SQLite backup")
    return path


def _tally_scopes(
    conn: sqlite3.Connection,
    tables: set[str],
    columns: dict[str, list[str]],
    dispositions: dict[str, str],
) -> dict[str, Any]:
    """Count every direct and shared scope identity, exactly as spelled.

    Empty string and '*' are audit sentinels, never audience grants; a
    non-string identity is malformed and reported instead of coerced.
    """
    content: dict[str, int] = {}
    audit_only: dict[str, int] = {}
    shared: dict[str, int] = {}
    occurrences: dict[str, dict[str, int]] = {}
    sentinels: dict[str, dict[str, int]] = {"": {}, "*": {}, "<null>": {}}
    direct: set[str] = set()
    unsupported: list[dict[str, Any]] = []
    for table in sorted(tables):
        for column in ("scope_id", "shared_scope_id"):
            if column not in columns[table]:
                continue
            key = f"{table}.{column}"
            for raw, count in conn.execute(f"SELECT {column}, count(*) FROM [{table}] GROUP BY {column}"):
                count = int(count)
                if raw is None:
                    sentinels["<null>"][key] = count
                    continue
                if type(raw) is not str:
                    unsupported.append({
                        "table": table,
                        "key": "<data>",
                        "reason": "malformed_non_string_identity",
                        "column": column,
                        "auto_promoted": False,
                    })
                    continue
                if column == "scope_id":
                    direct.add(raw)
                per_key = occurrences.setdefault(raw, {})
                per_key[key] = per_key.get(key, 0) + count
                if raw in ("", "*"):
                    sentinels[raw][key] = count
                    continue
                if column == "shared_scope_id":
                    bucket = shared
                elif dispositions[table] in {"content", "lineage"}:
                    bucket = content
                else:
                    bucket = audit_only
                bucket[raw] = bucket.get(raw, 0) + count
    audit_only = {scope: n for scope, n in audit_only.items() if scope not in content}
    shared_only = {
        scope: n for scope, n in shared.items()
        if scope not in content and scope not in audit_only
    }
    return {
        "content": content,
        "audit_only": audit_only,
        "shared_only": shared_only,
        "sentinels": sentinels,
        "occurrences": occurrences,
        "direct": direct,
        "unsupported": unsupported,
    }


def _schema_issues(
    tables: set[str], columns: dict[str, list[str]], dispositions: dict[str, str]
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for table in sorted(tables - {"sqlite_sequence"}):
        present = set(columns[table])
        missing = sorted(_REQUIRED.get(table, frozenset()) - present)
        if missing:
            issues.append({
                "table": table,
                "key": "<schema>",
                "reason": "legacy_schema_column_missing_blocks_cutover",
                "missing_columns": missing,
                "auto_promoted": False,
            })
        if table in _SUPPORTED_COLUMNS:
            extra = sorted(present - _SUPPORTED_COLUMNS[table])
            if extra:
                issues.append({
                    "table": table,
                    "key": "<schema>",
                    "reason": "unknown_legacy_columns_blocks_cutover",
                    "columns": extra,
                    "auto_promoted": False,
                })
        if dispositions[table] == "unknown":
            issues.append({
                "table": table,
                "key": "<table>",
                "reason": "unknown_legacy_table_blocks_cutover",
                "columns": columns[table],
                "auto_promoted": False,
            })
    return issues


def _catalog(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    dispositions = {t: _classify_table_disposition(t) for t in sorted(tables)}
    row_counts = {
        t: int(conn.execute(f"SELECT count(*) FROM [{t}]").fetchone()[0])
        for t in sorted(tables)
    }
    columns = {t: _columns(conn, t) for t in sorted(tables)}
    tally = _tally_scopes(conn, tables, columns, dispositions)
    unsupported = tally["unsupported"] + _schema_issues(tables, columns, dispositions)
    sentinels = tally["sentinels"]
    # The hash covers the raw tallies, so a manifest bound to one catalog
    # cannot be reused against a snapshot that differs in any identity.
    summary = {
        "content_scopes": tally["content"],
        "shared_only_scopes": tally["shared_only"],
        "audit_only_scopes": tally["audit_only"],
        "audit_sentinels": sentinels,
        "table_dispositions": dispositions,
        "table_row_counts": row_counts,
        "table_columns": columns,
        "unsupported": unsupported,
        "scope_occurrences": tally["occurrences"],
    }
    return {
        "catalog_sha256": hashlib.sha256(_canon(summary).encode("utf-8")).hexdigest(),
        "is_supported": not unsupported,
        "content_scopes": sorted(tally["content"]),
        "shared_only_scopes": sorted(tally["shared_only"]),
        "audit_only_scopes": sorted(tally["audit_only"]),
        "audit_sentinels": {
            marker: {"total": sum(found.values()), "occurrences": found}
            for marker, found in sentinels.items()
        },
        "sentinel_rules": {
            "empty_string_is_audit_sentinel": True,
            "star_is_audit_sentinel": True,
            "sentinels_prohibited_from_audience_grants": True,
        },
        "scope_counts": {
            "content": tally["content"],
            "shared_only": tally["shared_only"],
            "audit_only": tally["audit_only"],
        },
        "scope_occurrences": tally["occurrences"],
        "table_dispositions": dispositions,
        "table_row_counts": row_counts,
        "unsupported": unsupported,
        "direct_scope_count": len(tally["direct"]),
        "total_nonempty_raw_values": sum(1 for key in tally["occurrences"] if key != ""),
    }


def build_legacy_catalog(source: str | Path | sqlite3.Connection) -> dict[str, Any]:
    """Read-only catalog/preflight covering direct scope columns, shared references, and dispositions.

    Original identifiers are preserved exactly without synthetic equivalences.
    """
    conn = source
    source_path: Path | None = None
    source_sha256: str | None = None
    if isinstance(source, (str, Path)):
        source_path = _offline_source_path(source)
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        conn = _open_immutable(source_path)
    try:
        return {
            "format": "scope-recall-legacy-catalog/1",
            "source_path": str(source_path) if source_path else None,
            "source_sha256": source_sha256,
            **_catalog(conn),
        }
    finally:
        if conn is not source:
            conn.close()
