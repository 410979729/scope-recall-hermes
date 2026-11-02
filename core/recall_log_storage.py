"""Disposable, bounded SQLite storage for content-free recall attempts.

The truth database never attaches this file. A nonblocking writer boundary
serializes checkpoints and atomic retention changes. Database pages, one WAL
image, shared-memory blocks and health metadata share one physical budget.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .file_lock import advisory_file_lock

LOG_POLICY_BOUNDS = {"retention_days": (1, 365), "max_bytes": (131072, 2**30), "queue_size": (1, 4096)}


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
        self.cancelled = threading.Event()
        self.active_connection = None

    def _check_path(self):
        if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (self.path, *self.path.parents)):
            raise OSError("recall log path is linked")

    def _open(self):
        self._check_path()
        conn = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 1:
                conn.execute("PRAGMA auto_vacuum=FULL")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("PRAGMA cache_spill=OFF")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA journal_size_limit=0")
            conn.set_progress_handler(lambda: int(self.cancelled.is_set()), 100)
            return conn
        except BaseException:
            conn.close()
            raise

    def cancel(self) -> None:
        """Interrupt SQL during bounded writer shutdown; the owning thread closes it."""
        self.cancelled.set()
        conn = self.active_connection
        if conn is not None:
            try:
                conn.interrupt()
            except sqlite3.Error:
                pass

    def _schema(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS attempts (sequence INTEGER PRIMARY KEY,ref TEXT UNIQUE NOT NULL,"
            "timestamp TEXT NOT NULL,installation_id TEXT NOT NULL,session_id TEXT NOT NULL,"
            "request_id TEXT NOT NULL,payload TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS retention (singleton INTEGER PRIMARY KEY,days INTEGER NOT NULL,"
            "max_bytes INTEGER,watermark TEXT)"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(retention)")}
        for name, kind in (
            ("max_bytes", "INTEGER"),
            ("watermark", "TEXT"),
            ("health_since", "TEXT"),
            ("capacity_loss_at", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE retention ADD COLUMN {name} {kind}")
        conn.execute("CREATE INDEX IF NOT EXISTS attempts_session ON attempts(installation_id,session_id,sequence)")
        conn.execute("CREATE INDEX IF NOT EXISTS attempts_request ON attempts(installation_id,request_id)")
        conn.execute(
            "INSERT OR IGNORE INTO retention(singleton,days,max_bytes,watermark) VALUES(1,?,?,NULL)",
            (self.policy.retention_days, self.policy.max_bytes),
        )
        conn.execute("UPDATE retention SET max_bytes=? WHERE max_bytes IS NULL", (self.policy.max_bytes,))

    @staticmethod
    def _page_limit(max_bytes, page_size):
        # A transaction cannot spill, so each page contributes at most one WAL
        # frame. Reserve complete WAL-index blocks, including its first header.
        shared = 32768
        while True:
            pages = (max_bytes - shared - 4096 - 32) // (2 * page_size + 24)
            required = max(1, (pages + 3999) // 4000) * 32768
            if required <= shared:
                return pages
            shared = required

    @contextmanager
    def _transaction(self, *, writing):
        self._check_path()
        with advisory_file_lock(self.path.with_suffix(".write.lock"), timeout_seconds=0), closing(self._open()) as conn:
            if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise sqlite3.OperationalError("recall log checkpoint busy")
            conn.execute("BEGIN IMMEDIATE")
            self.active_connection = conn
            try:
                self._schema(conn)
                days, maximum, watermark = conn.execute("SELECT days,max_bytes,watermark FROM retention").fetchone()
                if writing and (days, maximum) != (self.policy.retention_days, self.policy.max_bytes):
                    raise ValueError("recall log policy differs from its persisted policy")
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                limit = self._page_limit(maximum, page_size)
                if conn.execute("PRAGMA page_count").fetchone()[0] > limit:
                    raise ValueError("existing recall log exceeds WAL capacity reserve")
                conn.execute(f"PRAGMA max_page_count={limit}")
                if not conn.execute("PRAGMA user_version").fetchone()[0]:
                    conn.execute("UPDATE attempts SET payload=json_remove(payload,'$.query.sha256')")
                    conn.execute("PRAGMA user_version=1")
                yield conn, days, watermark, page_size, limit
                conn.commit()
            except BaseException:
                conn.set_progress_handler(None, 0)
                conn.rollback()
                raise
            finally:
                self.active_connection = None
            # A reader starting during this commit may retain this one image.
            # The next writer must truncate it before appending anything else.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def write(self, record: dict) -> None:
        """Atomically expire and append; busy writers never wait or restore old dates."""
        raw = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._transaction(writing=True) as (conn, days, watermark, page_size, limit):
            latest = conn.execute("SELECT max(timestamp) FROM attempts").fetchone()[0]
            today = max(record["timestamp"], latest or record["timestamp"], watermark or record["timestamp"])
            conn.execute("UPDATE retention SET health_since=coalesce(health_since,?)", (today,))
            self._expire(conn, today, days)
            if record["timestamp"] < cutoff(today, days):
                return
            needed = (len(raw.encode("utf-8")) + page_size - 1) // page_size + 3
            self._make_room(conn, needed, limit, today)
            conn.execute(
                "INSERT OR REPLACE INTO attempts(ref,timestamp,installation_id,session_id,request_id,payload) "
                "VALUES (?,?,?,?,?,?)",
                (*(record[k] for k in ("ref", "timestamp", "installation_id", "session_id", "request_id")), raw),
            )

    @staticmethod
    def _expire(conn, timestamp, days):
        conn.execute("UPDATE retention SET watermark=?", (timestamp,))
        conn.execute("DELETE FROM attempts WHERE timestamp<?", (cutoff(timestamp, days),))

    def prune(self, timestamp: str) -> None:
        """Keep the shared policy and high-water date even when history becomes empty."""
        if not self.path.exists():
            return
        with self._transaction(writing=False) as (conn, days, watermark, _page_size, limit):
            self._expire(conn, max(timestamp, watermark or timestamp), days)
            self._make_room(conn, 0, limit, max(timestamp, watermark or timestamp))

    @staticmethod
    def _make_room(conn, needed, limit, timestamp):
        while (
            conn.execute("PRAGMA page_count").fetchone()[0]
            - conn.execute("PRAGMA freelist_count").fetchone()[0]
            + needed
            > limit
        ):
            count = conn.execute("SELECT count(*) FROM attempts").fetchone()[0]
            if not count:
                raise ValueError("recall record exceeds usable log capacity")
            conn.execute(
                "DELETE FROM attempts WHERE sequence IN "
                "(SELECT sequence FROM attempts ORDER BY timestamp,sequence LIMIT ?)",
                (max(1, count // 4),),
            )
            conn.execute("UPDATE retention SET capacity_loss_at=?", (timestamp,))

    def list(
        self,
        *,
        installation_id=None,
        session_id=None,
        request_id=None,
        ref=None,
        entry_id=None,
        before=None,
        limit=30,
        now=None,
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
        if entry_id is not None:
            clauses.append("json_extract(payload,'$.entry_id')=?")
            values.append(entry_id)
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
            values = [dict(json.loads(raw), sequence=sequence) for sequence, raw in rows]
            for value in values:
                value.get("query", {}).pop("sha256", None)
            return values
