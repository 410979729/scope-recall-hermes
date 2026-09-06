"""Experience preflight and promotion runtime.

Storage keeps bootstrap/connection only. Prefetch must call this owner
with ``record_run=False`` so a reader path cannot write experience_runs.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ...recall_sqlite_budget import using_request_busy_timeout
from ...sqlite_recovery import is_sqlite_lock_contention
from ..recall.deadline import acquire_until, current_request_deadline
from ..recall.sources import SourceUnavailable


def backfill_skill_anchors(*args: Any, **kwargs: Any) -> Any:
    """Load the Playbook migration only after Experience is enabled."""

    from ...experience_store import backfill_skill_anchors as implementation

    return implementation(*args, **kwargs)


def run_experience_preflight(provider: Any, *, query: str) -> dict[str, Any]:
    from ...experience_preflight import experience_preflight

    deadline = current_request_deadline()
    lock = getattr(provider, "_lock", None)
    held = False
    if lock is not None:
        if not acquire_until(lock, deadline):
            raise SourceUnavailable("experience", "provider_lock_timeout")
        held = True
    try:
        conn = provider._require_conn()
        try:
            with using_request_busy_timeout(conn):
                return experience_preflight(
                    conn,
                    query=query,
                    accessible_scope_ids=provider._accessible_scope_ids,
                    config=provider._config,
                    record_run=False,
                    scope_id=provider._scope_id,
                )
        except sqlite3.Error as exc:
            if is_sqlite_lock_contention(exc):
                raise SourceUnavailable("experience", "sqlite_lock_timeout") from exc
            raise
    finally:
        if held and lock is not None:
            lock.release()


__all__ = [
    "backfill_skill_anchors",
    "run_experience_preflight",
    "run_experience_promotion",
]


def run_experience_promotion(
    provider: Any,
    *,
    limit_sessions: int,
    promote_fn: Any | None = None,
) -> dict[str, Any]:
    from ...experience_promotion import promote_experiences as default_promote

    promote = promote_fn or default_promote
    with provider._lock:
        return promote(
            provider._require_conn(),
            accessible_scope_ids=provider._accessible_scope_ids,
            scope_id=provider._scope_id,
            shared_scope_id=provider._shared_scope_id,
            config=provider._config,
            limit_sessions=max(1, limit_sessions),
            dry_run=False,
        )
