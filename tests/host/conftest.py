"""Fixtures every host adapter's tests share."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

_REAL_MONOTONIC = time.monotonic


class StoreMeteredClock:
    """``time.monotonic`` for a test of a budget: real time, except the store's.  While the store works this clock
    stands still, and after each of its transactions it moves on by ``per_operation`` seconds, however long the store
    took: a budget meets the same store on any machine, and everything else the code under test does (attaching a
    runtime, starting a process, sleeping, looping, waiting for another writer's lease) counts as long as it really
    takes.  Transactions that overlap, as two threads' can, count once.  ``advance`` is time a fake spends, as a
    writer that waits out what it was given."""

    def __init__(self, per_operation: float) -> None:
        self.per_operation = per_operation
        self.operations = 0
        self._shift = 0.0
        self._depth = 0
        self._started = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            if self._depth:
                return self._started + self._shift
            return _REAL_MONOTONIC() + self._shift

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._shift += seconds

    @contextmanager
    def operation(self):
        with self._lock:
            self._depth += 1
            if self._depth == 1:
                self._started = _REAL_MONOTONIC()
        try:
            yield
        finally:
            with self._lock:
                self._depth -= 1
                if self._depth == 0:
                    self._shift += self.per_operation - (_REAL_MONOTONIC() - self._started)
                    self.operations += 1


class _LeasePolls:
    """The ``time`` module as the store sees it under a ``StoreMeteredClock``: a writer's sleep between its polls for
    the lease moves the clock on by what it really slept, since another writer is waited for in real time."""

    def __init__(self, clock: StoreMeteredClock) -> None:
        self._clock = clock

    def sleep(self, seconds: float) -> None:
        started = _REAL_MONOTONIC()
        time.sleep(seconds)
        self._clock.advance(_REAL_MONOTONIC() - started)

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture
def metered_store(monkeypatch):
    """``metered_store(per_operation)``: ``time.monotonic``, which a hook's clock, a core's and the Hermes adapter's
    deadlines all read, becomes a ``StoreMeteredClock`` charging that much for each of the store's transactions; the
    clock is returned.  For a test that holds code to its budget on any machine's store."""
    from scope_recall.core import storage
    from scope_recall.core.storage import SQLiteStorage

    def install(per_operation: float) -> StoreMeteredClock:
        clock = StoreMeteredClock(per_operation)
        transaction = SQLiteStorage._transaction

        @contextmanager
        def metered(self, *args, **kwargs):
            with clock.operation(), transaction(self, *args, **kwargs) as tx:
                yield tx

        monkeypatch.setattr(SQLiteStorage, "_transaction", metered)
        monkeypatch.setattr(storage, "time", _LeasePolls(clock))
        monkeypatch.setattr(time, "monotonic", clock)
        return clock

    return install
