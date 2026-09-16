"""Hermes lifecycle view over the shared trusted runtime glue."""
from __future__ import annotations

from functools import partial

from scope_recall.adapters.runtime_wiring import (
    GAP_BINDING_MISMATCH,
    GAP_INVALID,
    GAP_UNCONFIGURED,
    GAP_WORKER_BUSY,
    GAP_WORKER_LAUNCH_FAILED,
    TrustedHostRuntime,
    attach_trusted_host_runtime as _attach_common,
    close_audience_workers,
    launch_audience_worker,
    write_ephemeral_worker_config,
)
from scope_recall.runtime.worker_launch import launch_worker


class HermesHostRuntime(TrustedHostRuntime):
    """Wake the existing durable queue in an independent bounded watchdog.

    The helper owns its RuntimeInstance. A Hermes shutdown can therefore close
    the foreground Lance handles without racing a consolidation request.
    """

    def maybe_launch_bounded_worker(
        self, *, session_id: str, allowed_scope_ids: frozenset[str],
        project_id: str | None = None, branch_id: str | None = None,
    ) -> tuple[str, ...]:
        return launch_audience_worker(self, session_id=session_id,
                                      allowed_scope_ids=allowed_scope_ids, launcher=launch_worker,
                                      project_id=project_id, branch_id=branch_id)

    def close(self, *, detach_worker: bool = True) -> None:
        # Lifecycle shutdown never waits for/kills the helper: the existing
        # watchdog owns its deadline and config cleanup; SQLite owns recovery.
        # No active drain ever uses this foreground RuntimeInstance.
        del detach_worker
        with self._runtime_lock:
            close_audience_workers(self, detach=True)
            super().close()


attach_trusted_host_runtime = partial(_attach_common, host_adapter="hermes", runtime_class=HermesHostRuntime)


__all__ = [
    "GAP_BINDING_MISMATCH",
    "GAP_INVALID",
    "GAP_UNCONFIGURED",
    "GAP_WORKER_BUSY",
    "GAP_WORKER_LAUNCH_FAILED",
    "HermesHostRuntime",
    "TrustedHostRuntime",
    "attach_trusted_host_runtime",
    "write_ephemeral_worker_config",
]
