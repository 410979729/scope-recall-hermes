"""Hermes lifecycle view over the shared trusted runtime glue."""
from __future__ import annotations

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

    The watchdog's worker owns its RuntimeInstance, so a Hermes shutdown never
    races a consolidation request.  The foreground vector store is the
    process's shared one (``attach_trusted_host_runtime``): a shutdown leaves
    its helper to the gateway's other runtimes, and it ends with the process.
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


def attach_trusted_host_runtime(**kwargs):
    """The common attach, for Hermes, with one vector helper for the whole process.

    A gateway attaches a runtime for every agent it makes, and Hermes does not always shut down the one it made
    before.  Each runtime held a vector helper of its own (about 1.15 GB): yuheng's gateway held two after its agent
    was made again, one per registration of the provider (19:20 and 22:42 on 2026-10-02), and tianji's one for its
    one.  Every runtime of the process now searches one store of each table through one helper, as a server's do
    (``vector.process_store.share``).  Stores are such helpers only on Windows; elsewhere sharing changes nothing.
    """
    from scope_recall.vector.process_store import share

    share()
    return _attach_common(host_adapter="hermes", runtime_class=HermesHostRuntime, **kwargs)


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
