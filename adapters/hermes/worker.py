"""One bounded daemon wakeup worker for already-persisted Core work items."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Callable

_SHUTDOWN_DRAIN_TIMEOUT_S = 5.0


@dataclass
class AdapterWorker:
    """Run at most one callback, with one coalesced pending wakeup.

    The adapter submits only a callback that drains durable Core work items;
    raw host events are never retained here. A daemon thread makes shutdown
    bounded even when an external storage call is stuck.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _wake: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _pending: Callable[[], None] | None = None
    _active: bool = False
    _shutting_down: bool = False
    drain_state: dict[str, int | str] = field(
        default_factory=lambda: {
            "status": "not_started",
            "abandoned_writes": 0,
            "active_tasks": 0,
            "pending_wakeups": 0,
        }
    )

    def submit(self, fn: Callable[[], None], *, kind: str = "write") -> bool:
        del kind
        with self._lock:
            if self._shutting_down:
                return False
            self._pending = fn
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="hermes-adapter-worker",
                    daemon=True,
                )
                self._thread.start()
            self.drain_state["status"] = "running"
            self.drain_state["pending_wakeups"] = 1
            self._wake.set()
        return True

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            while True:
                with self._lock:
                    fn = self._pending
                    self._pending = None
                    if fn is None:
                        self._active = False
                        self.drain_state["pending_wakeups"] = 0
                        if self._shutting_down:
                            self.drain_state["status"] = "drained"
                        break
                    self._active = True
                    self.drain_state["pending_wakeups"] = 0
                    self.drain_state["active_tasks"] = 1
                try:
                    fn()
                except Exception:
                    # The Core work item remains durable for a later retry.
                    pass
                finally:
                    with self._lock:
                        self._active = False
                        self.drain_state["active_tasks"] = 0
                        if self._pending is None and self._shutting_down:
                            self.drain_state["status"] = "drained"
                with self._lock:
                    if self._pending is None:
                        if self._shutting_down:
                            self.drain_state["status"] = "drained"
                        break
            with self._lock:
                if self._shutting_down and self._pending is None and not self._active:
                    return

    def shutdown(self, *, timeout: float = _SHUTDOWN_DRAIN_TIMEOUT_S) -> dict[str, int | str]:
        with self._lock:
            self._shutting_down = True
            thread = self._thread
            self.drain_state["status"] = "draining" if thread is not None else "drained"
            self._wake.set()
        if thread is not None:
            thread.join(max(0.0, float(timeout)))
        with self._lock:
            alive = bool(thread is not None and thread.is_alive())
            pending = int(self._pending is not None)
            active = int(self._active)
            self.drain_state.update(
                status="timed_out" if alive else "drained",
                abandoned_writes=pending,
                active_tasks=active,
                pending_wakeups=pending,
            )
            return dict(self.drain_state)

    @property
    def shutting_down(self) -> bool:
        with self._lock:
            return self._shutting_down
