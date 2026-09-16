"""Offline legacy-to-Core conversion and its single write transaction.

Retains status, exact evidence, scope and deletion semantics. No host lifecycle
or model work; reports gate activation and derived indexes remain rebuildable.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, cast


from .backup import _safe_path


from scope_recall.contracts import ClaimProposal, InstanceBinding, TrustedContext
from scope_recall.core.claims import claim_slot
from scope_recall.core.events import lexical_terms
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
from scope_recall.maintenance.legacy_tianshu_compat import BRIDGE_TABLE, LegacyCompatibilityError, prepare_memory_storage_authority, resolve_memory_scope, verify_completed_bridge_archive, IMPORT_LEDGER_TABLE, verify_import_ledger_archive
from scope_recall.maintenance.legacy_episode_membership import (
    plan_legacy_episode_memberships,
)


from .migration_records import (
    LEGACY_BASELINE, REPORT_FORMAT, MigrationError, _write_report, _materialize_explicit_scope_selection, _blocked_prewrite_report, _canon, _digest, _stable, _safe, _safe_text, _json, _tables, _columns, _rows, _time, _recorded,
)
from .legacy_catalog import _HISTORY, _COMPAT, _DIGEST_TABLES, _KNOWN, _REQUIRED, _LEGACY_COLUMNS, _offline_source_path, build_legacy_catalog
from .migration_activation import (
    _load_installation_handoff, _resolve_scope_mapping, _existing_target_scopes,
)

def _procedure_claim_id(old_id: str) -> str:
    """Use the Core claim namespace for imported procedures."""
    return _stable("claim", f"procedure:{old_id}")


def _role_origin(role: object) -> tuple[str, str]:
    value = str(role or "").lower()
    if value in {"user", "human"}:
        return "user", "human_direct"
    if value in {"assistant", "model"}:
        return "assistant", "assistant_visible"
    if value in {"tool", "function"}:
        return "tool", "tool_observation"
    if value in {"document", "doc"}:
        return "document", "external_document"
    if value == "system":
        return "system", "host_generated"
    return "unknown", "origin_unknown"


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = _json(row.get("metadata"), {})
    return value if isinstance(value, dict) else {}


def _scope(row: dict[str, Any]) -> dict[str, Any]:
    """Use resolved old scope_id; shared_scope_id alone is not a block."""
    metadata = _metadata(row)
    source_scope = str(
        row.get("__legacy_source_scope_id") or row.get("scope_id") or "legacy-scope"
    )
    row_scope = str(row.get("scope_id") or "legacy-scope")
    runtime = str(metadata.get("runtime_scope_id") or source_scope)
    shared = str(metadata.get("shared_scope_id") or row.get("shared_scope_id") or "")
    pool = str(
        metadata.get("shared_pool_scope_id") or row.get("shared_pool_scope_id") or ""
    )
    explicit = str(metadata.get("scope_mode") or "").strip().lower()
    mode = (
        explicit
        if explicit in {"local", "shared", "shared_pool"}
        else "legacy_row_scope"
    )
    gap = ""
    if explicit == "local" and runtime != source_scope:
        gap = "explicit_local_scope_mismatch"
    if explicit == "shared" and (not shared or shared != source_scope):
        gap = "explicit_shared_scope_mismatch"
    if explicit == "shared_pool" and (not pool or pool != source_scope):
        gap = "explicit_shared_pool_scope_mismatch"
    if explicit and explicit not in {"local", "shared", "shared_pool"}:
        gap = "unsupported_scope_mode"
    result = {
        "row_scope_id": row_scope,
        "source_scope_id": source_scope,
        "runtime_scope_id": runtime,
        "shared_scope_id": shared,
        "shared_pool_scope_id": pool,
        "scope_mode": mode,
        "gap": gap,
    }
    return result


def _map_scope_rows(
    rows: list[dict[str, Any]],
    mapping: Mapping[str, str],
    *,
    default_source_scope: str | None = None,
) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for row in rows:
        raw_source = row.get("scope_id")
        if (
            raw_source is None or raw_source == ""
        ) and default_source_scope is not None:
            row = {
                **row,
                "scope_id": default_source_scope,
                "__legacy_default_scope": default_source_scope,
            }
            raw_source = default_source_scope
        if raw_source is None or raw_source == "":
            mapped.append(row)
            continue
        source = str(raw_source)
        copy = dict(row)
        copy["__legacy_source_scope_id"] = source
        target = mapping.get(source)
        if target is None:
            copy["__scope_mapping_gap"] = source
            mapped.append(copy)
            continue
        copy["scope_id"] = target
        mapped.append(copy)
    return mapped


def _source(
    table: str,
    row: dict[str, Any],
    scope: dict[str, Any],
    content: str,
    kind: str,
    original: str,
    extra: dict[str, Any],
    redacted: bool,
) -> dict[str, Any]:
    if table == "fact_claim_evidence":
        identity = row.get("evidence_id") or row.get("claim_id") or "unknown"
    elif table == "fact_claims":
        identity = row.get("claim_id") or row.get("id") or "unknown"
    else:
        identity = (
            row.get("id")
            or row.get("claim_id")
            or row.get("action_id")
            or row.get("evidence_id")
            or row.get("playbook_id")
            or "unknown"
        )
    key = f"legacy:{table}:{identity}"
    event_id = _stable("event", f"{table}:{identity}")
    occurred, precision = _time(
        row.get("created_at") or row.get("updated_at") or row.get("started_at")
    )
    recorded = _recorded(
        row.get("updated_at") or row.get("created_at") or row.get("started_at")
    )
    role = _role_origin(row.get("role"))[0] if table == "journal_entries" else "unknown"
    body = {
        "protocol_version": "1.1",
        "source_event_key": key,
        "source_revision": 1,
        "origin": "imported",
        "role": role,
        "content": content,
        "occurred_at": occurred,
        "recorded_at": recorded,
        "time_precision": precision,
        "capture_state": "complete",
        "evidence_refs": [],
    }
    extra_payload = {
        "legacy_table": table,
        "legacy_id": str(identity),
        "source_kind": kind,
        "redaction_applied": redacted,
        "scope_authorization": scope,
        **cast(dict[str, Any], _safe(extra)),
    }
    return {
        "event_id": event_id,
        "source_event_key": key,
        "source_revision": 1,
        "source_group_key": key,
        "segment_index": 0,
        "segment_total": None,
        "scope_id": scope["row_scope_id"],
        "session_id": str(
            row.get("session_id") or f"legacy-session-{scope['row_scope_id']}"
        ),
        "project_id": str(row["project_id"])
        if row.get("project_id") is not None
        else None,
        "branch_id": str(row["branch_id"])
        if row.get("branch_id") is not None
        else None,
        "origin": "imported",
        "role": role,
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "event_sha256": _digest(body),
        "occurred_at": occurred,
        "recorded_at": recorded,
        "persisted_at": recorded,
        "time_precision": precision,
        "capture_state": "complete",
        "source_original_origin": original,
        "dataset_id": "legacy-578b",
        "import_provenance_sha256": _digest({"baseline": LEGACY_BASELINE, "key": key}),
        "extra_json": _canon(extra_payload),
        "capture_gaps_json": _canon([scope["gap"]] if scope["gap"] else []),
        "read_blocked": int(bool(scope["gap"])),
        "suppressed": 0,
        "scope_gap": scope["gap"],
    }


def _resolve(
    raw: object,
    source_type: object,
    journals: dict[str, str],
    memories: dict[str, str],
    archives: dict[tuple[str, str], str],
) -> str | None:
    value, kind = str(raw or "").strip(), str(source_type or "").lower().strip()
    if (kind, value) in archives:
        return archives[(kind, value)]
    if value in journals:
        return journals[value]
    if value in memories:
        return memories[value]
    tail = re.split(r"[:/]+", value)[-1]
    if kind in {"memory", "memory_id", "fact_memory"}:
        return memories.get(tail)
    if kind in {"journal", "journal_entry", "event", "source"}:
        return journals.get(tail)
    return journals.get(tail) or memories.get(tail)


def _evidence_items(value: object) -> list[dict[str, Any]]:
    parsed = _json(value, [])
    return (
        [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, list)
        else []
    )


def _json_list(value: object) -> list[object]:
    parsed = _json(value, [])
    return parsed if isinstance(parsed, list) else []


def _safe_list(value: object) -> list[str]:
    parsed = _json(value, [])
    if not isinstance(parsed, list):
        return []
    result: list[str] = []
    for item in parsed:
        result.append(
            _safe_text(item)[0] if isinstance(item, str) else _canon(_safe(item))
        )
    return result


def _claim_payload(
    kind: str,
    subject: str,
    predicate: str,
    value: str,
    conditions: list[str],
    legacy: dict[str, Any],
    procedure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # ClaimProposal is an existing closed schema.  Migration provenance lives
    # in the sanitized source archive and history links, never in authority
    # payload extensions that would make ordinary Core revise invalid.
    payload: dict[str, Any] = {
        "kind": kind,
        "subject": subject or "legacy:unknown-subject",
        "predicate": predicate or "legacy:unknown-predicate",
        "value_text": value,
        "conditions": conditions,
        "statement_kind": "assertion",
        "valid_from": legacy.get("valid_from"),
        "valid_to": legacy.get("valid_to"),
        "evidence_spans": [],
    }
    if procedure is not None:
        payload["procedure"] = procedure
    return payload


def _claim_basis(assertion: object, origin: str) -> str:
    value = str(assertion or "").lower()
    if value == "direct":
        return "direct_report"
    if value == "validated":
        return "observed" if origin == "tool_observation" else "direct_report"
    if value == "inferred":
        return "inferred_suggestion"
    return "unknown"


def _procedure_slot_collisions(
    playbooks: list[dict[str, Any]],
    versions_by: Mapping[str, list[dict[str, Any]]],
) -> dict[str, tuple[str, ...]]:
    """Find different legacy identities competing for one Core procedure slot.

    Plan the complete batch before inserting any authority: arrival order must
    not select a winner. History remains in same-scope source_events; old IDs
    are provenance, never invented semantic conditions or replacement chains.
    Histories changing slots already have their own explicit conversion gap.
    """
    owners: defaultdict[str, set[str]] = defaultdict(set)
    for playbook in playbooks:
        old_id = str(playbook.get("id") or "")
        if not old_id:
            continue
        slots: set[str] = set()
        for snap in versions_by.get(old_id) or [{"snapshot": _canon(playbook)}]:
            data = _json(snap.get("snapshot"), playbook)
            data = data if isinstance(data, dict) else playbook
            title, _ = _safe_text(data.get("title") or playbook.get("title") or old_id)
            proposal = cast(ClaimProposal, {
                "kind": "procedure", "subject": str(playbook.get("task_class") or old_id),
                "predicate": title or "procedure", "conditions": _safe_list(data.get("preconditions")),
            })
            slots.add(claim_slot(
                _scope(playbook)["row_scope_id"],
                str(playbook["project_id"]) if playbook.get("project_id") is not None else None,
                str(playbook["branch_id"]) if playbook.get("branch_id") is not None else None,
                proposal,
            ))
        if len(slots) == 1:
            owners[next(iter(slots))].add(old_id)
    return {
        old_id: tuple(sorted(ids))
        for ids in owners.values() if len(ids) > 1
        for old_id in ids
    }


def _insert_version(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    scope_id: str,
    project_id: str | None,
    branch_id: str | None,
    payload: dict[str, Any],
    state: str,
    basis: str,
    reason: str,
    recorded: str,
    valid_from: str | None,
    valid_to: str | None,
    evidence: list[tuple[str, str, str | None]],
    counts: defaultdict[str, int],
) -> int:
    slot = claim_slot(
        scope_id, project_id, branch_id, cast(ClaimProposal, payload)
    )
    head = conn.execute(
        "SELECT claim_id FROM claims WHERE slot_key=?", (slot,)
    ).fetchone()
    if head is None:
        conn.execute(
            "INSERT INTO claims(claim_id,scope_id,project_id,branch_id,subject,predicate,kind,slot_key,current_revision) VALUES (?,?,?,?,?,?,?,?,1)",
            (
                claim_id,
                scope_id,
                project_id,
                branch_id,
                payload["subject"],
                payload["predicate"],
                payload["kind"],
                slot,
            ),
        )
        revision = 1
    else:
        if str(head[0]) != claim_id:
            raise MigrationError(f"claim slot collision: {slot}")
        revision = int(
            conn.execute(
                "SELECT COALESCE(max(revision),0)+1 FROM claim_versions WHERE claim_id=?",
                (claim_id,),
            ).fetchone()[0]
        )
        previous = revision - 1
        conn.execute(
            "UPDATE claim_versions SET recorded_to=? WHERE claim_id=? AND revision=?",
            (recorded, claim_id, previous),
        )
        conn.execute(
            "UPDATE claims SET current_revision=? WHERE claim_id=?",
            (revision, claim_id),
        )
    serialized = _canon(payload)
    if len(serialized.encode()) > 131072:
        raise MigrationError(f"claim payload exceeds Core limit: {claim_id}")
    conn.execute(
        "INSERT INTO claim_versions(claim_id,revision,payload_json,state,basis,qualification_reason,valid_from,valid_to,recorded_from,replaces_revision,conflict_revisions_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            revision,
            serialized,
            state,
            basis,
            reason,
            valid_from,
            valid_to,
            recorded,
            revision - 1 if revision > 1 else None,
            "[]",
        ),
    )
    for ref, quote, location in evidence:
        conn.execute(
            "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote,location) VALUES ('claim',?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (claim_id, revision, ref, 1, "supports", quote[:4096], location),
        )
    counts["claims"] += int(revision == 1)
    counts["claim_versions"] += 1
    return revision


def migrate_legacy(
    source: str | Path,
    target_directory: str | Path | None = None,
    *,
    agent_id: str = "p15-synthetic-agent",
    installation_id: str = "p15-synthetic-installation",
    scope_ids: Iterable[str] | None = None,
    batch_key: str = "p15-fixed-batch-001",
    report_path: str | Path | None = None,
    installation_manifest: str | Path | None = None,
    host: str | None = None,
    source_scope_map: Mapping[str, str] | None = None,
    single_scope_to: str | None = None,
    legacy_memory_reader_contract: str | None = None,
    completed_bridge_archive_path: str | Path | None = None,
    import_ledger_archive_path: str | Path | None = None,
    project_legacy_memberships: bool = False,
) -> dict[str, Any]:
    """Import a frozen store; explicitly opt in to verified legacy compatibility.

    The bridge sidecar must already be persisted in restricted offline storage.
    It is verified before target writes and never becomes searchable content.
    Membership projection retains every evidence edge, but secondary episodes
    are not additional processing owners; its exact audit is in the report.
    """
    if single_scope_to is not None and installation_manifest is None:
        raise MigrationError("single_scope_to requires an installation manifest")
    if single_scope_to is not None and (
        type(single_scope_to) is not str or not single_scope_to.strip()
    ):
        raise MigrationError(
            "single_scope_to must be a non-empty audience name or scope ID"
        )
    if single_scope_to is not None and source_scope_map is not None:
        raise MigrationError("single_scope_to cannot be combined with source_scope_map")
    explicit_scopes = _materialize_explicit_scope_selection(scope_ids)
    handoff = None
    manifest_file: Path | None = None
    audience_scopes: dict[str, str] = {}
    scope_mapping: dict[str, str] = {}
    handoff_issues: list[dict[str, Any]] = []
    if installation_manifest is not None:
        binding, manifest_target, manifest_file, audience_scopes, resolved_host = (
            _load_installation_handoff(installation_manifest, host)
        )
        target_dir = _safe_path(manifest_target, error_type=MigrationError)
        if (
            target_directory is not None
            and _safe_path(target_directory, error_type=MigrationError) != target_dir
        ):
            raise MigrationError(
                "target directory differs from trusted installation manifest"
            )
        agent_id = binding.agent_id
        installation_id = binding.installation_id
        payload = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file and manifest_file.exists() else {}
        handoff = {
            "host": resolved_host,
            "manifest": str(manifest_file),
            "agent_id": agent_id,
            "installation_id": installation_id,
            "target_directory": str(target_dir),
            "target_scope_ids": sorted(binding.scope_ids),
            "test_mode": bool(binding.test_mode),
            "audience_scopes": dict(audience_scopes),
            "archive_snapshot_hash": payload.get("archive_snapshot_hash"),
            "archive_catalog_hash": payload.get("archive_catalog_hash"),
            "archive_source_map": payload.get("archive_source_map"),
            "archive_retention_scopes": payload.get("archive_retention_scopes"),
            "archive_scopes": payload.get("archive_scopes"),
        }
    elif target_directory is None:
        raise MigrationError("target directory or installation manifest is required")
    else:
        target_dir = _safe_path(target_directory, error_type=MigrationError)
    source_path = _offline_source_path(source)
    if report_path is not None:
        _safe_path(report_path, error_type=MigrationError)
    if (
        source_path.is_symlink()
        or not source_path.is_file()
        or source_path == target_dir
    ):
        raise MigrationError("source must be a distinct regular offline SQLite file")
    
    catalog = build_legacy_catalog(source_path)
    malformed_scopes = [
        item
        for item in catalog.get("unsupported", [])
        if item.get("reason") == "malformed_non_string_identity"
    ]
    if malformed_scopes:
        report = _blocked_prewrite_report(
            batch_key=batch_key,
            unmapped=malformed_scopes,
            reasons=["malformed_non_string_identity"],
        )
        _write_report(report, report_path)
        return report
    if handoff and handoff.get("archive_snapshot_hash"):
        if handoff["archive_snapshot_hash"] != catalog["source_sha256"]:
            raise MigrationError("source snapshot digest mismatch against trusted manifest")
    if handoff and handoff.get("archive_catalog_hash"):
        if handoff["archive_catalog_hash"] != catalog["catalog_sha256"]:
            raise MigrationError("catalog digest mismatch against trusted manifest")
    is_archive = handoff is not None and (bool(handoff.get("archive_snapshot_hash")) or bool(handoff.get("archive_catalog_hash")) or bool(handoff.get("archive_source_map")))
    if is_archive:
        if single_scope_to is not None:
            raise MigrationError("single_scope_to is forbidden in archive mode")
        if source_scope_map is None:
            source_scope_map = handoff.get("archive_source_map", {})
        elif source_scope_map != handoff.get("archive_source_map", {}):
            raise MigrationError("source_scope_map conflicts with trusted archive manifest map")
        
        expected_keys = frozenset(handoff.get("archive_source_map", {}).keys())
        real_scopes = (
            frozenset(catalog["content_scopes"]) |
            frozenset(catalog["shared_only_scopes"]) |
            frozenset(catalog["audit_only_scopes"])
        )
        if expected_keys != real_scopes:
            raise MigrationError("source_scope_map must match EXACT complete real source-map key set")
            
        if explicit_scopes is not None and frozenset(explicit_scopes) != real_scopes:
            raise MigrationError("explicit scope selection must equal the ENTIRE real catalog set in archive mode")
    
    source_conn = sqlite3.connect(f"{source_path.as_uri()}?mode=ro&immutable=1", uri=True)
    source_conn.row_factory = sqlite3.Row

    try:
        tables = _tables(source_conn)
        import_ledger_receipt = None
        if IMPORT_LEDGER_TABLE in tables:
            if import_ledger_archive_path is None:
                raise LegacyCompatibilityError("import ledger requires a verified offline audit sidecar")
            ledger_file = Path(import_ledger_archive_path)
            if ledger_file.is_symlink() or not ledger_file.is_file():
                raise LegacyCompatibilityError("import ledger sidecar must be a regular file")
            import_ledger_receipt = verify_import_ledger_archive(
                source_conn, json.loads(ledger_file.read_text(encoding="utf-8"))
            )
        bridge_receipt = None
        if BRIDGE_TABLE in tables:
            if completed_bridge_archive_path is None:
                raise LegacyCompatibilityError("completed bridge requires a verified offline audit sidecar")
            bridge_file = Path(completed_bridge_archive_path)
            if bridge_file.is_symlink() or not bridge_file.is_file():
                raise LegacyCompatibilityError("completed bridge sidecar must be a regular file")
            bridge_receipt = verify_completed_bridge_archive(
                source_conn, json.loads(bridge_file.read_text(encoding="utf-8"))
            )
        unsupported_columns = []
        for table in sorted(tables & _LEGACY_COLUMNS.keys()):
            supported = set(_LEGACY_COLUMNS[table].split())
            if "scope_id" in supported:
                supported.update({"project_id", "branch_id"})
            extra_columns = sorted(set(_columns(source_conn, table)) - supported)
            if extra_columns:
                unsupported_columns.append(
                    {
                        "table": table,
                        "key": "<schema>",
                        "reason": "unknown_legacy_columns_blocks_cutover",
                        "columns": extra_columns,
                        "auto_promoted": False,
                    }
                )
        if unsupported_columns:
            # Do not copy even a sanitized row: an unknown column can restrict
            # the row and every derived copy. Leave an existing target intact.
            report = {
                "format": REPORT_FORMAT,
                "baseline": LEGACY_BASELINE,
                "target_schema": SCHEMA_VERSION,
                "batch_key": batch_key,
                "completion_status": "blocked",
                "source_path_excluded": True,
                "credentials_exported": False,
                "target_content_written": False,
                "counts": {
                    "source_events": 0,
                    "deletion_operations": 0,
                    "episodes": 0,
                    "claims": 0,
                    "claim_versions": 0,
                    "fact_claims_mapped": 0,
                    "procedure_claims_mapped": 0,
                    "procedure_versions": 0,
                    "history_archived": 0,
                    "unmapped": len(unsupported_columns),
                },
                "unmapped": unsupported_columns,
                "cutover_block": {
                    "blocked": True,
                    "reasons": ["unknown_legacy_columns_blocks_cutover"],
                },
                "schema_inventory": {
                    "tables": sorted(tables),
                    "schema_gaps": unsupported_columns,
                },
                "source_status": [],
                "deletion_receipts": [],
                "permission_classification": {
                    "rows_with_explicit_scope_gap": 0,
                    "schema_permission_unknown": True,
                },
                "candidate_auto_promoted": False,
            }
            _write_report(report, report_path)
            return report
        journal_rows, memory_rows, links, episodes = (
            _rows(source_conn, name)
            for name in (
                "journal_entries",
                "memories",
                "memory_journal_sources",
                "task_episodes",
            )
        )
        fact_rows, fact_evidence = (
            _rows(source_conn, "fact_claims"),
            _rows(source_conn, "fact_claim_evidence"),
        )
        purge_rows, tombstones = (
            _rows(source_conn, "privacy_purge_operations"),
            _rows(source_conn, "privacy_purge_source_tombstones"),
        )
        target_tombstones = _rows(source_conn, "privacy_purge_tombstones")
        playbooks, playbook_versions = (
            _rows(source_conn, "procedural_playbooks"),
            _rows(source_conn, "playbook_versions"),
        )
        history_rows = {table: _rows(source_conn, table) for table in _HISTORY}
        digest_rows = {table: _rows(source_conn, table) for table in _DIGEST_TABLES}
        base_rows = (journal_rows, memory_rows, episodes, fact_rows, playbooks)
        base_missing_scope = any(
            row.get("scope_id") is None or row.get("scope_id") == ""
            for rows in base_rows
            for row in rows
        )
        direct_scope_rows = [
            *journal_rows,
            *memory_rows,
            *episodes,
            *fact_rows,
            *fact_evidence,
            *purge_rows,
            *tombstones,
            *target_tombstones,
            *playbooks,
            *playbook_versions,
            *(row for rows in history_rows.values() for row in rows),
        ]
        if handoff and handoff.get("archive_snapshot_hash"):
            source_scopes = frozenset(catalog["content_scopes"]) | frozenset(catalog["shared_only_scopes"]) | frozenset(catalog["audit_only_scopes"])
            if base_missing_scope:
                source_scopes = frozenset({*source_scopes, "legacy-scope"})
        else:
            source_scopes = frozenset(
                str(row.get("scope_id")) if row.get("scope_id") is not None and row.get("scope_id") != "" else "legacy-scope"
                for row in direct_scope_rows
                if (row.get("scope_id") is not None and row.get("scope_id") != "") or not direct_scope_rows
            )
            if base_missing_scope:
                source_scopes = frozenset({*source_scopes, "legacy-scope"})
            if not source_scopes:
                source_scopes = frozenset({"legacy-scope"})
        
        if explicit_scopes is not None:
            requested_explicit = frozenset(explicit_scopes)
            if not requested_explicit.issubset(source_scopes):
                raise MigrationError("explicit scope selection is incompatible with actual source scopes")
            requested_source = requested_explicit
        else:
            requested_source = source_scopes
            
        if not requested_source or any(item == "" for item in requested_source):
            raise MigrationError("no valid legacy scopes")
        if base_missing_scope and "legacy-scope" not in requested_source:
            raise MigrationError(
                "legacy default scope is outside explicit target binding"
            )
        if any(
            (str(row.get("scope_id")) if row.get("scope_id") is not None and row.get("scope_id") != "" else "legacy-scope") not in requested_source
            for row in direct_scope_rows
            if (row.get("scope_id") is not None and row.get("scope_id") != "") or not direct_scope_rows
        ):
            raise MigrationError("legacy scope is outside explicit target binding")
        applied_scope_map: Mapping[str, str] | None = source_scope_map
        if handoff is not None:
            if single_scope_to is not None:
                if len(requested_source) == 1:
                    applied_scope_map = {
                        next(iter(requested_source)): single_scope_to.strip()
                    }
                    handoff_issues = []
                    scope_mapping, resolved_issues = _resolve_scope_mapping(
                        requested_source,
                        frozenset(handoff["target_scope_ids"]),
                        audience_scopes,
                        applied_scope_map,
                    )
                else:
                    handoff_issues = [
                        {
                            "key": scope,
                            "target": single_scope_to.strip(),
                            "reason": "single_scope_to_requires_single_legacy_scope",
                            "auto_promoted": False,
                        }
                        for scope in sorted(requested_source)
                    ]
                    applied_scope_map = {}
                    scope_mapping, resolved_issues = {}, []
            else:
                scope_mapping, resolved_issues = _resolve_scope_mapping(
                    requested_source,
                    frozenset(handoff["target_scope_ids"]),
                    audience_scopes,
                    applied_scope_map,
                )
            handoff_issues.extend(resolved_issues)
            if handoff_issues:
                report = {
                    "format": REPORT_FORMAT,
                    "baseline": LEGACY_BASELINE,
                    "target_schema": SCHEMA_VERSION,
                    "batch_key": batch_key,
                    "completion_status": "blocked",
                    "source_path_excluded": True,
                    "credentials_exported": False,
                    "target_content_written": False,
                    "counts": {
                        "source_events": 0,
                        "deletion_operations": 0,
                        "episodes": 0,
                        "claims": 0,
                        "claim_versions": 0,
                        "fact_claims_mapped": 0,
                        "procedure_claims_mapped": 0,
                        "procedure_versions": 0,
                        "history_archived": 0,
                        "unmapped": len(handoff_issues),
                    },
                    "unmapped": handoff_issues,
                    "cutover_block": {
                        "blocked": True,
                        "reasons": sorted(
                            {str(item["reason"]) for item in handoff_issues}
                        ),
                    },
                    "installation_handoff": {
                        **handoff,
                        "source_scope_mapping": dict(applied_scope_map or {}),
                        "resolved_scope_mapping": scope_mapping,
                    },
                }
                _write_report(report, report_path)
                return report
            for rows in base_rows:
                rows[:] = _map_scope_rows(
                    rows, scope_mapping, default_source_scope="legacy-scope"
                )
            # Evidence/history rows without a direct scope inherit it from a
            # mapped parent below; rows that do have one are mapped here.
            for rows in (
                fact_evidence,
                purge_rows,
                tombstones,
                target_tombstones,
                playbook_versions,
            ):
                rows[:] = _map_scope_rows(rows, scope_mapping)
            for rows in history_rows.values():
                rows[:] = _map_scope_rows(rows, scope_mapping)
            for rows in digest_rows.values():
                rows[:] = _map_scope_rows(rows, scope_mapping)
            requested = frozenset(scope_mapping.values())
        else:
            requested = requested_source
        memory_authority = None
        if legacy_memory_reader_contract is not None:
            memory_authority = prepare_memory_storage_authority(
                source_conn,
                source_scope_map=scope_mapping if handoff else {scope: scope for scope in requested_source},
                bound_target_scopes=frozenset(handoff["target_scope_ids"]) if handoff else requested,
                verified_reader_contract=legacy_memory_reader_contract,
            )
        has_digest_rows = any(len(digest_rows.get(t, [])) > 0 for t in _DIGEST_TABLES)
        retention_scopes = (handoff.get("archive_retention_scopes") or {}) if handoff else {}
        _ORPHAN_BRIDGE_SCOPE = retention_scopes.get("orphan_bridge")
        _DIGEST_AUDIT_SCOPE = retention_scopes.get("digest_audit")
        
        if has_digest_rows:
            valid_retention = (
                handoff is not None and
                _ORPHAN_BRIDGE_SCOPE and
                _DIGEST_AUDIT_SCOPE and
                _ORPHAN_BRIDGE_SCOPE != _DIGEST_AUDIT_SCOPE and
                _ORPHAN_BRIDGE_SCOPE in handoff.get("archive_scopes", []) and
                _DIGEST_AUDIT_SCOPE in handoff.get("archive_scopes", [])
            )
            if not valid_retention:
                unmapped_digest_tables = [
                    {
                        "table": table,
                        "reason": "digest_table_requires_distinct_registered_archive_retention_scopes",
                        "auto_promoted": False,
                    }
                    for table in sorted(_DIGEST_TABLES)
                    if len(digest_rows.get(table, [])) > 0
                ]
                report = _blocked_prewrite_report(
                    batch_key=batch_key,
                    unmapped=unmapped_digest_tables,
                    reasons=["digest_table_requires_distinct_registered_archive_retention_scopes"],
                    extra={
                        "installation_handoff": {
                            **(handoff or {}),
                            "source_scope_mapping": dict(applied_scope_map or {}),
                            "resolved_scope_mapping": scope_mapping if "scope_mapping" in locals() else {},
                        },
                    },
                )
                _write_report(report, report_path)
                return report

        target_dir.mkdir(parents=True, exist_ok=True)
        existing = _existing_target_scopes(target_dir)
        if existing is not None:
            if handoff is not None:
                if not requested <= existing or existing != frozenset(
                    handoff["target_scope_ids"]
                ):
                    raise MigrationError(
                        "target scope binding differs; refusing mixed authority"
                    )
            elif existing != requested:
                raise MigrationError(
                    "target scope binding differs; refusing mixed authority"
                )

        report_rows: list[dict[str, Any]] = []
        unknown_tables: list[str] = []
        derived_tables: list[str] = []
        schema_gaps: list[dict[str, Any]] = []
        for table in sorted(tables - {"sqlite_sequence"}):
            missing = sorted(
                _REQUIRED.get(table, set()) - set(_columns(source_conn, table))
            )
            if missing:
                item = {
                    "table": table,
                    "key": "<schema>",
                    "reason": "legacy_schema_column_missing_blocks_cutover",
                    "missing_columns": missing,
                    "auto_promoted": False,
                }
                report_rows.append(item)
                schema_gaps.append(item)
            lower = table.lower()
            if table not in _KNOWN:
                if (
                    lower.endswith("_fts")
                    or "_fts_" in lower
                    or lower.startswith("vector_")
                    or lower.startswith("embedding_")
                    or lower.startswith("relation_")
                    or lower.startswith("lexical_")
                    or lower
                    in {
                        "memory_entities",
                        "memory_relations",
                        "memory_feedback",
                        "operator_operations",
                        "governance_audit_events",
                    }
                ):
                    derived_tables.append(table)
                else:
                    unknown_tables.append(table)
                    values = _rows(source_conn, table)
                    report_rows.append(
                        {
                            "table": table,
                            "key": "<table>",
                            "reason": "unknown_legacy_table_blocks_cutover",
                            "columns": _columns(source_conn, table),
                            "row_count": len(values),
                            "redacted_rows": [_safe(row) for row in values[:200]],
                            "truncated": len(values) > 200,
                            "auto_promoted": False,
                        }
                    )
        for table in sorted(_COMPAT & tables):
            reason = (
                "alias_or_reference_authority_not_losslessly_mapped"
                if table in {"aliases", "references"}
                else "attachment_metadata_or_bytes_not_losslessly_mapped"
                if table in {"artifacts", "artifact_versions"}
                else "fact_version_authority_not_losslessly_mapped"
            )
            for row in _rows(source_conn, table):
                report_rows.append(
                    {
                        "table": table,
                        "key": str(row.get("id") or row.get("claim_id") or "unknown"),
                        "reason": reason,
                        "row_digest": _digest(_safe(row)),
                        "redacted_row": _safe(row),
                        "status": str(row.get("status") or "unknown"),
                        "auto_promoted": False,
                    }
                )

        journal_refs: dict[str, str] = {}
        memory_refs: dict[str, str] = {}
        memory_items: dict[str, dict[str, Any]] = {}
        archives: dict[tuple[str, str], str] = {}
        sources: list[dict[str, Any]] = []
        redactions = 0
        permission_gaps = 0
        journals_by_id = {str(row.get("id")): row for row in journal_rows}  # noqa: F841 - retained source index for migration diagnostics
        memories_by_id = {str(row.get("id")): row for row in memory_rows}
        facts_by_id = {str(row.get("claim_id")): row for row in fact_rows}
        playbooks_by_id = {str(row.get("id")): row for row in playbooks}

        def add(
            table: str,
            row: dict[str, Any],
            content: str,
            kind: str,
            original: str,
            extra: dict[str, Any],
            scope: dict[str, Any],
            redacted: bool = False,
        ) -> dict[str, Any]:
            nonlocal redactions, permission_gaps
            redactions += int(redacted)
            permission_gaps += int(bool(scope["gap"]))
            item = _source(table, row, scope, content, kind, original, extra, redacted)
            sources.append(item)
            if scope["gap"]:
                report_rows.append(
                    {
                        "table": table,
                        "key": item["source_event_key"],
                        "reason": scope["gap"],
                        "scope_authorization": _safe(scope),
                        "auto_promoted": False,
                    }
                )
            return item

        for row in journal_rows:
            role, original = _role_origin(row.get("role"))
            content, changed = _safe_text(row.get("content"))
            item = add(
                "journal_entries",
                row,
                content,
                "raw_event",
                original,
                {
                    "role": role,
                    "turn_number": row.get("turn_number"),
                    "platform": row.get("platform"),
                    "user_id": row.get("user_id"),
                    "chat_id": row.get("chat_id"),
                    "thread_id": row.get("thread_id"),
                    "agent_identity": row.get("agent_identity"),
                    "agent_workspace": row.get("agent_workspace"),
                    "metadata": _metadata(row),
                },
                _scope(row),
                changed,
            )
            journal_refs[str(row.get("id"))] = item["event_id"]
        link_map: dict[str, list[str]] = defaultdict(list)
        for row in links:
            link_map[str(row.get("memory_id"))].append(str(row.get("journal_entry_id")))
        for row in memory_rows:
            content, changed1 = _safe_text(row.get("content") or row.get("summary"))
            summary, changed2 = _safe_text(row.get("summary"))
            meta = _metadata(row)
            ids = list(link_map.get(str(row.get("id")), []))
            ids.extend(
                str(x)
                for x in _json_list(meta.get("journal_entry_ids"))
                if str(x) not in ids
            )
            item = add(
                "memories",
                row,
                content,
                "durable_memory_evidence",
                "origin_unknown",
                {
                    "summary": summary,
                    "source": row.get("source") or "",
                    "target": row.get("target") or "",
                    "legacy_journal_ids": ids,
                    "legacy_metadata": meta,
                },
                resolve_memory_scope(row, _scope(row), authority=memory_authority)
                if memory_authority is not None else _scope(row),
                changed1 or changed2,
            )
            memory_refs[str(row.get("id"))] = item["event_id"]
            memory_items[str(row.get("id"))] = item
        for table, table_rows in (
            ("fact_claims", fact_rows),
            ("fact_claim_evidence", fact_evidence),
        ):
            for row in table_rows:
                identity = str(
                    (row.get("evidence_id") or row.get("claim_id") or "unknown")
                    if table == "fact_claim_evidence"
                    else (row.get("claim_id") or row.get("id") or "unknown")
                )
                scope_row = row
                if table == "fact_claim_evidence" and not row.get("scope_id"):
                    parent = next(
                        (
                            item
                            for item in fact_rows
                            if str(item.get("claim_id")) == str(row.get("claim_id"))
                        ),
                        {},
                    )
                    scope_row = {
                        **row,
                        "scope_id": parent.get("scope_id"),
                        "project_id": parent.get("project_id"),
                        "branch_id": parent.get("branch_id"),
                    }
                archive_row = {
                    **row,
                    "id": identity,
                    "project_id": scope_row.get("project_id"),
                    "branch_id": scope_row.get("branch_id"),
                }
                content, changed = _safe_text(_canon(_safe(row)))
                item = add(
                    table,
                    archive_row,
                    content,
                    "legacy_fact_record",
                    "origin_unknown",
                    {"legacy_row": _safe(row)},
                    _scope(scope_row),
                    changed,
                )
                archives[(table, identity)] = item["event_id"]
        for row in target_tombstones:
            identity = _digest(
                {
                    "operation_id": row.get("operation_id"),
                    "target_hash": row.get("target_hash"),
                    "content_hash": row.get("content_hash"),
                    "erased_at": row.get("erased_at"),
                }
            )
            report_rows.append(
                {
                    "table": "privacy_purge_tombstones",
                    "key": identity,
                    "reason": "unmapped_target_tombstone_blocks_cutover",
                    "redacted_row": _safe(row),
                    "auto_promoted": False,
                }
            )

        def inherited_scope(table: str, row: dict[str, Any]) -> dict[str, Any] | None:
            if row.get("scope_id"):
                return row
            parent: dict[str, Any] = {}
            if table in {"skill_anchors", "skill_conflicts"}:
                parent = playbooks_by_id.get(str(row.get("playbook_id")), {})
            elif table == "fact_claim_evidence":
                parent = facts_by_id.get(str(row.get("claim_id")), {})
            elif table == "fact_freshness":
                subject_type = str(row.get("subject_type") or "").lower()
                if subject_type in {"memory", "memories"}:
                    parent = memories_by_id.get(str(row.get("subject_id")), {})
                elif subject_type in {"fact", "fact_claim", "claim"}:
                    parent = facts_by_id.get(str(row.get("subject_id")), {})
            elif table == "playbook_versions":
                parent = playbooks_by_id.get(str(row.get("playbook_id")), {})
            if parent.get("scope_id"):
                return {
                    **row,
                    "scope_id": parent.get("scope_id"),
                    "project_id": parent.get("project_id"),
                    "branch_id": parent.get("branch_id"),
                }
            return None

        for table in sorted(_HISTORY | {"procedural_playbooks", "playbook_versions"}):
            table_rows = history_rows.get(
                table,
                playbooks
                if table == "procedural_playbooks"
                else playbook_versions
                if table == "playbook_versions"
                else [],
            )
            for row in table_rows:
                identity = str(
                    row.get("id")
                    or row.get("action_id")
                    or row.get("evidence_id")
                    or row.get("playbook_id")
                    or row.get("version")
                    or "unknown"
                )
                scope_row = inherited_scope(table, row)
                if scope_row is None:
                    report_rows.append(
                        {
                            "table": table,
                            "key": identity,
                            "reason": "history_scope_unresolved_blocks_cutover",
                            "redacted_row": _safe(row),
                            "auto_promoted": False,
                        }
                    )
                    continue
                content, changed = _safe_text(_canon(_safe(row)))
                item = add(
                    table,
                    {**scope_row, "id": identity},
                    content,
                    "legacy_history",
                    "origin_unknown",
                    {"legacy_row": _safe(row)},
                    _scope(scope_row),
                    changed,
                )
                archives[(table, identity)] = item["event_id"]
        
        retention_scopes = (handoff.get("archive_retention_scopes") or {}) if handoff else {}
        _ORPHAN_BRIDGE_SCOPE = retention_scopes.get("orphan_bridge")
        _DIGEST_AUDIT_SCOPE = retention_scopes.get("digest_audit")

        # event_sha256 hashes protocol body only (key/revision/content/times),
        # not source_group_key or segment_index; those stay writable after add().
        next_group_segment: dict[tuple[str, int], int] = {}
        for existing in sources:
            pair = (str(existing["source_group_key"]), int(existing["source_revision"]))
            used = int(existing["segment_index"])
            if used >= next_group_segment.get(pair, 0):
                next_group_segment[pair] = used + 1

        for table in sorted(_DIGEST_TABLES):
            table_rows = digest_rows.get(table, [])
            for row in table_rows:
                parent = {}
                parent_item = None
                if table == "memory_digest_sources":
                    mid = str(row.get("memory_id") or "")
                    rid = str(row.get("run_id") or "")
                    sid = str(row.get("session_id") or "")
                    identity = f"{len(mid)}:{mid}-{len(rid)}:{rid}-{len(sid)}:{sid}"
                    parent = memories_by_id.get(mid, {})
                    parent_item = memory_items.get(mid)
                    if parent_item and parent.get("scope_id"):
                        parent_event_id = parent_item["event_id"]
                        parent_source_group_key = parent_item["source_group_key"]
                        scope_row = {
                            **parent,
                            "id": identity,
                            "session_id": sid,
                            "created_at": row.get("created_at"),
                            "run_id": rid,
                            "message_ids": row.get("message_ids"),
                            "source_hash": row.get("source_hash"),
                            "__digest_parent_source_group_key": parent_source_group_key,
                        }
                    else:
                        scope_row = {**row, "scope_id": _ORPHAN_BRIDGE_SCOPE}
                else:
                    identity = str(row.get("id") or "unknown")
                    scope_row = {**row, "scope_id": _DIGEST_AUDIT_SCOPE}

                content, changed = _safe_text(_canon(_safe(row)))
                extra: dict[str, Any] = {
                    "legacy_row": _safe(row),
                    "source_kind": "legacy_audit",
                    "retention_namespace": "hermes_digest_archive",
                    "composite_id": identity,
                    "read_blocked": True,
                }
                if table == "memory_digest_sources":
                    extra["message_ids_namespace"] = "hermes_external_session_messages"
                    extra["source_hash_meaning"] = "sha1_of_candidate_content"
                    extra["attachment_resolution"] = "absent_memory" if not parent_item else "attached"
                    extra["evidence_resolution"] = "unresolved"
                    if parent_item and parent.get("scope_id"):
                        extra["legacy_parent_event_id"] = parent_item["event_id"]
                elif table == "nightly_digest_quarantine":
                    extra["retention_kind"] = "rejected_candidate_hash"
                elif table == "nightly_digest_runs":
                    extra["retention_kind"] = "run_metadata_path"
                    extra["deleted_counter_is_aggregate_only"] = True

                attached_parent = (
                    table == "memory_digest_sources"
                    and parent_item is not None
                    and parent.get("scope_id")
                )
                if attached_parent:
                    try:
                        parent_extra = json.loads(parent_item["extra_json"])
                        parent_auth = parent_extra.get("scope_authorization")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parent_auth = None
                    auth_scope = (
                        parent_auth if isinstance(parent_auth, dict) else _scope(parent)
                    )
                else:
                    auth_scope = _scope(scope_row)

                item = add(
                    table,
                    {**scope_row, "id": identity},
                    content,
                    "legacy_audit",
                    "origin_unknown",
                    extra,
                    auth_scope,
                    changed,
                )
                item["read_blocked"] = 1
                if attached_parent:
                    item["source_group_key"] = parent_item["source_group_key"]
                    item["scope_id"] = parent_item["scope_id"]
                    item["project_id"] = parent_item["project_id"]
                    item["branch_id"] = parent_item["branch_id"]
                    pair = (
                        str(parent_item["source_group_key"]),
                        int(parent_item["source_revision"]),
                    )
                    item["segment_index"] = next_group_segment.get(pair, 0)
                    next_group_segment[pair] = int(item["segment_index"]) + 1
                archives[(table, identity)] = item["event_id"]


        # A procedure and all its historical snapshots form one deletion
        # object. A deleted source cited by any snapshot affects the complete
        # archive group, including versions no longer used as current evidence.
        procedure_archive_groups: list[tuple[set[str], set[str]]] = []
        for playbook in playbooks:
            old_id = str(playbook.get("id") or "")
            snapshots = [
                row
                for row in playbook_versions
                if str(row.get("playbook_id")) == old_id
            ]
            group = (
                {archives[("procedural_playbooks", old_id)]}
                if ("procedural_playbooks", old_id) in archives
                else set()
            )
            dependencies: set[str] = set()
            records = [playbook]
            for snapshot in snapshots:
                archive_ref = archives.get(
                    ("playbook_versions", str(snapshot.get("id")))
                )
                if archive_ref:
                    group.add(archive_ref)
                data = _json(snapshot.get("snapshot"), {})
                if isinstance(data, dict):
                    records.append(data)
            for record in records:
                for anchor in _evidence_items(record.get("evidence_anchors")):
                    ref = _resolve(
                        anchor.get("source_ref")
                        or anchor.get("source_id")
                        or anchor.get("ref"),
                        anchor.get("source_type"),
                        journal_refs,
                        memory_refs,
                        archives,
                    )
                    if ref:
                        dependencies.add(ref)
            procedure_archive_groups.append((group, dependencies))

        evidence_by_claim: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in fact_evidence:
            evidence_by_claim[str(row.get("claim_id"))].append(row)
        episode_refs = {
            str(row.get("id")): _stable("episode", row.get("id")) for row in episodes
        }
        deletion_specs: list[dict[str, Any]] = []
        for row in purge_rows:
            op = str(row.get("operation_id") or "")
            ids = [
                str(item.get("journal_entry_id"))
                for item in tombstones
                if str(item.get("operation_id")) == op
            ]
            refs = {journal_refs[x] for x in ids if x in journal_refs}
            for episode in episodes:
                members = [
                    str(x) for x in _json_list(episode.get("journal_entry_ids"))
                ]
                if any(x in ids for x in members):
                    refs.add(episode_refs[str(episode.get("id"))])
            for memory in memory_rows:
                if (
                    any(x in ids for x in link_map.get(str(memory.get("id")), []))
                    and str(memory.get("id")) in memory_refs
                ):
                    refs.add(memory_refs[str(memory.get("id"))])
            previous_size = -1
            while len(refs) != previous_size:
                previous_size = len(refs)
                for evidence_row in fact_evidence:
                    resolved = _resolve(
                        evidence_row.get("source_ref"),
                        evidence_row.get("source_type"),
                        journal_refs,
                        memory_refs,
                        archives,
                    )
                    if resolved in refs:
                        claim_id = str(evidence_row.get("claim_id") or "")
                        evidence_id = str(evidence_row.get("evidence_id") or "")
                        if ("fact_claims", claim_id) in archives:
                            refs.add(archives[("fact_claims", claim_id)])
                        if ("fact_claim_evidence", evidence_id) in archives:
                            refs.add(archives[("fact_claim_evidence", evidence_id)])
                for group, dependencies in procedure_archive_groups:
                    if refs & (group | dependencies):
                        refs.update(group)
            if str(row.get("status") or "") != "completed":
                report_rows.append(
                    {
                        "table": "privacy_purge_operations",
                        "key": op,
                        "reason": "non_completed_tombstone_not_replayed_blocks_cutover",
                        "redacted_row": _safe(row),
                        "auto_promoted": False,
                    }
                )
                continue
            scopes = sorted(
                {
                    str(
                        next(
                            (
                                item.get("scope_id")
                                for item in journal_rows
                                if str(item.get("id")) == x
                            ),
                            sorted(requested)[0],
                        )
                    )
                    for x in ids
                }
            ) or [sorted(requested)[0]]
            deletion_specs.append(
                {
                    "operation_id": _stable("deletion", op),
                    "legacy_operation_id": op,
                    "refs": sorted(refs),
                    "scopes": scopes,
                    "created_at": _recorded(row.get("created_at")),
                }
            )
        for row in tombstones:
            if str(row.get("journal_entry_id")) not in journal_refs:
                report_rows.append(
                    {
                        "table": "privacy_purge_source_tombstones",
                        "key": str(row.get("journal_entry_id")),
                        "reason": "source_missing_unknown_blocks_cutover",
                        "redacted_row": _safe(row),
                        "auto_promoted": False,
                    }
                )

        if handoff is not None:
            binding = InstanceBinding(
                agent_id,
                installation_id,
                target_dir,
                frozenset(handoff["target_scope_ids"]),
                bool(handoff["test_mode"]),
            )
        else:
            binding = InstanceBinding(
                agent_id, installation_id, target_dir, requested, True
            )
        storage = SQLiteStorage(binding, timeout_seconds=3.0)
        storage.initialize()
        context = TrustedContext(
            binding, "p15-maintenance", requested, "host_generated"
        )
        inserted: defaultdict[str, int] = defaultdict(int)
        mapped_facts: set[str] = set()
        mapped_procedures: set[str] = set()
        fact_claim_refs: dict[str, str] = {}
        active_facts = proposed_facts = procedure_versions_count = 0
        with storage.write(context, remaining_seconds=30.0) as tx:
            conn = tx._check(write=True)
            fields = (
                "event_id",
                "source_event_key",
                "source_revision",
                "source_group_key",
                "segment_index",
                "segment_total",
                "scope_id",
                "session_id",
                "project_id",
                "branch_id",
                "origin",
                "role",
                "content",
                "content_sha256",
                "event_sha256",
                "occurred_at",
                "recorded_at",
                "persisted_at",
                "time_precision",
                "capture_state",
                "source_original_origin",
                "dataset_id",
                "import_provenance_sha256",
                "extra_json",
                "capture_gaps_json",
                "read_blocked",
                "suppressed",
            )
            for item in sources:
                conn.execute(
                    f"INSERT INTO source_events({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) ON CONFLICT(event_id,source_revision) DO NOTHING",
                    tuple(item[key] for key in fields),
                )
                current = conn.execute(
                    "SELECT source_event_key,content_sha256,scope_id FROM source_events WHERE event_id=?",
                    (item["event_id"],),
                ).fetchone()
                if current is None or str(current[2]) != str(item["scope_id"]):
                    raise MigrationError(
                        f"idempotence conflict: source scope {item['event_id']}"
                    )
                if tuple(current[:2]) != (
                    item["source_event_key"],
                    item["content_sha256"],
                ) and not str(current[0]).startswith("removed-"):
                    raise MigrationError(
                        f"idempotence conflict: source {item['event_id']}"
                    )
                inserted["source_events"] += 1
            for row in links:
                mr, jr = (
                    memory_refs.get(str(row.get("memory_id"))),
                    journal_refs.get(str(row.get("journal_entry_id"))),
                )
                if mr and jr:
                    conn.execute(
                        "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote,location) VALUES ('event',?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                        (
                            mr,
                            1,
                            jr,
                            1,
                            "derived_from",
                            "",
                            "legacy:memory_journal_sources",
                        ),
                    )
                    inserted["evidence_links"] += 1
            # Note: memory_digest_sources bridge events share the parent memory's
            # source_group_key (set during the digest table loop above).  Core's existing
            # source-group sibling closure in Deletions.closure() therefore fences the
            # bridge event automatically when the parent memory is deleted.
            # No event-to-event evidence_links are inserted: object_kind='event' is not
            # a durable object in OBJECT_TABLES and would reach DERIVATION_INVALID.

            membership_plan = None
            membership_audit = None
            if project_legacy_memberships:
                membership_plan = plan_legacy_episode_memberships({
                    episode_refs[str(row.get("id"))]: [
                        journal_refs[str(ref)]
                        for ref in _json_list(row.get("journal_entry_ids"))
                        if str(ref) in journal_refs
                    ]
                    for row in episodes
                })
            seq = int(
                conn.execute(
                    "SELECT COALESCE(max(sequence),0) FROM episode_events"
                ).fetchone()[0]
            )
            for row in episodes:
                eid, scope = episode_refs[str(row.get("id"))], _scope(row)
                project = (
                    str(row["project_id"])
                    if row.get("project_id") is not None
                    else None
                )
                branch = (
                    str(row["branch_id"]) if row.get("branch_id") is not None else None
                )
                ids = [str(x) for x in _json_list(row.get("journal_entry_ids"))]
                refs = [journal_refs[x] for x in ids if x in journal_refs]
                missing = [x for x in ids if x not in journal_refs]
                goal, _ = _safe_text(row.get("task_goal"))
                resume = {
                    "episode_ref": eid,
                    "goal": {"text": goal, "evidence_refs": refs[:32]},
                    "decisions": [],
                    "verified_progress": [],
                    "open_items": [],
                    "blockers": [f"source_missing:{x}" for x in missing],
                    "next_step": None,
                    "next_step_basis": "unknown",
                    "artifact_refs": [],
                    "source_watermark": _digest(
                        {"legacy_episode": str(row.get("id")), "batch_key": batch_key}
                    ),
                    "evidence_refs": refs[:32],
                    "legacy_fields": _safe(
                        {
                            key: row.get(key)
                            for key in (
                                "shared_scope_id",
                                "user_intent",
                                "message_ids",
                                "journal_entry_ids",
                                "tool_names",
                                "evidence",
                                "verification",
                                "environment",
                                "metadata",
                            )
                        }
                    ),
                    "scope_authorization": scope,
                }
                state = str(row.get("status") or "unknown").lower()
                state = (
                    state
                    if state
                    in {
                        "open",
                        "completed",
                        "failed",
                        "cancelled",
                        "interrupted",
                        "unknown",
                    }
                    else "unknown"
                )
                conn.execute(
                    "INSERT INTO episodes(episode_id,scope_id,project_id,branch_id,anchor_key,anchor_kind,series_key,segment_index,current_revision,read_blocked,suppressed) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(episode_id) DO NOTHING",
                    (
                        eid,
                        scope["row_scope_id"],
                        project,
                        branch,
                        f"legacy-task:{row.get('id')}",
                        "task",
                        f"legacy-series:{row.get('id')}",
                        0,
                        1,
                        0,
                        0,
                    ),
                )
                conn.execute(
                    "INSERT INTO episode_versions(episode_id,revision,state,resume_json,source_watermark,processed_sequence,recorded_at,environment_revision) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (
                        eid,
                        1,
                        state,
                        _canon(resume),
                        resume["source_watermark"],
                        0,
                        _recorded(row.get("started_at")),
                        None,
                    ),
                )
                for ref in refs:
                    if membership_plan is not None:
                        membership_plan.apply(conn, eid, ref)
                    elif (
                        conn.execute(
                            "SELECT 1 FROM episode_events WHERE episode_id=? AND source_ref=?",
                            (eid, ref),
                        ).fetchone()
                        is None
                    ):
                        seq += 1
                        conn.execute(
                            "INSERT INTO episode_events(sequence,episode_id,source_ref,source_revision,membership,environment_revision) VALUES (?,?,?,?,?,?)",
                            (seq, eid, ref, 1, "anchored", None),
                        )
                    conn.execute(
                        "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote) VALUES ('episode',?,?,?,?,?,'') ON CONFLICT DO NOTHING",
                        (eid, 1, ref, 1, "derived_from"),
                    )
                if missing:
                    report_rows.append(
                        {
                            "table": "task_episodes",
                            "key": str(row.get("id")),
                            "reason": "source_missing_unknown",
                            "missing": missing,
                            "auto_promoted": False,
                        }
                    )
                inserted["episodes"] += 1

            if membership_plan is not None:
                membership_audit = membership_plan.verify(conn)

            current_counts: defaultdict[tuple[Any, ...], int] = defaultdict(int)
            for row in fact_rows:
                if (
                    str(row.get("status") or "").lower() == "current"
                    and str(row.get("cardinality") or "single").lower() == "single"
                ):
                    current_counts[
                        (
                            row.get("scope_id"),
                            row.get("project_id"),
                            row.get("branch_id"),
                            row.get("subject_key"),
                            row.get("predicate_key"),
                            row.get("fact_key"),
                        )
                    ] += 1
            collisions = {key for key, count in current_counts.items() if count > 1}
            # A legacy fact_key is an identifier, not a Core semantic
            # condition.  Two single-valued legacy keys for the same natural
            # subject/predicate cannot be selected without an explicit
            # operator decision, so preserve both in the archive and block
            # cutover instead of merging them into one authority slot.
            natural_fact_keys: defaultdict[tuple[Any, ...], set[str]] = defaultdict(set)
            for candidate in fact_rows:
                if str(candidate.get("cardinality") or "single").lower() != "single":
                    continue
                candidate_subject, _ = _safe_text(candidate.get("subject_key"))
                candidate_predicate, _ = _safe_text(candidate.get("predicate_key"))
                candidate_key, _ = _safe_text(candidate.get("fact_key"))
                natural_fact_keys[
                    (
                        candidate.get("scope_id"),
                        candidate.get("project_id"),
                        candidate.get("branch_id"),
                        candidate_subject,
                        candidate_predicate,
                    )
                ].add(candidate_key)
            natural_fact_conflicts = {
                key for key, keys in natural_fact_keys.items() if len(keys) > 1
            }
            fact_history_groups: defaultdict[tuple[Any, ...], list[dict[str, Any]]] = (
                defaultdict(list)
            )
            for candidate in fact_rows:
                if str(candidate.get("cardinality") or "single").lower() != "single":
                    continue
                candidate_subject, _ = _safe_text(candidate.get("subject_key"))
                candidate_predicate, _ = _safe_text(candidate.get("predicate_key"))
                candidate_key, _ = _safe_text(candidate.get("fact_key"))
                fact_history_groups[
                    (
                        candidate.get("scope_id"),
                        candidate.get("project_id"),
                        candidate.get("branch_id"),
                        candidate_subject,
                        candidate_predicate,
                        candidate_key,
                    )
                ].append(candidate)
            unordered_current_slots: set[tuple[Any, ...]] = set()
            for key, candidates in fact_history_groups.items():
                current_rows = [
                    candidate
                    for candidate in candidates
                    if str(candidate.get("status") or "").lower() == "current"
                    and not candidate.get("retired_at")
                ]
                if len(current_rows) != 1:
                    continue
                current_recorded = _recorded(current_rows[0].get("recorded_at"))
                if any(
                    str(candidate.get("status") or "").lower() != "current"
                    and _recorded(candidate.get("recorded_at")) > current_recorded
                    for candidate in candidates
                ):
                    unordered_current_slots.add(key)
            preexisting_claim_ids = {
                str(item[0])
                for item in conn.execute("SELECT claim_id FROM claims").fetchall()
            }
            current_fact_revisions: dict[str, int] = {}
            for row in sorted(
                fact_rows,
                key=lambda x: (
                    _recorded(x.get("recorded_at")),
                    1 if str(x.get("status") or "").lower() == "current" else 0,
                    str(x.get("claim_id")),
                ),
            ):
                old_id = str(row.get("claim_id") or "")
                if not old_id:
                    report_rows.append(
                        {
                            "table": "fact_claims",
                            "key": "<missing>",
                            "reason": "missing_fact_claim_id",
                            "auto_promoted": False,
                        }
                    )
                    continue
                scope_id = str(row.get("scope_id") or "legacy-scope")
                project = (
                    str(row["project_id"])
                    if row.get("project_id") is not None
                    else None
                )
                branch = (
                    str(row["branch_id"]) if row.get("branch_id") is not None else None
                )
                subject, sr = _safe_text(row.get("subject_key"))
                predicate, pr = _safe_text(row.get("predicate_key"))
                value, vr = _safe_text(row.get("value") or row.get("normalized_value"))
                fact_key, fr = _safe_text(row.get("fact_key"))
                cardinality = str(row.get("cardinality") or "single").lower()
                base = (
                    row.get("scope_id"),
                    row.get("project_id"),
                    row.get("branch_id"),
                    row.get("subject_key"),
                    row.get("predicate_key"),
                    row.get("fact_key"),
                )
                natural_base = (
                    row.get("scope_id"),
                    row.get("project_id"),
                    row.get("branch_id"),
                    subject,
                    predicate,
                )
                conditions: list[str] = []
                fingerprint = str(row.get("value_fingerprint") or _digest(value))
                claim_id = _stable(
                    "claim",
                    f"{scope_id}:{project}:{branch}:fact:{subject}:{predicate}:{conditions}",
                )
                temporal_base = (
                    row.get("scope_id"),
                    row.get("project_id"),
                    row.get("branch_id"),
                    subject,
                    predicate,
                    fact_key,
                )
                if temporal_base in unordered_current_slots:
                    report_rows.append(
                        {
                            "table": "fact_claims",
                            "key": old_id,
                            "reason": "fact_current_not_latest_recorded_blocks_cutover",
                            "status": str(row.get("status") or "unknown"),
                            "archive_source_ref": archives.get(("fact_claims", old_id)),
                            "auto_promoted": False,
                        }
                    )
                    continue
                if natural_base in natural_fact_conflicts:
                    report_rows.append(
                        {
                            "table": "fact_claims",
                            "key": old_id,
                            "reason": "fact_slot_conflict_different_legacy_fact_key",
                            "status": str(row.get("status") or "unknown"),
                            "legacy_fact_key": fact_key,
                            "archive_source_ref": archives.get(("fact_claims", old_id)),
                            "auto_promoted": False,
                        }
                    )
                    continue
                # Existing claims at entry are from an earlier migration.  A
                # claim created earlier in this same pass must still receive
                # later legacy rows as revisions.  This distinction also
                # makes a post-delete rerun idempotent: scrubbed payloads are
                # not reconstructed from the immutable source snapshot.
                if claim_id in preexisting_claim_ids:
                    mapped_facts.add(old_id)
                    fact_claim_refs[old_id] = claim_id
                    continue
                if cardinality == "multi":
                    report_rows.append(
                        {
                            "table": "fact_claims",
                            "key": old_id,
                            "reason": "multi_value_fact_not_losslessly_mapped_to_core_slot",
                            "status": str(row.get("status") or "unknown"),
                            "archive_source_ref": archives.get(("fact_claims", old_id)),
                            "auto_promoted": False,
                        }
                    )
                    continue
                if base in collisions:
                    report_rows.append(
                        {
                            "table": "fact_claims",
                            "key": old_id,
                            "reason": "single_slot_collision_not_losslessly_mapped",
                            "status": str(row.get("status") or "unknown"),
                            "archive_source_ref": archives.get(("fact_claims", old_id)),
                            "auto_promoted": False,
                        }
                    )
                    continue
                evidence_rows = evidence_by_claim.get(old_id, [])
                evidence: list[tuple[str, str, str | None]] = []
                evidence_ok = False
                origin = ""
                for ev in evidence_rows:
                    ref = _resolve(
                        ev.get("source_ref"),
                        ev.get("source_type"),
                        journal_refs,
                        memory_refs,
                        archives,
                    )
                    quote, _ = _safe_text(ev.get("excerpt"))
                    item = next((s for s in sources if s["event_id"] == ref), None)
                    if (
                        ref
                        and item
                        and quote
                        and quote[:4096] in item["content"]
                        and item["scope_id"] == scope_id
                        and item.get("project_id") == project
                        and item.get("branch_id") == branch
                        and not item.get("scope_gap")
                    ):
                        evidence_ok = True
                        origin = item.get("source_original_origin") or ""
                        evidence.append(
                            (
                                ref,
                                quote,
                                str(ev.get("location"))
                                if ev.get("location") is not None
                                else None,
                            )
                        )
                old_status = str(row.get("status") or "unknown").lower()
                state = {
                    "superseded": "superseded",
                    "retracted": "retracted",
                    "uncertain": "disputed",
                    "current": "active",
                }.get(old_status, "proposed")
                reason = "legacy_status_preserved"
                if row.get("retired_at") and state == "active":
                    state, reason = "superseded", "legacy_retired_at_not_active"
                if state == "active" and not evidence_ok:
                    state, reason = (
                        "proposed",
                        "legacy_current_without_live_exact_evidence",
                    )
                payload = _claim_payload(
                    "fact",
                    subject,
                    predicate,
                    value,
                    conditions,
                    {
                        "claim_id": old_id,
                        "memory_id": row.get("memory_id"),
                        "fact_key": fact_key,
                        "normalized_value": row.get("normalized_value"),
                        "value_fingerprint": fingerprint,
                        "cardinality": cardinality,
                        "assertion_kind": row.get("assertion_kind"),
                        "status": old_status,
                        "retired_at": row.get("retired_at"),
                        "confidence": row.get("confidence"),
                        "superseded_by_claim_id": row.get("superseded_by_claim_id"),
                        "source_type": row.get("source_type"),
                        "source_ref": row.get("source_ref"),
                        "evidence_hash": row.get("evidence_hash"),
                        "evidence": _safe(evidence_rows),
                        "scope_authorization": _scope(row),
                        "valid_from": _time(row.get("valid_from"))[0],
                        "valid_to": _time(row.get("valid_to"))[0],
                    },
                )
                revision = _insert_version(
                    conn,
                    claim_id=claim_id,
                    scope_id=scope_id,
                    project_id=project,
                    branch_id=branch,
                    payload=payload,
                    state=state,
                    basis=_claim_basis(row.get("assertion_kind"), origin),
                    reason=reason,
                    recorded=_recorded(row.get("recorded_at")),
                    valid_from=payload["valid_from"],
                    valid_to=payload["valid_to"],
                    evidence=evidence,
                    counts=inserted,
                )
                archive_ref = archives.get(("fact_claims", old_id))
                if archive_ref:
                    conn.execute(
                        "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote) VALUES ('claim',?,?,?,?,?,'') ON CONFLICT DO NOTHING",
                        (claim_id, revision, archive_ref, 1, "derived_from"),
                    )
                    inserted["history_links"] += 1
                mapped_facts.add(old_id)
                fact_claim_refs[old_id] = claim_id
                active_facts += int(state == "active")
                proposed_facts += int(state != "active")
                redactions += int(sr or pr or vr or fr)
                if old_status == "current":
                    current_fact_revisions[claim_id] = int(
                        conn.execute(
                            "SELECT current_revision FROM claims WHERE claim_id=?",
                            (claim_id,),
                        ).fetchone()[0]
                    )
                if old_status == "current" and state != "active":
                    report_rows.append(
                        {
                            "table": "fact_claims",
                            "key": old_id,
                            "reason": "current_fact_without_live_exact_evidence",
                            "claim_ref": claim_id,
                            "auto_promoted": False,
                        }
                    )

            # Legacy timestamps are not authoritative ordering when a source
            # repaired a row in place.  The explicit current marker selects
            # the head after the complete history has been appended.
            for claim_id, revision in current_fact_revisions.items():
                conn.execute(
                    "UPDATE claims SET current_revision=? WHERE claim_id=?",
                    (revision, claim_id),
                )

            versions_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in playbook_versions:
                versions_by[str(row.get("playbook_id"))].append(row)
            procedure_collisions = _procedure_slot_collisions(playbooks, versions_by)
            for playbook in playbooks:
                old_id = str(playbook.get("id") or "")
                if not old_id:
                    continue
                if old_id in procedure_collisions:
                    # All originals and snapshots were already imported as
                    # same-scope legacy_history source_events, with lexical
                    # terms and original identities/evidence in legacy_row.
                    # Do not silently turn unrelated playbooks into revisions.
                    report_rows.append({
                        "table": "procedural_playbooks", "key": old_id,
                        "reason": "procedure_slot_conflict_different_legacy_playbook",
                        "archive_source_ref": archives.get(("procedural_playbooks", old_id)),
                        "version_source_refs": [archives[("playbook_versions", str(snap.get("id")))]
                                                for snap in versions_by.get(old_id, [])
                                                if ("playbook_versions", str(snap.get("id"))) in archives],
                        "conflicting_playbook_ids": list(procedure_collisions[old_id]),
                        "preservation": "same_scope_sqlite_history_sources",
                        "automatic_procedure_eligible": False,
                        "auto_promoted": False,
                    })
                    continue
                procedure_claim_id = _procedure_claim_id(old_id)
                snapshots = versions_by.get(old_id) or [
                    {
                        "version": 1,
                        "snapshot": _canon(playbook),
                        "created_at": playbook.get("created_at"),
                    }
                ]
                prepared: list[
                    tuple[
                        dict[str, Any],
                        dict[str, Any],
                        str,
                        str,
                        str,
                        str,
                        list[str],
                        list[str],
                        list[str],
                    ]
                ] = []
                procedure_scope = _scope(playbook)
                procedure_project = (
                    str(playbook["project_id"])
                    if playbook.get("project_id") is not None
                    else None
                )
                procedure_branch = (
                    str(playbook["branch_id"])
                    if playbook.get("branch_id") is not None
                    else None
                )
                for snap in sorted(
                    snapshots,
                    key=lambda x: (
                        int(x.get("version") or 0),
                        str(x.get("created_at") or ""),
                    ),
                ):
                    data = _json(snap.get("snapshot"), playbook)
                    data = data if isinstance(data, dict) else playbook
                    title, _ = _safe_text(
                        data.get("title") or playbook.get("title") or old_id
                    )
                    goal, _ = _safe_text(data.get("goal") or playbook.get("goal"))
                    status = str(
                        data.get("status") or playbook.get("status") or "candidate"
                    ).lower()
                    method = _safe_list(data.get("steps"))
                    non_applicable = _safe_list(data.get("pitfalls"))
                    semantic_conditions = _safe_list(data.get("preconditions"))
                    prepared.append(
                        (
                            snap,
                            data,
                            title or "procedure",
                            goal,
                            status,
                            str(playbook.get("task_class") or old_id),
                            semantic_conditions,
                            method,
                            non_applicable,
                        )
                    )
                slots = {
                    (
                        procedure_scope["row_scope_id"],
                        procedure_project,
                        procedure_branch,
                        item[5],
                        item[2],
                        tuple(sorted(set(item[6]))),
                    )
                    for item in prepared
                }
                if len(slots) > 1:
                    for (
                        snap,
                        _data,
                        _title,
                        _goal,
                        _status,
                        _subject,
                        _conditions,
                        _method,
                        _non_applicable,
                    ) in prepared:
                        report_rows.append(
                            {
                                "table": "playbook_versions"
                                if versions_by.get(old_id)
                                else "procedural_playbooks",
                                "key": str(snap.get("id") or old_id),
                                "reason": "procedure_version_slot_changed_blocks_cutover",
                                "archive_source_ref": archives.get(
                                    ("playbook_versions", str(snap.get("id")))
                                ),
                                "auto_promoted": False,
                            }
                        )
                    continue
                if procedure_claim_id in preexisting_claim_ids:
                    mapped_procedures.add(old_id)
                    continue
                for (
                    snap,
                    data,
                    title,
                    goal,
                    status,
                    subject,
                    semantic_conditions,
                    method,
                    non_applicable,
                ) in prepared:
                    evidence: list[tuple[str, str, str | None]] = []
                    for anchor in _evidence_items(
                        data.get("evidence_anchors") or playbook.get("evidence_anchors")
                    ):
                        ref = _resolve(
                            anchor.get("source_ref")
                            or anchor.get("source_id")
                            or anchor.get("ref"),
                            "",
                            journal_refs,
                            memory_refs,
                            archives,
                        )
                        quote, _ = _safe_text(
                            anchor.get("quote") or anchor.get("excerpt")
                        )
                        item = next((s for s in sources if s["event_id"] == ref), None)
                        if (
                            ref
                            and item
                            and quote
                            and quote[:4096] in item["content"]
                            and item["scope_id"] == procedure_scope["row_scope_id"]
                            and item.get("project_id") == procedure_project
                            and item.get("branch_id") == procedure_branch
                            and not item.get("scope_gap")
                        ):
                            evidence.append((ref, quote, None))
                    procedure = {
                        "conditions": semantic_conditions,
                        "non_applicable": non_applicable,
                        "method": method,
                        "verification_basis": "user_accepted"
                        if status == "promoted"
                        else "inferred_suggestion",
                        "counterexample_refs": [],
                    }
                    payload = _claim_payload(
                        "procedure",
                        subject,
                        title,
                        goal,
                        semantic_conditions,
                        {
                            "playbook_id": old_id,
                            "version": snap.get("version"),
                            "status": status,
                            "snapshot": _safe(data),
                            "scope_authorization": procedure_scope,
                        },
                        procedure,
                    )
                    active = status == "promoted" and bool(evidence)
                    revision = _insert_version(
                        conn,
                        claim_id=procedure_claim_id,
                        scope_id=procedure_scope["row_scope_id"],
                        project_id=procedure_project,
                        branch_id=procedure_branch,
                        payload=payload,
                        state="active" if active else "proposed",
                        basis="observed" if active else "inferred_suggestion",
                        reason="legacy_promoted_with_exact_evidence"
                        if active
                        else "legacy_promoted_without_same_scope_exact_evidence"
                        if status == "promoted"
                        else "legacy_candidate_or_missing_exact_evidence",
                        recorded=_recorded(
                            snap.get("created_at") or playbook.get("updated_at")
                        ),
                        valid_from=None,
                        valid_to=None,
                        evidence=evidence,
                        counts=inserted,
                    )
                    archive_ref = archives.get(
                        ("playbook_versions", str(snap.get("id")))
                    ) or archives.get(("procedural_playbooks", old_id))
                    if archive_ref:
                        conn.execute(
                            "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote) VALUES ('claim',?,?,?,?,?,'') ON CONFLICT DO NOTHING",
                            (
                                procedure_claim_id,
                                revision,
                                archive_ref,
                                1,
                                "derived_from",
                            ),
                        )
                        inserted["history_links"] += 1
                    procedure_versions_count += 1
                    if status == "promoted" and not active:
                        report_rows.append(
                            {
                                "table": "procedural_playbooks",
                                "key": old_id,
                                "reason": "promoted_procedure_without_live_exact_evidence",
                                "auto_promoted": False,
                            }
                        )
                mapped_procedures.add(old_id)

            # Old action receipts and experience records remain historical
            # imported sources, but their exact relationship to the new
            # authority is retained where the old IDs make it unambiguous.
            def link_history(
                target_kind: str, target_ref: str, target_revision: int, source_ref: str
            ) -> None:
                conn.execute(
                    "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote) VALUES (?,?,?,?,?,'derived_from','') ON CONFLICT DO NOTHING",
                    (target_kind, target_ref, target_revision, source_ref, 1),
                )
                inserted["history_links"] += 1

            for row in _rows(source_conn, "fact_action_receipts"):
                source_ref = archives.get(
                    ("fact_action_receipts", str(row.get("action_id")))
                )
                if not source_ref:
                    continue
                raw = str(row.get("receipt_json") or "")
                for old_id, claim_ref in fact_claim_refs.items():
                    if old_id in raw:
                        revision = int(
                            conn.execute(
                                "SELECT current_revision FROM claims WHERE claim_id=?",
                                (claim_ref,),
                            ).fetchone()[0]
                        )
                        link_history("claim", claim_ref, revision, source_ref)
            for table in (
                "experience_runs",
                "reflection_events",
                "skill_anchors",
                "skill_conflicts",
            ):
                for row in _rows(source_conn, table):
                    identity = str(row.get("id") or "unknown")
                    source_ref = archives.get((table, identity))
                    if not source_ref:
                        continue
                    playbook_id = str(row.get("playbook_id") or "")
                    if playbook_id and playbook_id in mapped_procedures:
                        claim_ref = _procedure_claim_id(playbook_id)
                        revision = int(
                            conn.execute(
                                "SELECT current_revision FROM claims WHERE claim_id=?",
                                (claim_ref,),
                            ).fetchone()[0]
                        )
                        link_history("claim", claim_ref, revision, source_ref)
                    episode_id = str(row.get("episode_id") or "")
                    if episode_id in episode_refs:
                        link_history("episode", episode_refs[episode_id], 1, source_ref)

            for spec in deletion_specs:
                op_id, request = (
                    spec["operation_id"],
                    {
                        "legacy_operation_id": spec["legacy_operation_id"],
                        "batch_key": batch_key,
                        "refs": spec["refs"],
                    },
                )
                existing_op = conn.execute(
                    "SELECT memory_epoch FROM deletion_operations WHERE operation_id=?",
                    (op_id,),
                ).fetchone()
                if existing_op is None:
                    epoch = (
                        int(
                            conn.execute(
                                "SELECT memory_epoch FROM instance_meta WHERE singleton=1"
                            ).fetchone()[0]
                        )
                        + 1
                    )
                    layers = {
                        "sqlite_active": "pending",
                        "vector_active": "pending",
                        "attachments": "pending",
                        "historical_storage": "pending",
                    }
                    conn.execute(
                        "INSERT INTO deletion_operations(operation_id,request_sha256,mode,scope_ids_json,project_id,branch_id,requested_refs_json,expected_revisions_json,created_at,memory_epoch,layers_json,active_content_removed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            op_id,
                            _digest(request),
                            "delete",
                            _canon(spec["scopes"]),
                            None,
                            None,
                            _canon(spec["refs"]),
                            _canon({x: 1 for x in spec["refs"]}),
                            spec["created_at"],
                            epoch,
                            _canon(layers),
                            0,
                        ),
                    )
                    conn.execute(
                        "UPDATE instance_meta SET memory_epoch=max(memory_epoch,?) WHERE singleton=1",
                        (epoch,),
                    )
                for ref in spec["refs"]:
                    obj = (
                        conn.execute(
                            "SELECT scope_id,project_id,branch_id FROM source_events WHERE event_id=?",
                            (ref,),
                        ).fetchone()
                        or conn.execute(
                            "SELECT scope_id,project_id,branch_id FROM episodes WHERE episode_id=?",
                            (ref,),
                        ).fetchone()
                    )
                    if obj is None:
                        continue
                    kind = (
                        "episode"
                        if conn.execute(
                            "SELECT 1 FROM episodes WHERE episode_id=?", (ref,)
                        ).fetchone()
                        else "event"
                    )
                    conn.execute(
                        "INSERT INTO deletion_members(operation_id,object_kind,object_ref) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                        (op_id, kind, ref),
                    )
                    conn.execute(
                        "INSERT INTO object_blocks(object_kind,object_ref,scope_id,project_id,branch_id,read_blocked,suppressed,operation_id) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(object_kind,object_ref) DO UPDATE SET read_blocked=1,suppressed=1,operation_id=excluded.operation_id",
                        (kind, ref, obj[0], obj[1], obj[2], 1, 1, op_id),
                    )
                    conn.execute(
                        f"UPDATE {'episodes' if kind == 'episode' else 'source_events'} SET read_blocked=1,suppressed=1 WHERE {'episode_id' if kind == 'episode' else 'event_id'}=?",
                        (ref,),
                    )
                for row in conn.execute(
                    "SELECT DISTINCT object_ref FROM evidence_links WHERE object_kind='claim' AND source_ref IN (SELECT object_ref FROM deletion_members WHERE operation_id=? AND object_kind='event')",
                    (op_id,),
                ).fetchall():
                    claim = conn.execute(
                        "SELECT scope_id,project_id,branch_id FROM claims WHERE claim_id=?",
                        (row[0],),
                    ).fetchone()
                    if claim:
                        conn.execute(
                            "INSERT INTO deletion_members(operation_id,object_kind,object_ref) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                            (op_id, "claim", row[0]),
                        )
                        conn.execute(
                            "INSERT INTO object_blocks(object_kind,object_ref,scope_id,project_id,branch_id,read_blocked,suppressed,operation_id) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(object_kind,object_ref) DO UPDATE SET read_blocked=1,suppressed=1,operation_id=excluded.operation_id",
                            (
                                "claim",
                                row[0],
                                claim[0],
                                claim[1],
                                claim[2],
                                1,
                                1,
                                op_id,
                            ),
                        )
                        conn.execute(
                            "UPDATE claims SET read_blocked=1,suppressed=1 WHERE claim_id=?",
                            (row[0],),
                        )
                tx.deletions.purge_sqlite(op_id)
            for item in sources:
                if conn.execute(
                    "SELECT read_blocked FROM source_events WHERE event_id=?",
                    (item["event_id"],),
                ).fetchone()[0]:
                    continue
                for term in lexical_terms(item["content"]):
                    conn.execute(
                        "INSERT INTO lexical_projection(term,event_id,source_revision) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                        (term, item["event_id"], 1),
                    )
                    inserted["lexical_projection"] += 1

            # Preserve the old ordinary-recall lifecycle policy using the same
            # Core suppression/group/dependency mechanism as runtime governance.
            # Run after all sources and derived links exist, before commit.
            from .legacy_lifecycle import apply_lifecycle_suppression
            lifecycle_suppression = apply_lifecycle_suppression(
                tx,
                [row for row in memory_rows if str(row.get("id")) in memory_refs],
                now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )

        with storage.read(context) as tx:
            conn = tx._check()
            source_status = [
                {
                    "read_blocked": int(x[0]),
                    "capture_state": str(x[1]),
                    "count": int(x[2]),
                }
                for x in conn.execute(
                    "SELECT read_blocked,capture_state,count(*) FROM source_events GROUP BY read_blocked,capture_state"
                )
            ]
            deletion_receipts = [
                {
                    "operation_id": str(x[0]),
                    "memory_epoch": int(x[1]),
                    "active_content_removed": int(x[2]),
                    "layers": json.loads(x[3]),
                    "member_count": int(x[4]),
                }
                for x in conn.execute(
                    "SELECT d.operation_id,d.memory_epoch,d.active_content_removed,d.layers_json,(SELECT count(*) FROM deletion_members m WHERE m.operation_id=d.operation_id) FROM deletion_operations d"
                )
            ]
            claim_count = int(conn.execute("SELECT count(*) FROM claims").fetchone()[0])
            version_count = int(
                conn.execute("SELECT count(*) FROM claim_versions").fetchone()[0]
            )
            procedure_versions_count = int(
                conn.execute(
                    "SELECT count(*) FROM claim_versions v JOIN claims c USING(claim_id) WHERE c.kind='procedure'"
                ).fetchone()[0]
            )
            authority = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT v.state,count(*) FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.kind='fact' GROUP BY v.state"
                )
            }
        hard = {
            str(item.get("reason"))
            for item in report_rows
            if str(item.get("reason"))
            in {
                "unknown_legacy_table_blocks_cutover",
                "legacy_schema_column_missing_blocks_cutover",
                "source_missing_unknown_blocks_cutover",
                "non_completed_tombstone_not_replayed_blocks_cutover",
                "unmapped_target_tombstone_blocks_cutover",
                "history_scope_unresolved_blocks_cutover",
                "explicit_local_scope_mismatch",
                "explicit_shared_scope_mismatch",
                "explicit_shared_pool_scope_mismatch",
                "unsupported_scope_mode",
                "legacy_metadata_not_json_object",
                "legacy_memory_scope_not_bound",
                "promoted_procedure_without_live_exact_evidence",
                "multi_value_fact_not_losslessly_mapped_to_core_slot",
                "single_slot_collision_not_losslessly_mapped",
                "fact_slot_conflict_different_legacy_fact_key",
                "fact_current_not_latest_recorded_blocks_cutover",
                "procedure_version_slot_changed_blocks_cutover",
                "procedure_slot_conflict_different_legacy_playbook",
            }
        }
        report = {
            "format": REPORT_FORMAT,
            "baseline": LEGACY_BASELINE,
            "target_schema": SCHEMA_VERSION,
            "batch_key": batch_key,
            "source_path_excluded": True,
            "credentials_exported": False,
            "completion_status": "blocked" if hard else "complete",
            "counts": {
                "source_events": len(sources),
                "deletion_operations": len(deletion_receipts),
                "episodes": len(episodes),
                "claims": claim_count,
                "claim_versions": version_count,
                "fact_claims_mapped": len(mapped_facts),
                "procedure_claims_mapped": len(mapped_procedures),
                "procedure_versions": procedure_versions_count,
                "history_archived": sum(
                    len(_rows(source_conn, table)) for table in _HISTORY
                ),
                "unmapped": len(report_rows),
            },
            "fact_authority": {
                "active": authority.get("active", 0),
                "proposed_or_disputed": sum(
                    value for state, value in authority.items() if state != "active"
                ),
                "slot_encoding": "semantic conditions only; legacy fact_key and value_fingerprint remain in sanitized source archives linked to claim revisions; conflicts remain archived with explicit incomplete conversion",
            },
            "source_status": source_status,
            "deletion_receipts": deletion_receipts,
            "insert_attempts": dict(inserted),
            "schema_inventory": {
                "tables": sorted(tables),
                "unknown_tables": unknown_tables,
                "derived_tables": sorted(derived_tables),
                "schema_gaps": schema_gaps,
            },
            "unmapped": report_rows,
            "cutover_block": {"blocked": bool(hard), "reasons": sorted(hard)},
            "permission_classification": {
                "rows_with_explicit_scope_gap": permission_gaps,
                "shared_scope_id_is_not_blanket_block": True,
            },
            "redaction": {
                "pipeline": "scope_recall.capture_filters.sanitize_report_text/sanitize_structured_value",
                "redacted_records": redactions,
            },
            "rollback_boundary": "new sources/deletions/corrections not representable in 578b require Core restore-required stop-write protection; old snapshot is never overwritten",
            "candidate_auto_promoted": False,
            "vectors": "not imported; rebuildable",
            "old_governance_engines": "sanitized imported history only; not executable in new Core",
        }
        if legacy_memory_reader_contract is not None:
            report["legacy_memory_reader_contract"] = legacy_memory_reader_contract
        report["legacy_lifecycle_suppression"] = lifecycle_suppression
        if bridge_receipt is not None:
            report["completed_bridge_audit"] = bridge_receipt
        if import_ledger_receipt is not None:
            report["import_ledger_audit"] = import_ledger_receipt
        if membership_audit is not None:
            report["legacy_episode_memberships"] = membership_audit
        report["legacy_procedure_collisions"] = {
            "group_count": len(set(procedure_collisions.values())),
            "playbook_count": len(procedure_collisions),
            "version_count": sum(len(versions_by.get(old_id, [])) for old_id in procedure_collisions),
            "preservation": "same_scope_sqlite_history_sources",
            "automatic_procedure_eligible": False,
            "auto_promoted": False,
        }
        if handoff is not None:
            report["installation_handoff"] = {
                **handoff,
                "source_scope_mapping": dict(applied_scope_map or {}),
                "resolved_scope_mapping": scope_mapping,
            }
            if handoff.get("archive_snapshot_hash"):
                report["archive_retention_summary"] = {
                    "orphan_bridge_scope": _ORPHAN_BRIDGE_SCOPE,
                    "digest_audit_scope": _DIGEST_AUDIT_SCOPE,
                }
                report["legacy_catalog_summary"] = {
                    "content_scopes": catalog["content_scopes"],
                    "shared_only_scopes": catalog["shared_only_scopes"],
                    "audit_only_scopes": catalog["audit_only_scopes"],
                    "audit_sentinels": catalog["audit_sentinels"],
                    "table_dispositions": catalog["table_dispositions"],
                    "table_row_counts": catalog["table_row_counts"],
                    "direct_scope_count": catalog["direct_scope_count"],
                    "total_nonempty_raw_values": catalog["total_nonempty_raw_values"],
                }
        _write_report(report, report_path)
        return report
    finally:
        source_conn.close()


