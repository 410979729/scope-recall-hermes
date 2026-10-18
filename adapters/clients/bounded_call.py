"""A call a hook waits for no longer than the time it has left (``BoundedCall``)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from scope_recall.core.deadline import RequestDeadline, using_request_deadline


class BoundedCall:
    """``run`` on a daemon thread of its own.  ``wait`` waits for it at most the seconds given and says whether it
    finished; ``result`` is then what it returned, or raises what it raised.  One still running when its caller stops
    waiting finishes on its thread: ``abandon(close)`` hands what it made, once it is made, to ``close``, so nothing it
    made outlives a caller that has closed; a process that exits first ends it, as a host ends a hook past its
    timeout."""

    def __init__(self, run: Callable[[], Any], *, name: str, remaining_seconds: float | None = None) -> None:
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._value: Any = None
        self._error: Exception | None = None
        self._close: Callable[[Any], None] | None = None
        deadline = None if remaining_seconds is None else RequestDeadline.from_budget(remaining_seconds)
        threading.Thread(target=self._run, args=(run, deadline), name=name, daemon=True).start()

    def _run(self, run: Callable[[], Any], deadline: RequestDeadline | None) -> None:
        try:
            if deadline is None:
                value = run()
            else:
                with using_request_deadline(deadline):
                    value = run()
        except Exception as exc:  # noqa: BLE001 - raised again to whoever reads the result
            with self._lock:
                self._error = exc
                self._done.set()
            return
        with self._lock:
            self._value = value
            self._done.set()
            close = self._close
        if close is not None:
            close(value)

    def wait(self, seconds: float | None) -> bool:
        """Wait for the call at most ``seconds`` (for good with None); whether it has finished."""
        return self._done.wait(None if seconds is None else max(0.0, seconds))

    def result(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._value

    def abandon(self, close: Callable[[Any], None]) -> None:
        """No one waits for the call any more: what it made, or makes, goes to ``close``."""
        with self._lock:
            if not self._done.is_set():
                self._close = close
                return
            value, failed = self._value, self._error is not None
        if not failed:
            threading.Thread(target=close, args=(value,), name="scope-recall-runtime-close", daemon=True).start()
