"""Codex lifecycle wakeup over the shared trusted runtime glue."""
from __future__ import annotations

from dataclasses import dataclass

from scope_recall.adapters.runtime_wiring import (
    GAP_BINDING_MISMATCH,
    GAP_INVALID,
    GAP_UNCONFIGURED,
    TrustedHostRuntime,
    attach_trusted_host_runtime as _attach_common,
    write_ephemeral_worker_config,
    launch_audience_worker, close_audience_workers,
)
from scope_recall.contracts import InstanceBinding
from scope_recall.core import MemoryCore
from scope_recall.runtime.worker_launch import WorkerProcess, launch_worker

GAP_WORKER_BUSY = "capability_gap:trusted_runtime_worker_busy"
GAP_WORKER_LAUNCH_FAILED = "capability_gap:trusted_runtime_worker_launch_failed"


@dataclass
class CodexHostRuntime(TrustedHostRuntime):
    """A trusted runtime plus one owned, bounded Codex helper process."""

    _owned_worker: WorkerProcess | None = None
    _trailing_worker: WorkerProcess | None = None
    _last_worker_launch: float = 0.0

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
        close_audience_workers(self, detach=detach_worker)
        # Detached helpers retain their ephemeral config until they exit; the
        # worker itself is bounded by RuntimeInstanceConfig.drain_seconds.
        if detach_worker:
            runtime = self._runtime
            self._runtime = None
            if runtime is not None:
                runtime.close()
            return
        super().close()


def attach_trusted_host_runtime(
    *,
    config_path: object,
    expected_binding: InstanceBinding,
    session_id: str,
    allowed_scope_ids: frozenset[str],
    project_id: str | None = None,
    branch_id: str | None = None,
    core: MemoryCore | None = None,
    clock: object | None = None,
) -> CodexHostRuntime:
    base = _attach_common(
        config_path=config_path,
        expected_binding=expected_binding,
        session_id=session_id,
        allowed_scope_ids=allowed_scope_ids,
        core=core,
        clock=clock,
        host_adapter="codex",
        project_id=project_id,
        branch_id=branch_id,
    )
    return CodexHostRuntime(
        core=base.core,
        capability_gaps=base.capability_gaps,
        _runtime=base._runtime,
        _config_path=base._config_path,
        _ephemeral_configs=base._ephemeral_configs,
        _host_adapter=base._host_adapter,
    )


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
