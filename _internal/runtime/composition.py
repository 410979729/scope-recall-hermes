"""Assemble one runtime of owners. Must not import the Hermes adapter class."""

from __future__ import annotations

from typing import Any

from ..application.capture_journal import CaptureApplication, JournalApplication
from ..application.memory_commands import MemoryCommandApplication
from ..application.memory_queries import MemoryQueryApplication
from ..application.runtime_state import RuntimeStateSnapshot
from ..application.vector_service import VectorApplication
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
from .vector_service import bind_vector_gateway


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
        self.vector = VectorApplication(bind_vector_gateway(adapter, self.vector_view))
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

        self.vector.setup()
        self.capture.start_writer()

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

    return RuntimeComposition(
        adapter,
        truth_cls=truth_cls or TruthSession,
        background_cls=background_cls or BackgroundWork,
    )
