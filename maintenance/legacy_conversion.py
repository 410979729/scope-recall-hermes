"""Offline legacy-to-Core conversion and its single write transaction.

Retains status, exact evidence, scope and deletion semantics. No host lifecycle
or model work; reports gate activation and derived indexes remain rebuildable.

The pipeline is ``migrate_legacy`` -> ``_prepare`` -> ``_convert``; each stage
below reads and extends one ``Conversion``. Any pre-write stage may raise
``Blocked`` with its report instead of writing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core.events import lexical_terms
from scope_recall.core.schema import SCHEMA_VERSION, normalize_scope_authorizations
from scope_recall.core.storage import SQLiteStorage
from scope_recall.maintenance.legacy_episode_membership import plan_legacy_episode_memberships
from scope_recall.maintenance.legacy_v2_compat import (
    BRIDGE_TABLE, IMPORT_LEDGER_TABLE, LegacyCompatibilityError,
    prepare_memory_storage_authority, verify_completed_bridge_archive, verify_import_ledger_archive,
)

from .backup import _safe_path
from .legacy_catalog import (
    _COMPAT, _DERIVED_NAMES, _DIGEST_TABLES, _HISTORY, _KNOWN, _REQUIRED,
    _is_derived_index, _offline_source_path, build_legacy_catalog,
)
from .legacy_claims import link_history_records, versions_by_playbook, write_fact_claims, write_procedures
from .legacy_deletions import plan_deletions, write_deletions
from .legacy_lifecycle import apply_lifecycle_suppression
from .legacy_plan import Blocked, Conversion, Row
from .legacy_sources import (
    SOURCE_EVENT_FIELDS, _json_list, _map_scope_rows, _safe, _safe_text, _scope, _scope_value,
    _text_or_none, archive_sources,
)
from .migration_activation import _existing_target_scopes, _load_installation_handoff, _resolve_scope_mapping
from .migration_records import (
    LEGACY_BASELINE, REPORT_FORMAT, MigrationError, _blocked_prewrite_report, _blocked_report,
    _canon, _columns, _digest, _materialize_explicit_scope_selection, _open_immutable,
    _recorded, _rows, _tables, _write_report,
)

# Tables whose rows own a scope (an empty one means the legacy default), and
# tables whose rows inherit a parent's scope when theirs is empty.
_SCOPE_OWNING = ("journal_entries", "memories", "task_episodes", "fact_claims", "procedural_playbooks")
_SCOPE_INHERITING = (
    "fact_claim_evidence",
    "privacy_purge_operations",
    "privacy_purge_source_tombstones",
    "privacy_purge_tombstones",
    "playbook_versions",
    *sorted(_HISTORY),
)
_LEGACY_TABLES = ("memory_journal_sources", *_SCOPE_OWNING, *_SCOPE_INHERITING, *sorted(_DIGEST_TABLES))
_ARCHIVE_MANIFEST_KEYS = (
    "archive_snapshot_hash", "archive_catalog_hash", "archive_source_map", "archive_retention_scopes", "archive_scopes",
)
_CONVERSION_DERIVED_NAMES = _DERIVED_NAMES | {"governance_audit_events"}
_COMPAT_REASONS = {
    **{table: "fact_version_authority_not_losslessly_mapped" for table in _COMPAT},
    "aliases": "alias_or_reference_authority_not_losslessly_mapped",
    "references": "alias_or_reference_authority_not_losslessly_mapped",
    "artifacts": "attachment_metadata_or_bytes_not_losslessly_mapped",
    "artifact_versions": "attachment_metadata_or_bytes_not_losslessly_mapped",
}
_EPISODE_STATES = {"open", "completed", "failed", "cancelled", "interrupted", "unknown"}
_EPISODE_LEGACY_FIELDS = (
    "shared_scope_id", "user_intent", "message_ids", "journal_entry_ids", "tool_names",
    "evidence", "verification", "environment", "metadata",
)
_DIGEST_RETENTION_REASON = "digest_table_requires_distinct_registered_archive_retention_scopes"
# Report reasons that block cutover; everything else is an audited gap.
_BLOCKING_REASONS = frozenset({
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
})


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
    try:
        conversion = _prepare(
            source, target_directory, agent_id=agent_id, installation_id=installation_id,
            scope_ids=scope_ids, batch_key=batch_key, report_path=report_path,
            installation_manifest=installation_manifest, host=host, source_scope_map=source_scope_map,
            single_scope_to=single_scope_to, legacy_memory_reader_contract=legacy_memory_reader_contract,
            completed_bridge_archive_path=completed_bridge_archive_path,
            import_ledger_archive_path=import_ledger_archive_path,
            project_legacy_memberships=project_legacy_memberships,
        )
        report = _convert(conversion)
    except Blocked as blocked:
        report = blocked.report
    _write_report(report, report_path)
    return report


# --- preflight -------------------------------------------------------------

def _prepare(
    source: str | Path,
    target_directory: str | Path | None,
    *,
    agent_id: str,
    installation_id: str,
    scope_ids: Iterable[str] | None,
    batch_key: str,
    report_path: str | Path | None,
    installation_manifest: str | Path | None,
    host: str | None,
    source_scope_map: Mapping[str, str] | None,
    single_scope_to: str | None,
    legacy_memory_reader_contract: str | None,
    completed_bridge_archive_path: str | Path | None,
    import_ledger_archive_path: str | Path | None,
    project_legacy_memberships: bool,
) -> Conversion:
    """Validate the request and bind it to a trusted target before reading rows."""
    if single_scope_to is not None:
        if installation_manifest is None:
            raise MigrationError("single_scope_to requires an installation manifest")
        if type(single_scope_to) is not str or not single_scope_to.strip():
            raise MigrationError("single_scope_to must be a non-empty audience name or scope ID")
        if source_scope_map is not None:
            raise MigrationError("single_scope_to cannot be combined with source_scope_map")
    explicit_scopes = _materialize_explicit_scope_selection(scope_ids)
    handoff: dict[str, Any] | None = None
    audience_scopes: dict[str, str] = {}
    if installation_manifest is not None:
        handoff, audience_scopes, target_dir = _trusted_handoff(installation_manifest, host, target_directory)
        agent_id, installation_id = handoff["agent_id"], handoff["installation_id"]
    elif target_directory is None:
        raise MigrationError("target directory or installation manifest is required")
    else:
        target_dir = _safe_path(target_directory, error_type=MigrationError)
    source_path = _offline_source_path(source)
    if report_path is not None:
        _safe_path(report_path, error_type=MigrationError)
    if source_path.is_symlink() or not source_path.is_file() or source_path == target_dir:
        raise MigrationError("source must be a distinct regular offline SQLite file")
    catalog = build_legacy_catalog(source_path)
    malformed = _catalog_issues(catalog, "malformed_non_string_identity")
    if malformed:
        raise Blocked(_blocked_prewrite_report(batch_key=batch_key, unmapped=malformed, reasons=["malformed_non_string_identity"]))
    source_scope_map = _check_archive_manifest(handoff, catalog, source_scope_map, single_scope_to, explicit_scopes)
    return Conversion(
        batch_key=batch_key,
        agent_id=agent_id,
        installation_id=installation_id,
        source_path=source_path,
        target_dir=target_dir,
        catalog=catalog,
        explicit_scopes=explicit_scopes,
        handoff=handoff,
        audience_scopes=audience_scopes,
        source_scope_map=source_scope_map,
        single_scope_to=single_scope_to,
        memory_reader_contract=legacy_memory_reader_contract,
        bridge_archive_path=completed_bridge_archive_path,
        import_ledger_archive_path=import_ledger_archive_path,
        project_memberships=project_legacy_memberships,
    )


def _trusted_handoff(
    installation_manifest: str | Path, host: str | None, target_directory: str | Path | None
) -> tuple[dict[str, Any], dict[str, str], Path]:
    """The host-owned identity and target; the manifest's archive fields ride along."""
    binding, manifest_target, manifest_file, audience_scopes, resolved_host = _load_installation_handoff(installation_manifest, host)
    target_dir = _safe_path(manifest_target, error_type=MigrationError)
    if target_directory is not None and _safe_path(target_directory, error_type=MigrationError) != target_dir:
        raise MigrationError("target directory differs from trusted installation manifest")
    payload = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file and manifest_file.exists() else {}
    handoff = {
        "host": resolved_host,
        "manifest": str(manifest_file),
        "agent_id": binding.agent_id,
        "installation_id": binding.installation_id,
        "target_directory": str(target_dir),
        "target_scope_ids": sorted(binding.scope_ids),
        "test_mode": bool(binding.test_mode),
        "audience_scopes": dict(audience_scopes),
        **{key: payload.get(key) for key in _ARCHIVE_MANIFEST_KEYS},
    }
    return handoff, dict(audience_scopes), target_dir


def _catalog_issues(catalog: dict[str, Any], reason: str) -> list[Row]:
    return [item for item in catalog.get("unsupported", []) if item.get("reason") == reason]


def _catalog_scopes(catalog: dict[str, Any]) -> frozenset[str]:
    return frozenset(catalog["content_scopes"]) | frozenset(catalog["shared_only_scopes"]) | frozenset(catalog["audit_only_scopes"])


def _check_archive_manifest(
    handoff: dict[str, Any] | None,
    catalog: dict[str, Any],
    source_scope_map: Mapping[str, str] | None,
    single_scope_to: str | None,
    explicit_scopes: list[str] | None,
) -> Mapping[str, str] | None:
    """An archive manifest binds the exact snapshot, catalog and complete scope map."""
    if handoff is None:
        return source_scope_map
    if handoff.get("archive_snapshot_hash") and handoff["archive_snapshot_hash"] != catalog["source_sha256"]:
        raise MigrationError("source snapshot digest mismatch against trusted manifest")
    if handoff.get("archive_catalog_hash") and handoff["archive_catalog_hash"] != catalog["catalog_sha256"]:
        raise MigrationError("catalog digest mismatch against trusted manifest")
    if not any(handoff.get(key) for key in ("archive_snapshot_hash", "archive_catalog_hash", "archive_source_map")):
        return source_scope_map
    if single_scope_to is not None:
        raise MigrationError("single_scope_to is forbidden in archive mode")
    archive_map = handoff.get("archive_source_map") or {}
    if source_scope_map is None:
        source_scope_map = archive_map
    elif source_scope_map != archive_map:
        raise MigrationError("source_scope_map conflicts with trusted archive manifest map")
    real_scopes = _catalog_scopes(catalog)
    if frozenset(archive_map) != real_scopes:
        raise MigrationError("source_scope_map must match EXACT complete real source-map key set")
    if explicit_scopes is not None and frozenset(explicit_scopes) != real_scopes:
        raise MigrationError("explicit scope selection must equal the ENTIRE real catalog set in archive mode")
    return source_scope_map


# --- the pipeline ----------------------------------------------------------

def _convert(cv: Conversion) -> dict[str, Any]:
    conn = _open_immutable(cv.source_path)
    try:
        _read_legacy(conn, cv)
        _plan_scopes(conn, cv)
        _bind_target(cv)
        _inventory_schema(conn, cv)
        archive_sources(cv)
        plan_deletions(cv)
        storage, context = _open_target(cv)
        _write_target(cv, storage, context)
        return _report(cv, _read_back(storage, context))
    finally:
        conn.close()


def _read_legacy(conn: sqlite3.Connection, cv: Conversion) -> None:
    cv.tables = _tables(conn)
    cv.import_ledger_receipt = _verify_sidecar(conn, cv.tables, IMPORT_LEDGER_TABLE, cv.import_ledger_archive_path, "import ledger", verify_import_ledger_archive)
    cv.bridge_receipt = _verify_sidecar(conn, cv.tables, BRIDGE_TABLE, cv.bridge_archive_path, "completed bridge", verify_completed_bridge_archive)
    unsupported = _catalog_issues(cv.catalog, "unknown_legacy_columns_blocks_cutover")
    if unsupported:
        # Do not copy even a sanitized row: an unknown column can restrict
        # the row and every derived copy. Leave an existing target intact.
        raise Blocked(_blocked_prewrite_report(
            batch_key=cv.batch_key, unmapped=unsupported, reasons=["unknown_legacy_columns_blocks_cutover"],
            extra={"schema_inventory": {"tables": sorted(cv.tables), "schema_gaps": unsupported}},
        ))
    cv.rows = {table: _rows(conn, table) for table in _LEGACY_TABLES}


def _verify_sidecar(
    conn: sqlite3.Connection,
    tables: set[str],
    table: str,
    sidecar: str | Path | None,
    label: str,
    verify: Callable[[sqlite3.Connection, Any], dict[str, Any]],
) -> dict[str, Any] | None:
    """An audit-only legacy table converts only against its persisted archive."""
    if table not in tables:
        return None
    if sidecar is None:
        raise LegacyCompatibilityError(f"{label} requires a verified offline audit sidecar")
    file = Path(sidecar)
    if file.is_symlink() or not file.is_file():
        raise LegacyCompatibilityError(f"{label} sidecar must be a regular file")
    return verify(conn, json.loads(file.read_text(encoding="utf-8")))


def _plan_scopes(conn: sqlite3.Connection, cv: Conversion) -> None:
    """Decide which legacy scopes convert and, under a handoff, where they land."""
    owning = [row for table in _SCOPE_OWNING for row in cv.rows[table]]
    direct = owning + [row for table in _SCOPE_INHERITING for row in cv.rows[table]]
    base_missing_scope = any(_scope_value(row) is None for row in owning)
    direct_scopes = frozenset(scope for row in direct if (scope := _scope_value(row)) is not None)
    if cv.handoff and cv.handoff.get("archive_snapshot_hash"):
        source_scopes = _catalog_scopes(cv.catalog)
        if base_missing_scope:
            source_scopes |= {"legacy-scope"}
    else:
        source_scopes = direct_scopes
        if base_missing_scope:
            source_scopes |= {"legacy-scope"}
        if not source_scopes:
            source_scopes = frozenset({"legacy-scope"})
    if cv.explicit_scopes is not None:
        requested_source = frozenset(cv.explicit_scopes)
        if not requested_source.issubset(source_scopes):
            raise MigrationError("explicit scope selection is incompatible with actual source scopes")
    else:
        requested_source = source_scopes
    if not requested_source or "" in requested_source:
        raise MigrationError("no valid legacy scopes")
    if base_missing_scope and "legacy-scope" not in requested_source:
        raise MigrationError("legacy default scope is outside explicit target binding")
    if not direct_scopes <= requested_source:
        raise MigrationError("legacy scope is outside explicit target binding")
    cv.requested_source = requested_source
    cv.applied_scope_map = cv.source_scope_map
    if cv.handoff is None:
        cv.requested = requested_source
    else:
        issues = _map_handoff_scopes(cv)
        if issues:
            raise Blocked(_blocked_report(
                batch_key=cv.batch_key, unmapped=issues,
                reasons=sorted({str(item["reason"]) for item in issues}),
                installation_handoff=cv.installation_handoff(),
            ))
        _apply_scope_mapping(cv.rows, cv.scope_mapping)
        cv.requested = frozenset(cv.scope_mapping.values())
    if cv.memory_reader_contract is not None:
        cv.memory_authority = prepare_memory_storage_authority(
            conn,
            source_scope_map=cv.scope_mapping if cv.handoff else {scope: scope for scope in requested_source},
            bound_target_scopes=frozenset(cv.handoff["target_scope_ids"]) if cv.handoff else cv.requested,
            verified_reader_contract=cv.memory_reader_contract,
        )
    _plan_digest_retention(cv)


def _map_handoff_scopes(cv: Conversion) -> list[Row]:
    """Resolve every requested legacy scope to a manifest-bound target scope."""
    assert cv.handoff is not None
    targets = frozenset(cv.handoff["target_scope_ids"])
    if cv.single_scope_to is not None:
        target = cv.single_scope_to.strip()
        if len(cv.requested_source) != 1:
            cv.applied_scope_map = {}
            return [
                {"key": scope, "target": target, "reason": "single_scope_to_requires_single_legacy_scope", "auto_promoted": False}
                for scope in sorted(cv.requested_source)
            ]
        cv.applied_scope_map = {next(iter(cv.requested_source)): target}
    cv.scope_mapping, issues = _resolve_scope_mapping(cv.requested_source, targets, cv.audience_scopes, cv.applied_scope_map)
    return issues


def _apply_scope_mapping(rows: dict[str, list[Row]], mapping: Mapping[str, str]) -> None:
    for table in _SCOPE_OWNING:
        rows[table] = _map_scope_rows(rows[table], mapping, default_source_scope="legacy-scope")
    for table in (*_SCOPE_INHERITING, *sorted(_DIGEST_TABLES)):
        rows[table] = _map_scope_rows(rows[table], mapping)


def _plan_digest_retention(cv: Conversion) -> None:
    """Digest audit rows need two distinct registered archive scopes to land in."""
    retention = (cv.handoff.get("archive_retention_scopes") or {}) if cv.handoff else {}
    cv.orphan_bridge_scope = retention.get("orphan_bridge")
    cv.digest_audit_scope = retention.get("digest_audit")
    populated = [table for table in sorted(_DIGEST_TABLES) if cv.rows[table]]
    if not populated:
        return
    archive_scopes = (cv.handoff.get("archive_scopes") or []) if cv.handoff else []
    if (
        cv.orphan_bridge_scope
        and cv.digest_audit_scope
        and cv.orphan_bridge_scope != cv.digest_audit_scope
        and cv.orphan_bridge_scope in archive_scopes
        and cv.digest_audit_scope in archive_scopes
    ):
        return
    raise Blocked(_blocked_prewrite_report(
        batch_key=cv.batch_key,
        unmapped=[{"table": table, "reason": _DIGEST_RETENTION_REASON, "auto_promoted": False} for table in populated],
        reasons=[_DIGEST_RETENTION_REASON],
        extra={"installation_handoff": cv.installation_handoff()},
    ))


def _bind_target(cv: Conversion) -> None:
    cv.target_dir.mkdir(parents=True, exist_ok=True)
    existing = _existing_target_scopes(cv.target_dir)
    if existing is None:
        return
    bound = frozenset(cv.handoff["target_scope_ids"]) if cv.handoff else cv.requested
    if existing != bound or not cv.requested <= existing:
        raise MigrationError("target scope binding differs; refusing mixed authority")


def _inventory_schema(conn: sqlite3.Connection, cv: Conversion) -> None:
    """Classify every source table; unknown tables and authority we cannot map block."""
    for table in sorted(cv.tables - {"sqlite_sequence"}):
        missing = sorted(_REQUIRED.get(table, frozenset()) - set(_columns(conn, table)))
        if missing:
            cv.schema_gaps.append(cv.unmapped(table, "<schema>", "legacy_schema_column_missing_blocks_cutover", missing_columns=missing))
        if table in _KNOWN:
            continue
        if _is_derived_index(table, _CONVERSION_DERIVED_NAMES):
            cv.derived_tables.append(table)
            continue
        cv.unknown_tables.append(table)
        values = _rows(conn, table)
        cv.unmapped(
            table, "<table>", "unknown_legacy_table_blocks_cutover",
            columns=_columns(conn, table), row_count=len(values),
            redacted_rows=[_safe(row) for row in values[:200]], truncated=len(values) > 200,
        )
    for table in sorted(_COMPAT & cv.tables):
        for row in _rows(conn, table):
            cv.unmapped(
                table, str(row.get("id") or row.get("claim_id") or "unknown"), _COMPAT_REASONS[table],
                row_digest=_digest(_safe(row)), redacted_row=_safe(row), status=str(row.get("status") or "unknown"),
            )


# --- the write transaction -------------------------------------------------

def _open_target(cv: Conversion) -> tuple[SQLiteStorage, TrustedContext]:
    if cv.handoff is not None:
        binding = InstanceBinding(cv.agent_id, cv.installation_id, cv.target_dir, frozenset(cv.handoff["target_scope_ids"]), bool(cv.handoff["test_mode"]))
    else:
        binding = InstanceBinding(cv.agent_id, cv.installation_id, cv.target_dir, cv.requested, True)
    storage = SQLiteStorage(binding, timeout_seconds=3.0)
    storage.initialize()
    return storage, TrustedContext(binding, "p15-maintenance", cv.requested, "host_generated")


def _write_target(cv: Conversion, storage: SQLiteStorage, context: TrustedContext) -> None:
    with storage.write(context, remaining_seconds=30.0) as tx:
        conn = tx._check(write=True)
        _insert_sources(cv, conn)
        _insert_memory_links(cv, conn)
        _insert_episodes(cv, conn)
        write_fact_claims(cv, conn)
        write_procedures(cv, conn)
        link_history_records(cv, conn)
        write_deletions(cv, tx)
        _project_lexical_terms(cv, conn)
        normalize_scope_authorizations(conn)
        # Preserve the old ordinary-recall lifecycle policy using the same Core
        # suppression/group/dependency mechanism as runtime governance. Run
        # after all sources and derived links exist, before commit.
        cv.lifecycle_suppression = apply_lifecycle_suppression(
            tx,
            [row for row in cv.rows["memories"] if str(row.get("id")) in cv.memory_refs],
            now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )


def _insert_sources(cv: Conversion, conn: sqlite3.Connection) -> None:
    """Insert archived events; a rerun must find the same content at the same id."""
    placeholders = ",".join("?" for _ in SOURCE_EVENT_FIELDS)
    for item in cv.sources:
        conn.execute(
            f"INSERT INTO source_events({','.join(SOURCE_EVENT_FIELDS)}) VALUES ({placeholders}) ON CONFLICT(event_id,source_revision) DO NOTHING",
            tuple(item[key] for key in SOURCE_EVENT_FIELDS),
        )
        current = conn.execute(
            "SELECT source_event_key,content_sha256,scope_id FROM source_events WHERE event_id=?", (item["event_id"],)
        ).fetchone()
        if current is None or str(current[2]) != str(item["scope_id"]):
            raise MigrationError(f"idempotence conflict: source scope {item['event_id']}")
        if tuple(current[:2]) != (item["source_event_key"], item["content_sha256"]) and not str(current[0]).startswith("removed-"):
            raise MigrationError(f"idempotence conflict: source {item['event_id']}")
        cv.inserted["source_events"] += 1


def _insert_memory_links(cv: Conversion, conn: sqlite3.Connection) -> None:
    for row in cv.rows["memory_journal_sources"]:
        memory_ref = cv.memory_refs.get(str(row.get("memory_id")))
        journal_ref = cv.journal_refs.get(str(row.get("journal_entry_id")))
        if memory_ref and journal_ref:
            conn.execute(
                "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote,location) VALUES ('event',?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (memory_ref, 1, journal_ref, 1, "derived_from", "", "legacy:memory_journal_sources"),
            )
            cv.inserted["evidence_links"] += 1


def _episode_resume(cv: Conversion, row: Row, episode_ref: str, refs: list[str], missing: list[str], scope: dict[str, Any]) -> dict[str, Any]:
    goal, _ = _safe_text(row.get("task_goal"))
    return {
        "episode_ref": episode_ref,
        "goal": {"text": goal, "evidence_refs": refs[:32]},
        "decisions": [],
        "verified_progress": [],
        "open_items": [],
        "blockers": [f"source_missing:{x}" for x in missing],
        "next_step": None,
        "next_step_basis": "unknown",
        "artifact_refs": [],
        "source_watermark": _digest({"legacy_episode": str(row.get("id")), "batch_key": cv.batch_key}),
        "evidence_refs": refs[:32],
        "legacy_fields": _safe({key: row.get(key) for key in _EPISODE_LEGACY_FIELDS}),
        "scope_authorization": scope,
    }


def _insert_episodes(cv: Conversion, conn: sqlite3.Connection) -> None:
    episodes = cv.rows["task_episodes"]
    plan = None
    if cv.project_memberships:
        plan = plan_legacy_episode_memberships({
            cv.episode_refs[str(row.get("id"))]: [
                cv.journal_refs[str(ref)] for ref in _json_list(row.get("journal_entry_ids")) if str(ref) in cv.journal_refs
            ]
            for row in episodes
        })
    sequence = int(conn.execute("SELECT COALESCE(max(sequence),0) FROM episode_events").fetchone()[0])
    for row in episodes:
        episode_ref, scope = cv.episode_refs[str(row.get("id"))], _scope(row)
        ids = [str(x) for x in _json_list(row.get("journal_entry_ids"))]
        refs = [cv.journal_refs[x] for x in ids if x in cv.journal_refs]
        missing = [x for x in ids if x not in cv.journal_refs]
        resume = _episode_resume(cv, row, episode_ref, refs, missing, scope)
        state = str(row.get("status") or "unknown").lower()
        conn.execute(
            "INSERT INTO episodes(episode_id,scope_id,project_id,branch_id,anchor_key,anchor_kind,series_key,segment_index,current_revision,read_blocked,suppressed) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(episode_id) DO NOTHING",
            (
                episode_ref, scope["row_scope_id"], _text_or_none(row.get("project_id")), _text_or_none(row.get("branch_id")),
                f"legacy-task:{row.get('id')}", "task", f"legacy-series:{row.get('id')}", 0, 1, 0, 0,
            ),
        )
        conn.execute(
            "INSERT INTO episode_versions(episode_id,revision,state,resume_json,source_watermark,processed_sequence,recorded_at,environment_revision) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (episode_ref, 1, state if state in _EPISODE_STATES else "unknown", _canon(resume), resume["source_watermark"], 0, _recorded(row.get("started_at")), None),
        )
        for ref in refs:
            if plan is not None:
                plan.apply(conn, episode_ref, ref)
            elif conn.execute("SELECT 1 FROM episode_events WHERE episode_id=? AND source_ref=?", (episode_ref, ref)).fetchone() is None:
                sequence += 1
                conn.execute(
                    "INSERT INTO episode_events(sequence,episode_id,source_ref,source_revision,membership,environment_revision) VALUES (?,?,?,?,?,?)",
                    (sequence, episode_ref, ref, 1, "anchored", None),
                )
            conn.execute(
                "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote) VALUES ('episode',?,?,?,?,?,'') ON CONFLICT DO NOTHING",
                (episode_ref, 1, ref, 1, "derived_from"),
            )
        if missing:
            cv.unmapped("task_episodes", str(row.get("id")), "source_missing_unknown", missing=missing)
        cv.inserted["episodes"] += 1
    if plan is not None:
        cv.membership_audit = plan.verify(conn)


def _project_lexical_terms(cv: Conversion, conn: sqlite3.Connection) -> None:
    for item in cv.sources:
        if conn.execute("SELECT read_blocked FROM source_events WHERE event_id=?", (item["event_id"],)).fetchone()[0]:
            continue
        for term in lexical_terms(item["content"]):
            conn.execute(
                "INSERT INTO lexical_projection(term,event_id,source_revision) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                (term, item["event_id"], 1),
            )
            cv.inserted["lexical_projection"] += 1


# --- the report ------------------------------------------------------------

def _read_back(storage: SQLiteStorage, context: TrustedContext) -> dict[str, Any]:
    """Counts and receipts as the target now holds them, not as we meant to write."""
    with storage.read(context) as tx:
        conn = tx._check()

        def count(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])

        return {
            "source_status": [
                {"read_blocked": int(x[0]), "capture_state": str(x[1]), "count": int(x[2])}
                for x in conn.execute("SELECT read_blocked,capture_state,count(*) FROM source_events GROUP BY read_blocked,capture_state")
            ],
            "deletion_receipts": [
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
            ],
            "claims": count("SELECT count(*) FROM claims"),
            "claim_versions": count("SELECT count(*) FROM claim_versions"),
            "procedure_versions": count("SELECT count(*) FROM claim_versions v JOIN claims c USING(claim_id) WHERE c.kind='procedure'"),
            "fact_authority": {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT v.state,count(*) FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.kind='fact' GROUP BY v.state"
                )
            },
        }


def _report(cv: Conversion, target: dict[str, Any]) -> dict[str, Any]:
    hard = {str(item.get("reason")) for item in cv.report_rows if str(item.get("reason")) in _BLOCKING_REASONS}
    authority = target["fact_authority"]
    versions = versions_by_playbook(cv.rows["playbook_versions"])
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "baseline": LEGACY_BASELINE,
        "target_schema": SCHEMA_VERSION,
        "batch_key": cv.batch_key,
        "source_path_excluded": True,
        "credentials_exported": False,
        "completion_status": "blocked" if hard else "complete",
        "counts": {
            "source_events": len(cv.sources),
            "deletion_operations": len(target["deletion_receipts"]),
            "episodes": len(cv.rows["task_episodes"]),
            "claims": target["claims"],
            "claim_versions": target["claim_versions"],
            "fact_claims_mapped": len(cv.mapped_facts),
            "procedure_claims_mapped": len(cv.mapped_procedures),
            "procedure_versions": target["procedure_versions"],
            "history_archived": sum(len(cv.rows[table]) for table in _HISTORY),
            "unmapped": len(cv.report_rows),
        },
        "fact_authority": {
            "active": authority.get("active", 0),
            "proposed_or_disputed": sum(value for state, value in authority.items() if state != "active"),
            "slot_encoding": "semantic conditions only; legacy fact_key and value_fingerprint remain in sanitized source archives linked to claim revisions; conflicts remain archived with explicit incomplete conversion",
        },
        "source_status": target["source_status"],
        "deletion_receipts": target["deletion_receipts"],
        "insert_attempts": dict(cv.inserted),
        "schema_inventory": {
            "tables": sorted(cv.tables),
            "unknown_tables": cv.unknown_tables,
            "derived_tables": sorted(cv.derived_tables),
            "schema_gaps": cv.schema_gaps,
        },
        "unmapped": cv.report_rows,
        "cutover_block": {"blocked": bool(hard), "reasons": sorted(hard)},
        "permission_classification": {
            "rows_with_explicit_scope_gap": cv.permission_gaps,
            "shared_scope_id_is_not_blanket_block": True,
        },
        "redaction": {
            "pipeline": "scope_recall.core.capture_filters.sanitize_report_text/sanitize_structured_value",
            "redacted_records": cv.redactions,
        },
        "rollback_boundary": "new sources/deletions/corrections not representable in 578b require Core restore-required stop-write protection; old snapshot is never overwritten",
        "candidate_auto_promoted": False,
        "vectors": "not imported; rebuildable",
        "old_governance_engines": "sanitized imported history only; not executable in new Core",
    }
    if cv.memory_reader_contract is not None:
        report["legacy_memory_reader_contract"] = cv.memory_reader_contract
    report["legacy_lifecycle_suppression"] = cv.lifecycle_suppression
    if cv.bridge_receipt is not None:
        report["completed_bridge_audit"] = cv.bridge_receipt
    if cv.import_ledger_receipt is not None:
        report["import_ledger_audit"] = cv.import_ledger_receipt
    if cv.membership_audit is not None:
        report["legacy_episode_memberships"] = cv.membership_audit
    report["legacy_procedure_collisions"] = {
        "group_count": len(set(cv.procedure_collisions.values())),
        "playbook_count": len(cv.procedure_collisions),
        "version_count": sum(len(versions.get(old_id, [])) for old_id in cv.procedure_collisions),
        "preservation": "same_scope_sqlite_history_sources",
        "automatic_procedure_eligible": False,
        "auto_promoted": False,
    }
    if cv.handoff is not None:
        report["installation_handoff"] = cv.installation_handoff()
        if cv.handoff.get("archive_snapshot_hash"):
            report["archive_retention_summary"] = {
                "orphan_bridge_scope": cv.orphan_bridge_scope,
                "digest_audit_scope": cv.digest_audit_scope,
            }
            report["legacy_catalog_summary"] = {
                key: cv.catalog[key]
                for key in (
                    "content_scopes", "shared_only_scopes", "audit_only_scopes", "audit_sentinels",
                    "table_dispositions", "table_row_counts", "direct_scope_count", "total_nonempty_raw_values",
                )
            }
    return report
