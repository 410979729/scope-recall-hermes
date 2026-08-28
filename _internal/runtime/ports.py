"""Small Core-facing capability ports.

Concrete infrastructure may own SQLite, locks, and host compatibility.  Those
details must never cross the typed Application boundary declared here.
"""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence, TypedDict, runtime_checkable

from ...fact_actions import EvolutionProposal, EvolutionResult

from ..application.memory_commands import (
    ArchiveMemoriesRequest,
    DedupeMemoriesRequest,
    DeleteMemoriesRequest,
    DeleteMemoriesResult,
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

    def delete(self, request: DeleteMemoriesRequest) -> DeleteMemoriesResult: ...

    def feedback(self, request: FeedbackMemoryRequest) -> dict[str, object]: ...

    def govern(self, request: GovernMemoriesRequest) -> dict[str, object]: ...

    def dedupe(self, request: DedupeMemoriesRequest) -> dict[str, object]: ...

    def repair(self) -> dict[str, object]: ...

    def fact_owned(self, request: FactOwnedMemoryIdsRequest) -> list[str]: ...

    def purge(self, request: PrivacyPurgeRequest) -> dict[str, object]: ...


@runtime_checkable
class RuntimeAdapterPort(MemoryQueryPort, Protocol):
    """Legacy name retained as a narrow read-side compatibility view."""


class FactMemoryRow(TypedDict):
    """Provider-neutral projection needed by structured update tooling."""

    id: str
    source: str
    target: str
    scope_id: str
    metadata: object


class FactTargetRow(TypedDict):
    """Provider-neutral target identity used by maintenance proposals."""

    id: str
    source: str
    target: str
    scope_id: str


@runtime_checkable
class FactToolPort(Protocol):
    """Narrow host-neutral capabilities required by structured fact tooling.

    The adapter owns compatibility with the concrete Provider.  Fact tooling
    receives only this explicit surface, so it cannot grow new private-host
    dependencies without extending and reviewing the port first.
    """

    def scope_id_for_mode(self, scope_mode: str) -> str: ...

    def writable_scope_ids(self) -> list[str]: ...

    def session_id(self) -> str: ...

    def shared_pool_scope_id(self) -> str: ...

    def shared_scope_id(self) -> str: ...

    def scope_mode_for(self, target: str, source: str = "") -> str: ...

    def clean_text(self, text: str) -> str: ...

    def fact_memory_row(
        self, memory_id: str, writable_scope_ids: Sequence[str]
    ) -> FactMemoryRow | None: ...

    def fact_target_rows(
        self, target_ids: Sequence[str], writable_scope_ids: Sequence[str]
    ) -> list[FactTargetRow]: ...

    def fact_memory_updated_at(self, memory_id: str) -> str: ...

    def fact_pipeline_receipt_exists(
        self,
        *,
        lane: str,
        run_id: str,
        source_key: str,
        scope_id: str,
    ) -> bool: ...

    def execute_fact_proposal(
        self,
        *,
        proposal: EvolutionProposal,
        lane: str,
        run_id: str,
        source_key: str,
        trusted_scope_id: str,
        writable_scope_ids: Sequence[str],
        actor: str,
        source: str,
        target: str,
        content: str,
        metadata: Mapping[str, object],
        dry_run: bool,
        provenance_refs: Sequence[Mapping[str, object]],
    ) -> EvolutionResult: ...


@runtime_checkable
class ToolRuntimePort(Protocol):
    """Compatibility marker for the concrete outer tool adapter."""

@runtime_checkable
class ToolRouterPort(Protocol):
    """Public tool dispatch. The Hermes adapter only forwards into this port."""

    def route_tool(self, tool_name: str, args: dict[str, object]) -> str: ...
