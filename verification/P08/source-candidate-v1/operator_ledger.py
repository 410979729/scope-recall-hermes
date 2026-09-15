"""Authoritative SQLite ledger and idempotent filesystem receipt mirror.

A mutating operator command records its committed result in this table inside the
same SQLite transaction as the business mutation.  JSON receipts are derived
mirrors written only after commit.  Their deterministic operation-id path makes
a crash after rename but before the ledger state update safely retryable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from .capture_filters import sanitize_report_text
    from .sqlite_schema import execute_script_transaction_neutral
except ImportError:  # pragma: no cover - direct source-script fallback
    from capture_filters import sanitize_report_text
    from sqlite_schema import execute_script_transaction_neutral

OPERATOR_LEDGER_SCHEMA_VERSION = 10802
OPERATOR_LEDGER_MIGRATION_ID = "0004_operator_ledger_v1_8_0"
OPERATOR_LEDGER_MIGRATION_PLUGIN_VERSION = "1.8.0"
OPERATOR_LEDGER_MIGRATION_DESCRIPTION = (
    "Authoritative committed operator ledger and receipt mirror debt"
)
_RECEIPT_STATES = ("pending", "mirrored", "failed")
_MAX_JSON_BYTES = 131_072
_REQUIRED_COLUMNS = {
    "operation_id",
    "operation_kind",
    "target_ref",
    "request_fingerprint",
    "before_json",
    "result_json",
    "backup_path",
    "status",
    "receipt_state",
    "receipt_path",
    "receipt_attempts",
    "receipt_last_error",
    "committed_at",
    "updated_at",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_GENERIC_RECEIPT_KINDS = frozenset(
    {
        "journal.source_restore",
        "relation.legacy_cleanup",
        "privacy_purge.deny",
        "privacy_purge.erase",
    }
)
_PLAYBOOK_RECEIPT_SCHEMA = "playbook_operator_receipt.v2"
_OPERATOR_RECEIPT_SCHEMA = "operator_receipt.v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_operations'"
        ).fetchone()
        is not None
    )


def ensure_operator_ledger_schema(conn: sqlite3.Connection) -> None:
    """Create additive ledger schema without committing caller-owned work."""

    execute_script_transaction_neutral(
        conn,
        """
        CREATE TABLE IF NOT EXISTS operator_operations (
            operation_id TEXT PRIMARY KEY,
            operation_kind TEXT NOT NULL,
            target_ref TEXT NOT NULL DEFAULT '',
            request_fingerprint TEXT NOT NULL,
            before_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL,
            backup_path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'committed'
                CHECK(status = 'committed'),
            receipt_state TEXT NOT NULL DEFAULT 'pending'
                CHECK(receipt_state IN ('pending','mirrored','failed')),
            receipt_path TEXT NOT NULL DEFAULT '',
            receipt_attempts INTEGER NOT NULL DEFAULT 0,
            receipt_last_error TEXT NOT NULL DEFAULT '',
            committed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_operator_operations_receipt
            ON operator_operations(receipt_state, committed_at, operation_id);
        CREATE INDEX IF NOT EXISTS idx_operator_operations_target
            ON operator_operations(operation_kind, target_ref, committed_at);
        """,
    )


def operator_ledger_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Inspect the ledger schema contract without mutating SQLite."""

    if not _table_exists(conn):
        return {
            "current": False,
            "schema_version": OPERATOR_LEDGER_SCHEMA_VERSION,
            "table_present": False,
            "missing_columns": sorted(_REQUIRED_COLUMNS),
        }
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(operator_operations)")
    }
    missing = sorted(_REQUIRED_COLUMNS - columns)
    return {
        "current": not missing,
        "schema_version": OPERATOR_LEDGER_SCHEMA_VERSION,
        "table_present": True,
        "missing_columns": missing,
    }


def _safe_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "[depth-limited]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_report_text(value)[:8_000]
    if isinstance(value, Mapping):
        return {
            sanitize_report_text(str(key))[:200]: _safe_json_value(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_json_value(item, depth=depth + 1) for item in list(value)[:500]]
    return sanitize_report_text(str(value))[:2_000]


def _canonical_json(value: Any) -> str:
    encoded = json.dumps(
        _safe_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("operator ledger JSON payload exceeds 128 KiB")
    return encoded


def _clean_operation_id(value: str) -> str:
    cleaned = str(value or "").strip()
    if not _SAFE_ID.fullmatch(cleaned):
        raise ValueError("operation_id must contain only letters, digits, dot, underscore, or dash")
    return cleaned


def _clean_kind(value: str) -> str:
    cleaned = sanitize_report_text(str(value or "").strip())[:160]
    if not cleaned:
        raise ValueError("operation_kind is required")
    return cleaned


def _clean_fingerprint(value: str, *, fallback_payload: str) -> str:
    cleaned = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", cleaned):
        return cleaned
    return hashlib.sha256(fallback_payload.encode("utf-8")).hexdigest()


def record_committed_operator_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    operation_kind: str,
    target_ref: str,
    before: Mapping[str, Any] | None,
    result: Mapping[str, Any],
    backup_path: str,
    request_fingerprint: str = "",
    commit: bool = False,
) -> dict[str, Any]:
    """Record a committed-intent row inside the caller's business transaction.

    The row says ``status=committed`` because it becomes visible only if the same
    SQLite commit that contains the business mutation succeeds.
    """

    op_id = _clean_operation_id(operation_id)
    kind = _clean_kind(operation_kind)
    target = sanitize_report_text(str(target_ref or ""))[:500]
    before_json = _canonical_json(dict(before or {}))
    result_json = _canonical_json(dict(result))
    backup = sanitize_report_text(str(backup_path or ""))[:4_000]
    fingerprint = _clean_fingerprint(
        request_fingerprint,
        fallback_payload=f"{kind}\n{target}\n{before_json}\n{result_json}",
    )
    now = _now_iso()
    values = (
        op_id,
        kind,
        target,
        fingerprint,
        before_json,
        result_json,
        backup,
        now,
        now,
    )
    try:
        conn.execute(
            """
            INSERT INTO operator_operations(
                operation_id, operation_kind, target_ref, request_fingerprint,
                before_json, result_json, backup_path, status, receipt_state,
                receipt_path, receipt_attempts, receipt_last_error,
                committed_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'committed', 'pending', '', 0, '', ?, ?)
            """,
            values,
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            """
            SELECT operation_kind, target_ref, request_fingerprint, before_json,
                   result_json, backup_path
            FROM operator_operations WHERE operation_id = ?
            """,
            (op_id,),
        ).fetchone()
        expected = values[1:7]
        if existing is None or tuple(existing) != expected:
            raise ValueError("operation_id already belongs to a different operation") from None
    if commit:
        conn.commit()
    return {
        "operation_id": op_id,
        "operation_kind": kind,
        "target_ref": target,
        "status": "committed",
        "receipt_state": "pending",
        "committed_at": now,
    }


def _row_dict(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT * FROM operator_operations WHERE operation_id = ?",
        (_clean_operation_id(operation_id),),
    )
    row = cursor.fetchone()
    if row is None:
        raise KeyError(f"unknown operator operation: {operation_id}")
    names = [str(item[0]) for item in cursor.description or []]
    return dict(zip(names, tuple(row), strict=True))


def _uses_generic_operator_receipt(operation_kind: str) -> bool:
    """Name non-playbook source-restore mirrors as generic operator receipts.

    Existing playbook kinds keep ``playbook_operator_receipt.v2`` and
    ``playbooks.*`` filenames byte-compatible.
    """

    return str(operation_kind) in _GENERIC_RECEIPT_KINDS


def _mirror_result_payload(operation_kind: str, result: Any) -> Any:
    """Copy ledger result for the filesystem mirror.

    Generic source-restore mirrors omit the bounded remap pair list. The
    private ``operator_operations.result_json`` retains those pairs. Playbook
    receipt bytes stay unchanged.
    """

    if not _uses_generic_operator_receipt(operation_kind):
        return result
    if not isinstance(result, dict):
        return result
    cleaned = dict(result)
    cleaned.pop("pairs", None)
    return cleaned


def _receipt_payload(row: Mapping[str, Any], *, db_path: Path) -> dict[str, Any]:
    try:
        before = json.loads(str(row["before_json"]))
        result = json.loads(str(row["result_json"]))
    except json.JSONDecodeError as exc:
        raise ValueError("operator ledger JSON is corrupt") from exc
    operation_kind = str(row["operation_kind"])
    schema = (
        _OPERATOR_RECEIPT_SCHEMA
        if _uses_generic_operator_receipt(operation_kind)
        else _PLAYBOOK_RECEIPT_SCHEMA
    )
    return {
        "schema_version": schema,
        "receipt_state": "mirrored",
        "operation_id": str(row["operation_id"]),
        "committed_at": str(row["committed_at"]),
        "db_path": str(db_path.resolve()),
        "action": operation_kind.rsplit(".", 1)[-1],
        "operation_kind": operation_kind,
        "target_ref": str(row["target_ref"]),
        "request_fingerprint": str(row["request_fingerprint"]),
        "backup_path": str(row["backup_path"]),
        "before": before,
        "result": _mirror_result_payload(operation_kind, result),
    }


def _receipt_path(db_path: Path, row: Mapping[str, Any]) -> Path:
    action = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(row["operation_kind"]).rsplit(".", 1)[-1])
    prefix = (
        "operator"
        if _uses_generic_operator_receipt(str(row["operation_kind"]))
        else "playbooks"
    )
    return db_path.resolve().parent / "receipts" / (
        f"{prefix}.{action}.{row['operation_id']}.json"
    )


def read_operator_operation(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    """Read one authoritative operation row, or None when absent."""

    if not _table_exists(conn):
        return None
    try:
        return _row_dict(conn, operation_id)
    except (KeyError, ValueError):
        return None


def _serialize_receipt(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _receipt_publish_path(path: Path) -> str | Path:
    """Use Win32 extended paths for hard-link publication when required."""

    if os.name != "nt":
        return path
    resolved = str(path.resolve())
    if resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


def _receipt_requires_extended_io(path: Path) -> bool:
    return os.name == "nt" and len(str(_receipt_publish_path(path))) >= 260


def _receipt_path_exists(path: Path) -> bool:
    if _receipt_requires_extended_io(path):
        return os.path.exists(_receipt_publish_path(path))
    return path.exists()


def _receipt_path_is_symlink(path: Path) -> bool:
    if _receipt_requires_extended_io(path):
        return os.path.islink(_receipt_publish_path(path))
    return path.is_symlink()


def _read_receipt_bytes(path: Path) -> bytes:
    if not _receipt_requires_extended_io(path):
        return path.read_bytes()
    with open(_receipt_publish_path(path), "rb") as handle:
        return handle.read()


def _publish_receipt_link(source: Path, destination: Path) -> None:
    """Atomically publish one receipt without overwriting raced evidence."""

    source_path = _receipt_publish_path(source)
    destination_path = _receipt_publish_path(destination)
    if os.name != "nt" or max(len(str(source_path)), len(str(destination_path))) < 260:
        os.link(source, destination)
        return

    # CPython's os.link can still route through the legacy Win32 path handling
    # on some supported Windows/Python combinations.  CreateHardLinkW accepts
    # the extended paths above while preserving the required no-overwrite
    # publication semantics.
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_hard_link = kernel32.CreateHardLinkW
    create_hard_link.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p]
    create_hard_link.restype = ctypes.c_int
    if create_hard_link(str(destination_path), str(source_path), None):
        return
    error = ctypes.get_last_error()
    message = ctypes.FormatError(error)
    if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
        raise FileExistsError(error, message, destination)
    if error in {2, 3}:  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
        raise FileNotFoundError(error, message, destination)
    if error == 5:  # ERROR_ACCESS_DENIED
        raise PermissionError(error, message, destination)
    raise OSError(error, message, destination)


def _write_receipt_mirror(path: Path, content: bytes) -> None:
    os.makedirs(_receipt_publish_path(path.parent), exist_ok=True)
    if _receipt_path_exists(path) or _receipt_path_is_symlink(path):
        if _receipt_path_is_symlink(path):
            raise FileExistsError(f"receipt path is not regular evidence: {path}")
        if _read_receipt_bytes(path) != content:
            raise FileExistsError(f"receipt path contains different evidence: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        # The unique staging name can cross MAX_PATH even when the stable
        # receipt itself does not, so open and remove it through the same
        # extended-path adapter used for publication.
        with open(_receipt_publish_path(temporary), "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Hard-link publication is atomic and never overwrites evidence
            # that raced with the earlier existence check.
            _publish_receipt_link(temporary, path)
        except FileExistsError:
            if _receipt_path_is_symlink(path) or _read_receipt_bytes(path) != content:
                raise FileExistsError(
                    f"receipt path contains different evidence: {path}"
                ) from None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(_receipt_publish_path(temporary))
        except FileNotFoundError:
            pass


def _mark_receipt_mirrored(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    receipt_path: Path,
) -> None:
    now = _now_iso()
    conn.execute(
        """
        UPDATE operator_operations
        SET receipt_state='mirrored', receipt_path=?, receipt_attempts=receipt_attempts+1,
            receipt_last_error='', updated_at=?
        WHERE operation_id=? AND status='committed'
        """,
        (str(receipt_path), now, operation_id),
    )
    conn.commit()


def _record_receipt_failure(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    error: BaseException,
    max_attempts: int,
) -> None:
    row = conn.execute(
        "SELECT receipt_attempts FROM operator_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    attempts = int(row[0] or 0) + 1 if row is not None else 1
    state = "failed" if attempts >= max(1, int(max_attempts)) else "pending"
    conn.execute(
        """
        UPDATE operator_operations
        SET receipt_state=?, receipt_attempts=?, receipt_last_error=?, updated_at=?
        WHERE operation_id=? AND status='committed'
        """,
        (
            state,
            attempts,
            sanitize_report_text(str(error))[:500],
            _now_iso(),
            operation_id,
        ),
    )
    conn.commit()


def mirror_operator_receipt(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    operation_id: str,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Write or verify one deterministic receipt, then mark its ledger row."""

    row = _row_dict(conn, operation_id)
    if str(row["status"]) != "committed":
        raise ValueError("operator operation is not committed")
    path = _receipt_path(Path(db_path), row)
    payload = _receipt_payload(row, db_path=Path(db_path))
    content = _serialize_receipt(payload)
    try:
        _write_receipt_mirror(path, content)
        _mark_receipt_mirrored(
            conn,
            operation_id=str(row["operation_id"]),
            receipt_path=path,
        )
    except BaseException as exc:
        try:
            _record_receipt_failure(
                conn,
                operation_id=str(row["operation_id"]),
                error=exc,
                max_attempts=max_attempts,
            )
        except BaseException:
            pass
        raise
    return {
        "operation_id": str(row["operation_id"]),
        "receipt_state": "mirrored",
        "receipt_path": str(path),
        "receipt_sha256": hashlib.sha256(content).hexdigest(),
    }


def recover_operator_receipts(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    limit: int = 50,
    include_failed: bool = False,
) -> dict[str, Any]:
    """Retry a bounded number of pending/failed deterministic receipt mirrors."""

    states = ["pending"]
    if include_failed:
        states.append("failed")
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute(
        f"""
        SELECT operation_id FROM operator_operations
        WHERE status='committed' AND receipt_state IN ({placeholders})
        ORDER BY committed_at, operation_id
        LIMIT ?
        """,
        [*states, max(1, min(500, int(limit or 50)))],
    ).fetchall()
    mirrored = 0
    failed = 0
    errors: list[dict[str, str]] = []
    for row in rows:
        operation_id = str(row[0])
        try:
            mirror_operator_receipt(
                conn,
                db_path=Path(db_path),
                operation_id=operation_id,
            )
            mirrored += 1
        except BaseException as exc:
            failed += 1
            errors.append(
                {
                    "operation_id": operation_id,
                    "error": sanitize_report_text(str(exc))[:300],
                }
            )
    return {
        "attempted": len(rows),
        "mirrored": mirrored,
        "failed": failed,
        "errors": errors,
    }


def operator_ledger_report(
    conn: sqlite3.Connection,
    *,
    sample_limit: int = 5,
) -> dict[str, Any]:
    """Return redacted receipt-mirror debt for doctor/stats."""

    if not _table_exists(conn):
        return {
            "status": "schema_missing",
            "total": 0,
            "pending": 0,
            "mirrored": 0,
            "failed": 0,
            "unresolved": 0,
            "oldest_unresolved_age_seconds": 0.0,
            "samples": [],
        }
    counts = {state: 0 for state in _RECEIPT_STATES}
    for row in conn.execute(
        "SELECT receipt_state, COUNT(*) FROM operator_operations GROUP BY receipt_state"
    ).fetchall():
        counts[str(row[0])] = int(row[1] or 0)
    total = sum(counts.values())
    unresolved = counts["pending"] + counts["failed"]
    oldest = conn.execute(
        """
        SELECT MIN(committed_at) FROM operator_operations
        WHERE receipt_state IN ('pending','failed')
        """
    ).fetchone()[0]
    age = 0.0
    if oldest:
        try:
            parsed = datetime.fromisoformat(str(oldest))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
        except ValueError:
            age = 0.0
    samples = [
        {
            "operation_id": str(row[0]),
            "operation_kind": str(row[1]),
            "target_ref": sanitize_report_text(str(row[2] or ""))[:160],
            "receipt_state": str(row[3]),
            "receipt_attempts": int(row[4] or 0),
            "receipt_last_error": sanitize_report_text(str(row[5] or ""))[:240],
            "committed_at": str(row[6]),
        }
        for row in conn.execute(
            """
            SELECT operation_id, operation_kind, target_ref, receipt_state,
                   receipt_attempts, receipt_last_error, committed_at
            FROM operator_operations
            WHERE receipt_state IN ('pending','failed')
            ORDER BY committed_at, operation_id
            LIMIT ?
            """,
            (max(0, int(sample_limit)),),
        ).fetchall()
    ]
    return {
        "status": "debt" if unresolved else "ready",
        "total": total,
        **counts,
        "unresolved": unresolved,
        "oldest_unresolved_age_seconds": round(age, 3),
        "samples": samples,
    }


__all__ = [
    "OPERATOR_LEDGER_MIGRATION_DESCRIPTION",
    "OPERATOR_LEDGER_MIGRATION_ID",
    "OPERATOR_LEDGER_MIGRATION_PLUGIN_VERSION",
    "OPERATOR_LEDGER_SCHEMA_VERSION",
    "ensure_operator_ledger_schema",
    "mirror_operator_receipt",
    "operator_ledger_report",
    "operator_ledger_schema_status",
    "read_operator_operation",
    "record_committed_operator_operation",
    "recover_operator_receipts",
]
