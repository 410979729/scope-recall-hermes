"""Bounded, audited restore of approved journal rows from a trusted snapshot.

This maintenance path copies a pre-approved journal/digest-run window from a
checkpointed SQLite snapshot into an offline target. It does not call the
normal append/sanitize/now path, does not replay dead letters, and does not
execute digest logic. Default mode is dry-run and query-only.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from .capture_filters import sanitize_report_text, sanitize_structured_value
from .journal_source_restore_rows import (
    DIGEST_RUN_LOGICAL_FIELDS,
    JOURNAL_SEMANTIC_FIELDS,
    JOURNAL_SET_DIGEST_FIELDS,
    bind_selection,
    classify_rows,
    compute_digest_run_set_digest,
    compute_journal_set_digest,
    digest_run_logical_record,
    insert_missing_rows,
    journal_identity_record,
    journal_semantic_record,
    lookup_source_digest,
    lookup_target_digest,
    lookup_target_journal,
    require_digest_references,
    require_excluded_tail,
    require_half_open_window,
    select_digest_window,
    select_journal_window,
    verify_journal_content_hashes,
)
from .operator_ledger import (
    mirror_operator_receipt,
    operator_ledger_schema_status,
    read_operator_operation,
    record_committed_operator_operation,
)
from .journal_source_restore_snapshot import (
    JournalSourceRestoreError,
    any_sqlite_sidecar_present,
    as_absolute_path,
    canonical_json,
    capture_regular_file_identity,
    compute_schema_digest,
    compute_table_logical_digest,
    compute_target_epoch,
    compute_target_epoch_from_connection,
    identities_match,
    inspect_source_artifact,
    open_checkpointed_target_reader,
    open_immutable_source_connection,
    require_main_only_sqlite_file,
    sha256_file,
    sha256_text,
    sidecar_paths,
)
from . import truth_connection as _truth_connection
from .maintenance_lease import (
    install_activation_lease_authorizer,
    read_activation_lease,
)
from .sqlite_backup import SqliteBackupError, verified_online_backup
from .truth_connection import connect_truth_database
from .writer_lease import TruthWriterBusyError, holding_truth_writer_lease

JOURNAL_SOURCE_RESTORE_WRITER_ROLE = "journal_source_restore"
_RECEIPT_ALLOWED_KEYS = frozenset(
    {
        "backup_digest",
        "batch_digest",
        "cursor_reset_count",
        "cursor_reset_digest",
        "digest_run_already_present_count",
        "digest_run_conflict_count",
        "digest_run_inserted_count",
        "digest_run_selected_count",
        "digest_run_set_digest",
        "dry_run",
        "error_code",
        "fts_aftercare_required",
        "journal_already_present_count",
        "journal_conflict_count",
        "journal_inserted_count",
        "journal_selected_count",
        "journal_set_digest",
        "mapping_count",
        "mapping_digest",
        "ok",
        "operation_id",
        "receipt_repair_required",
        "receipt_state",
        "remapping_occurred",
        "request_fingerprint",
        "secondary_error_code",
        "source_epoch_digest",
        "stage",
        "status",
        "target_epoch_digest",
        "verdict",
    }
)
_RECEIPT_DENYLIST_KEYS = frozenset(
    {
        "backup_path",
        "candidate",
        "content",
        "digest_id",
        "exception",
        "id",
        "id_map",
        "journal_entry_id",
        "map",
        "metadata",
        "password",
        "path",
        "processed_run_id",
        "secret",
        "source_path",
        "sql",
        "stderr",
        "stdout",
        "target_path",
        "token",
        "traceback",
    }
)
TargetConnectionFactory = Callable[[Path], sqlite3.Connection]


def _as_path(path: str | Path) -> Path:
    return as_absolute_path(path)


def _dirty_sqlite_sidecars(path: Path) -> bool:
    return any_sqlite_sidecar_present(path)


def _wal_visible_sibling_present(path: Path) -> bool:
    """WAL-only crash-reconciliation exemption: ``-wal`` and/or ``-shm``.

    True when a WAL or SHM sibling exists, including zero-byte files and
    symlink presence. A rollback ``-journal`` is never WAL-visible and
    must not satisfy this predicate, even if the requested operation_id
    is already committed and readable. Unreadable WAL/SHM siblings do
    not grant the exemption; the later checkpoint gate still fail-closes.
    """

    wal_path, shm_path, _journal = sidecar_paths(path)
    for sidecar in (wal_path, shm_path):
        try:
            if sidecar.is_symlink() or sidecar.exists():
                return True
        except OSError:
            continue
    return False


def _empty_receipt(*, dry_run: bool) -> dict[str, Any]:
    return {
        "backup_digest": "",
        "batch_digest": "",
        "cursor_reset_count": 0,
        "cursor_reset_digest": "",
        "digest_run_already_present_count": 0,
        "digest_run_conflict_count": 0,
        "digest_run_inserted_count": 0,
        "digest_run_selected_count": 0,
        "digest_run_set_digest": "",
        "dry_run": bool(dry_run),
        "error_code": "",
        "fts_aftercare_required": False,
        "journal_already_present_count": 0,
        "journal_conflict_count": 0,
        "journal_inserted_count": 0,
        "journal_selected_count": 0,
        "journal_set_digest": "",
        "mapping_count": 0,
        "mapping_digest": "",
        "ok": False,
        "operation_id": "",
        "receipt_repair_required": False,
        "receipt_state": "",
        "remapping_occurred": False,
        "request_fingerprint": "",
        "secondary_error_code": "",
        "source_epoch_digest": "",
        "stage": "plan" if dry_run else "apply",
        "status": "error",
        "target_epoch_digest": "",
        "verdict": "refused",
    }


def _redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 16:
        return "[REDACTED_DEPTH_LIMIT]"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in _RECEIPT_DENYLIST_KEYS or name.casefold() in _RECEIPT_DENYLIST_KEYS:
                output[name] = "[REDACTED]"
                continue
            output[name] = _redact_value(item, depth=depth + 1)
        return output
    if isinstance(value, list):
        return [_redact_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return sanitize_report_text(value)
    return value


def redact_source_restore_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop non-receipt fields and recursively redact denylisted material."""

    redacted = _redact_value(dict(payload))
    if not isinstance(redacted, dict):
        return {"ok": False, "error_code": "redaction_failed", "stage": "refused", "status": "error", "verdict": "refused"}
    cleaned, _changed = sanitize_structured_value(redacted)
    if not isinstance(cleaned, dict):
        cleaned = redacted
    official = {key: cleaned[key] for key in _RECEIPT_ALLOWED_KEYS if key in cleaned}
    return official


def _finish(receipt: dict[str, Any]) -> dict[str, Any]:
    return redact_source_restore_payload(receipt)


def _fail(receipt: dict[str, Any], code: str) -> dict[str, Any]:
    receipt["ok"] = False
    receipt["error_code"] = code
    receipt["status"] = "error"
    receipt["verdict"] = "refused"
    receipt["stage"] = "refused"
    return _finish(receipt)


def _not_ready(receipt: dict[str, Any], code: str) -> dict[str, Any]:
    receipt["ok"] = False
    receipt["error_code"] = code
    receipt["status"] = "conflict"
    receipt["verdict"] = "not_ready"
    receipt["stage"] = "plan"
    return _finish(receipt)


def source_restore_error_receipt(code: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Build one official refusal receipt for CLI/operator mapping."""

    return _fail(_empty_receipt(dry_run=dry_run), code)


def mark_source_restore_cleanup_failure(
    receipt: Mapping[str, Any],
    *,
    original_code: str = "",
) -> dict[str, Any]:
    """Preserve committed or refused cause after activation-lease release failure."""

    payload = dict(receipt)
    original = str(original_code or payload.get("error_code") or "")
    committed = bool(payload.get("ok")) and str(payload.get("stage") or "") == "apply"
    payload["ok"] = False
    payload["error_code"] = "activation_lease_cleanup_failed"
    payload["secondary_error_code"] = original if original != "activation_lease_cleanup_failed" else ""
    payload["status"] = "manual_recovery_required"
    payload["verdict"] = "applied_cleanup_failed" if committed else "refused_cleanup_failed"
    payload["stage"] = "apply" if committed else str(payload.get("stage") or "refused")
    payload["dry_run"] = False
    return _finish(payload)


def _mark_committed_cleanup_failure(receipt: dict[str, Any]) -> dict[str, Any]:
    """Bound a known-committed apply after later cleanup failed.

    Writer-close, truth-writer lease-context exit, or receipt finalization
    may fail after durable restore bytes exist. This receipt must never
    claim rollback and must not restore or re-run business DML.
    """

    receipt["ok"] = False
    receipt["error_code"] = "committed_cleanup_failed"
    receipt["status"] = "manual_recovery_required"
    receipt["verdict"] = "applied_cleanup_failed"
    receipt["stage"] = "apply"
    return _finish(receipt)


def _bind_committed_apply_receipt(
    receipt: dict[str, Any],
    *,
    operation_id: str,
    fingerprint: str,
    journal_inserted: int,
    digest_inserted: int,
    remapping: bool,
    mapping: Mapping[str, Any],
) -> None:
    """Copy known-committed apply facts onto the public receipt.

    Call this before writer close or any later cleanup that may raise, so
    the function-level committed boundary can still preserve counts.
    """

    receipt.update(
        {
            "digest_run_inserted_count": digest_inserted,
            "fts_aftercare_required": True,
            "journal_inserted_count": journal_inserted,
            "mapping_count": mapping["mapping_count"],
            "mapping_digest": mapping["mapping_digest"],
            "cursor_reset_count": int(mapping.get("cursor_reset_count") or 0),
            "cursor_reset_digest": str(mapping.get("cursor_reset_digest") or ""),
            "ok": not receipt.get("receipt_repair_required"),
            "operation_id": operation_id,
            "remapping_occurred": bool(remapping and journal_inserted),
            "request_fingerprint": fingerprint,
            "stage": "apply",
            "status": "ok" if not receipt.get("receipt_repair_required") else "error",
            "verdict": (
                "applied" if journal_inserted or digest_inserted else "already_present"
            ),
        }
    )


def _require_distinct_paths(source: Path, target: Path) -> None:
    if source == target:
        raise JournalSourceRestoreError("same_path")
    try:
        if source.exists() and target.exists() and os.path.samefile(source, target):
            raise JournalSourceRestoreError("same_path")
    except OSError:
        pass


def _require_half_open_window(start: str, end: str, *, code: str) -> tuple[str, str]:
    return require_half_open_window(start, end, code=code)


def _optional_excluded_window(start: str, end: str, *, code: str) -> tuple[str, str] | None:
    if not str(start or "").strip() and not str(end or "").strip():
        return None
    return require_half_open_window(start, end, code=code)


def _require_checkpointed_file(path: Path, *, wal_code: str) -> None:
    require_main_only_sqlite_file(path, wal_code=wal_code)


def _after_source_inspection_hook(source: Path) -> None:
    """Deterministic test seam after initial source health/hash capture.

    Production is a no-op. Tests may wrap ``_inspect_source`` or this hook to
    mutate the snapshot after inspection but before fenced selection.
    """

    del source
    return None


def _require_target_schema_contract(
    source_info: Mapping[str, Any], target_epoch: Mapping[str, Any]
) -> None:
    if str(target_epoch.get("schema_digest") or "") != str(
        source_info.get("schema_digest") or ""
    ):
        raise JournalSourceRestoreError("target_schema_digest_mismatch")
    if int(target_epoch.get("user_version") or 0) != int(source_info.get("user_version") or 0):
        raise JournalSourceRestoreError("target_user_version_mismatch")


def _inspect_source(source: Path) -> dict[str, Any]:
    """Inspect the main-only source on one immutable connection."""

    return inspect_source_artifact(source)


def _load_selection(
    source: Path,
    *,
    journal_created_at_start: str,
    journal_created_at_end: str,
    digest_started_at_start: str,
    digest_started_at_end: str,
    journal_excluded: tuple[str, str] | None,
    digest_excluded: tuple[str, str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str, int, str]:
    """Select approved and excluded rows on one immutable source connection."""

    conn = open_immutable_source_connection(source)
    try:
        journals = select_journal_window(
            conn, start=journal_created_at_start, end=journal_created_at_end
        )
        digests = select_digest_window(
            conn, start=digest_started_at_start, end=digest_started_at_end
        )
        excluded_journals = (
            select_journal_window(conn, start=journal_excluded[0], end=journal_excluded[1])
            if journal_excluded is not None
            else []
        )
        excluded_digests = (
            select_digest_window(conn, start=digest_excluded[0], end=digest_excluded[1])
            if digest_excluded is not None
            else []
        )
        verify_journal_content_hashes(journals)
        verify_journal_content_hashes(excluded_journals)
        schema_digest = compute_schema_digest(conn)
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        file_sha256 = sha256_file(source)
        return (
            journals,
            digests,
            excluded_journals,
            excluded_digests,
            schema_digest,
            user_version,
            file_sha256,
        )
    finally:
        conn.close()


def _activation_lease_token(target: Path) -> str:
    payload = read_activation_lease(target)
    if payload is None:
        raise JournalSourceRestoreError("activation_lease_required")
    try:
        owner_pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    if owner_pid != os.getpid():
        raise JournalSourceRestoreError("activation_lease_required")
    bound = str(payload.get("database_path") or "")
    if bound:
        if _as_path(bound) != target:
            raise JournalSourceRestoreError("activation_lease_required")
    token = str(payload.get("token") or "")
    if not token:
        raise JournalSourceRestoreError("activation_lease_required")
    return token


def _open_target_writer(
    target: Path,
    *,
    lease_token: str,
    connection_factory: TargetConnectionFactory | None,
) -> sqlite3.Connection:
    if connection_factory is not None:
        conn = connection_factory(target)
    else:
        # Bind through the truth-connection module, not this module's imported
        # name, so an ungated ``jsr.connect_truth_database(..., mode="rw")``
        # repair path stays distinguishable from the authorized opener.
        conn = _truth_connection.connect_truth_database(target, mode="rw")
    try:
        install_activation_lease_authorizer(conn, target, lease_token=lease_token)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


_PROTECTED_EPOCH_TABLES = (
    "journal_rejections",
    "memories",
    "memories_fts",
    "memory_journal_sources",
    "procedural_playbooks",
)


def _compute_request_fingerprint(
    *,
    source_info: Mapping[str, Any],
    journal_created_at_start: str,
    journal_created_at_end: str,
    digest_started_at_start: str,
    digest_started_at_end: str,
    journal_excluded: tuple[str, str] | None,
    digest_excluded: tuple[str, str] | None,
    expected_journal_count: int,
    expected_digest_run_count: int,
    expected_journal_set_digest: str,
    expected_digest_run_set_digest: str,
    expected_target_epoch_digest: str,
    batch_digest: str,
    backup_digest: str,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "backup_digest": backup_digest,
                "batch_digest": batch_digest,
                "digest_excluded": list(digest_excluded or ("", "")),
                "digest_started_at_end": digest_started_at_end,
                "digest_started_at_start": digest_started_at_start,
                "expected_digest_run_count": int(expected_digest_run_count),
                "expected_digest_run_set_digest": expected_digest_run_set_digest,
                "expected_journal_count": int(expected_journal_count),
                "expected_journal_set_digest": expected_journal_set_digest,
                "expected_target_epoch_digest": expected_target_epoch_digest,
                "journal_created_at_end": journal_created_at_end,
                "journal_created_at_start": journal_created_at_start,
                "journal_excluded": list(journal_excluded or ("", "")),
                "source_epoch_digest": source_info.get("source_epoch_digest"),
                "source_schema_digest": source_info.get("schema_digest"),
                "source_sha256": source_info.get("file_sha256"),
                "source_user_version": source_info.get("user_version"),
            }
        )
    )


def _require_operator_ledger(conn: sqlite3.Connection) -> None:
    status = operator_ledger_schema_status(conn)
    if not status.get("current"):
        raise JournalSourceRestoreError("operator_ledger_unavailable")


def _verify_post_invariants(
    conn: sqlite3.Connection,
    *,
    before: Mapping[str, Any],
    journal_inserted: int,
    digest_inserted: int,
) -> None:
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) != int(before["user_version"]):
        raise JournalSourceRestoreError("post_invariant_failed")
    if compute_schema_digest(conn) != str(before["schema_digest"]):
        raise JournalSourceRestoreError("post_invariant_failed")
    for table in _PROTECTED_EPOCH_TABLES:
        count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        digest = compute_table_logical_digest(conn, table)
        expected = before["tables"][table]
        if count != int(expected["count"]) or digest != str(expected["digest"]):
            raise JournalSourceRestoreError("post_invariant_failed")
    journal_count = int(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0])
    digest_count = int(conn.execute("SELECT COUNT(*) FROM journal_digest_runs").fetchone()[0])
    if journal_count != int(before["tables"]["journal_entries"]["count"]) + int(journal_inserted):
        raise JournalSourceRestoreError("post_invariant_failed")
    if digest_count != int(before["tables"]["journal_digest_runs"]["count"]) + int(digest_inserted):
        raise JournalSourceRestoreError("post_invariant_failed")
    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'journal_entries'"
    ).fetchone()
    live_seq = 0 if sequence is None else int(sequence[0])
    if live_seq < int(before.get("sqlite_sequence", {}).get("journal_entries") or 0):
        raise JournalSourceRestoreError("post_invariant_failed")
    quick = conn.execute("PRAGMA quick_check").fetchone()
    if quick is None or str(quick[0]).strip().lower() != "ok":
        raise JournalSourceRestoreError("post_invariant_failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise JournalSourceRestoreError("post_invariant_failed")


class _CommittedLookup:
    """Tri-state operator-ledger readback: found, confirmed absent, or indeterminate."""

    __slots__ = ("status", "row")

    def __init__(self, status: str, row: dict[str, Any] | None = None) -> None:
        self.status = status
        self.row = row


def _open_operator_ledger_reader(target: Path) -> sqlite3.Connection:
    """Open one reader for committed operator-ledger lookup.

    Preflight consults the ledger before the apply writer starts. A
    checkpointed main-only target must use the sidecar-safe immutable URI
    so that lookup cannot materialize ``-wal``/``-shm``/``-journal`` and
    then fail the later checkpoint gate. When siblings already exist,
    ordinary read-only truth open is required so WAL-visible committed
    rows remain readable after crash recovery or while the apply writer
    is still open.     Ordinary ``mode=ro`` must never run on a main-only
    file: that is the dest-open sidecar hazard. This opener is not the
    WAL-only reconciliation exemption; it still treats any sibling as
    dirty so lookup can see already-present WAL or journal bytes.
    """

    if _dirty_sqlite_sidecars(target):
        return connect_truth_database(target, mode="ro")
    return open_checkpointed_target_reader(target)


def _lookup_committed_operation(target: Path, operation_id: str) -> _CommittedLookup:
    """Read one committed operation without collapsing errors into absence.

    Open or ledger-schema failures are ``indeterminate``. A successful
    query that finds no row is ``absent``. Callers that receive ``None``
    from a patched seam must treat that as indeterminate, never rollback.
    """

    try:
        conn = _open_operator_ledger_reader(target)
    except Exception:
        return _CommittedLookup("indeterminate")
    try:
        try:
            _require_operator_ledger(conn)
        except JournalSourceRestoreError:
            return _CommittedLookup("indeterminate")
        try:
            row = read_operator_operation(conn, operation_id)
        except Exception:
            return _CommittedLookup("indeterminate")
        if row is None:
            return _CommittedLookup("absent")
        return _CommittedLookup("found", row)
    except Exception:
        return _CommittedLookup("indeterminate")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _found_committed_row(lookup: Any) -> dict[str, Any] | None:
    if lookup is None:
        return None
    if getattr(lookup, "status", None) == "found":
        row = getattr(lookup, "row", None)
        return row if isinstance(row, dict) else None
    if isinstance(lookup, dict):
        return lookup
    return None


def _resolve_commit_outcome(
    lookup: Any,
    *,
    fingerprint: str,
    writer: sqlite3.Connection,
) -> str:
    """Classify post-commit readback as committed, rolled_back, or unknown."""

    if lookup is None:
        return "unknown"
    status = getattr(lookup, "status", None)
    if status == "found":
        row = getattr(lookup, "row", None) or {}
        if isinstance(row, dict) and str(row.get("request_fingerprint") or "") == fingerprint:
            return "committed"
        return "unknown"
    if status == "absent":
        if writer.in_transaction:
            return "rolled_back"
        return "unknown"
    if isinstance(lookup, dict):
        if str(lookup.get("request_fingerprint") or "") == fingerprint:
            return "committed"
        return "unknown"
    return "unknown"


def _reconcile_committed_operation(
    *,
    target: Path,
    existing: Mapping[str, Any],
    receipt: dict[str, Any],
    source_info: Mapping[str, Any],
    journal_created_at_start: str,
    journal_created_at_end: str,
    digest_started_at_start: str,
    digest_started_at_end: str,
    journal_excluded: tuple[str, str] | None,
    digest_excluded: tuple[str, str] | None,
    expected_journal_count: int,
    expected_digest_run_count: int,
    expected_journal_set_digest: str,
    expected_digest_run_set_digest: str,
    expected_target_epoch_digest: str,
    batch_digest: str,
    connection_factory: TargetConnectionFactory | None,
) -> dict[str, Any]:
    """Repair one known-committed apply from the operator ledger.

    Used for a clean checkpointed retry and for WAL-visible crash
    reconciliation. Unknown IDs must not reach this helper.
    """

    prior: dict[str, Any] = {}
    try:
        loaded = json.loads(str(existing.get("result_json") or "{}"))
        if isinstance(loaded, dict):
            prior = loaded
    except json.JSONDecodeError:
        prior = {}
    backup_digest = str(prior.get("backup_digest") or "")
    stored_backup = str(existing.get("backup_path") or "")
    if not backup_digest and stored_backup and Path(stored_backup).is_file():
        backup_digest = sha256_file(Path(stored_backup))
    fingerprint = _compute_request_fingerprint(
        source_info=source_info,
        journal_created_at_start=journal_created_at_start,
        journal_created_at_end=journal_created_at_end,
        digest_started_at_start=digest_started_at_start,
        digest_started_at_end=digest_started_at_end,
        journal_excluded=journal_excluded,
        digest_excluded=digest_excluded,
        expected_journal_count=expected_journal_count,
        expected_digest_run_count=expected_digest_run_count,
        expected_journal_set_digest=expected_journal_set_digest,
        expected_digest_run_set_digest=expected_digest_run_set_digest,
        expected_target_epoch_digest=str(expected_target_epoch_digest or ""),
        batch_digest=batch_digest,
        backup_digest=backup_digest,
    )
    if fingerprint != str(existing.get("request_fingerprint") or ""):
        raise JournalSourceRestoreError("operation_fingerprint_conflict")
    receipt = _receipt_from_ledger_row(existing, receipt=receipt)
    from . import writer_lease as writer_lease_module

    if JOURNAL_SOURCE_RESTORE_WRITER_ROLE not in writer_lease_module.ALLOWED_TRUTH_WRITER_ROLES:
        raise JournalSourceRestoreError("writer_role_unavailable")
    lease_token = _activation_lease_token(target)
    try:
        with holding_truth_writer_lease(
            target.parent, role=JOURNAL_SOURCE_RESTORE_WRITER_ROLE
        ):
            repair = _open_target_writer(
                target,
                lease_token=lease_token,
                connection_factory=connection_factory,
            )
            try:
                _mirror_committed_receipt(
                    repair,
                    db_path=target,
                    operation_id=str(existing.get("operation_id") or ""),
                )
                receipt["receipt_state"] = "mirrored"
                receipt["receipt_repair_required"] = False
            finally:
                repair.close()
    except JournalSourceRestoreError:
        raise
    except Exception:
        receipt["ok"] = False
        receipt["error_code"] = "committed_receipt_debt"
        receipt["receipt_repair_required"] = True
        receipt["status"] = "error"
    return receipt


def _receipt_from_ledger_row(row: Mapping[str, Any], *, receipt: dict[str, Any]) -> dict[str, Any]:
    """Rebuild one public apply receipt from a committed ledger row.

    Cursor reset evidence is restored from ``result_json`` when present.
    Older rows that omit those fields default to ``0`` and ``""``.
    """

    try:
        result = json.loads(str(row.get("result_json") or "{}"))
    except json.JSONDecodeError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    receipt.update(
        {
            key: result[key]
            for key in _RECEIPT_ALLOWED_KEYS
            if key in result
        }
    )
    receipt["operation_id"] = str(row.get("operation_id") or "")
    receipt["request_fingerprint"] = str(row.get("request_fingerprint") or "")
    receipt["receipt_state"] = str(row.get("receipt_state") or "pending")
    receipt["cursor_reset_count"] = int(result.get("cursor_reset_count") or 0)
    receipt["cursor_reset_digest"] = str(result.get("cursor_reset_digest") or "")
    receipt["ok"] = True
    receipt["dry_run"] = False
    receipt["stage"] = "apply"
    receipt["status"] = "ok"
    receipt["verdict"] = "committed_reconciled"
    return receipt


def _mirror_committed_receipt(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    operation_id: str,
) -> dict[str, Any]:
    """Post-commit filesystem mirror. Safe to monkeypatch in death tests."""

    return mirror_operator_receipt(conn, db_path=db_path, operation_id=operation_id)


def _insert_missing(
    conn: sqlite3.Connection,
    *,
    journals: Sequence[Mapping[str, Any]],
    digests: Sequence[Mapping[str, Any]],
    operation_id: str = "",
    request_fingerprint: str = "",
) -> tuple[int, int, bool]:
    # journal_session_digest_state is neither an epoch/protected table nor a
    # nontarget invariant: restore never copies it. Unsafe cursors are
    # DELETE-reset in the apply transaction and reported via
    # cursor_reset_count / cursor_reset_digest only.
    journal_inserted, digest_inserted, remapping, _evidence = insert_missing_rows(
        conn,
        journals=journals,
        digests=digests,
        operation_id=operation_id,
        request_fingerprint=request_fingerprint,
    )
    return journal_inserted, digest_inserted, remapping


def run_journal_source_restore(
    *,
    source_path: str | Path,
    target_path: str | Path,
    journal_created_at_start: str,
    journal_created_at_end: str,
    digest_started_at_start: str,
    digest_started_at_end: str,
    expected_journal_count: int,
    expected_digest_run_count: int,
    expected_journal_set_digest: str,
    expected_digest_run_set_digest: str,
    expected_source_sha256: str,
    expected_schema_digest: str,
    expected_user_version: int,
    dry_run: bool = True,
    maintenance_confirmed: bool = False,
    expected_target_epoch_digest: str = "",
    prewrite_backup_path: str | Path | None = None,
    connection_factory: TargetConnectionFactory | None = None,
    journal_excluded_start: str = "",
    journal_excluded_end: str = "",
    digest_excluded_start: str = "",
    digest_excluded_end: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    """Plan or apply one bounded journal source-restore.

    Planning opens source immutable-main-only and inspects a checkpointed
    main-only target without creating sidecars. Apply preflight ledger
    lookup uses that same sidecar-safe reader on a main-only target.
    A same-operation apply whose target still has a WAL sibling
    (``-wal`` and/or ``-shm``, including symlink presence) after a
    committed crash-before-checkpoint consults the WAL-visible ledger
    and reconciles; unknown IDs still fail closed. A rollback
    ``-journal`` never uses that exemption.
    Apply requires confirmation, an activation lease owned by this
    process, the dedicated writer role, a matching target epoch computed
    on the authorized writer after ``BEGIN IMMEDIATE``, and a verified
    prewrite backup taken under that empty writer fence. Same-operation
    receipt repair uses those same writer gates and never an ungated
    read-write open. A known-committed apply whose later writer-close,
    lease-context, or receipt-finalization cleanup fails returns
    ``committed_cleanup_failed`` and never claims rollback.
    """

    receipt = _empty_receipt(dry_run=dry_run)
    committed = False
    try:
        journal_created_at_start, journal_created_at_end = _require_half_open_window(
            str(journal_created_at_start),
            str(journal_created_at_end),
            code="journal_window_invalid",
        )
        digest_started_at_start, digest_started_at_end = _require_half_open_window(
            str(digest_started_at_start),
            str(digest_started_at_end),
            code="digest_window_invalid",
        )
        journal_excluded = _optional_excluded_window(
            journal_excluded_start, journal_excluded_end, code="journal_window_invalid"
        )
        digest_excluded = _optional_excluded_window(
            digest_excluded_start, digest_excluded_end, code="digest_window_invalid"
        )
        source = _as_path(source_path)
        target = _as_path(target_path)
        _require_distinct_paths(source, target)
        source_info = _inspect_source(source)
        if source_info["file_sha256"] != str(expected_source_sha256 or ""):
            raise JournalSourceRestoreError("source_sha256_mismatch")
        if source_info["schema_digest"] != str(expected_schema_digest or ""):
            raise JournalSourceRestoreError("source_schema_digest_mismatch")
        if int(source_info["user_version"]) != int(expected_user_version):
            raise JournalSourceRestoreError("source_user_version_mismatch")
        _after_source_inspection_hook(source)
        (
            journals,
            digests,
            excluded_journals,
            excluded_digests,
            live_schema,
            live_user_version,
            live_sha,
        ) = _load_selection(
            source,
            journal_created_at_start=str(journal_created_at_start),
            journal_created_at_end=str(journal_created_at_end),
            digest_started_at_start=str(digest_started_at_start),
            digest_started_at_end=str(digest_started_at_end),
            journal_excluded=journal_excluded,
            digest_excluded=digest_excluded,
        )
        _require_checkpointed_file(source, wal_code="source_wal_present")
        live_identity = capture_regular_file_identity(source)
        if (
            live_sha != source_info["file_sha256"]
            or live_schema != source_info["schema_digest"]
            or int(live_user_version) != int(source_info["user_version"])
            or (
                "identity" in source_info
                and not identities_match(live_identity, source_info["identity"])
            )
        ):
            raise JournalSourceRestoreError("source_snapshot_changed")
        journal_digest, digest_digest = bind_selection(
            journals,
            digests,
            expected_journal_count=expected_journal_count,
            expected_digest_run_count=expected_digest_run_count,
            expected_journal_set_digest=expected_journal_set_digest,
            expected_digest_run_set_digest=expected_digest_run_set_digest,
        )
        apply_operation_id = str(operation_id or "").strip()
        if not dry_run and apply_operation_id and _wal_visible_sibling_present(target):
            # Crash-before-checkpoint: only a WAL-visible committed row for
            # this ID may skip the checkpoint gate. Rollback ``-journal``
            # and unknown or unreadable dirty targets still hit
            # target_wal_incoherent below.
            existing_lookup = _lookup_committed_operation(target, apply_operation_id)
            existing = _found_committed_row(existing_lookup)
            if existing is not None:
                receipt["operation_id"] = apply_operation_id
                bound_target_epoch = str(expected_target_epoch_digest or "")
                if not bound_target_epoch:
                    raise JournalSourceRestoreError("target_epoch_required")
                batch_digest = sha256_text(
                    canonical_json(
                        {
                            "digest_run_set_digest": digest_digest,
                            "journal_set_digest": journal_digest,
                            "source_epoch_digest": source_info["source_epoch_digest"],
                            "target_epoch_digest": bound_target_epoch,
                        }
                    )
                )
                receipt.update(
                    {
                        "batch_digest": batch_digest,
                        "digest_run_set_digest": digest_digest,
                        "journal_set_digest": journal_digest,
                        "source_epoch_digest": source_info["source_epoch_digest"],
                    }
                )
                return _finish(
                    _reconcile_committed_operation(
                        target=target,
                        existing=existing,
                        receipt=receipt,
                        source_info=source_info,
                        journal_created_at_start=journal_created_at_start,
                        journal_created_at_end=journal_created_at_end,
                        digest_started_at_start=digest_started_at_start,
                        digest_started_at_end=digest_started_at_end,
                        journal_excluded=journal_excluded,
                        digest_excluded=digest_excluded,
                        expected_journal_count=expected_journal_count,
                        expected_digest_run_count=expected_digest_run_count,
                        expected_journal_set_digest=expected_journal_set_digest,
                        expected_digest_run_set_digest=expected_digest_run_set_digest,
                        expected_target_epoch_digest=str(expected_target_epoch_digest or ""),
                        batch_digest=batch_digest,
                        connection_factory=connection_factory,
                    )
                )
        _require_checkpointed_file(target, wal_code="target_wal_incoherent")
        target_epoch = compute_target_epoch(target)
        _require_target_schema_contract(source_info, target_epoch)
        target_conn = open_checkpointed_target_reader(target)
        source_conn = open_immutable_source_connection(source)
        try:
            require_digest_references(
                journals,
                digests,
                source_lookup=lambda run_id: lookup_source_digest(source_conn, run_id),
                target_lookup=lambda run_id: lookup_target_digest(target_conn, run_id),
            )
            if excluded_journals:
                require_excluded_tail(
                    excluded_journals,
                    lambda row: lookup_target_journal(target_conn, row),
                    journal_semantic_record,
                    missing_code="excluded_tail_missing",
                    conflict_code="excluded_tail_conflict",
                )
            if excluded_digests:
                require_excluded_tail(
                    excluded_digests,
                    lambda row: lookup_target_digest(target_conn, str(row["id"])),
                    digest_run_logical_record,
                    missing_code="excluded_tail_missing",
                    conflict_code="excluded_tail_conflict",
                )
            _missing_journals, journal_already, journal_conflicts = classify_rows(
                journals,
                lambda row: lookup_target_journal(target_conn, row),
                journal_semantic_record,
            )
            _missing_digests, digest_already, digest_conflicts = classify_rows(
                digests,
                lambda row: lookup_target_digest(target_conn, str(row["id"])),
                digest_run_logical_record,
            )
        finally:
            source_conn.close()
            target_conn.close()
        _require_checkpointed_file(source, wal_code="source_wal_present")
        _require_checkpointed_file(target, wal_code="target_wal_incoherent")
        bound_target_epoch = str(expected_target_epoch_digest or target_epoch["epoch_digest"])
        batch_digest = sha256_text(
            canonical_json(
                {
                    "digest_run_set_digest": digest_digest,
                    "journal_set_digest": journal_digest,
                    "source_epoch_digest": source_info["source_epoch_digest"],
                    "target_epoch_digest": bound_target_epoch,
                }
            )
        )
        receipt.update(
            {
                "batch_digest": batch_digest,
                "digest_run_already_present_count": digest_already,
                "digest_run_conflict_count": digest_conflicts,
                "digest_run_selected_count": len(digests),
                "digest_run_set_digest": digest_digest,
                "journal_already_present_count": journal_already,
                "journal_conflict_count": journal_conflicts,
                "journal_selected_count": len(journals),
                "journal_set_digest": journal_digest,
                "source_epoch_digest": source_info["source_epoch_digest"],
                "target_epoch_digest": target_epoch["epoch_digest"],
            }
        )
        if dry_run:
            if journal_conflicts:
                return _not_ready(receipt, "journal_logical_conflict")
            if digest_conflicts:
                return _not_ready(receipt, "digest_logical_conflict")
            receipt.update(
                {
                    "ok": True,
                    "stage": "plan",
                    "status": "ok",
                    "verdict": "ready",
                }
            )
            return _finish(receipt)

        if not maintenance_confirmed:
            raise JournalSourceRestoreError("confirmation_required")
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            raise JournalSourceRestoreError("operation_id_required")
        receipt["operation_id"] = operation_id
        existing_lookup = _lookup_committed_operation(target, operation_id)
        existing = _found_committed_row(existing_lookup)
        if existing is None and getattr(existing_lookup, "status", None) == "indeterminate":
            raise JournalSourceRestoreError("operator_ledger_unavailable")
        if existing is not None:
            return _finish(
                _reconcile_committed_operation(
                    target=target,
                    existing=existing,
                    receipt=receipt,
                    source_info=source_info,
                    journal_created_at_start=journal_created_at_start,
                    journal_created_at_end=journal_created_at_end,
                    digest_started_at_start=digest_started_at_start,
                    digest_started_at_end=digest_started_at_end,
                    journal_excluded=journal_excluded,
                    digest_excluded=digest_excluded,
                    expected_journal_count=expected_journal_count,
                    expected_digest_run_count=expected_digest_run_count,
                    expected_journal_set_digest=expected_journal_set_digest,
                    expected_digest_run_set_digest=expected_digest_run_set_digest,
                    expected_target_epoch_digest=str(expected_target_epoch_digest or ""),
                    batch_digest=batch_digest,
                    connection_factory=connection_factory,
                )
            )
        if not str(expected_target_epoch_digest or "").strip():
            raise JournalSourceRestoreError("target_epoch_required")
        if prewrite_backup_path in (None, ""):
            raise JournalSourceRestoreError("prewrite_backup_required")
        if target_epoch["epoch_digest"] != str(expected_target_epoch_digest):
            raise JournalSourceRestoreError("target_epoch_stale")
        if journal_conflicts:
            raise JournalSourceRestoreError("journal_logical_conflict")
        if digest_conflicts:
            raise JournalSourceRestoreError("digest_logical_conflict")
        from . import writer_lease as writer_lease_module

        if JOURNAL_SOURCE_RESTORE_WRITER_ROLE not in writer_lease_module.ALLOWED_TRUTH_WRITER_ROLES:
            raise JournalSourceRestoreError("writer_role_unavailable")
        lease_token = _activation_lease_token(target)
        backup = Path(prewrite_backup_path)
        mapping: dict[str, Any] = {"mapping_count": 0, "mapping_digest": "", "pairs": []}
        journal_inserted = 0
        digest_inserted = 0
        remapping = False
        fingerprint = ""
        committed = False
        with holding_truth_writer_lease(
            target.parent, role=JOURNAL_SOURCE_RESTORE_WRITER_ROLE
        ):
            _require_checkpointed_file(target, wal_code="target_wal_incoherent")
            bound_identity = capture_regular_file_identity(target)
            writer = _open_target_writer(
                target,
                lease_token=lease_token,
                connection_factory=connection_factory,
            )
            try:
                _require_operator_ledger(writer)
                writer.execute("BEGIN IMMEDIATE")
                live_epoch = compute_target_epoch_from_connection(
                    writer, file_sha256=str(bound_identity["sha256"])
                )
                if live_epoch["epoch_digest"] != str(expected_target_epoch_digest):
                    raise JournalSourceRestoreError("target_epoch_stale")
                backup_result = verified_online_backup(target, backup)
                receipt["backup_digest"] = str(
                    backup_result.get("backup_logical_fingerprint")
                    or backup_result.get("logical_fingerprint")
                    or sha256_file(backup)
                )
                fingerprint = _compute_request_fingerprint(
                    source_info=source_info,
                    journal_created_at_start=journal_created_at_start,
                    journal_created_at_end=journal_created_at_end,
                    digest_started_at_start=digest_started_at_start,
                    digest_started_at_end=digest_started_at_end,
                    journal_excluded=journal_excluded,
                    digest_excluded=digest_excluded,
                    expected_journal_count=expected_journal_count,
                    expected_digest_run_count=expected_digest_run_count,
                    expected_journal_set_digest=expected_journal_set_digest,
                    expected_digest_run_set_digest=expected_digest_run_set_digest,
                    expected_target_epoch_digest=str(expected_target_epoch_digest or ""),
                    batch_digest=batch_digest,
                    backup_digest=receipt["backup_digest"],
                )
                receipt["request_fingerprint"] = fingerprint
                # Restore inserts journal/digest rows only. journal_session_digest_state
                # is not copied. insert_missing_rows DELETE-resets a session
                # cursor only when restored unprocessed rows or remaps make the
                # prior high cursor unsafe, then reports cursor_reset_count.
                journal_inserted, digest_inserted, remapping, mapping = insert_missing_rows(
                    writer,
                    journals=journals,
                    digests=digests,
                    operation_id=operation_id,
                    request_fingerprint=fingerprint,
                )
                _verify_post_invariants(
                    writer,
                    before=live_epoch,
                    journal_inserted=journal_inserted,
                    digest_inserted=digest_inserted,
                )
                ledger_result = {
                    "backup_digest": receipt["backup_digest"],
                    "batch_digest": batch_digest,
                    "cursor_reset_count": int(mapping.get("cursor_reset_count") or 0),
                    "cursor_reset_digest": str(mapping.get("cursor_reset_digest") or ""),
                    "digest_run_inserted_count": digest_inserted,
                    "journal_inserted_count": journal_inserted,
                    "mapping_count": mapping["mapping_count"],
                    "mapping_digest": mapping["mapping_digest"],
                    "pairs": mapping["pairs"],
                    "remapping_occurred": bool(remapping and journal_inserted),
                    "request_fingerprint": fingerprint,
                    "verdict": "applied" if journal_inserted or digest_inserted else "already_present",
                }
                record_committed_operator_operation(
                    writer,
                    operation_id=operation_id,
                    operation_kind="journal.source_restore",
                    target_ref="journal.source_restore",
                    before={"target_epoch_digest": live_epoch["epoch_digest"]},
                    result=ledger_result,
                    backup_path=str(backup),
                    request_fingerprint=fingerprint,
                    commit=False,
                )
                try:
                    writer.commit()
                    committed = True
                except Exception:
                    outcome = _resolve_commit_outcome(
                        _lookup_committed_operation(target, operation_id),
                        fingerprint=fingerprint,
                        writer=writer,
                    )
                    if outcome == "committed":
                        committed = True
                    elif outcome == "rolled_back":
                        raise JournalSourceRestoreError("apply_rolled_back")
                    else:
                        raise JournalSourceRestoreError("commit_outcome_unknown")
                if committed:
                    try:
                        _mirror_committed_receipt(
                            writer, db_path=target, operation_id=operation_id
                        )
                        receipt["receipt_state"] = "mirrored"
                    except Exception:
                        receipt["receipt_state"] = "pending"
                        receipt["receipt_repair_required"] = True
                    _bind_committed_apply_receipt(
                        receipt,
                        operation_id=operation_id,
                        fingerprint=fingerprint,
                        journal_inserted=journal_inserted,
                        digest_inserted=digest_inserted,
                        remapping=remapping,
                        mapping=mapping,
                    )
            except Exception:
                if not committed and writer.in_transaction:
                    writer.rollback()
                if committed:
                    outcome = _resolve_commit_outcome(
                        _lookup_committed_operation(target, operation_id),
                        fingerprint=fingerprint,
                        writer=writer,
                    )
                    if outcome != "committed":
                        raise JournalSourceRestoreError("commit_outcome_unknown")
                    _bind_committed_apply_receipt(
                        receipt,
                        operation_id=operation_id,
                        fingerprint=fingerprint,
                        journal_inserted=journal_inserted,
                        digest_inserted=digest_inserted,
                        remapping=remapping,
                        mapping=mapping,
                    )
                else:
                    raise
            finally:
                # Never swallow close(): the function-level committed
                # boundary classifies cleanup failure vs rollback.
                writer.close()
        if receipt.get("receipt_repair_required"):
            receipt["error_code"] = "committed_receipt_debt"
        return _finish(receipt)
    except JournalSourceRestoreError as exc:
        return _fail(receipt, exc.code)
    except TruthWriterBusyError:
        return _fail(receipt, "truth_writer_busy")
    except SqliteBackupError:
        return _fail(receipt, "prewrite_backup_failed")
    except Exception:
        if committed:
            return _mark_committed_cleanup_failure(receipt)
        return _fail(receipt, "apply_rolled_back" if not dry_run else "source_unhealthy")


__all__ = [
    "DIGEST_RUN_LOGICAL_FIELDS",
    "JOURNAL_SEMANTIC_FIELDS",
    "JOURNAL_SET_DIGEST_FIELDS",
    "JOURNAL_SOURCE_RESTORE_WRITER_ROLE",
    "JournalSourceRestoreError",
    "canonical_json",
    "compute_digest_run_set_digest",
    "compute_journal_set_digest",
    "compute_schema_digest",
    "compute_target_epoch",
    "compute_target_epoch_from_connection",
    "digest_run_logical_record",
    "journal_identity_record",
    "insert_missing_rows",
    "journal_semantic_record",
    "mark_source_restore_cleanup_failure",
    "mirror_operator_receipt",
    "open_checkpointed_target_reader",
    "open_immutable_source_connection",
    "redact_source_restore_payload",
    "run_journal_source_restore",
    "source_restore_error_receipt",
    "sha256_file",
    "sha256_text",
]
