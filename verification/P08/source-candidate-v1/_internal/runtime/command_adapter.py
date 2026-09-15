"""Compatibility gateway from application commands to the legacy runtime."""

from __future__ import annotations

from typing import Any, cast

from ..application.memory_commands import (
    ArchiveMemoriesRequest,
    DedupeMemoriesRequest,
    DeleteMemoriesRequest,
    DeleteMemoriesResult,
    FactOwnedMemoryIdsRequest,
    FeedbackMemoryRequest,
    GovernMemoriesRequest,
    MemoryCommandGateway,
    MergeMemoriesRequest,
    PrivacyPurgeRequest,
    ReviewMemoryCandidateRequest,
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
        with write_kernel.command_write_access(self._host, user_initiated=True):
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

    def review_candidate(self, request: ReviewMemoryCandidateRequest) -> dict[str, object]:
        from contextlib import nullcontext

        access = nullcontext() if request.dry_run else write_kernel.command_write_access(
            self._host, capture_barrier=True, user_initiated=True,
        )
        with access:
            return memory_ops.review_memory_candidate(
                self._host, memory_id=request.memory_id, action=request.action,
                dry_run=request.dry_run, expected_updated_at=request.expected_updated_at,
                expected_lifecycle=request.expected_lifecycle,
            )

    def update(self, request: UpdateMemoryRequest) -> tuple[bool, str, str]:
        with write_kernel.command_write_access(self._host, user_initiated=True):
            return memory_ops.update_memory(
                self._host, request.memory_id, request.content, request.target
            )

    def merge(self, request: MergeMemoriesRequest) -> dict[str, object]:
        with write_kernel.command_write_access(
            self._host, capture_barrier=True, user_initiated=True
        ):
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
        with write_kernel.command_write_access(
            self._host, capture_barrier=True, user_initiated=True
        ):
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

    def delete(self, request: DeleteMemoriesRequest) -> DeleteMemoriesResult:
        with write_kernel.command_write_access(
            self._host, capture_barrier=True, user_initiated=True
        ):
            return memory_ops.delete_memories_result(self._host, list(request.ids))

    def feedback(self, request: FeedbackMemoryRequest) -> dict[str, object]:
        with write_kernel.command_write_access(self._host, user_initiated=True):
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
        if request.dry_run:
            return cast(
                dict[str, object],
                memory_ops.govern_memories(
                    self._host, dry_run=True, scope_only=request.scope_only
                ),
            )
        with write_kernel.command_write_access(self._host, user_initiated=True):
            return cast(
                dict[str, object],
                memory_ops.govern_memories(
                    self._host, dry_run=False, scope_only=request.scope_only
                ),
            )

    def dedupe(self, request: DedupeMemoriesRequest) -> dict[str, object]:
        if request.dry_run:
            return cast(
                dict[str, object],
                memory_ops.dedupe_memories(
                    self._host, dry_run=True, scope_only=request.scope_only
                ),
            )
        with write_kernel.command_write_access(
            self._host, capture_barrier=True, user_initiated=True
        ):
            return cast(
                dict[str, object],
                memory_ops.dedupe_memories(
                    self._host, dry_run=False, scope_only=request.scope_only
                ),
            )

    def repair(self) -> dict[str, object]:
        with write_kernel.command_write_access(self._host, user_initiated=True):
            return cast(dict[str, object], memory_ops.repair_vector(self._host))

    def fact_owned(self, request: FactOwnedMemoryIdsRequest) -> list[str]:
        return list(memory_ops.fact_owned_memory_ids(self._host, list(request.ids)))

    def purge(self, request: PrivacyPurgeRequest) -> dict[str, object]:
        from ...privacy_purge import run_privacy_purge

        action = str(request.action or "").strip().lower().replace("-", "_")
        if action in {"plan", "status"}:
            return cast(
                dict[str, object],
                run_privacy_purge(
                    self._host,
                    action=request.action,
                    ids=request.ids,
                    operation_id=request.operation_id,
                    confirmation=request.confirmation,
                ),
            )
        with write_kernel.command_write_access(
            self._host, capture_barrier=True, user_initiated=True
        ):
            return cast(
                dict[str, object],
                run_privacy_purge(
                    self._host,
                    action=request.action,
                    ids=request.ids,
                    operation_id=request.operation_id,
                    confirmation=request.confirmation,
                ),
            )


def bind_provider_command_adapter(obj: Any) -> MemoryCommandGateway:
    """Return the infrastructure gateway for a composition host."""

    if isinstance(obj, ProviderCommandAdapter):
        return obj
    return ProviderCommandAdapter(obj)
