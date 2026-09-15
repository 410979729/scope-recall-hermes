"""Bounded process-local work queue for the capture writer.

Capture payloads exist only in this queue until the writer consumes them.  The
queue's structural ``maxsize`` is the backpressure contract; ``qsize()`` is
observability only and is never used as an enqueue guard.
"""

from __future__ import annotations

import queue
from typing import Any, cast

DEFAULT_CAPTURE_QUEUE_CAPACITY = 256
MIN_CAPTURE_QUEUE_CAPACITY = 8
MAX_CAPTURE_QUEUE_CAPACITY = 4096
CONTROL_PUT_TIMEOUT_SECONDS = 0.2


def queue_capacity(config: dict[str, Any] | None = None) -> int:
    """Return the clamped configured process-local queue capacity."""

    raw = (config or {}).get("capture_queue_capacity", DEFAULT_CAPTURE_QUEUE_CAPACITY)
    try:
        configured = int(raw)
    except (TypeError, ValueError):
        configured = DEFAULT_CAPTURE_QUEUE_CAPACITY
    return max(MIN_CAPTURE_QUEUE_CAPACITY, min(MAX_CAPTURE_QUEUE_CAPACITY, configured))


def new_write_queue(config: dict[str, Any] | None = None) -> queue.Queue[Any]:
    """Create the finite capture work queue."""

    return queue.Queue(maxsize=queue_capacity(config))


def queue_maxsize(work_queue: Any) -> int:
    """Return a queue's structural bound, or zero when it is unbounded."""

    try:
        return int(getattr(work_queue, "maxsize", 0) or 0)
    except (TypeError, ValueError):
        return 0


def bind_write_queue(provider: Any) -> queue.Queue[Any]:
    """Bind the configured finite queue before the writer starts.

    A live writer is never detached from its queue.  Before startup an empty
    queue with the wrong capacity is safely replaced.
    """

    desired = queue_capacity(getattr(provider, "_config", None))
    current = getattr(provider, "_write_queue", None)
    if queue_maxsize(current) == desired:
        return cast(queue.Queue[Any], current)
    thread = getattr(provider, "_writer_thread", None)
    alive = thread is not None and bool(getattr(thread, "is_alive", lambda: False)())
    if alive:
        if queue_maxsize(current) <= 0:
            raise RuntimeError("Scope Recall capture queue must have a finite maxsize")
        return cast(queue.Queue[Any], current)
    if current is not None and not bool(getattr(current, "empty", lambda: True)()):
        if queue_maxsize(current) <= 0:
            raise RuntimeError("Scope Recall capture queue must have a finite maxsize")
        return cast(queue.Queue[Any], current)
    bound = new_write_queue(getattr(provider, "_config", None))
    provider._write_queue = bound
    return bound


def put_work(work_queue: Any, item: Any, *, timeout: float | None = None) -> bool:
    """Put work without waiting indefinitely; return ``False`` on backpressure."""

    if work_queue is None:
        return False
    if queue_maxsize(work_queue) <= 0:
        raise RuntimeError("Scope Recall capture queue must have a finite maxsize")
    try:
        if timeout is None or float(timeout) <= 0:
            work_queue.put_nowait(item)
        else:
            work_queue.put(item, timeout=float(timeout))
        return True
    except queue.Full:
        return False


def capture_queue_report(provider: Any) -> dict[str, Any]:
    """Return process-local queue state without inspecting queued payloads."""

    work_queue = getattr(provider, "_write_queue", None)
    capacity = queue_maxsize(work_queue) or queue_capacity(
        getattr(provider, "_config", None)
    )
    try:
        depth = max(0, int(work_queue.qsize())) if work_queue is not None else 0
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        depth = 0
    processing = max(0, int(getattr(provider, "_capture_queue_processing", 0) or 0))
    return {
        "status": "ready" if capacity > 0 else "unavailable",
        "capacity": capacity,
        "depth": depth,
        "pending": depth,
        "processing": processing,
        "oldest_age_seconds": 0.0,
        "rejected": max(
            0, int(getattr(provider, "_capture_queue_rejected", 0) or 0)
        ),
        "deferred": max(
            0, int(getattr(provider, "_capture_queue_deferred", 0) or 0)
        ),
    }


__all__ = [
    "CONTROL_PUT_TIMEOUT_SECONDS",
    "DEFAULT_CAPTURE_QUEUE_CAPACITY",
    "MAX_CAPTURE_QUEUE_CAPACITY",
    "MIN_CAPTURE_QUEUE_CAPACITY",
    "bind_write_queue",
    "capture_queue_report",
    "new_write_queue",
    "put_work",
    "queue_capacity",
    "queue_maxsize",
]
