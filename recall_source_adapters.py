"""Outer-host adapters that bind Provider/search hooks to typed source collectors."""

from __future__ import annotations

import sqlite3
import time
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

from .sqlite_recovery import is_sqlite_lock_contention
from .temporal_query import TemporalQueryError
from ._internal.recall.deadline import acquire_until
from ._internal.recall.sources import (
    CompanionUnavailable,
    RecallSourceContext,
    SourceCapabilities,
    SourceResult,
    SourceUnavailable,
)


def bind_source_capabilities(host: Any) -> SourceCapabilities:
    """Close over the live search host. The collection closure never sees this."""

    return SourceCapabilities(
        lexical=lambda context: _collect_lexical(host, context),
        vector=lambda context: _collect_vector(host, context),
        curated=lambda context: _collect_curated(host, context),
        temporal=lambda context, memory_ids: _collect_temporal(
            host, context, memory_ids
        ),
    )


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000.0, 3)


def _acquire_host_lock(host: Any, context: RecallSourceContext, *, source: str) -> bool:
    lock = getattr(getattr(host, "provider", None), "_lock", None)
    if lock is None:
        return False
    if not acquire_until(lock, context.deadline):
        raise SourceUnavailable(source, "provider_lock_timeout")
    return True


def _release_host_lock(host: Any, held: bool) -> None:
    if not held:
        return
    lock = getattr(getattr(host, "provider", None), "_lock", None)
    release = getattr(lock, "release", None)
    if callable(release):
        release()


def _collect_lexical(host: Any, context: RecallSourceContext) -> SourceResult:
    started_at = time.perf_counter()
    held = _acquire_host_lock(host, context, source="lexical")
    try:
        items = host.provider._search_db_memories(
            context.query, limit=context.candidate_pool
        )
    except sqlite3.Error as exc:
        if is_sqlite_lock_contention(exc):
            return SourceResult.unavailable(
                "lexical",
                "sqlite_lock_timeout",
                elapsed_ms=_elapsed_ms(started_at),
            )
        raise
    finally:
        _release_host_lock(host, held)
    return SourceResult.ok(
        "lexical",
        list(items or []),
        elapsed_ms=_elapsed_ms(started_at),
    )


def _collect_vector(host: Any, context: RecallSourceContext) -> SourceResult:
    started_at = time.perf_counter()
    provider = host.provider
    if hasattr(provider, "_vector_query_last_error"):
        provider._vector_query_last_error = ""
    try:
        if context.query_vector is None:
            items = provider._search_vector_memories(
                context.query, limit=context.vector_depth
            )
        else:
            items = provider._search_vector_memories_with_vector(
                list(context.query_vector),
                limit=context.vector_depth,
            )
    except CompanionUnavailable as exc:
        return SourceResult.unavailable(
            "vector",
            exc.reason_code,
            elapsed_ms=_elapsed_ms(started_at),
        )
    except TimeoutError:
        return SourceResult.unavailable(
            "vector",
            "query_embedding_timeout",
            elapsed_ms=_elapsed_ms(started_at),
        )
    last_error = str(getattr(provider, "_vector_query_last_error", "") or "")
    if last_error:
        reason = (
            "query_embedding_timeout"
            if "timeout" in last_error.casefold()
            else "vector_source_unavailable"
        )
        return SourceResult.unavailable(
            "vector",
            reason,
            elapsed_ms=_elapsed_ms(started_at),
        )
    return SourceResult.ok(
        "vector",
        list(items or []),
        elapsed_ms=_elapsed_ms(started_at),
    )


def _collect_curated(host: Any, context: RecallSourceContext) -> SourceResult:
    started_at = time.perf_counter()
    items = host.provider._search_curated_memories(context.query)
    return SourceResult.ok(
        "curated",
        list(items or []),
        elapsed_ms=_elapsed_ms(started_at),
    )


def _temporal_reason(exc: BaseException) -> str:
    if isinstance(exc, ZoneInfoNotFoundError):
        return "temporal_timezone_unavailable"
    if isinstance(exc, TemporalQueryError) and "timezone" in str(exc).casefold():
        return "temporal_timezone_unavailable"
    return "temporal_query_unusable"


def _collect_temporal(
    host: Any,
    context: RecallSourceContext,
    memory_ids: tuple[str, ...],
) -> SourceResult:
    started_at = time.perf_counter()
    # The bound host capability owns eligibility. Re-reading temporal_queries
    # here would skip subclass overrides that supply evidence when provider
    # config is absent. None still means skipped.
    held = False
    try:
        held = _acquire_host_lock(host, context, source="temporal")
        payload = host._temporal_current_candidates(
            context.query,
            limit=context.candidate_pool,
            candidate_memory_ids=list(memory_ids),
        )
    except SourceUnavailable:
        raise
    except (TemporalQueryError, ZoneInfoNotFoundError) as exc:
        return SourceResult.unavailable(
            "temporal",
            _temporal_reason(exc),
            elapsed_ms=_elapsed_ms(started_at),
            extra={"current_claims_usable": False},
        )
    except sqlite3.Error as exc:
        if is_sqlite_lock_contention(exc):
            return SourceResult.unavailable(
                "temporal",
                "temporal_sqlite_lock_timeout",
                elapsed_ms=_elapsed_ms(started_at),
                extra={"current_claims_usable": False},
            )
        raise
    finally:
        _release_host_lock(host, held)
    if payload is None:
        return SourceResult.skipped(
            "temporal",
            "temporal_disabled",
            elapsed_ms=_elapsed_ms(started_at),
        )
    candidates, suppressed = payload
    return SourceResult.ok(
        "temporal",
        list(candidates or []),
        elapsed_ms=_elapsed_ms(started_at),
        extra={"suppressed_memory_ids": frozenset(suppressed or ())},
    )
