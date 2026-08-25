"""Bounded hosted-embedding request runner with a plugin-wide worker cap.

Each OpenAI-compatible embedder owns one runner. The per-object contract is:

- at most one worker thread
- at most one in-flight job
- zero queued jobs

The plugin/process contract is a separate finite permit pool. Starting a
worker requires a nonblocking acquire of one permit. A stuck underlying
SDK call keeps that permit until the vendor call actually returns, so a
later ``setup`` / repair cannot spawn an unbounded number of abandoned
workers. Excess work fails immediately instead of creating another
thread.

Python cannot forcibly kill an arbitrary SDK thread. The bound is
therefore a hard cap on live workers, not a cancellation guarantee.

A timed-out caller returns on its operation budget and never joins the
worker. The occupied per-embedder slot stays with that job until the
underlying call finishes; later callers on the same runner fail
immediately. When the stuck call later exits, that same still-open
embedder may run again without a process restart.

Terminal ``shutdown()`` only stops accepting new work and wakes an idle
worker so it can exit. It never ``join()``s the worker. Process teardown
therefore cannot block on a half-open transport.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

# Plugin-wide hard cap on live hosted-embedding request workers.
# One leftover stuck SDK call plus one current embedder is enough for
# ordinary setup/repair. A third construction's first request fails fast
# so recall can fall back lexically. This is a documented constant, not a
# user-tunable key, so the pool cannot be configured unbounded.
MAX_LIVE_HOSTED_EMBEDDING_WORKERS = 2


class InFlightEmbedderRequestError(TimeoutError):
    """Rejected because the embedder's single request slot is occupied."""


class EmbedderRequestDeadlineError(TimeoutError):
    """The caller-side budget elapsed while the worker may still be running."""


class HostedEmbedderWorkerLimitError(TimeoutError):
    """Rejected because the plugin-wide hosted worker budget is exhausted."""


class EmbedderRequestClosedError(TimeoutError):
    """Rejected because the runner has been terminally shut down."""


class _Job:
    """One callable plus its completion state. Never shared across submits."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self._value: Any = None
        self._error: BaseException | None = None

    def succeed(self, value: Any) -> None:
        self._value = value
        self.done.set()

    def fail(self, exc: BaseException) -> None:
        self._error = exc
        self.done.set()

    def result(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._value


class _PluginHostedEmbeddingWorkerBudget:
    """Finite process-wide permit pool. Not a registry or queue.

    Bound proof:
    - ``maximum`` is a fixed constant assigned at construction.
    - ``try_acquire`` is nonblocking and refuses to increment past that
      constant. There is no wait-list.
    - A permit is released only when the worker that acquired it exits.
    - The object stores one integer, not a list of runners or threads.
    """

    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError("hosted embedding worker budget must be at least 1")
        self.maximum = int(maximum)
        self._lock = threading.Lock()
        self._live = 0

    def try_acquire(self) -> bool:
        with self._lock:
            if self._live >= self.maximum:
                return False
            self._live += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._live <= 0:
                raise RuntimeError(
                    "hosted embedding worker permit released when none are held"
                )
            self._live -= 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "max_live_workers": self.maximum,
                "live_workers": self._live,
            }


_WORKER_BUDGET = _PluginHostedEmbeddingWorkerBudget(MAX_LIVE_HOSTED_EMBEDDING_WORKERS)


def hosted_embedding_worker_budget() -> dict[str, int]:
    """Return the plugin-wide hosted-worker cap and current occupancy."""

    return _WORKER_BUDGET.snapshot()


class BoundedEmbedderRequestRunner:
    """One reusable worker, one slot, no queue, one global permit.

    Bound proof:
    - ``MAX_IN_FLIGHT`` and ``MAX_QUEUED`` are the per-embedder limits.
    - ``MAX_LIVE_HOSTED_EMBEDDING_WORKERS`` is the plugin-wide limit.
    - ``_job`` holds at most one callable; ``run`` rejects while it is set
      and a live worker still owns it.
    - There is no job list, queue, or ``ThreadPoolExecutor``.
    - ``_worker`` is created only after a nonblocking global permit
      acquire; a live worker is never replaced.
    - A stuck worker keeps its permit until the thread exits, so repeated
      reinitialize cannot grow workers past the cap.
    - Deadline expiry leaves ``_job`` in place, so abandoned work cannot
      accumulate as extra threads or pending items on this runner.
    - ``shutdown()`` flips ``_accepting`` and returns without ``join``.
      An idle worker then exits and releases its permit.
    """

    MAX_IN_FLIGHT = 1
    MAX_QUEUED = 0

    def __init__(self, *, name: str | None = None) -> None:
        self.thread_name = name or f"scope-recall-embedder-request-{id(self)}"
        self._guard = threading.Lock()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._job: _Job | None = None
        self._accepting = True
        self._owns_worker_permit = False

    @property
    def accepting(self) -> bool:
        with self._guard:
            return self._accepting

    @property
    def in_flight(self) -> bool:
        with self._guard:
            return self._job is not None

    def snapshot(self) -> dict[str, int]:
        """Return declared bounds and the currently occupied resources."""

        with self._guard:
            worker_alive = self._worker is not None and self._worker.is_alive()
            local = {
                "max_in_flight": self.MAX_IN_FLIGHT,
                "max_queued": self.MAX_QUEUED,
                "in_flight": 1 if self._job is not None else 0,
                "queued": 0,
                "workers": 1 if worker_alive else 0,
            }
        local.update(hosted_embedding_worker_budget())
        return local

    def run(self, fn: Callable[[], Any], *, timeout: float) -> Any:
        """Run ``fn`` in the single worker or fail immediately.

        ``timeout`` is the caller-side budget. Expiry raises
        ``EmbedderRequestDeadlineError`` without clearing the slot. A second
        ``run`` while that job is still executing raises
        ``InFlightEmbedderRequestError`` without waiting. A terminally shut
        down runner rejects new work without creating a worker. Exhausting
        the plugin-wide permit raises ``HostedEmbedderWorkerLimitError``
        without starting another thread.
        """

        if timeout <= 0.0:
            raise EmbedderRequestDeadlineError(
                "embedding request budget is exhausted"
            )
        job = _Job(fn)
        with self._guard:
            if not self._accepting:
                raise EmbedderRequestClosedError(
                    "embedding request runner is shut down"
                )
            if self._job is not None:
                worker = self._worker
                if worker is not None and worker.is_alive():
                    raise InFlightEmbedderRequestError(
                        "embedding request rejected because a request is already in flight"
                    )
                # A dead worker cannot complete the stale slot; reclaim it so
                # a later call can recover instead of failing forever.
                self._job = None
            self._job = job
            try:
                self._ensure_worker_locked()
            except HostedEmbedderWorkerLimitError:
                self._job = None
                raise
            self._wake.set()
        if job.done.wait(timeout=timeout):
            return job.result()
        raise EmbedderRequestDeadlineError(
            "embedding request exceeded the caller-side operation budget"
        )

    def shutdown(self) -> None:
        """Stop accepting work and wake an idle worker. Never joins."""

        with self._guard:
            self._accepting = False
            if self._job is None:
                self._wake.set()

    def _ensure_worker_locked(self) -> None:
        worker = self._worker
        if worker is not None and worker.is_alive():
            return
        if not _WORKER_BUDGET.try_acquire():
            raise HostedEmbedderWorkerLimitError(
                "embedding request rejected because the plugin hosted-embedding "
                f"worker limit ({MAX_LIVE_HOSTED_EMBEDDING_WORKERS}) is exhausted"
            )
        self._owns_worker_permit = True
        try:
            self._worker = threading.Thread(
                target=self._loop,
                name=self.thread_name,
                daemon=True,
            )
            self._worker.start()
        except BaseException:
            self._owns_worker_permit = False
            self._worker = None
            _WORKER_BUDGET.release()
            raise

    def _loop(self) -> None:
        try:
            while True:
                self._wake.wait()
                with self._guard:
                    job = self._job
                    if job is None:
                        self._wake.clear()
                        if not self._accepting:
                            return
                        continue
                try:
                    job.succeed(job.fn())
                except BaseException as exc:
                    job.fail(exc)
                with self._guard:
                    if self._job is job:
                        self._job = None
                    if self._job is None:
                        self._wake.clear()
                    if not self._accepting and self._job is None:
                        return
        finally:
            should_release = False
            with self._guard:
                if self._worker is threading.current_thread():
                    self._worker = None
                if self._owns_worker_permit:
                    self._owns_worker_permit = False
                    should_release = True
            if should_release:
                _WORKER_BUDGET.release()
