"""Opt-in timing instrumentation for synthetic capture measurements only.

SQL measurements include execute, fetch and iteration, but exclude commit and
BEGIN waits. Scan/admission CPU time excludes their nested SQL measurements.
No queries, parameters or source text are retained.
"""

import importlib
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager

ACTIVE = None


class CaptureTiming:
    """One serial operation's capture-only timings, allowing nested core calls."""

    def __init__(self):
        self.values = Counter()
        self.sql_by_part = Counter()
        self.part = "other"
        self.depth = 0

    @contextmanager
    def capture(self):
        outer = not self.depth
        self.depth += 1
        start = time.perf_counter()
        try:
            yield
        finally:
            self.depth -= 1
            if outer:
                self.values["capture_ms"] += (time.perf_counter() - start) * 1000

    @contextmanager
    def phase(self, name):
        previous, self.part = self.part, name
        start = time.perf_counter()
        try:
            yield
        finally:
            self.values[name + "_inclusive_ms"] += (time.perf_counter() - start) * 1000
            self.part = previous

    def result(self):
        values = dict(self.values)
        for name in ("scan", "admission"):
            values[name + "_ms"] = values.get(name + "_inclusive_ms", 0) - self.sql_by_part[name]
        parts = ("wait_ms", "sql_ms", "scan_ms", "admission_ms", "commit_ms")
        values["other_ms"] = values.get("capture_ms", 0) - sum(values.get(name, 0) for name in parts)
        return values


def _timed(call, category, *args, **kwargs):
    trace = ACTIVE
    if trace is None or not trace.depth:
        return call(*args, **kwargs)
    start = time.perf_counter()
    try:
        return call(*args, **kwargs)
    finally:
        elapsed = (time.perf_counter() - start) * 1000
        trace.values[category] += elapsed
        if category == "sql_ms":
            trace.sql_by_part[trace.part] += elapsed


class _Cursor(sqlite3.Cursor):
    def execute(self, sql, *args):
        category = "wait_ms" if sql.upper().startswith("BEGIN") else "sql_ms"
        if ACTIVE is not None and ACTIVE.depth:
            ACTIVE.values["sql_count"] += 1
        return _timed(super().execute, category, sql, *args)

    def executemany(self, *args):
        return _timed(super().executemany, "sql_ms", *args)

    def fetchone(self):
        return _timed(super().fetchone, "sql_ms")

    def fetchall(self):
        return _timed(super().fetchall, "sql_ms")

    def __next__(self):
        return _timed(super().__next__, "sql_ms")


class _ConnectionTiming:
    def execute(self, *args):
        return self.cursor(factory=_Cursor).execute(*args)

    def executemany(self, *args):
        return self.cursor(factory=_Cursor).executemany(*args)

    def commit(self):
        return _timed(super().commit, "commit_ms")


def _wrap(owner, name, category):
    original = getattr(owner, name)

    def measured(*args, **kwargs):
        trace = ACTIVE
        if trace is None:
            return original(*args, **kwargs)
        manager = trace.capture() if category == "capture" else trace.phase(category)
        with manager:
            return original(*args, **kwargs)

    setattr(owner, name, measured)


def install():
    """Instrument only this benchmark child, preserving the production lease factory."""
    from scope_recall.core import MemoryCore, capture, truth_connection, writer_lease

    leased = truth_connection._LeasedTruthConnection
    truth_connection._LeasedTruthConnection = type("TimedLeased", (_ConnectionTiming, leased), {})
    ordinary = type("TimedOrdinary", (_ConnectionTiming, sqlite3.Connection), {})
    original = sqlite3.connect

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", ordinary)
        return original(*args, **kwargs)

    sqlite3.connect = connect
    acquire = writer_lease.TruthWriterLease.acquire
    writer_lease.TruthWriterLease.acquire = lambda *a, **k: _timed(acquire, "wait_ms", *a, **k)
    for name in ("record_event", "record_host_event"):
        _wrap(MemoryCore, name, "capture")
    if hasattr(capture, "screen_sources"):
        _wrap(capture, "screen_sources", "scan")
        _wrap(capture, "admit_source", "admission")
    else:
        _wrap(capture, "decide", "admission")
        for module, cls, method in (
            ("source_storage", "Sources", "index_source"),
            ("source_storage", "Sources", "enqueue_source"),
            ("claim_storage", "Claims", "link_source"),
            ("episode_storage", "Episodes", "attach"),
        ):
            owner = getattr(importlib.import_module("scope_recall.core." + module), cls)
            _wrap(owner, method, "admission")
