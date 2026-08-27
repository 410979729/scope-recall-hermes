"""Compatibility gateway from application commands to the legacy runtime."""

from __future__ import annotations

from typing import Any, cast

from ..application.memory_commands import (
    ArchiveMemoriesRequest,
    DedupeMemoriesRequest,
    DeleteMemoriesRequest,
    FactOwnedMemoryIdsRequest,
    FeedbackMemoryRequest,
    GovernMemoriesRequest,
    MemoryCommandGateway,
    MergeMemoriesRequest,
    StoreMemoryRequest,
    UpdateMemoryRequest,
)
from ... import memory_ops, write_kernel


def _rollback_after_error(host: Any, context: str) -> None:
    fn = getattr(host, "rollback_conn_after_error", None)
    if not callable(fn):
        fn = getattr(host, "_rollback_conn_after_error", None)
    if callable(fn):
        fn(context)


def _store_operation(host: Any) -> Any:
    bound = getattr(host, "_store_now", None)
    function = getattr(bound, "__func__", bound)
    namespace = getattr(function, "__globals__", {})
    candidate = namespace.get("store_memory_now") if isinstance(namespace, dict) else None
    return candidate if callable(candidate) else memory_ops.store_memory_now


class ProviderCommandAdapter:
    """Infrastructure gateway; Provider-shaped access is confined here."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def store(self, request: StoreMemoryRequest) -> tuple[str, bool, str]:
        operation = _store_operation(self._host)
        with write_kernel.hold_positive_write_authority(self._host):
            try:
                return operation(
                    self._host,
                    content=request.content,
                    source=request.source,
                    target=request.target,
                    session_id=request.session_id,
                    metadata=request.metadata,
                    allow_duplicate=request.allow_duplicate,
                    semantic_merge=request.semantic_merge,
                    scope_mode=request.scope_mode,
                )
            except Exception:
                _rollback_after_error(self._host, "store_now")
                raise

    def update(self, request: UpdateMemoryRequest) -> tuple[bool, str, str]:
        return memory_ops.update_memory(
            self._host, request.memory_id, request.content, request.target
        )

    def merge(self, request: MergeMemoriesRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            memory_ops.merge_memories(
                self._host,
                request.target_id,
                list(request.source_ids),
                request.content,
                request.target,
            ),
        )

    def archive(self, request: ArchiveMemoriesRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            memory_ops.archive_memories(
                self._host,
                list(request.ids),
                reason=request.reason,
                actor=request.actor,
                batch_id=request.batch_id,
            ),
        )

    def delete(self, request: DeleteMemoriesRequest) -> int:
        return int(memory_ops.delete_memories(self._host, list(request.ids)))

    def feedback(self, request: FeedbackMemoryRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            memory_ops.feedback_memory(
                self._host,
                memory_id=request.memory_id,
                rating=request.rating,
                note=request.note,
            ),
        )

    def govern(self, request: GovernMemoriesRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            memory_ops.govern_memories(
                self._host, dry_run=request.dry_run, scope_only=request.scope_only
            ),
        )

    def dedupe(self, request: DedupeMemoriesRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            memory_ops.dedupe_memories(
                self._host, dry_run=request.dry_run, scope_only=request.scope_only
            ),
        )

    def repair(self) -> dict[str, object]:
        return cast(dict[str, object], memory_ops.repair_vector(self._host))

    def fact_owned(self, request: FactOwnedMemoryIdsRequest) -> list[str]:
        return list(memory_ops.fact_owned_memory_ids(self._host, list(request.ids)))


def bind_provider_command_adapter(obj: Any) -> MemoryCommandGateway:
    """Return the infrastructure gateway for a composition host."""

    if isinstance(obj, ProviderCommandAdapter):
        return obj
    return ProviderCommandAdapter(obj)
