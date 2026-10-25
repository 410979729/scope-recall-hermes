"""Shared transaction clocks for semantic tests and explicit storage budgets."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from functools import wraps

import pytest

_REAL_MONOTONIC = time.monotonic

# Thread dispatch, timed waits, JSON, diagnostics and shutdown spool I/O retain their real cost.
HOOK_MARGIN_SECONDS = 0.1

# This real-time limit only bounds a hang while transactions pause the semantic clock.
_HANG_TIMEOUT_SECONDS = 60.0


def assert_hook_budget(elapsed: float, budget: float) -> None:
    """Check a hook's total time with an allowance for non-store scheduling and I/O."""
    assert elapsed <= budget + HOOK_MARGIN_SECONDS, (elapsed, budget)


class StoreMeteredClock:
    """Read real monotonic time except while a storage transaction runs.

    Each completed transaction costs ``per_operation`` seconds regardless of disk speed. Overlapping transactions
    count once. Runtime attachment, process startup, sleeps and writer-lease waits retain their real cost.
    """

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
        """Charge explicit time spent by a fake operation without sleeping."""
        with self._lock:
            self._shift += seconds

    def wait(self, event: threading.Event, deadline: float) -> bool:
        """Wait within a semantic deadline and a physical cap, even if a transaction stalls."""
        physical_deadline = time.perf_counter() + _HANG_TIMEOUT_SECONDS
        while not event.is_set():
            remaining = min(deadline - self(), physical_deadline - time.perf_counter())
            if remaining <= 0:
                return False
            event.wait(remaining)
        return True

    @contextmanager
    def operation(self):
        """Meter one transaction, charging its cost only after storage admits it."""
        with self._lock:
            self._depth += 1
            if self._depth == 1:
                self._started = _REAL_MONOTONIC()
        entered = [False]
        try:
            yield entered
        finally:
            with self._lock:
                self._depth -= 1
                if self._depth == 0:
                    if entered[0]:
                        self._shift += self.per_operation - (_REAL_MONOTONIC() - self._started)
                        self.operations += 1


class _LeasePolls:
    """Preserve real writer-lease polling time while a transaction clock is stopped."""

    def __init__(self, clock: StoreMeteredClock) -> None:
        self._clock = clock

    def sleep(self, seconds: float) -> None:
        """Charge the actual wait for another writer's lease."""
        started = _REAL_MONOTONIC()
        time.sleep(seconds)
        self._clock.advance(_REAL_MONOTONIC() - started)

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture
def metered_store(monkeypatch):
    """Install a shared clock charging ``per_operation`` seconds per storage transaction.

    Repeated calls reconfigure the same clock without nesting transaction wrappers. Hooks, core deadlines and
    capture admission read it together; transactions still execute against the synthetic store.

    A test and its fixtures share one ``monkeypatch``, whose ``undo()`` removes this clock as well: keep a test's
    temporary patches in ``monkeypatch.context()``. At teardown the test fails if an attribute the clock replaced
    has its pre-install value again. The check compares identities: it passes a patch layered over the clock, and it
    cannot see a clock removed and installed again before teardown, or one replaced by objects that do not meter.
    """
    from scope_recall.core import storage
    from scope_recall.core.storage import SQLiteStorage

    clock = StoreMeteredClock(0.0)
    originals = []

    def install(per_operation: float) -> StoreMeteredClock:
        clock.per_operation = per_operation
        if originals:
            return clock
        transaction = SQLiteStorage._transaction

        @contextmanager
        def metered(self, *args, **kwargs):
            with clock.operation() as entered:
                with transaction(self, *args, **kwargs) as tx:
                    entered[0] = True
                    yield tx

        for owner, name, replacement in (
            (SQLiteStorage, "_transaction", metered),
            (storage, "time", _LeasePolls(clock)),
            (time, "monotonic", clock),
        ):
            originals.append((owner, name, getattr(owner, name)))
            monkeypatch.setattr(owner, name, replacement)
        return clock

    yield install
    # Comparing pre-install values permits test patches layered on the clock.
    if any(getattr(owner, name) is original for owner, name, original in originals):
        pytest.fail(
            "The store clock is no longer installed at test teardown: a patched attribute has its pre-install value. "
            "A common cause is monkeypatch.undo() also removing the fixture's clock patches. "
            "Keep temporary test patches in monkeypatch.context()."
        )


@pytest.fixture
def metered_ledger(monkeypatch, metered_store):
    """Exclude ledger transaction time from the shared clock while executing every read and write.

    Reservations, settlements and provider-hold reads include SQLite connection and pager waits. Transport locks,
    thread dispatch and provider waits remain outside these operations and keep their real cost.
    """
    from scope_recall.runtime import model_budget

    clock = metered_store(0.0)

    def meter(operation):
        @wraps(operation)
        def measured(*args, **kwargs):
            with clock.operation() as entered:
                entered[0] = True
                return operation(*args, **kwargs)

        return measured

    for owner, name in (
        (model_budget.AuxiliaryBudgetLedger, "reserve"),
        (model_budget.AuxiliaryBudgetLedger, "_settle"),
        (model_budget, "provider_hold_until"),
    ):
        monkeypatch.setattr(owner, name, meter(getattr(owner, name)))
    return clock


@pytest.fixture(autouse=True)
def semantic_store(request, metered_store):
    """Exclude physical transaction time unless a test explicitly measures real storage latency.

    Import this fixture into the conftest or test module owning semantic capture assertions. The opt-out is checked
    before other function fixtures construct storage, which retains the monotonic function it sees at construction.
    Budget tests set their own cost with ``metered_store`` after setup instead of opting out.
    """
    if request.node.get_closest_marker("real_store_time") is None:
        metered_store(0.0)


@pytest.fixture
def slow_capture_store(monkeypatch):
    """Delay the next real write after setup beyond half of a capture's admission budget.

    The delay is inside the storage transaction, so semantic clocks exclude it while real clocks defer the source
    commit. The actual inbox and source writes still execute; no receipt or stored row is substituted.
    """
    from scope_recall.core.storage import SQLiteStorage

    def install():
        opened = SQLiteStorage._open
        delayed = False

        def slow_open(self, mode, *args, **kwargs):
            nonlocal delayed
            connection = opened(self, mode, *args, **kwargs)
            if mode == "rw" and not delayed:
                delayed = True
                time.sleep(0.6)
            return connection

        monkeypatch.setattr(SQLiteStorage, "_open", slow_open)

    return install
