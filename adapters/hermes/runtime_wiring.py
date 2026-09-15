"""Hermes lifecycle view over the shared trusted runtime glue."""
from __future__ import annotations

from dataclasses import dataclass
import json

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
class HermesHostRuntime(TrustedHostRuntime):
    """Wake the existing durable queue in an independent bounded watchdog.

    The helper owns its RuntimeInstance. A Hermes shutdown can therefore close
    the foreground Lance handles without racing a consolidation request.
    """

    _owned_worker: WorkerProcess | None = None
    _trailing_worker: WorkerProcess | None = None
    _last_worker_launch: float = 0.0

    def background_status(self) -> dict:
        """Read the bounded receipt; the worker itself persists completion."""
        if self._runtime is None:
            return {}
        path = self._runtime.config.binding.data_directory / "runtime-worker-status.json"
        try:
            if path.is_symlink() or path.stat().st_size > 65536:
                return {"status": "unavailable"}
            result = json.loads(path.read_text(encoding="utf-8"))
            if result.get("installation_id") != self._runtime.config.binding.installation_id:
                return {"status": "unavailable"}
            return result
        except (OSError, ValueError, AttributeError):
            return {}

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


def attach_trusted_host_runtime(
    *, config_path: object, expected_binding: InstanceBinding, session_id: str,
    allowed_scope_ids: frozenset[str], core: MemoryCore | None = None,
    clock: object | None = None, project_id: str | None = None,
    branch_id: str | None = None,
) -> HermesHostRuntime:
    base = _attach_common(
        config_path=config_path, expected_binding=expected_binding,
        session_id=session_id, allowed_scope_ids=allowed_scope_ids,
        core=core, clock=clock, host_adapter="hermes",
        project_id=project_id, branch_id=branch_id,
    )
    return HermesHostRuntime(
        core=base.core, capability_gaps=base.capability_gaps,
        _runtime=base._runtime, _config_path=base._config_path,
        _ephemeral_configs=base._ephemeral_configs,
        _hook_processing_seconds=base._hook_processing_seconds,
        _host_adapter=base._host_adapter,
    )

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
