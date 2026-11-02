"""Budgeted schema maintenance before any model-work runtime is constructed."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from ..contracts import InstanceBinding
from ..core.backup_primitives import sqlite_busy
from ..core.lexical_index import claim_backfill_pending
from ..core.schema import SCHEMA_VERSION, UPGRADE_CHAIN, stale_header_schema
from ..core.truth_connection import connect_truth_database

# Maintenance has its own watchdog window, independent of a model drain.
STORE_UPGRADE_SECONDS = 180.0


def store_upgrade_pending(binding: InstanceBinding, *, timeout_seconds: float = 1) -> bool:
    """Inspect schema markers and durable index progress without writing the store."""
    path = binding.data_directory / "memory.sqlite3"
    if not path.is_file():
        return False
    try:
        with closing(connect_truth_database(path, mode="ro", timeout=timeout_seconds)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            return (
                version in UPGRADE_CHAIN
                or stale_header_schema(conn) is not None
                or (version == SCHEMA_VERSION and claim_backfill_pending(conn))
            )
    except sqlite3.OperationalError as exc:
        if sqlite_busy(exc):
            return False
        raise
