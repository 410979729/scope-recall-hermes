"""Bounded wake/control channel for the capture writer.

Durable intent capacity lives in SQLite and is advertised separately as
``capture_queue_capacity``. This module owns only the in-memory control
queue: drain hints, flush markers, shutdown sentinels, and stub-test store
hints. ``maxsize`` is the structural bound. ``qsize()`` is never treated as
a concurrency guard.
"""

from __future__ import annotations

import queue
from typing import Any, cast

CONTROL_QUEUE_MAXSIZE = 4
CONTROL_PUT_TIMEOUT_SECONDS = 0.2


def new_write_control_queue() -> queue.Queue[Any]:
    """Return the process-local wake/control queue with a finite maxsize."""

    return queue.Queue(maxsize=CONTROL_QUEUE_MAXSIZE)


def control_queue_maxsize(work_queue: Any) -> int:
    """Return the structural maxsize, or 0 when the object is not bounded."""

    try:
        return int(getattr(work_queue, "maxsize", 0) or 0)
    except (TypeError, ValueError):
        return 0


def bind_write_control_queue(provider: Any) -> queue.Queue[Any]:
    """Ensure ``provider._write_queue`` is a finite control channel.

    An already-bounded queue is kept. An unbounded queue is replaced only
    when no writer thread is alive, so a running ``get()`` is not detached
    from the object callers put into.
    """

    current = getattr(provider, "_write_queue", None)
    if control_queue_maxsize(current) > 0:
        return cast(queue.Queue[Any], current)
    thread = getattr(provider, "_writer_thread", None)
    alive = thread is not None and bool(getattr(thread, "is_alive", lambda: False)())
    if alive and current is not None:
        return cast(queue.Queue[Any], current)
    bound = new_write_control_queue()
    provider._write_queue = bound
    return bound


def put_control(
    work_queue: Any,
    item: Any,
    *,
    timeout: float | None = None,
) -> bool:
    """Nonblocking or bounded-time put onto the control channel.

    Returns False when the finite queue is full. Raises if the channel is
    unbounded so capture never grows a silent in-memory pile.
    """

    if work_queue is None:
        return False
    if control_queue_maxsize(work_queue) <= 0:
        raise RuntimeError("Scope Recall write control queue must have a finite maxsize")
    try:
        if timeout is None or float(timeout) <= 0:
            work_queue.put_nowait(item)
        else:
            work_queue.put(item, timeout=float(timeout))
        return True
    except queue.Full:
        return False


def wake_writer(provider: Any) -> bool:
    """Hint the writer that durable intents are waiting. Never blocks."""

    wakeup = getattr(provider, "_write_wakeup", None)
    if wakeup is not None:
        wakeup.set()
    work_queue = getattr(provider, "_write_queue", None)
    if work_queue is None:
        return False
    try:
        return put_control(work_queue, {"kind": "drain"})
    except RuntimeError:
        return False


__all__ = [
    "CONTROL_PUT_TIMEOUT_SECONDS",
    "CONTROL_QUEUE_MAXSIZE",
    "bind_write_control_queue",
    "control_queue_maxsize",
    "new_write_control_queue",
    "put_control",
    "wake_writer",
]
