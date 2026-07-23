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
import sqlite3
from pathlib import Path
from typing import Any


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
    "assert_activation_write_allowed",
    "ensure_activation_guard_triggers",
    "install_activation_lease_authorizer",
    "read_activation_lease",
    "remove_activation_guard_triggers",
]
