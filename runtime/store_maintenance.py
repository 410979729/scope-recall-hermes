"""Budgeted schema maintenance before any model-work runtime is constructed."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from ..contracts import InstanceBinding
from ..core.backup_primitives import sqlite_busy
from ..core.schema import UPGRADE_CHAIN, stale_header_schema
from ..core.truth_connection import connect_truth_database

# Maintenance has its own watchdog window, independent of a model drain.
STORE_UPGRADE_SECONDS = 180.0


def store_upgrade_pending(binding: InstanceBinding, *, timeout_seconds: float = 1) -> bool:
    """Inspect only the schema markers; never initialize or upgrade the store."""
    path = binding.data_directory / "memory.sqlite3"
    if not path.is_file():
        return False
    try:
        with closing(connect_truth_database(path, mode="ro", timeout=timeout_seconds)) as conn:
            return (
                conn.execute("PRAGMA user_version").fetchone()[0] in UPGRADE_CHAIN
                or stale_header_schema(conn) is not None
            )
    except sqlite3.OperationalError as exc:
        if sqlite_busy(exc):
            return False
        raise
