"""Assemble one runtime of owners. Must not import the Hermes adapter class."""

from __future__ import annotations

import sys
from typing import Any

from ..application.capture_journal import CaptureApplication, JournalApplication
from ..application.memory_commands import MemoryCommandApplication
from ..application.memory_queries import MemoryQueryApplication
from ..application.runtime_state import RuntimeStateSnapshot
from .background import BackgroundWork
from .bootstrap import RuntimeBootstrap
from .capture_service import bind_capture_gateway
from .command_adapter import bind_provider_command_adapter
from .journal_service import bind_journal_gateway
from .ports import MemoryCommandPort, RuntimeAdapterPort, ToolRuntimePort
from .process_lifecycle import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS, ProcessLifecycle
from .query_adapter import bind_provider_query_adapter
from .tool_port import bind_tool_runtime_port
from .truth_session import TruthSession
from .vector_view import RuntimeVectorView


def _adapter_provider_module(adapter: RuntimeAdapterPort) -> Any:
    provider_mod = sys.modules.get(getattr(type(adapter), "__module__", "") or "")
    if provider_mod is None:
        provider_mod = sys.modules.get("scope_recall.provider")
    return provider_mod


class RuntimeComposition:
    """Unique TruthSession, BackgroundWork, ProcessLifecycle, bootstrap, vector view, and query port."""

    def __init__(
        self,
        adapter: RuntimeAdapterPort,
        *,
        truth_cls: type[TruthSession] = TruthSession,
        background_cls: type[BackgroundWork] = BackgroundWork,
        lifecycle: ProcessLifecycle | None = None,
    ) -> None:
        self.adapter: RuntimeAdapterPort = adapter
        self.truth = truth_cls(adapter)
        self.background = background_cls(adapter)
        self.lifecycle = lifecycle if lifecycle is not None else ProcessLifecycle()
        self.bootstrap = RuntimeBootstrap(adapter)
        self.vector_view = RuntimeVectorView(adapter)
        self.capture = CaptureApplication(bind_capture_gateway(adapter))
        self.journal = JournalApplication(bind_journal_gateway(adapter))
        command_gateway = bind_provider_command_adapter(adapter)
        query_gateway = bind_provider_query_adapter(adapter)
        self._command_port: MemoryCommandPort = MemoryCommandApplication(command_gateway)
        self._query_port = MemoryQueryApplication(query_gateway)
        self.tool_port: ToolRuntimePort = bind_tool_runtime_port(
            adapter,
            command_port=self._command_port,
            query_port=self._query_port,
        )

    @property
    def query_port(self) -> MemoryQueryApplication:
        return self._query_port

    @property
    def runtime_state(self) -> RuntimeStateSnapshot:
        return self._query_port.runtime_state()

    @property
    def command_port(self) -> MemoryCommandPort:
        return self._command_port

    def attach_writer_runtime(self) -> None:
        """Own vector / capture-writer startup. Not Provider."""

        from ...capture import start_writer as default_start_writer
        from ...vector_runtime import setup_vector_layer as default_setup_vector_layer

        adapter = self.adapter
        provider_mod = _adapter_provider_module(adapter)

        def _hook(name: str, default: Any) -> Any:
            fn = getattr(provider_mod, name, None) if provider_mod is not None else None
            return fn if callable(fn) else default

        _hook("setup_vector_layer", default_setup_vector_layer)(adapter)
        _hook("start_writer", default_start_writer)(adapter)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Own process initialize. Provider keeps a one-line delegate."""

        self.lifecycle.initialize(self.adapter, session_id, **kwargs)

    def promote_to_writer(self) -> None:
        """Own reader-to-writer promotion. Provider keeps a one-line delegate."""

        self.lifecycle.promote_to_writer(self.adapter)

    def shutdown(
        self, *, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    ) -> None:
        self.lifecycle.shutdown(self.adapter, timeout=timeout)


def assemble_runtime(
    adapter: RuntimeAdapterPort,
    *,
    truth_cls: type[TruthSession] | None = None,
    background_cls: type[BackgroundWork] | None = None,
) -> RuntimeComposition:
    """Build the single composition and bind compatible adapter aliases."""

    provider_mod = sys.modules.get(type(adapter).__module__)
    if provider_mod is None:
        provider_mod = sys.modules.get("scope_recall.provider")
    resolved_truth = truth_cls
    resolved_background = background_cls
    if resolved_truth is None and provider_mod is not None:
        maybe = getattr(provider_mod, "TruthSession", None)
        if isinstance(maybe, type):
            resolved_truth = maybe
    if resolved_background is None and provider_mod is not None:
        maybe = getattr(provider_mod, "BackgroundWork", None)
        if isinstance(maybe, type):
            resolved_background = maybe
    composition = RuntimeComposition(
        adapter,
        truth_cls=resolved_truth or TruthSession,
        background_cls=resolved_background or BackgroundWork,
    )
    return composition
