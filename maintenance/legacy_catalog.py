"""Read-only legacy schema/catalog discovery; never opens a target.

Supported legacy tables and their dispositions have one owner here.
"""
from __future__ import annotations
import hashlib

import sqlite3
from pathlib import Path
from typing import Any
from .backup import _safe_path
from .legacy_tianshu_compat import (
    BRIDGE_TABLE, BRIDGE_COLUMNS, IMPORT_LEDGER_TABLE, IMPORT_LEDGER_COLUMNS,
)
from .migration_records import MigrationError, _canon, _tables, _columns

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
    *_HISTORY,
    *_COMPAT,
    *_DIGEST_TABLES,
}
_REQUIRED = {
    "journal_entries": {
        "id",
        "scope_id",
        "shared_scope_id",
        "session_id",
        "role",
        "content",
        "created_at",
    },
    "memories": {
        "id",
        "scope_id",
        "session_id",
        "source",
        "target",
        "content",
        "summary",
        "created_at",
        "updated_at",
    },
    "memory_journal_sources": {"memory_id", "journal_entry_id"},
    "task_episodes": {
        "id",
        "scope_id",
        "session_id",
        "task_goal",
        "status",
        "started_at",
    },
    "fact_claims": {
        "claim_id",
        "memory_id",
        "scope_id",
        "subject_key",
        "predicate_key",
        "fact_key",
        "value",
        "cardinality",
        "assertion_kind",
        "recorded_at",
        "status",
    },
    "fact_claim_evidence": {
        "evidence_id",
        "claim_id",
        "source_type",
        "source_ref",
        "evidence_hash",
        "excerpt",
        "recorded_at",
    },
    "fact_action_receipts": {
        "action_id",
        "idempotency_key",
        "request_hash",
        "scope_id",
        "requested_action",
        "effective_action",
        "status",
        "applied",
        "receipt_json",
        "created_at",
        "updated_at",
    },
    "procedural_playbooks": {
        "id",
        "scope_id",
        "task_class",
        "title",
        "goal",
        "status",
        "created_at",
        "updated_at",
    },
    "playbook_versions": {
        "id",
        "playbook_id",
        "version",
        "change_type",
        "snapshot",
        "created_at",
    },
    "experience_runs": {"id", "playbook_id", "scope_id", "decision", "started_at"},
    "reflection_events": {
        "id",
        "episode_id",
        "scope_id",
        "event_type",
        "outcome",
        "created_at",
    },
    "fact_freshness": {
        "id",
        "subject_type",
        "subject_id",
        "fact_key",
        "truth_type",
        "created_at",
        "updated_at",
    },
    "skill_anchors": {"id", "playbook_id", "skill_name", "created_at"},
    "skill_conflicts": {"id", "playbook_id", "conflict_summary", "created_at"},
    "memory_digest_sources": {
        "memory_id",
        "run_id",
        "session_id",
        "message_ids",
        "source_hash",
        "created_at",
    },
    "nightly_digest_quarantine": {
        "id",
        "run_id",
        "session_id",
        "candidate_hash",
        "reason_codes",
        "created_at",
    },
    "nightly_digest_runs": {
        "id",
        "digest_date",
        "source_db",
        "started_at",
        "extractor",
        "status",
    },
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

_LINEAGE_TABLES = {
    "memory_journal_sources",
    "memory_digest_sources",
}

_AUDIT_TABLES = {
    "nightly_digest_quarantine",
    "nightly_digest_runs",
    "governance_audit_events",
}

_PURGE_TABLES = {
    "privacy_purge_operations",
    "privacy_purge_tombstones",
    "privacy_purge_source_tombstones",
    "privacy_purge_vector_intents",
}


_KNOWN.add(BRIDGE_TABLE)
_REQUIRED[BRIDGE_TABLE] = set(BRIDGE_COLUMNS)
_LEGACY_COLUMNS[BRIDGE_TABLE] = " ".join(BRIDGE_COLUMNS)
_KNOWN.add(IMPORT_LEDGER_TABLE)
_REQUIRED[IMPORT_LEDGER_TABLE] = set(IMPORT_LEDGER_COLUMNS)
_LEGACY_COLUMNS[IMPORT_LEDGER_TABLE] = " ".join(IMPORT_LEDGER_COLUMNS)



def _classify_table_disposition(table: str) -> str:
    lower = table.lower()
    if table == BRIDGE_TABLE:
        return "audit_completed_transport"
    if table == IMPORT_LEDGER_TABLE:
        return "audit_import_provenance"
    if table in _CONTENT_TABLES:
        return "content"
    if table in _LINEAGE_TABLES:
        return "lineage"
    if table == "nightly_digest_quarantine":
        return "audit_quarantine"
    if table == "nightly_digest_runs":
        return "audit_run"
    if table == "governance_audit_events":
        return "audit_governance"
    if table in _PURGE_TABLES:
        return "purge_authority"
    if (
        lower.endswith("_fts")
        or "_fts_" in lower
        or lower.startswith("vector_")
        or lower.startswith("embedding_")
        or lower.startswith("relation_")
        or lower.startswith("lexical_")
        or lower in {
            "schema_migrations",
            "journal_digest_runs",
            "journal_rejections",
            "journal_session_digest_state",
            "memory_entities",
            "memory_relations",
            "memory_feedback",
            "operator_operations",
            "sqlite_sequence",
            *_COMPAT,
        }
    ):
        return "derived_index"
    return "unknown"


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


def build_legacy_catalog(source: str | Path | sqlite3.Connection) -> dict[str, Any]:
    """Read-only catalog/preflight covering direct scope columns, shared references, and dispositions.

    Empty string and '*' are classified as audit sentinels, not normal audience grants.
    Original identifiers are preserved exactly without synthetic equivalences.
    """
    should_close = False
    source_path: Path | None = None
    source_sha256: str | None = None
    if isinstance(source, (str, Path)):
        source_path = _offline_source_path(source)
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        conn = sqlite3.connect(f"{source_path.as_uri()}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        should_close = True
    else:
        conn = source
    try:
        tables = _tables(conn)
        table_dispositions = {t: _classify_table_disposition(t) for t in sorted(tables)}
        table_row_counts = {
            t: int(conn.execute(f"SELECT count(*) FROM [{t}]").fetchone()[0])
            for t in sorted(tables)
        }

        content_scopes: dict[str, int] = {}
        audit_only_scopes: dict[str, int] = {}
        audit_sentinels: dict[str, dict[str, int]] = {"": {}, "*": {}, "<null>": {}}
        shared_refs: dict[str, int] = {}
        unsupported: list[dict[str, Any]] = []
        scope_occurrences: dict[str, dict[str, int]] = {}
        direct_scope_strings: set[str] = set()

        for table in sorted(tables):
            cols = _columns(conn, table)
            if "scope_id" in cols:
                for row in conn.execute(f"SELECT scope_id, count(*) FROM [{table}] GROUP BY scope_id"):
                    raw = row[0]
                    cnt = int(row[1])
                    key = f"{table}.scope_id"
                    if raw is None:
                        audit_sentinels["<null>"][key] = cnt
                    elif type(raw) is not str:
                        unsupported.append({
                            "table": table,
                            "key": "<data>",
                            "reason": "malformed_non_string_identity",
                            "column": "scope_id",
                            "auto_promoted": False,
                        })
                    else:
                        direct_scope_strings.add(raw)
                        if raw not in scope_occurrences:
                            scope_occurrences[raw] = {}
                        scope_occurrences[raw][key] = scope_occurrences[raw].get(key, 0) + cnt
                        
                        if raw == "":
                            audit_sentinels[""][key] = cnt
                        elif raw == "*":
                            audit_sentinels["*"][key] = cnt
                        else:
                            if table_dispositions.get(table) in {"content", "lineage"}:
                                content_scopes[raw] = content_scopes.get(raw, 0) + cnt
                            else:
                                audit_only_scopes[raw] = audit_only_scopes.get(raw, 0) + cnt

            if "shared_scope_id" in cols:
                for row in conn.execute(f"SELECT shared_scope_id, count(*) FROM [{table}] GROUP BY shared_scope_id"):
                    raw = row[0]
                    cnt = int(row[1])
                    key = f"{table}.shared_scope_id"
                    if raw is None:
                        audit_sentinels["<null>"][key] = cnt
                    elif type(raw) is not str:
                        unsupported.append({
                            "table": table,
                            "key": "<data>",
                            "reason": "malformed_non_string_identity",
                            "column": "shared_scope_id",
                            "auto_promoted": False,
                        })
                    else:
                        if raw not in scope_occurrences:
                            scope_occurrences[raw] = {}
                        scope_occurrences[raw][key] = scope_occurrences[raw].get(key, 0) + cnt
                        
                        if raw == "":
                            audit_sentinels[""][key] = cnt
                        elif raw == "*":
                            audit_sentinels["*"][key] = cnt
                        else:
                            shared_refs[raw] = shared_refs.get(raw, 0) + cnt

        for s in list(audit_only_scopes):
            if s in content_scopes:
                del audit_only_scopes[s]

        shared_only_scopes = {
            s: cnt
            for s, cnt in shared_refs.items()
            if s not in content_scopes and s not in audit_only_scopes
        }

        for table in sorted(tables - {"sqlite_sequence"}):
            missing = sorted(_REQUIRED.get(table, set()) - set(_columns(conn, table)))
            if missing:
                unsupported.append({
                    "table": table,
                    "key": "<schema>",
                    "reason": "legacy_schema_column_missing_blocks_cutover",
                    "missing_columns": missing,
                    "auto_promoted": False,
                })
            if table in _LEGACY_COLUMNS:
                supported = set(_LEGACY_COLUMNS[table].split())
                if "scope_id" in supported:
                    supported.update({"project_id", "branch_id"})
                extra_columns = sorted(set(_columns(conn, table)) - supported)
                if extra_columns:
                    unsupported.append({
                        "table": table,
                        "key": "<schema>",
                        "reason": "unknown_legacy_columns_blocks_cutover",
                        "columns": extra_columns,
                        "auto_promoted": False,
                    })
            if table_dispositions.get(table) == "unknown":
                unsupported.append({
                    "table": table,
                    "key": "<table>",
                    "reason": "unknown_legacy_table_blocks_cutover",
                    "columns": _columns(conn, table),
                    "auto_promoted": False,
                })

        canonical_summary = {
            "content_scopes": content_scopes,
            "shared_only_scopes": shared_only_scopes,
            "audit_only_scopes": audit_only_scopes,
            "audit_sentinels": audit_sentinels,
            "table_dispositions": table_dispositions,
            "table_row_counts": table_row_counts,
            "table_columns": {t: _columns(conn, t) for t in sorted(tables)},
            "unsupported": unsupported,
            "scope_occurrences": scope_occurrences,
        }
        catalog_sha256 = hashlib.sha256(_canon(canonical_summary).encode("utf-8")).hexdigest()

        direct_count = len(direct_scope_strings)
        total_raw_nonempty = sum(1 for key in scope_occurrences if key != "")

        return {
            "format": "scope-recall-legacy-catalog/1",
            "source_path": str(source_path) if source_path else None,
            "source_sha256": source_sha256,
            "catalog_sha256": catalog_sha256,
            "is_supported": len(unsupported) == 0,
            "content_scopes": sorted(content_scopes),
            "shared_only_scopes": sorted(shared_only_scopes),
            "audit_only_scopes": sorted(audit_only_scopes),
            "audit_sentinels": {
                "": {
                    "total": sum(audit_sentinels[""].values()),
                    "occurrences": audit_sentinels[""],
                },
                "*": {
                    "total": sum(audit_sentinels["*"].values()),
                    "occurrences": audit_sentinels["*"],
                },
                "<null>": {
                    "total": sum(audit_sentinels["<null>"].values()),
                    "occurrences": audit_sentinels["<null>"],
                },
            },
            "sentinel_rules": {
                "empty_string_is_audit_sentinel": True,
                "star_is_audit_sentinel": True,
                "sentinels_prohibited_from_audience_grants": True,
            },
            "scope_counts": {
                "content": content_scopes,
                "shared_only": shared_only_scopes,
                "audit_only": audit_only_scopes,
            },
            "scope_occurrences": scope_occurrences,
            "table_dispositions": table_dispositions,
            "table_row_counts": table_row_counts,
            "unsupported": unsupported,
            "direct_scope_count": direct_count,
            "total_nonempty_raw_values": total_raw_nonempty,
        }
    finally:
        if should_close:
            conn.close()


