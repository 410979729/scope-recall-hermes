"""Assemble one runtime of owners. Must not import the Hermes adapter class."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from ..application.capture_journal import CaptureApplication, JournalApplication
from ..application.memory_queries import MemoryQueryApplication
from ..application.runtime_state import RuntimeStateSnapshot
from ..application.vector_service import VectorApplication
from .bootstrap import RuntimeBootstrap
from .background import BackgroundWork
from .ports import MemoryCommandPort, ToolRuntimePort
from .process_lifecycle import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
from .truth_session import TruthSession
from .vector_view import RuntimeVectorView


class BoundLifecycle(Protocol):
    """Process lifecycle already bound to the outer Hermes adapter."""

    def initialize(self, session_id: str, values: Mapping[str, object]) -> None: ...

    def has_live_initialize_runtime(self) -> bool: ...

    def initialize_under_lifecycle_lock(
        self, session_id: str, values: Mapping[str, object]
    ) -> None: ...

    def initialize_writer_runtime(self) -> None: ...

    def initialize_read_only_runtime(self) -> None: ...

    def cleanup_failed_writer_initialization(
        self, *, reraise_companion_errors: bool = False
    ) -> bool: ...

    def promote_to_writer(self) -> None: ...

    def shutdown(self, *, timeout: float) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """Explicit infrastructure and Application services built by Provider."""

    truth: TruthSession
    background: BackgroundWork
    lifecycle: BoundLifecycle
    bootstrap: RuntimeBootstrap
    vector_view: RuntimeVectorView
    vector: VectorApplication
    capture: CaptureApplication
    journal: JournalApplication
    command_port: MemoryCommandPort
    query_port: MemoryQueryApplication
    tool_port: ToolRuntimePort


class RuntimeComposition:
    """Unique TruthSession, BackgroundWork, ProcessLifecycle, bootstrap, vector view, and query port."""

    def __init__(
        self,
        dependencies: RuntimeDependencies,
    ) -> None:
        self.truth = dependencies.truth
        self.background = dependencies.background
        self.lifecycle = dependencies.lifecycle
        self.bootstrap = dependencies.bootstrap
        self.vector_view = dependencies.vector_view
        self.vector = dependencies.vector
        self.capture = dependencies.capture
        self.journal = dependencies.journal
        self._command_port = dependencies.command_port
        self._query_port = dependencies.query_port
        self.tool_port = dependencies.tool_port

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

    def initialize(self, session_id: str, **kwargs: object) -> None:
        """Own process initialize. Provider keeps a one-line delegate."""

        self.lifecycle.initialize(session_id, kwargs)

    def promote_to_writer(self) -> None:
        """Own reader-to-writer promotion. Provider keeps a one-line delegate."""

        self.lifecycle.promote_to_writer()

    def shutdown(
        self, *, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    ) -> None:
        self.lifecycle.shutdown(timeout=timeout)


def assemble_runtime(
    dependencies: RuntimeDependencies,
) -> RuntimeComposition:
    """Build the single composition from explicit, already-bound services."""

    return RuntimeComposition(dependencies)
