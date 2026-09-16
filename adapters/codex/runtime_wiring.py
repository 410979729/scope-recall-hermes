"""Codex lifecycle wakeup over the shared trusted runtime glue."""
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


class CodexHostRuntime(TrustedHostRuntime):
    """A trusted runtime plus one owned, bounded Codex helper process."""

    def maybe_launch_bounded_worker(
        self,
        *,
        session_id: str,
        allowed_scope_ids: frozenset[str],
        project_id: str | None = None,
        branch_id: str | None = None,
    ) -> tuple[str, ...]:
        return launch_audience_worker(self, session_id=session_id,
                                      allowed_scope_ids=allowed_scope_ids, launcher=launch_worker,
                                      project_id=project_id, branch_id=branch_id)

    def close(self, *, detach_worker: bool = False) -> None:
        # Detached helpers retain their ephemeral config until they exit; the
        # worker itself is bounded by RuntimeInstanceConfig.drain_seconds.
        close_audience_workers(self, detach=detach_worker)
        super().close()


attach_trusted_host_runtime = partial(_attach_common, host_adapter="codex", runtime_class=CodexHostRuntime)


__all__ = [
    "CodexHostRuntime",
    "GAP_BINDING_MISMATCH",
    "GAP_INVALID",
    "GAP_UNCONFIGURED",
    "GAP_WORKER_BUSY",
    "GAP_WORKER_LAUNCH_FAILED",
    "TrustedHostRuntime",
    "attach_trusted_host_runtime",
    "write_ephemeral_worker_config",
]
