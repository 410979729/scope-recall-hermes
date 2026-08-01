"""Cooperative maintenance lease for Scope Recall SQLite writers.

Activation creates a durable token file before snapshotting. Writable connections
install an authorizer that denies DML/DDL while that lease exists, unless that
specific connection is explicitly constructed with the matching owner token. A
final logical SQLite fingerprint remains the fail-closed guard for uncooperative
raw connections.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

try:
    from .operator_ledger import record_committed_operator_operation
except ImportError:  # pragma: no cover - direct source-script fallback
    from operator_ledger import record_committed_operator_operation


ACTIVATION_LEASE_FILENAME = ".activation-maintenance.json"
ACTIVATION_AUTHORIZATION_FUNCTION = "scope_recall_activation_authorized"
ACTIVATION_GUARD_TRIGGER_PREFIX = "scope_recall_activation_guard_"

_WRITE_ACTIONS = frozenset(
    value
    for name in (
        "SQLITE_ALTER_TABLE",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_DELETE",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_INSERT",
        "SQLITE_REINDEX",
        "SQLITE_UPDATE",
    )
    if isinstance((value := getattr(sqlite3, name, None)), int)
)

_IS_WINDOWS = os.name == "nt"


def _connect_truth_database(
    database_path: Path,
    *,
    mode: Literal["ro", "rw", "rwc"],
    timeout: float,
) -> sqlite3.Connection:
    """Open truth storage lazily to avoid the deliberate lease/connection cycle."""

    try:
        from .truth_connection import connect_truth_database
    except ImportError:  # pragma: no cover - direct source-script fallback
        from truth_connection import connect_truth_database

    return connect_truth_database(database_path, mode=mode, timeout=timeout)


class MaintenanceLeaseError(RuntimeError):
    """Raised when a writer is blocked by an active activation lease."""


def activation_lease_path(database_path: Path) -> Path:
    return database_path.parent / ACTIVATION_LEASE_FILENAME


def read_activation_lease(database_path: Path) -> dict[str, Any] | None:
    path = activation_lease_path(database_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaintenanceLeaseError(
            f"activation maintenance lease is unreadable: {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or not str(payload.get("token") or ""):
        raise MaintenanceLeaseError(f"activation maintenance lease is invalid: {path}")
    return payload


def _windows_pid_liveness(process_id: int) -> str:
    """Probe a Windows PID without emitting console control events.

    ``os.kill(pid, 0)`` is not a harmless existence check on Windows because
    signal zero is ``CTRL_C_EVENT``. A child doctor probing a process-group
    owner can therefore interrupt the owner it is validating. Querying the
    process handle is read-only and treats access-denied or unknown states as
    non-recoverable rather than declaring the owner dead.
    """

    import ctypes
    from ctypes import wintypes

    win_dll = getattr(ctypes, "WinDLL", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if win_dll is None or get_last_error is None:
        return "unknown"

    process_query_limited_information = 0x1000
    error_access_denied = 5
    error_invalid_parameter = 87
    still_active = 259
    kernel32 = win_dll("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, process_id)
    if not handle:
        error_code = int(get_last_error())
        if error_code == error_invalid_parameter:
            return "dead"
        if error_code == error_access_denied:
            return "alive"
        return "unknown"
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code_process(handle, ctypes.byref(exit_code)):
            return "unknown"
        return "alive" if exit_code.value == still_active else "dead"
    finally:
        close_handle(handle)


def _pid_liveness(pid: Any) -> str:
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return "unknown"
    if process_id <= 0:
        return "unknown"
    if _IS_WINDOWS:
        return _windows_pid_liveness(process_id)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        # POSIX may use a generic error other than ESRCH. Unknown liveness
        # remains active/non-recoverable so recovery stays fail closed.
        return "unknown"
    return "alive"


def _lease_age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        created = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds())


def activation_lease_status(
    database_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Report lease owner liveness without exposing the capability token."""

    path = activation_lease_path(database_path)
    payload = read_activation_lease(database_path)
    if payload is None:
        return {
            "status": "absent",
            "path": str(path),
            "active": False,
            "recoverable": False,
            "owner_liveness": "absent",
            "pid": 0,
            "created_at": "",
            "age_seconds": None,
        }
    liveness = _pid_liveness(payload.get("pid"))
    stale = liveness == "dead"
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return {
        "status": "stale" if stale else "active",
        "path": str(path),
        "active": not stale,
        "recoverable": stale,
        "owner_liveness": liveness,
        "pid": pid,
        "created_at": str(payload.get("created_at") or ""),
        "age_seconds": _lease_age_seconds(payload.get("created_at"), now=now),
    }


def _restore_lease_payload(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def recover_stale_activation_lease(
    database_path: Path,
    *,
    apply: bool = False,
    operation_id: str = "",
    reason: str = "",
    backup_path: str = "",
) -> dict[str, Any]:
    """Plan or recover a lease whose recorded PID is definitely dead.

    Apply removes guard triggers and the lease under one exclusive SQLite
    transaction. If commit fails after the file unlink, the original lease is
    restored so recovery cannot silently weaken the fail-closed boundary.
    """

    expanded_path = Path(database_path).expanduser()
    db_path = Path(os.path.abspath(os.fspath(expanded_path)))
    if db_path.is_symlink() or db_path.parent.is_symlink():
        raise MaintenanceLeaseError(
            "activation lease recovery cannot mutate a symlinked truth store"
        )
    status = activation_lease_status(db_path)
    result = {
        **status,
        "apply": bool(apply),
        "recovered": False,
        "guards_removed": 0,
    }
    if not apply:
        return result
    if not bool(status.get("recoverable")):
        raise RuntimeError("activation maintenance lease is not stale and cannot be recovered")
    original = read_activation_lease(db_path)
    if original is None:
        return {**result, "status": "absent"}
    clean_operation_id = str(operation_id or "").strip()
    clean_reason = str(reason or "").strip()[:500]
    clean_backup_path = str(backup_path or "").strip()[:4000]
    if not clean_operation_id or len(clean_reason) < 8 or not clean_backup_path:
        raise ValueError(
            "audited lease recovery requires operation_id, a specific reason, and backup_path"
        )
    expected_token = str(original.get("token") or "")
    lease_path = activation_lease_path(db_path)
    conn = _connect_truth_database(db_path, mode="rw", timeout=10.0)
    unlinked = False
    try:
        conn.execute("BEGIN EXCLUSIVE")
        current = read_activation_lease(db_path)
        if current is None or not hmac.compare_digest(
            str(current.get("token") or ""), expected_token
        ):
            raise RuntimeError("activation maintenance lease changed during recovery")
        removed = remove_activation_guard_triggers(conn)
        lease_path.unlink()
        unlinked = True
        operator_operation = record_committed_operator_operation(
            conn,
            operation_id=clean_operation_id,
            operation_kind="activation_lease.recover_stale",
            target_ref=str(db_path),
            before={
                "lease_status": status,
                "reason": clean_reason,
                "guard_count": len(removed),
            },
            result={
                "recovered": True,
                "guards_removed": len(removed),
            },
            backup_path=clean_backup_path,
            commit=False,
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        if unlinked:
            _restore_lease_payload(lease_path, original)
        raise
    finally:
        conn.close()
    return {
        **result,
        "status": "recovered",
        "active": False,
        "recoverable": False,
        "recovered": True,
        "guards_removed": len(removed),
        "operator_operation": operator_operation,
    }


def activation_lease_allows_token(database_path: Path, lease_token: str) -> bool:
    """Return whether an explicit connection token owns the active lease."""

    payload = read_activation_lease(database_path)
    if payload is None:
        return True
    expected = str(payload["token"])
    actual = str(lease_token or "")
    return bool(actual) and hmac.compare_digest(actual, expected)


def assert_activation_write_allowed(database_path: Path) -> None:
    """Fail closed when any activation lease blocks an ordinary writer."""

    if read_activation_lease(database_path) is None:
        return
    raise MaintenanceLeaseError(
        "Scope Recall writes are blocked by an active activation maintenance lease"
    )



def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _activation_guard_trigger_name(table: str, operation: str) -> str:
    token = hashlib.sha256(f"{table}:{operation}".encode("utf-8")).hexdigest()[:20]
    return f"{ACTIVATION_GUARD_TRIGGER_PREFIX}{operation.lower()}_{token}"


def ensure_activation_guard_triggers(
    connection: sqlite3.Connection,
    database_path: Path,
    *,
    lease_token: str = "",
) -> list[str]:
    """Install transaction-neutral DML guards for all ordinary truth tables."""

    payload = read_activation_lease(database_path)
    if payload is None:
        # A crash after lease unlink but before trigger cleanup must not strand
        # raw connections forever. Normal writable startup repairs this orphan
        # state while no maintenance owner exists.
        remove_activation_guard_triggers(connection)
        return []
    captured_token = str(lease_token or "")
    if not activation_lease_allows_token(database_path, captured_token):
        raise MaintenanceLeaseError(
            "cannot install activation guards without the active lease token"
        )

    def activation_authorized() -> int:
        try:
            return int(
                activation_lease_allows_token(database_path, captured_token)
            )
        except MaintenanceLeaseError:
            return 0

    connection.create_function(
        ACTIVATION_AUTHORIZATION_FUNCTION,
        0,
        activation_authorized,
    )
    rows = connection.execute(
        "SELECT name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    virtual_tables = {
        str(row[0])
        for row in rows
        if str(row[1]).strip().casefold().startswith("create virtual table")
    }
    table_names = [
        str(row[0])
        for row in rows
        if str(row[0]) not in virtual_tables
        and not any(str(row[0]).startswith(f"{name}_") for name in virtual_tables)
    ]
    trigger_names: list[str] = []
    for table in table_names:
        quoted_table = _quoted_identifier(table)
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger_name = _activation_guard_trigger_name(table, operation)
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {_quoted_identifier(trigger_name)} "
                f"BEFORE {operation} ON {quoted_table} "
                f"WHEN {ACTIVATION_AUTHORIZATION_FUNCTION}() != 1 "
                "BEGIN SELECT RAISE(ABORT, "
                "'Scope Recall activation maintenance guard'); END"
            )
            trigger_names.append(trigger_name)
    return trigger_names


def remove_activation_guard_triggers(connection: sqlite3.Connection) -> list[str]:
    """Drop every activation DML guard without committing the caller transaction."""

    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE ? "
        "ORDER BY name",
        (f"{ACTIVATION_GUARD_TRIGGER_PREFIX}%",),
    ).fetchall()
    names = [str(row[0]) for row in rows]
    for name in names:
        connection.execute(f"DROP TRIGGER {_quoted_identifier(name)}")
    return names


def install_activation_lease_authorizer(
    connection: sqlite3.Connection,
    database_path: Path,
    *,
    lease_token: str | None = None,
) -> None:
    """Bind lease authorization to this concrete SQLite connection.

    ``None`` or an empty string creates an ordinary connection. Only an explicit
    token supplied by the activation owner can authorize writes. The captured
    value is immutable for this authorizer and never comes from process or
    context-global state.
    """

    path = database_path.expanduser().resolve()
    captured_token = str(lease_token or "")

    def activation_authorized() -> int:
        try:
            return int(activation_lease_allows_token(path, captured_token))
        except MaintenanceLeaseError:
            return 0

    connection.create_function(
        ACTIVATION_AUTHORIZATION_FUNCTION,
        0,
        activation_authorized,
    )

    def authorizer(
        action: int,
        _arg1: str | None,
        _arg2: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action not in _WRITE_ACTIONS:
            return sqlite3.SQLITE_OK
        try:
            allowed = activation_lease_allows_token(path, captured_token)
        except MaintenanceLeaseError:
            allowed = False
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

    connection.set_authorizer(authorizer)


__all__ = [
    "ACTIVATION_AUTHORIZATION_FUNCTION",
    "ACTIVATION_GUARD_TRIGGER_PREFIX",
    "ACTIVATION_LEASE_FILENAME",
    "MaintenanceLeaseError",
    "activation_lease_allows_token",
    "activation_lease_path",
    "activation_lease_status",
    "assert_activation_write_allowed",
    "ensure_activation_guard_triggers",
    "install_activation_lease_authorizer",
    "read_activation_lease",
    "recover_stale_activation_lease",
    "remove_activation_guard_triggers",
]
