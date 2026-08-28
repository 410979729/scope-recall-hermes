"""Small Core-facing capability ports.

Concrete infrastructure may own SQLite, locks, and host compatibility.  Those
details must never cross the typed Application boundary declared here.
"""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from ..application.memory_commands import (
    ArchiveMemoriesRequest,
    DedupeMemoriesRequest,
    DeleteMemoriesRequest,
    FactOwnedMemoryIdsRequest,
    FeedbackMemoryRequest,
    GovernMemoriesRequest,
    MergeMemoriesRequest,
    PrivacyPurgeRequest,
    StoreMemoryRequest,
    UpdateMemoryRequest,
)


@runtime_checkable
class RecallServiceView(Protocol):
    """Read-only recall handle used by query payloads."""

    last_rejected_candidates: object
    last_funnel_trace: object
    last_recall_packet: object

    def search_memories(
        self, query: str, *, limit: int = 5, recall_mode: str = "advisory"
    ) -> object: ...


@runtime_checkable
class ScopeContextPort(Protocol):
    def query_scope_view(self) -> Mapping[str, object]: ...


@runtime_checkable
class TruthStorePort(Protocol):
    """Marker for a truth repository capability.

    Transactions and concrete connections remain private to infrastructure.
    """


@runtime_checkable
class VectorViewPort(Protocol):
    def vector_status_view(self) -> Mapping[str, object]: ...


@runtime_checkable
class RetrievalViewPort(Protocol):
    def retrieval_status_view(self) -> Mapping[str, object]: ...


@runtime_checkable
class RuntimeStatusPort(Protocol):
    def vector_status_view(self) -> Mapping[str, object]: ...

    def runtime_status_view(self) -> Mapping[str, object]: ...


@runtime_checkable
class RecallViewPort(Protocol):
    def recall_service_view(self) -> RecallServiceView: ...

    def recall_limit(self) -> int: ...


@runtime_checkable
class MemoryQueryPort(
    ScopeContextPort,
    TruthStorePort,
    VectorViewPort,
    RetrievalViewPort,
    RuntimeStatusPort,
    RecallViewPort,
    Protocol,
):
    """Read-only memory query surface. Callers must not reach private adapter fields."""


@runtime_checkable
class MemoryCommandPort(Protocol):
    """Provider-neutral application command surface."""

    def store(self, request: StoreMemoryRequest) -> tuple[str, bool, str]: ...

    def update(self, request: UpdateMemoryRequest) -> tuple[bool, str, str]: ...

    def merge(self, request: MergeMemoriesRequest) -> dict[str, object]: ...

    def archive(self, request: ArchiveMemoriesRequest) -> dict[str, object]: ...

    def delete(self, request: DeleteMemoriesRequest) -> int: ...

    def feedback(self, request: FeedbackMemoryRequest) -> dict[str, object]: ...

    def govern(self, request: GovernMemoriesRequest) -> dict[str, object]: ...

    def dedupe(self, request: DedupeMemoriesRequest) -> dict[str, object]: ...

    def repair(self) -> dict[str, object]: ...

    def fact_owned(self, request: FactOwnedMemoryIdsRequest) -> list[str]: ...

    def purge(self, request: PrivacyPurgeRequest) -> dict[str, object]: ...


@runtime_checkable
class RuntimeAdapterPort(MemoryQueryPort, Protocol):
    """Legacy name retained as a narrow read-side compatibility view."""


@runtime_checkable
class FactToolPort(Protocol):
    """Compatibility marker; concrete fact infrastructure remains private."""


@runtime_checkable
class ToolRuntimePort(Protocol):
    """Compatibility marker for the concrete outer tool adapter."""

@runtime_checkable
class ToolRouterPort(Protocol):
    """Public tool dispatch. The Hermes adapter only forwards into this port."""

    def route_tool(self, tool_name: str, args: dict[str, object]) -> str: ...
