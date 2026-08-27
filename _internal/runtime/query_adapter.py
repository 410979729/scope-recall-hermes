"""Infrastructure adapter for provider-neutral memory query use cases."""

from __future__ import annotations

from typing import Any, Mapping, cast

from ..application.memory_queries import (
    BenchmarkQueriesRequest,
    ContextQueryRequest,
    EntityQueryRequest,
    ExplainQueryRequest,
    ExportMemoriesRequest,
    HygieneQueryRequest,
    InspectMemoryRequest,
    MemoryQueryGateway,
    ProfileQueryRequest,
    RecallInspectorRequest,
)
from ..application.runtime_state import (
    AuthoritySnapshot,
    RuntimeStateSnapshot,
    ScopeSnapshot,
    VectorSnapshot,
)
from .ports import RuntimeAdapterPort
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
from ...recall_inspector import inspect_recall


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


class ProviderQueryAdapter:
    """Keep legacy query/runtime details outside the application layer."""

    def __init__(self, adapter: RuntimeAdapterPort) -> None:
        self._adapter = adapter

    def runtime_state(self) -> RuntimeStateSnapshot:
        scope = _mapping(self._adapter.query_scope_view())
        runtime = _mapping(self._adapter.runtime_status_view())
        vector = _mapping(self._adapter.vector_status_view())
        writer_role = str(runtime.get("truth_writer_role") or "unknown")
        status = str(runtime.get("status") or "uninitialized")
        return RuntimeStateSnapshot(
            status=status,
            scope=ScopeSnapshot(
                scope_id=str(scope.get("scope_id") or ""),
                shared_scope_id=str(scope.get("shared_scope_id") or ""),
                shared_pool_scope_id=str(scope.get("shared_pool_scope_id") or ""),
                accessible_scope_ids=tuple(
                    str(item) for item in (scope.get("accessible_scope_ids") or ())
                ),
                writable_scope_ids=tuple(
                    str(item) for item in (scope.get("writable_scope_ids") or ())
                ),
                shared_pool_enabled=bool(runtime.get("shared_pool_enabled")),
                shared_pool_write_enabled=bool(
                    runtime.get("shared_pool_write_enabled")
                ),
            ),
            authority=AuthoritySnapshot(
                writer_role=writer_role,
                writer_authorized=writer_role == "owner",
                read_only=status == "active_read_only" or writer_role == "reader",
            ),
            vector=VectorSnapshot(
                state=str(vector.get("state") or vector.get("status") or "disabled"),
                reason_code=str(vector.get("reason_code") or "unknown"),
                enabled=bool(vector.get("enabled")),
                ready=bool(vector.get("ready")),
                usable_for_query=bool(vector.get("usable_for_query")),
                repair_required=bool(vector.get("repair_required")),
            ),
        )

    def context(self, request: ContextQueryRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            context_payload(
                self._adapter,
                query=request.query,
                limit=request.limit,
                max_chars=request.max_chars,
            ),
        )

    def profile(self, request: ProfileQueryRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            profile_payload(
                self._adapter,
                query=request.query,
                entity=request.entity,
                targets=list(request.targets) if request.targets is not None else None,
                include_general=request.include_general,
                include_candidates=request.include_candidates,
                include_curated=request.include_curated,
                limit=request.limit,
                max_chars=request.max_chars,
            ),
        )

    def probe(self, request: EntityQueryRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            probe_entity(self._adapter, entity=request.entity, limit=request.limit),
        )

    def related(self, request: EntityQueryRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            related_entities(self._adapter, entity=request.entity, limit=request.limit),
        )

    def inspect(self, request: InspectMemoryRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            inspect_memory(self._adapter, memory_id=request.memory_id),
        )

    def explain(self, request: ExplainQueryRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            explain_query(self._adapter, query=request.query, limit=request.limit),
        )

    def inspector(self, request: RecallInspectorRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            inspect_recall(
                self._adapter,
                query=request.query,
                limit=request.limit,
                recall_mode=request.recall_mode,
                include_content=request.include_content,
                output_format=request.output_format,
            ),
        )

    def export(self, request: ExportMemoriesRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            export_memories(
                self._adapter, fmt=request.fmt, scope_only=request.scope_only
            ),
        )

    def hygiene(self, request: HygieneQueryRequest) -> dict[str, object]:
        return cast(
            dict[str, object],
            hygiene_report(self._adapter, limit=request.limit),
        )

    def benchmark(self, request: BenchmarkQueriesRequest) -> dict[str, object]:
        cases = (
            [cast(dict[str, Any], dict(case)) for case in request.cases]
            if request.cases is not None
            else None
        )
        return cast(
            dict[str, object],
            benchmark_queries(
                self._adapter,
                queries=list(request.queries) if request.queries is not None else None,
                cases=cases,
                limit=request.limit,
                auto_explain_on_fail=request.auto_explain_on_fail,
                include_trace=request.include_trace,
                prompt_budget_chars=request.prompt_budget_chars,
            ),
        )

    def stats(self) -> dict[str, object]:
        return cast(dict[str, object], stats_payload(self._adapter))


def bind_provider_query_adapter(adapter: RuntimeAdapterPort) -> MemoryQueryGateway:
    return ProviderQueryAdapter(adapter)
