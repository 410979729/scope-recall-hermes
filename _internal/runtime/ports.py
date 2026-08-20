"""Narrow capability ports. These protocols must not import the Hermes adapter class."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class QueryLock(Protocol):
    """Read-side mutex. Callers only enter/exit; they do not own SQLite."""

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool: ...

    def release(self) -> None: ...

    def __enter__(self) -> Any: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...


@runtime_checkable
class RecallServiceView(Protocol):
    """Read-only recall handle used by query payloads."""

    last_rejected_candidates: Any
    last_funnel_trace: Any

    def search_memories(self, query: str, limit: int = 5) -> Any: ...


@runtime_checkable
class ScopeContextPort(Protocol):
    def query_scope_view(self) -> Mapping[str, Any]: ...


@runtime_checkable
class TruthStorePort(Protocol):
    def query_connection(self) -> sqlite3.Connection: ...

    def query_lock(self) -> QueryLock: ...


@runtime_checkable
class VectorViewPort(Protocol):
    def vector_status_view(self) -> Mapping[str, Any]: ...


@runtime_checkable
class RetrievalViewPort(Protocol):
    def retrieval_status_view(self) -> Mapping[str, Any]: ...


@runtime_checkable
class RuntimeStatusPort(Protocol):
    def vector_status_view(self) -> Mapping[str, Any]: ...

    def runtime_status_view(self) -> Mapping[str, Any]: ...


@runtime_checkable
class RecallViewPort(Protocol):
    def recall_service_view(self) -> RecallServiceView: ...


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
class MemoryCommandPort(TruthStorePort, ScopeContextPort, VectorViewPort, Protocol):
    """Typed command surface for CommandKernel. Distinct from a bare Provider.

    CommandKernel calls only these declared methods. memory_ops and
    write_kernel use write_target() when they still need the Provider-shaped
    domain object. Cleanup, config, scope, and authority stay public here.
    """

    def rollback_conn_after_error(self, context: str) -> None: ...

    def write_target(self) -> object: ...

    def store_now(
        self,
        *,
        content: str,
        source: str,
        target: str,
        session_id: str,
        metadata: dict[str, object] | None = None,
        allow_duplicate: bool = False,
        semantic_merge: bool = False,
        scope_mode: str | None = None,
    ) -> tuple[str, bool, str]: ...

    def command_update_memory(
        self, memory_id: str, content: str, target: str | None = None
    ) -> tuple[bool, str, str]: ...

    def command_merge_memories(
        self,
        target_id: str,
        source_ids: list[str],
        content: str | None = None,
        target: str | None = None,
    ) -> dict[str, object]: ...

    def command_archive_memories(
        self,
        ids: list[str],
        *,
        reason: str = "scope_recall_forget",
        actor: str = "scope_recall_forget",
        batch_id: str = "",
    ) -> dict[str, object]: ...

    def command_delete_memories(self, ids: list[str]) -> int: ...

    def command_feedback_memory(
        self, *, memory_id: str, rating: str, note: str = ""
    ) -> dict[str, object]: ...

    def command_govern_memories(
        self, *, dry_run: bool = True, scope_only: bool = True
    ) -> dict[str, object]: ...

    def command_dedupe_memories(
        self, *, dry_run: bool = True, scope_only: bool = True
    ) -> dict[str, object]: ...

    def command_repair_vector(self) -> dict[str, object]: ...

    def fact_owned_memory_ids(self, ids: list[str]) -> list[str]: ...

    def clean_text(self, text: object) -> str: ...

    def config_view(self) -> dict[str, object]: ...

    def config_value(self, key: str, default: object = None) -> object: ...

    def scope_id(self) -> str: ...

    def shared_scope_id(self) -> str: ...

    def shared_pool_scope_id(self) -> str: ...

    def writable_scope_ids(self) -> list[str]: ...

    def has_positive_write_authority(self) -> bool: ...

    def writer_lifecycle_lock(self) -> object: ...


@runtime_checkable
class RuntimeAdapterPort(MemoryQueryPort, Protocol):
    """Composition identity: query surface plus the unique owner bind target.

    RuntimeComposition needs this object as:
    - ``query_port`` / ``MemoryQueryPort``
    - the single identity passed to TruthSession, BackgroundWork, and writer hooks
    ``command_port`` is a distinct command adapter, not this object.
    Compatible monkeypatches resolve from ``type(adapter).__module__``.
    """


@runtime_checkable
class FactToolPort(TruthStorePort, Protocol):
    """Narrow fact-tool surface. Reuses TruthStorePort connection/lock."""

    def clean_text(self, text: Any) -> str: ...

    def session_id(self) -> str: ...

    def scope_object(self) -> Any: ...

    def scope_id(self) -> str: ...

    def shared_scope_id(self) -> str: ...

    def shared_pool_scope_id(self) -> str: ...

    def writable_scope_ids(self) -> list[str]: ...

    def scope_id_for_mode(self, scope_mode: str) -> str: ...

    def scope_mode_for(self, target: str, source: str = "") -> str: ...

    def config_view(self) -> dict[str, Any]: ...


@runtime_checkable
class ToolRuntimePort(MemoryQueryPort, FactToolPort, Protocol):
    """Tool runtime surface. Query tools stay zero-write; writes use named wrappers."""

    def accessible_scope_ids(self) -> list[str]: ...

    def shared_pool_enabled(self) -> bool: ...

    def shared_pool_write_enabled(self) -> bool: ...

    def config_value(self, key: str, default: Any = None) -> Any: ...

    def normalize_query(self, query: str, char_limit: int) -> str: ...

    def retrieval_config_view(self) -> dict[str, Any]: ...

    def vector_store_view(self) -> Any: ...

    def writer_lifecycle_lock(self) -> Any: ...

    def has_positive_write_authority(self) -> bool: ...

    def rollback_conn_after_error(self, context: str) -> Any: ...

    def recover_sqlite_connection_after_error(self, context: str) -> Mapping[str, Any]: ...

    def store_now(self, *args: Any, **kwargs: Any) -> Any: ...

    def update_memory(self, *args: Any, **kwargs: Any) -> Any: ...

    def merge_memories(self, *args: Any, **kwargs: Any) -> Any: ...

    def archive_memories(self, *args: Any, **kwargs: Any) -> Any: ...

    def delete_memories(self, *args: Any, **kwargs: Any) -> Any: ...

    def feedback_memory(self, *args: Any, **kwargs: Any) -> Any: ...

    def fact_owned_memory_ids(self, *args: Any, **kwargs: Any) -> Any: ...

    def dedupe_memories(self, *args: Any, **kwargs: Any) -> Any: ...

    def govern_memories(self, *args: Any, **kwargs: Any) -> Any: ...

    def repair_vector(self, *args: Any, **kwargs: Any) -> Any: ...

    def hygiene_report(self, *args: Any, **kwargs: Any) -> Any: ...

    def stats_payload(self, *args: Any, **kwargs: Any) -> Any: ...

    def inspect_memory(self, *args: Any, **kwargs: Any) -> Any: ...

    def explain_query(self, *args: Any, **kwargs: Any) -> Any: ...

    def export_memories(self, *args: Any, **kwargs: Any) -> Any: ...

    def context_payload(self, *args: Any, **kwargs: Any) -> Any: ...

    def profile_payload(self, *args: Any, **kwargs: Any) -> Any: ...

    def probe_entity(self, *args: Any, **kwargs: Any) -> Any: ...

    def related_entities(self, *args: Any, **kwargs: Any) -> Any: ...

    def benchmark_queries(self, *args: Any, **kwargs: Any) -> Any: ...

    def run_reflection(self, args: Mapping[str, Any]) -> Any: ...

    def mark_vector_needs_repair(self, reason: str) -> None: ...

    def hermes_home_path(self) -> Any: ...

    def reflection_transport(self) -> Any: ...

    def evidence_runtime(self) -> Any: ...


@runtime_checkable
class ToolRouterPort(Protocol):
    """Public tool dispatch. The Hermes adapter only forwards into this port."""

    def route_tool(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str: ...
