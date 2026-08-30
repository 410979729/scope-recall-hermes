"""Outer-boundary binding of the legacy Provider to explicit runtime services.

Only this compatibility module and ``provider.py`` may retain the temporary
Provider-shaped host required by V1 monkeypatch and lifecycle contracts.  The
runtime composition receives the resulting typed dependency container, never
the Provider itself.
"""

from __future__ import annotations

from typing import Mapping

from ..application.capture_journal import CaptureApplication, JournalApplication
from ..application.memory_commands import MemoryCommandApplication
from ..application.memory_queries import MemoryQueryApplication
from ..application.vector_service import VectorApplication
from .provider_hooks import ProviderModuleHooks
from ..runtime.background import BackgroundWork
from ..runtime.bootstrap import RuntimeBootstrap
from ..runtime.capture_service import bind_capture_gateway
from ..runtime.command_adapter import bind_provider_command_adapter
from ..runtime.composition import RuntimeDependencies
from ..runtime.journal_service import bind_journal_gateway
from ..runtime.process_lifecycle import ProcessLifecycle
from ..runtime.provider_compat_hosts import LegacyProviderRuntimeHost
from ..runtime.query_adapter import bind_provider_query_adapter
from ..runtime.tool_port import bind_tool_runtime_port
from ..runtime.truth_session import TruthSession
from ..runtime.vector_service import bind_vector_gateway
from ..runtime.vector_view import RuntimeVectorView


class BoundProviderLifecycle:
    """Hide the Provider argument at the compatibility boundary."""

    def __init__(
        self,
        provider: LegacyProviderRuntimeHost,
        lifecycle: ProcessLifecycle | None = None,
    ) -> None:
        self._provider = provider
        self._lifecycle = lifecycle if lifecycle is not None else ProcessLifecycle()

    def initialize(self, session_id: str, values: Mapping[str, object]) -> None:
        self._lifecycle.initialize(self._provider, session_id, **dict(values))

    def has_live_initialize_runtime(self) -> bool:
        return self._lifecycle.has_live_initialize_runtime(self._provider)

    def initialize_under_lifecycle_lock(
        self, session_id: str, values: Mapping[str, object]
    ) -> None:
        self._lifecycle.initialize_under_lifecycle_lock(
            self._provider, session_id, **dict(values)
        )

    def initialize_writer_runtime(self) -> None:
        self._lifecycle.initialize_writer_runtime(self._provider)

    def initialize_read_only_runtime(self) -> None:
        self._lifecycle.initialize_read_only_runtime(self._provider)

    def cleanup_failed_writer_initialization(
        self, *, reraise_companion_errors: bool = False
    ) -> bool:
        return self._lifecycle.cleanup_failed_writer_initialization(
            self._provider,
            reraise_companion_errors=reraise_companion_errors,
        )

    def promote_to_writer(self) -> None:
        self._lifecycle.promote_to_writer(self._provider)

    def shutdown(self, *, timeout: float) -> None:
        self._lifecycle.shutdown(self._provider, timeout=timeout)


def build_provider_runtime_dependencies(
    provider: LegacyProviderRuntimeHost,
    *,
    truth_cls: type[TruthSession] = TruthSession,
    background_cls: type[BackgroundWork] = BackgroundWork,
) -> RuntimeDependencies:
    """Resolve all Provider-shaped adapters before entering composition."""

    truth = truth_cls(provider)
    background = background_cls(provider)
    lifecycle = BoundProviderLifecycle(provider)
    hooks = ProviderModuleHooks(provider)
    bootstrap = RuntimeBootstrap(provider, hooks)
    vector_view = RuntimeVectorView(provider)
    vector = VectorApplication(bind_vector_gateway(provider, vector_view, hooks))
    capture = CaptureApplication(bind_capture_gateway(provider, hooks))
    journal = JournalApplication(bind_journal_gateway(provider))
    command_port = MemoryCommandApplication(bind_provider_command_adapter(provider))
    query_port = MemoryQueryApplication(bind_provider_query_adapter(provider))
    tool_port = bind_tool_runtime_port(
        provider,
        command_port=command_port,
        query_port=query_port,
    )
    return RuntimeDependencies(
        truth=truth,
        background=background,
        lifecycle=lifecycle,
        bootstrap=bootstrap,
        vector_view=vector_view,
        vector=vector,
        capture=capture,
        journal=journal,
        command_port=command_port,
        query_port=query_port,
        tool_port=tool_port,
    )
