"""Typed source-collection closure for one recall request.

The closure receives an immutable context and capability callbacks. It must
not read a Provider object or Provider private attributes; adapters bind
those at the outer host boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable, Mapping

from ...models import RecallItem
from .deadline import RequestDeadline

AUTHORITY_BLOCKING_REASONS = frozenset(
    {
        "provider_lock_timeout",
        "request_deadline_exhausted",
        "sqlite_lock_timeout",
    }
)


class SourceUnavailable(Exception):
    """A recall source could not be read within this request's contract."""

    def __init__(self, source: str, reason_code: str) -> None:
        self.source = source
        self.reason_code = reason_code
        super().__init__(f"{source} unavailable: {reason_code}")


class CompanionUnavailable(SourceUnavailable):
    """Recoverable companion failure. Authority/scope/privacy errors must not use this."""


@dataclass(frozen=True)
class RecallSourceContext:
    """Immutable per-request values needed to collect recall sources."""

    query: str
    candidate_pool: int
    vector_depth: int
    query_vector: tuple[float, ...] | None
    deadline: RequestDeadline


@dataclass(frozen=True)
class SourceResult:
    """One source's typed collection outcome."""

    name: str
    items: tuple[RecallItem, ...]
    raw_count: int
    state: str
    reason_code: str
    elapsed_ms: float
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        name: str,
        items: list[RecallItem] | tuple[RecallItem, ...],
        *,
        raw_count: int | None = None,
        elapsed_ms: float = 0.0,
        extra: Mapping[str, Any] | None = None,
    ) -> SourceResult:
        materialized = tuple(items)
        return cls(
            name=name,
            items=materialized,
            raw_count=len(materialized) if raw_count is None else int(raw_count),
            state="ok",
            reason_code="",
            elapsed_ms=float(elapsed_ms),
            extra=dict(extra or {}),
        )

    @classmethod
    def unavailable(
        cls,
        name: str,
        reason_code: str,
        *,
        elapsed_ms: float = 0.0,
        extra: Mapping[str, Any] | None = None,
    ) -> SourceResult:
        return cls(
            name=name,
            items=(),
            raw_count=0,
            state="unavailable",
            reason_code=str(reason_code),
            elapsed_ms=float(elapsed_ms),
            extra=dict(extra or {}),
        )

    @classmethod
    def skipped(
        cls,
        name: str,
        reason_code: str,
        *,
        elapsed_ms: float = 0.0,
    ) -> SourceResult:
        return cls(
            name=name,
            items=(),
            raw_count=0,
            state="skipped",
            reason_code=str(reason_code),
            elapsed_ms=float(elapsed_ms),
            extra={},
        )


@dataclass(frozen=True)
class SourceCapabilities:
    """Host-bound collectors. Implementations may close over Provider state."""

    lexical: Callable[[RecallSourceContext], SourceResult]
    vector: Callable[[RecallSourceContext], SourceResult]
    curated: Callable[[RecallSourceContext], SourceResult]
    temporal: Callable[[RecallSourceContext, tuple[str, ...]], SourceResult]


@dataclass(frozen=True)
class CollectedSources:
    lexical: SourceResult
    vector: SourceResult
    curated: SourceResult
    temporal: SourceResult

    def unavailable_entries(self) -> list[dict[str, str]]:
        return [
            {"source": result.name, "reason_code": result.reason_code}
            for result in (self.lexical, self.vector, self.curated, self.temporal)
            if result.state == "unavailable"
        ]


def collect_sources(
    context: RecallSourceContext,
    capabilities: SourceCapabilities,
) -> CollectedSources:
    """Collect lexical, vector, curated, and temporal sources under one deadline.

    Recoverable companion failures become ``unavailable`` results. Authority,
    scope, privacy, purge, and integrity exceptions propagate. When the SQLite
    lexical read cannot be authenticated, vector/temporal are skipped so
    unverifiable companion rows cannot stand in for truth.
    """

    lexical = _invoke(context, capabilities.lexical, "lexical")
    if (
        lexical.state == "unavailable"
        and lexical.reason_code in AUTHORITY_BLOCKING_REASONS
    ):
        return CollectedSources(
            lexical=lexical,
            vector=SourceResult.skipped("vector", "authority_read_unavailable"),
            curated=_invoke(context, capabilities.curated, "curated"),
            temporal=SourceResult.skipped("temporal", "authority_read_unavailable"),
        )
    vector = _invoke(context, capabilities.vector, "vector")
    curated = _invoke(context, capabilities.curated, "curated")
    memory_ids = tuple(
        dict.fromkeys(
            item.id
            for item in (*lexical.items, *vector.items, *curated.items)
            if item.id
        )
    )
    if context.deadline.exhausted():
        temporal = SourceResult.unavailable("temporal", "request_deadline_exhausted")
    else:
        started_at = time.perf_counter()
        try:
            temporal = capabilities.temporal(context, memory_ids)
        except CompanionUnavailable as exc:
            temporal = SourceResult.unavailable(
                exc.source or "temporal",
                exc.reason_code,
                elapsed_ms=_elapsed_ms(started_at),
            )
        except SourceUnavailable as exc:
            temporal = SourceResult.unavailable(
                exc.source or "temporal",
                exc.reason_code,
                elapsed_ms=_elapsed_ms(started_at),
            )
    return CollectedSources(
        lexical=lexical,
        vector=vector,
        curated=curated,
        temporal=temporal,
    )


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000.0, 3)


def _invoke(
    context: RecallSourceContext,
    collector: Callable[[RecallSourceContext], SourceResult],
    name: str,
) -> SourceResult:
    if context.deadline.exhausted():
        return SourceResult.unavailable(name, "request_deadline_exhausted")
    started_at = time.perf_counter()
    try:
        return collector(context)
    except CompanionUnavailable as exc:
        return SourceResult.unavailable(
            exc.source or name,
            exc.reason_code,
            elapsed_ms=_elapsed_ms(started_at),
        )
    except SourceUnavailable as exc:
        return SourceResult.unavailable(
            exc.source or name,
            exc.reason_code,
            elapsed_ms=_elapsed_ms(started_at),
        )
