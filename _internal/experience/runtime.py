"""Experience preflight and promotion runtime.

Storage keeps bootstrap/connection only. Prefetch must call this owner
with ``record_run=False`` so a reader path cannot write experience_runs.
"""

from __future__ import annotations

from typing import Any


def backfill_skill_anchors(*args: Any, **kwargs: Any) -> Any:
    """Load the Playbook migration only after Experience is enabled."""

    from ...experience_store import backfill_skill_anchors as implementation

    return implementation(*args, **kwargs)


def run_experience_preflight(provider: Any, *, query: str) -> dict[str, Any]:
    from ...experience_preflight import experience_preflight

    with provider._lock:
        return experience_preflight(
            provider._require_conn(),
            query=query,
            accessible_scope_ids=provider._accessible_scope_ids,
            config=provider._config,
            record_run=False,
            scope_id=provider._scope_id,
        )


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
