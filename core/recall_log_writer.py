"""Lossy, nonblocking ingress to an independent recall log writer."""

from __future__ import annotations

import atexit
import json
import threading
import time
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from uuid import uuid4

from .file_lock import advisory_file_lock
from .recall_log_storage import RecallLogStore

_WRITERS: weakref.WeakSet = weakref.WeakSet()


class RecallLogWriter:
    """Own a bounded queue; start lazily and retire after the queue becomes idle."""

    def __init__(self, store: RecallLogStore):
        self.store = store
        self.observe: Callable[[], dict] | None = None
        self.queue: Queue = Queue(store.policy.queue_size)
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.closed = False
        self.stopping = threading.Event()
        self.dropped_full = 0
        self.write_failures = 0
        self.last_loss_at = None
        self.written = 0
        self.published = dict(dropped_full=0, write_failures=0, written=0)
        _WRITERS.add(self)

    def submit(self, record: dict) -> None:
        """Never wait for disk or queue capacity; failures are counted locally."""
        with self.lock:
            if self.closed:
                self.write_failures += 1
                self._note_loss(record)
                return
            try:
                self.queue.put_nowait(record)
            except Full:
                self.dropped_full += 1
                self._note_loss(record)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="recall-log", daemon=True)
                try:
                    self.thread.start()
                except RuntimeError:
                    self.thread = None
                    raise

    def _run(self):
        while not (self.stopping.is_set() and self.queue.empty()):
            try:
                record = self.queue.get(timeout=0.1)
            except Empty:
                with self.lock:
                    if self.queue.empty():
                        self.thread = None
                        return
                continue
            try:
                if "prune_at" in record:
                    self.store.prune(record["prune_at"])
                else:
                    if self.observe is not None:
                        record["background"].update(self.observe())
                    self.store.write(record)
                    self.written += 1
            except Exception:  # noqa: BLE001 - a disposable log cannot fail a conversation.
                self.write_failures += 1
                self._note_loss(record)
            finally:
                try:
                    self._persist_health()
                finally:
                    self.queue.task_done()
        with self.lock:
            self.thread = None

    def close(self, timeout: float = 1.0) -> bool:
        """Stop ingress, drain within one deadline, then cancel and discard pending work.

        False means an active operating-system operation has not returned yet;
        SQL is interrupted and WAL recovery still protects committed records.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self.lock:
            self.closed = True
            thread = self.thread
        reserve = min(0.1, max(0.0, timeout) / 4)
        if not self.flush(max(0.0, deadline - time.monotonic() - reserve)):
            self.store.cancel()
            while True:
                try:
                    record = self.queue.get_nowait()
                except Empty:
                    break
                self.write_failures += 1
                self._note_loss(record)
                self.queue.task_done()
        self.stopping.set()
        if thread is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
        return thread is None or not thread.is_alive()

    def health(self) -> dict:
        """Counters for this writer; persisted receipts aggregate completed deltas."""
        return dict(
            dropped_full=self.dropped_full,
            write_failures=self.write_failures,
            written=self.written,
            queued=self.queue.qsize(),
        )

    def _note_loss(self, record):
        stamp = record.get("timestamp") or record.get("prune_at") or datetime.now(UTC).isoformat()
        if self.last_loss_at is None or stamp > self.last_loss_at:
            self.last_loss_at = stamp

    def _persist_health(self):
        # Health uses a fixed sibling name, so it remains readable when the
        # database is locked. A completely full disk can prevent this too.
        path = self.store.path.with_suffix(".health.json")
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with advisory_file_lock(path.with_suffix(".lock"), timeout_seconds=0):
                previous = read_health(self.store.path)
                current = self.health()
                total = {key: previous.get(key, 0) + current[key] - self.published[key] for key in self.published}
                previous_loss = previous.get("last_loss_at")
                losses = [stamp for stamp in (previous_loss, self.last_loss_at) if stamp]
                last_loss = max(losses) if losses else None
                with temporary.open("x", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(dict(total, queued=current["queued"], last_loss_at=last_loss), separators=(",", ":"))
                    )
                temporary.replace(path)
                self.published = {key: current[key] for key in self.published}
        except (OSError, ValueError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait only for explicit readers, tests and orderly process exit."""
        until = time.monotonic() + timeout
        with self.queue.all_tasks_done:
            while self.queue.unfinished_tasks:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    return False
                self.queue.all_tasks_done.wait(remaining)
        return True


def read_health(path: Path) -> dict:
    """Read bounded numeric status without exposing exception messages or paths."""
    try:
        raw = path.with_suffix(".health.json").read_bytes()
        value = json.loads(raw) if len(raw) <= 1024 else {}
        if not isinstance(value, dict):
            return {}
        result = {
            key: value[key]
            for key in ("dropped_full", "write_failures", "written", "queued")
            if type(value.get(key)) is int and value[key] >= 0
        }
        for key in ("last_loss_at", "loss_tracking_since"):
            if type(value.get(key)) is str:
                result[key] = value[key]
        return result
    except (OSError, ValueError, TypeError):
        return {}


def close_writers(directory: Path | None = None, *, timeout: float = 1.0) -> bool:
    """Retire diagnostics within one budget, optionally only inside an owned disposable directory."""
    deadline = time.monotonic() + timeout
    closed = True
    for writer in list(_WRITERS):
        if directory is None or writer.store.path.is_relative_to(directory):
            closed = writer.close(timeout=max(0.0, deadline - time.monotonic())) and closed
    return closed


atexit.register(close_writers)
