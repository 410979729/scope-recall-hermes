"""Disposable, bounded SQLite storage for content-free recall attempts.

The truth database never attaches this file. Full auto-vacuum returns freed
pages at commit; an in-memory rollback journal keeps the physical disk ceiling
independent of a long-lived reader's WAL. Only the logging thread writes here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

LOG_POLICY_BOUNDS = {"retention_days": (1, 365), "max_bytes": (65536, 2**30), "queue_size": (1, 4096)}


@dataclass(frozen=True)
class LogPolicy:
    """UTC calendar retention and a physical database ceiling, with bounded ingress."""

    retention_days: int = 30
    max_bytes: int = 32 * 1024 * 1024
    queue_size: int = 128

    def __post_init__(self):
        for name, (low, high) in LOG_POLICY_BOUNDS.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("recall log policy outside supported bounds")


DEFAULT_LOG_POLICY = LogPolicy()


def cutoff(timestamp: str, days: int) -> str:
    """The first UTC calendar day retained, including the request's day."""
    return (datetime.fromisoformat(timestamp[:10]) - timedelta(days=days - 1)).date().isoformat()


class RecallLogStore:
    """Open per operation; a failed or locked log cannot hold a truth transaction."""

    def __init__(self, path: Path, policy: LogPolicy | None = None):
        self.path = path
        self.policy = policy or LogPolicy()

    def _open(self):
        # A replaceable log is never allowed to follow a link to another store.
        if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (self.path, *self.path.parents)):
            raise OSError("recall log path is linked")
        conn = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        conn.execute("PRAGMA auto_vacuum=FULL")
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(f"PRAGMA max_page_count={(self.policy.max_bytes - 4096) // 4096}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS attempts (sequence INTEGER PRIMARY KEY,ref TEXT UNIQUE NOT NULL,"
            "timestamp TEXT NOT NULL,installation_id TEXT NOT NULL,session_id TEXT NOT NULL,"
            "request_id TEXT NOT NULL,payload TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS retention (singleton INTEGER PRIMARY KEY,days INTEGER NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS attempts_session ON attempts(installation_id,session_id,sequence)")
        conn.execute("CREATE INDEX IF NOT EXISTS attempts_request ON attempts(installation_id,request_id)")
        return conn

    def write(self, record: dict) -> None:
        """Trim oldest records before insertion; oversized single records are refused."""
        raw = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        needed = (len(raw.encode("utf-8")) + 4095) // 4096 + 3
        if needed * 4096 > self.policy.max_bytes // 2:
            raise ValueError("recall record exceeds log capacity")
        with closing(self._open()) as conn:
            conn.execute("INSERT OR REPLACE INTO retention VALUES(1,?)", (self.policy.retention_days,))
            latest = conn.execute("SELECT max(timestamp) FROM attempts").fetchone()[0]
            today = max(record["timestamp"], latest or record["timestamp"])
            conn.execute("DELETE FROM attempts WHERE timestamp<?", (cutoff(today, self.policy.retention_days),))
            if record["timestamp"] < cutoff(today, self.policy.retention_days):
                return
            self._make_room(conn, needed)
            conn.execute(
                "INSERT OR REPLACE INTO attempts(ref,timestamp,installation_id,session_id,request_id,payload) "
                "VALUES (?,?,?,?,?,?)",
                (*(record[k] for k in ("ref", "timestamp", "installation_id", "session_id", "request_id")), raw),
            )

    def prune(self, timestamp: str) -> None:
        """Expire idle history when a reader or maintenance operation wakes the writer."""
        if not self.path.exists():
            return
        with closing(self._open()) as conn:
            setting = conn.execute("SELECT days FROM retention WHERE singleton=1").fetchone()
            days = setting[0] if setting else self.policy.retention_days
            conn.execute("DELETE FROM attempts WHERE timestamp<?", (cutoff(timestamp, days),))
            self._make_room(conn, 0)

    def _make_room(self, conn, needed):
        while conn.execute("PRAGMA page_count").fetchone()[0] + needed > (self.policy.max_bytes - 4096) // 4096:
            count = conn.execute("SELECT count(*) FROM attempts").fetchone()[0]
            if not count:
                raise ValueError("recall log ceiling below database overhead")
            conn.execute(
                "DELETE FROM attempts WHERE sequence IN "
                "(SELECT sequence FROM attempts ORDER BY timestamp,sequence LIMIT ?)",
                (max(1, count // 4),),
            )

    def list(
        self, *, installation_id=None, session_id=None, request_id=None, ref=None, before=None, limit=30, now=None
    ):
        """Read one bounded newest-first page; missing logs are an empty history."""
        if not self.path.exists():
            return []
        clauses, values = [], []
        for name, value in (
            ("installation_id", installation_id),
            ("session_id", session_id),
            ("request_id", request_id),
            ("ref", ref),
        ):
            if value is not None:
                clauses.append(f"{name}=?")
                values.append(value)
        if before is not None:
            clauses.append("sequence<?")
            values.append(before)
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.05)) as conn:
            if now is not None:
                setting = conn.execute("SELECT days FROM retention WHERE singleton=1").fetchone()
                clauses.append("timestamp>=?")
                values.append(cutoff(now, setting[0] if setting else self.policy.retention_days))
            rows = conn.execute(
                "SELECT sequence,payload FROM attempts"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY sequence DESC LIMIT ?",
                (*values, limit),
            )
            return [dict(json.loads(raw), sequence=sequence) for sequence, raw in rows]
