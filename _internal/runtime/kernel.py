"""Query and command facades. No SQL and no Hermes lifecycle."""

from __future__ import annotations

from typing import Any, cast

from ..application.memory_commands import (
    ArchiveMemoriesRequest,
    DedupeMemoriesRequest,
    DeleteMemoriesRequest,
    FactOwnedMemoryIdsRequest,
    FeedbackMemoryRequest,
    GovernMemoriesRequest,
    MergeMemoriesRequest,
    StoreMemoryRequest,
    UpdateMemoryRequest,
)
from .ports import MemoryCommandPort, MemoryQueryPort
from ...memory_queries import (
    benchmark_queries,
    context_payload,
    explain_query,
    export_memories,
    hygiene_report,
    inspect_memory,
    probe_entity,
    profile_payload,
    related_entities,
    stats_payload,
)


class QueryKernel:
    """Thin query/diagnostic facade used by the Hermes adapter."""

    def context(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return context_payload(port, **kwargs)

    def profile(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return profile_payload(port, **kwargs)

    def inspect(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return inspect_memory(port, **kwargs)

    def explain(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return explain_query(port, **kwargs)

    def stats(self, port: MemoryQueryPort) -> dict[str, Any]:
        return stats_payload(port)

    def probe(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return probe_entity(port, **kwargs)

    def related(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return related_entities(port, **kwargs)

    def export(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return export_memories(port, **kwargs)

    def hygiene(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return hygiene_report(port, **kwargs)

    def benchmark(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return benchmark_queries(port, **kwargs)


class _LegacyPersistCommandPort:
    """Isolated-host fallback for FakeProvider and external hosts.

    Production Provider and Tooling writes use the assembled composition
    ``command_port``. This wrapper exists only so hosts without a
    composition can still intercept ``store_now`` / ``_store_now``.
    CommandKernel must not select it for a live Provider.
    """

    def __init__(self, host: Any) -> None:
        self._host = host

    def store(self, request: StoreMemoryRequest) -> tuple[str, bool, str]:
        fn = getattr(self._host, "store_now", None)
        if callable(fn):
            return cast(
                tuple[str, bool, str],
                fn(
                    content=request.content,
                    source=request.source,
                    target=request.target,
                    session_id=request.session_id,
                    metadata=request.metadata,
                    allow_duplicate=request.allow_duplicate,
                    semantic_merge=request.semantic_merge,
                    scope_mode=request.scope_mode,
                ),
            )
        fn = getattr(self._host, "_store_now", None)
        if not callable(fn):
            raise AttributeError("store_now")
        return cast(
            tuple[str, bool, str],
            fn(
                content=request.content,
                source=request.source,
                target=request.target,
                session_id=request.session_id,
                metadata=request.metadata,
                allow_duplicate=request.allow_duplicate,
                semantic_merge=request.semantic_merge,
                scope_mode=request.scope_mode,
            ),
        )

    def update(self, request: UpdateMemoryRequest) -> tuple[bool, str, str]:
        fn = getattr(self._host, "_update_memory", None)
        if not callable(fn):
            raise AttributeError("update_memory")
        return cast(
            tuple[bool, str, str],
            fn(request.memory_id, request.content, request.target),
        )

    def merge(self, request: MergeMemoriesRequest) -> dict[str, object]:
        fn = getattr(self._host, "_merge_memories", None)
        if not callable(fn):
            raise AttributeError("merge_memories")
        return cast(
            dict[str, object],
            fn(
                request.target_id,
                list(request.source_ids),
                request.content,
                request.target,
            ),
        )

    def archive(self, request: ArchiveMemoriesRequest) -> dict[str, object]:
        fn = getattr(self._host, "_archive_memories", None)
        if not callable(fn):
            raise AttributeError("archive_memories")
        return cast(
            dict[str, object],
            fn(
                list(request.ids),
                reason=request.reason,
                actor=request.actor,
                batch_id=request.batch_id,
            ),
        )

    def feedback(self, request: FeedbackMemoryRequest) -> dict[str, object]:
        fn = getattr(self._host, "_feedback_memory", None)
        if not callable(fn):
            raise AttributeError("feedback_memory")
        return cast(
            dict[str, object],
            fn(memory_id=request.memory_id, rating=request.rating, note=request.note),
        )

    def govern(self, request: GovernMemoriesRequest) -> dict[str, object]:
        fn = getattr(self._host, "_govern_memories", None)
        if not callable(fn):
            raise AttributeError("govern_memories")
        return cast(
            dict[str, object],
            fn(dry_run=request.dry_run, scope_only=request.scope_only),
        )

    def delete(self, request: DeleteMemoriesRequest) -> int:
        fn = getattr(self._host, "_delete_memories", None)
        if not callable(fn):
            raise AttributeError("delete_memories")
        return cast(int, fn(list(request.ids)))

    def dedupe(self, request: DedupeMemoriesRequest) -> dict[str, object]:
        fn = getattr(self._host, "_dedupe_memories", None)
        if not callable(fn):
            raise AttributeError("dedupe_memories")
        return cast(
            dict[str, object],
            fn(dry_run=request.dry_run, scope_only=request.scope_only),
        )

    def repair(self) -> dict[str, object]:
        fn = getattr(self._host, "_repair_vector", None)
        if not callable(fn):
            raise AttributeError("repair_vector")
        return cast(dict[str, object], fn())

    def fact_owned(self, request: FactOwnedMemoryIdsRequest) -> list[str]:
        fn = getattr(self._host, "fact_owned_memory_ids", None)
        if not callable(fn):
            fn = getattr(self._host, "_fact_owned_memory_ids", None)
        if not callable(fn):
            raise AttributeError("fact_owned_memory_ids")
        return cast(list[str], fn(list(request.ids)))


def bind_memory_command_port(obj: Any) -> MemoryCommandPort:
    """Wrap isolated-host persist hooks. Production uses composition.command_port."""

    if isinstance(obj, _LegacyPersistCommandPort):
        return obj
    return _LegacyPersistCommandPort(obj)


class CommandKernel:
    """Build typed requests and invoke provider-neutral application commands."""

    def store(
        self,
        port: MemoryCommandPort,
        *,
        content: str,
        source: str,
        target: str,
        session_id: str,
        metadata: dict[str, object] | None = None,
        allow_duplicate: bool = False,
        semantic_merge: bool = False,
        scope_mode: str | None = None,
    ) -> tuple[str, bool, str]:
        return port.store(
            StoreMemoryRequest(
                content=content,
                source=source,
                target=target,
                session_id=session_id,
                metadata=metadata,
                allow_duplicate=allow_duplicate,
                semantic_merge=semantic_merge,
                scope_mode=scope_mode,
            )
        )

    def update(
        self,
        port: MemoryCommandPort,
        memory_id: str,
        content: str,
        target: str | None = None,
    ) -> tuple[bool, str, str]:
        return port.update(UpdateMemoryRequest(memory_id, content, target))

    def merge(
        self,
        port: MemoryCommandPort,
        target_id: str,
        source_ids: list[str],
        content: str | None = None,
        target: str | None = None,
    ) -> dict[str, object]:
        return port.merge(MergeMemoriesRequest(target_id, tuple(source_ids), content, target))

    def archive(
        self,
        port: MemoryCommandPort,
        ids: list[str],
        *,
        reason: str = "scope_recall_forget",
        actor: str = "scope_recall_forget",
        batch_id: str = "",
    ) -> dict[str, object]:
        return port.archive(ArchiveMemoriesRequest(tuple(ids), reason, actor, batch_id))

    def feedback(
        self,
        port: MemoryCommandPort,
        *,
        memory_id: str,
        rating: str,
        note: str = "",
    ) -> dict[str, object]:
        return port.feedback(FeedbackMemoryRequest(memory_id, rating, note))

    def govern(
        self,
        port: MemoryCommandPort,
        *,
        dry_run: bool = True,
        scope_only: bool = True,
    ) -> dict[str, object]:
        return port.govern(GovernMemoriesRequest(dry_run, scope_only))

    def delete(self, port: MemoryCommandPort, ids: list[str]) -> int:
        return port.delete(DeleteMemoriesRequest(tuple(ids)))

    def dedupe(
        self,
        port: MemoryCommandPort,
        *,
        dry_run: bool = True,
        scope_only: bool = True,
    ) -> dict[str, object]:
        return port.dedupe(DedupeMemoriesRequest(dry_run, scope_only))

    def repair(self, port: MemoryCommandPort) -> dict[str, object]:
        return port.repair()

    def fact_owned(self, port: MemoryCommandPort, ids: list[str]) -> list[str]:
        return port.fact_owned(FactOwnedMemoryIdsRequest(tuple(ids)))


class RuntimeKernel(QueryKernel, CommandKernel):
    """Compatibility facade used by existing KERNEL imports."""


KERNEL = RuntimeKernel()
COMMAND_KERNEL = KERNEL
