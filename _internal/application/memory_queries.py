"""Provider-neutral read-only memory query use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .runtime_state import RuntimeStateSnapshot


@dataclass(frozen=True, slots=True)
class ContextQueryRequest:
    query: str
    limit: int = 5
    max_chars: int = 900


@dataclass(frozen=True, slots=True)
class ProfileQueryRequest:
    query: str = ""
    entity: str = ""
    targets: tuple[str, ...] | None = None
    include_general: bool = False
    include_candidates: bool = False
    include_curated: bool = True
    limit: int = 5
    max_chars: int = 1200


@dataclass(frozen=True, slots=True)
class EntityQueryRequest:
    entity: str
    limit: int


@dataclass(frozen=True, slots=True)
class InspectMemoryRequest:
    memory_id: str


@dataclass(frozen=True, slots=True)
class ExplainQueryRequest:
    query: str
    limit: int = 5


@dataclass(frozen=True, slots=True)
class ExportMemoriesRequest:
    fmt: str = "jsonl"
    scope_only: bool = True


@dataclass(frozen=True, slots=True)
class HygieneQueryRequest:
    limit: int = 200


@dataclass(frozen=True, slots=True)
class BenchmarkQueriesRequest:
    queries: tuple[str, ...] | None = None
    cases: tuple[dict[str, object], ...] | None = None
    limit: int = 5
    auto_explain_on_fail: bool = False
    include_trace: bool = False
    prompt_budget_chars: int = 0


@runtime_checkable
class MemoryQueryGateway(Protocol):
    def runtime_state(self) -> RuntimeStateSnapshot: ...

    def context(self, request: ContextQueryRequest) -> dict[str, object]: ...

    def profile(self, request: ProfileQueryRequest) -> dict[str, object]: ...

    def probe(self, request: EntityQueryRequest) -> dict[str, object]: ...

    def related(self, request: EntityQueryRequest) -> dict[str, object]: ...

    def inspect(self, request: InspectMemoryRequest) -> dict[str, object]: ...

    def explain(self, request: ExplainQueryRequest) -> dict[str, object]: ...

    def export(self, request: ExportMemoriesRequest) -> dict[str, object]: ...

    def hygiene(self, request: HygieneQueryRequest) -> dict[str, object]: ...

    def benchmark(self, request: BenchmarkQueriesRequest) -> dict[str, object]: ...

    def stats(self) -> dict[str, object]: ...


class MemoryQueryApplication:
    """Typed read-only use cases; infrastructure is supplied by one gateway."""

    def __init__(self, gateway: MemoryQueryGateway) -> None:
        self._gateway = gateway

    def runtime_state(self) -> RuntimeStateSnapshot:
        return self._gateway.runtime_state()

    def context(self, request: ContextQueryRequest) -> dict[str, object]:
        return self._gateway.context(request)

    def profile(self, request: ProfileQueryRequest) -> dict[str, object]:
        return self._gateway.profile(request)

    def probe(self, request: EntityQueryRequest) -> dict[str, object]:
        return self._gateway.probe(request)

    def related(self, request: EntityQueryRequest) -> dict[str, object]:
        return self._gateway.related(request)

    def inspect(self, request: InspectMemoryRequest) -> dict[str, object]:
        return self._gateway.inspect(request)

    def explain(self, request: ExplainQueryRequest) -> dict[str, object]:
        return self._gateway.explain(request)

    def export(self, request: ExportMemoriesRequest) -> dict[str, object]:
        return self._gateway.export(request)

    def hygiene(self, request: HygieneQueryRequest) -> dict[str, object]:
        return self._gateway.hygiene(request)

    def benchmark(self, request: BenchmarkQueriesRequest) -> dict[str, object]:
        return self._gateway.benchmark(request)

    def stats(self) -> dict[str, object]:
        return self._gateway.stats()

