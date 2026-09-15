"""Query and command facades. No SQL and no Hermes lifecycle."""

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
    MergeMemoriesRequest,
    PrivacyPurgeRequest,
    ReviewMemoryCandidateRequest,
    StoreMemoryRequest,
    UpdateMemoryRequest,
)
from ..application.memory_queries import (
    BenchmarkQueriesRequest,
    ContextQueryRequest,
    EntityQueryRequest,
    ExplainQueryRequest,
    ExportMemoriesRequest,
    HygieneQueryRequest,
    InspectMemoryRequest,
    MemoryQueryApplication,
    ProfileQueryRequest,
    RecallInspectorRequest,
)
from .ports import MemoryCommandPort


class QueryKernel:
    """Thin query/diagnostic facade used by the Hermes adapter."""

    def context(
        self,
        port: MemoryQueryApplication,
        *,
        query: str,
        limit: int = 5,
        max_chars: int = 900,
    ) -> dict[str, object]:
        return port.context(ContextQueryRequest(query, limit, max_chars))

    def profile(
        self,
        port: MemoryQueryApplication,
        *,
        query: str = "",
        entity: str = "",
        targets: list[str] | None = None,
        include_general: bool = False,
        include_candidates: bool = False,
        include_curated: bool = True,
        limit: int = 5,
        max_chars: int = 1200,
    ) -> dict[str, object]:
        return port.profile(
            ProfileQueryRequest(
                query=query,
                entity=entity,
                targets=tuple(targets) if targets is not None else None,
                include_general=include_general,
                include_candidates=include_candidates,
                include_curated=include_curated,
                limit=limit,
                max_chars=max_chars,
            )
        )

    def inspect(
        self, port: MemoryQueryApplication, *, memory_id: str
    ) -> dict[str, object]:
        return port.inspect(InspectMemoryRequest(memory_id))

    def explain(
        self, port: MemoryQueryApplication, *, query: str, limit: int = 5
    ) -> dict[str, object]:
        return port.explain(ExplainQueryRequest(query, limit))

    def inspector(
        self,
        port: MemoryQueryApplication,
        *,
        query: str,
        limit: int = 5,
        recall_mode: str = "advisory",
        include_content: bool = False,
        output_format: str = "json",
    ) -> dict[str, object]:
        return port.inspector(
            RecallInspectorRequest(
                query=query,
                limit=limit,
                recall_mode=recall_mode,
                include_content=include_content,
                output_format=output_format,
            )
        )

    def stats(self, port: MemoryQueryApplication) -> dict[str, object]:
        return port.stats()

    def probe(
        self, port: MemoryQueryApplication, *, entity: str, limit: int = 10
    ) -> dict[str, object]:
        return port.probe(EntityQueryRequest(entity, limit))

    def related(
        self, port: MemoryQueryApplication, *, entity: str, limit: int = 12
    ) -> dict[str, object]:
        return port.related(EntityQueryRequest(entity, limit))

    def export(
        self,
        port: MemoryQueryApplication,
        *,
        fmt: str = "jsonl",
        scope_only: bool = True,
    ) -> dict[str, object]:
        return port.export(ExportMemoriesRequest(fmt, scope_only))

    def hygiene(
        self, port: MemoryQueryApplication, *, limit: int = 200
    ) -> dict[str, object]:
        return port.hygiene(HygieneQueryRequest(limit))

    def benchmark(
        self,
        port: MemoryQueryApplication,
        *,
        queries: list[str] | None = None,
        cases: list[dict[str, Any]] | None = None,
        limit: int = 5,
        auto_explain_on_fail: bool = False,
        include_trace: bool = False,
        prompt_budget_chars: int = 0,
    ) -> dict[str, object]:
        typed_cases = (
            tuple(cast(dict[str, object], dict(case)) for case in cases)
            if cases is not None
            else None
        )
        return port.benchmark(
            BenchmarkQueriesRequest(
                queries=tuple(queries) if queries is not None else None,
                cases=typed_cases,
                limit=limit,
                auto_explain_on_fail=auto_explain_on_fail,
                include_trace=include_trace,
                prompt_budget_chars=prompt_budget_chars,
            )
        )


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

    def review_candidate(self, request: ReviewMemoryCandidateRequest) -> dict[str, object]:
        from .command_adapter import ProviderCommandAdapter

        return ProviderCommandAdapter(self._host).review_candidate(request)

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

    def delete(self, request: DeleteMemoriesRequest) -> DeleteMemoriesResult:
        fn = getattr(self._host, "_delete_memories", None)
        if not callable(fn):
            raise AttributeError("delete_memories")
        raw_count = fn(list(request.ids))
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            raise RuntimeError("legacy delete result must be an integer count")
        legacy_count = raw_count
        requested_ids = tuple(dict.fromkeys(request.ids))
        if legacy_count == len(requested_ids):
            deleted_ids = requested_ids
            skipped_ids: tuple[str, ...] = ()
        elif legacy_count == 0:
            deleted_ids = ()
            skipped_ids = requested_ids
        else:
            raise RuntimeError(
                "legacy partial delete result cannot identify actual deleted ids"
            )
        return DeleteMemoriesResult(
            requested_ids=requested_ids,
            deleted_ids=deleted_ids,
            skipped_ids=skipped_ids,
            deleted_count=legacy_count,
            vector_pending=False,
            companion_erasure_pending=False,
            data_retained=bool(skipped_ids),
            mutation_applied=legacy_count > 0,
        )

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

    def purge(self, request: PrivacyPurgeRequest) -> dict[str, object]:
        fn = getattr(self._host, "_privacy_purge", None)
        if callable(fn):
            return cast(
                dict[str, object],
                fn(
                    action=request.action,
                    ids=list(request.ids),
                    operation_id=request.operation_id,
                    confirmation=request.confirmation,
                ),
            )
        from ...privacy_purge import run_privacy_purge

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


def bind_memory_command_port(obj: Any) -> MemoryCommandPort:
    """Wrap isolated-host persist hooks. Production uses composition.command_port."""

    if isinstance(obj, _LegacyPersistCommandPort):
        return obj
    return _LegacyPersistCommandPort(obj)


class CommandKernel:
    """Build typed requests and invoke provider-neutral application commands."""

    def review_candidate(
        self, port: MemoryCommandPort, *, memory_id: str, action: str,
        dry_run: bool = True, expected_updated_at: str = "", expected_lifecycle: str = "",
    ) -> dict[str, object]:
        return port.review_candidate(ReviewMemoryCandidateRequest(
            memory_id, action, dry_run, expected_updated_at, expected_lifecycle,
        ))

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

    def delete(self, port: MemoryCommandPort, ids: list[str]) -> DeleteMemoriesResult:
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

    def purge(
        self,
        port: MemoryCommandPort,
        *,
        action: str,
        ids: list[str] | tuple[str, ...] = (),
        operation_id: str = "",
        confirmation: str = "",
    ) -> dict[str, object]:
        return port.purge(
            PrivacyPurgeRequest(action, tuple(ids), operation_id, confirmation)
        )


class RuntimeKernel(QueryKernel, CommandKernel):
    """Compatibility facade used by existing KERNEL imports."""


KERNEL = RuntimeKernel()
COMMAND_KERNEL = KERNEL
