"""Read-only Fact Evolution adoption and coverage observability.

Feature configuration and durable Fact adoption are deliberately separate:
``fact_evolution.enabled`` describes which pipeline is allowed to run, while
the SQLite counts below describe what the instance has actually adopted.
"""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import Any

from .fact_evolution import evolution_policy_mode, fact_evolution_enabled
from .lifecycle_policy import PROFILE_HIDDEN_LIFECYCLES


FACT_OBSERVABILITY_SCHEMA_VERSION = "scope-recall.fact-observability.v1"
_FACT_MEMORY_TYPES = (
    "fact",
    "factual",
    "preference",
    "project",
    "resource",
    "constraint",
)
_ADOPTION_HIDDEN_LIFECYCLES = tuple(
    sorted({*PROFILE_HIDDEN_LIFECYCLES, "in_progress"})
)
_MODE_LANES = ("default", "nightly", "journal", "tool", "maintenance")
_REQUIRED_TABLES = frozenset(
    {"memories", "fact_claims", "fact_claim_evidence", "fact_action_receipts"}
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def fact_observability_config(
    runtime_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return only the non-secret configuration needed by Doctor and Stats."""

    root = _mapping(runtime_config)
    fact = _mapping(root.get("fact_evolution"))
    backfill = _mapping(root.get("fact_backfill"))
    default_mode = str(fact.get("mode") or "preview")
    return {
        "fact_evolution": {
            "enabled": fact.get("enabled") is True,
            "mode": default_mode,
            "nightly_mode": str(fact.get("nightly_mode") or default_mode),
            "journal_mode": str(fact.get("journal_mode") or default_mode),
            "tool_mode": str(fact.get("tool_mode") or default_mode),
            "maintenance_mode": str(
                fact.get("maintenance_mode") or default_mode
            ),
        },
        "fact_backfill": {
            "shadow_enabled": backfill.get("shadow_enabled") is True,
        },
    }


def fact_feature_observability(
    runtime_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Resolve the effective lane modes without reading or writing storage."""

    config = fact_observability_config(runtime_config)
    backfill = _mapping(config.get("fact_backfill"))
    return {
        "feature_enabled": fact_evolution_enabled(config),
        "effective_modes": {
            lane: evolution_policy_mode(config, lane=lane, dry_run=False)
            for lane in _MODE_LANES
        },
        "backfill_shadow_enabled": backfill.get("shadow_enabled") is True,
    }


def unavailable_fact_observability(
    runtime_config: Mapping[str, Any] | None,
    *,
    reason_code: str,
    missing_tables: list[str] | None = None,
) -> dict[str, Any]:
    """Build an honest unavailable payload; missing schema is never zero debt."""

    features = fact_feature_observability(runtime_config)
    return {
        "schema_version": FACT_OBSERVABILITY_SCHEMA_VERSION,
        "available": False,
        "state": "disabled" if not features["feature_enabled"] else "needs_review",
        "reason_codes": [str(reason_code or "unavailable")],
        **features,
        "claim_count": None,
        "current_claim_count": None,
        "projection_count": None,
        "evidence_count": None,
        "fact_owned_memory_count": None,
        "eligible_memory_count": None,
        "claimed_memory_count": None,
        "coverage_ratio": None,
        "review_proposal_count": None,
        "last_apply_at": None,
        "missing_tables": sorted(str(item) for item in (missing_tables or [])),
    }


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _visible_lifecycle_sql(alias: str) -> str:
    placeholders = ",".join("?" for _ in _ADOPTION_HIDDEN_LIFECYCLES)
    expression = (
        "LOWER(COALESCE(CASE WHEN json_valid("
        f"{alias}.metadata) THEN json_extract({alias}.metadata, '$.lifecycle') "
        "ELSE '' END, ''))"
    )
    return f"{expression} NOT IN ({placeholders})"


def _adoption_state(
    *,
    feature_enabled: bool,
    effective_modes: Mapping[str, Any],
    claim_count: int,
    eligible_memory_count: int,
    coverage_ratio: float,
    review_proposal_count: int,
) -> str:
    if not feature_enabled:
        return "disabled"
    active_mode = any(
        str(value or "preview") != "preview" for value in effective_modes.values()
    )
    if not active_mode:
        return (
            "preview_with_proposals"
            if claim_count or review_proposal_count
            else "preview_no_claims"
        )
    if review_proposal_count:
        return "needs_review"
    if claim_count == 0:
        return "active_no_claims"
    if eligible_memory_count and coverage_ratio < 1.0:
        return "active_partial_coverage"
    return "active"


def fact_observability_report(
    conn: sqlite3.Connection,
    runtime_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return content-free Fact adoption counts without mutating SQLite."""

    tables = _table_names(conn)
    missing = sorted(_REQUIRED_TABLES - tables)
    if missing:
        return unavailable_fact_observability(
            runtime_config,
            reason_code="schema_missing",
            missing_tables=missing,
        )

    features = fact_feature_observability(runtime_config)
    placeholders = ",".join("?" for _ in _FACT_MEMORY_TYPES)
    lifecycle_params = tuple(_ADOPTION_HIDDEN_LIFECYCLES)
    visible_sql = _visible_lifecycle_sql("m")
    eligible_memory_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM memories AS m
            WHERE {visible_sql}
              AND LOWER(COALESCE(
                    CASE WHEN json_valid(m.metadata)
                         THEN json_extract(m.metadata, '$.memory_type')
                         ELSE '' END,
                    ''
                  )) IN ({placeholders})
            """,
            (*lifecycle_params, *_FACT_MEMORY_TYPES),
        ).fetchone()[0]
    )
    claimed_memory_count = int(
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.id)
            FROM memories AS m
            JOIN fact_claims AS c ON c.memory_id = m.id
            WHERE c.status = 'current'
              AND (c.retired_at IS NULL OR c.retired_at = '')
              AND {visible_sql}
              AND LOWER(COALESCE(
                    CASE WHEN json_valid(m.metadata)
                         THEN json_extract(m.metadata, '$.memory_type')
                         ELSE '' END,
                    ''
                  )) IN ({placeholders})
            """,
            (*lifecycle_params, *_FACT_MEMORY_TYPES),
        ).fetchone()[0]
    )
    claim_count = int(
        conn.execute("SELECT COUNT(*) FROM fact_claims").fetchone()[0]
    )
    current_claim_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM fact_claims
            WHERE status = 'current'
              AND (retired_at IS NULL OR retired_at = '')
            """
        ).fetchone()[0]
    )
    projection_count = int(
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.id)
            FROM memories AS m
            JOIN fact_claims AS c ON c.memory_id = m.id
            WHERE c.status = 'current'
              AND (c.retired_at IS NULL OR c.retired_at = '')
              AND {visible_sql}
            """,
            lifecycle_params,
        ).fetchone()[0]
    )
    fact_owned_memory_count = int(
        conn.execute(
            "SELECT COUNT(DISTINCT memory_id) FROM fact_claims"
        ).fetchone()[0]
    )
    evidence_count = int(
        conn.execute("SELECT COUNT(*) FROM fact_claim_evidence").fetchone()[0]
    )
    receipt_review_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM fact_action_receipts
            WHERE applied = 0
              AND (status = 'review' OR effective_action = 'review')
            """
        ).fetchone()[0]
    )
    candidate_review_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM memories
            WHERE LOWER(COALESCE(
                    CASE WHEN json_valid(metadata)
                         THEN json_extract(metadata, '$.lifecycle')
                         ELSE '' END,
                    ''
                  )) = 'candidate'
              AND json_valid(metadata)
              AND (
                    CASE WHEN json_valid(metadata)
                         THEN json_type(metadata, '$.fact_evolution')
                         ELSE NULL END = 'object'
                 OR CASE WHEN json_valid(metadata)
                         THEN json_type(metadata, '$.evolution')
                         ELSE NULL END = 'object'
              )
            """
        ).fetchone()[0]
    )
    last_apply_row = conn.execute(
        """
        SELECT MAX(updated_at)
        FROM fact_action_receipts
        WHERE applied = 1 AND status IN ('applied', 'replayed')
        """
    ).fetchone()
    last_apply_at = str(last_apply_row[0] or "") if last_apply_row else ""
    coverage_ratio = (
        1.0
        if eligible_memory_count == 0
        else claimed_memory_count / eligible_memory_count
    )
    review_proposal_count = receipt_review_count + candidate_review_count
    state = _adoption_state(
        feature_enabled=bool(features["feature_enabled"]),
        effective_modes=_mapping(features["effective_modes"]),
        claim_count=current_claim_count,
        eligible_memory_count=eligible_memory_count,
        coverage_ratio=coverage_ratio,
        review_proposal_count=review_proposal_count,
    )
    return {
        "schema_version": FACT_OBSERVABILITY_SCHEMA_VERSION,
        "available": True,
        "state": state,
        "reason_codes": [],
        **features,
        "claim_count": claim_count,
        "current_claim_count": current_claim_count,
        "projection_count": projection_count,
        "evidence_count": evidence_count,
        "fact_owned_memory_count": fact_owned_memory_count,
        "eligible_memory_count": eligible_memory_count,
        "claimed_memory_count": claimed_memory_count,
        "coverage_ratio": coverage_ratio,
        "review_proposal_count": review_proposal_count,
        "last_apply_at": last_apply_at,
        "missing_tables": [],
    }


__all__ = [
    "FACT_OBSERVABILITY_SCHEMA_VERSION",
    "fact_feature_observability",
    "fact_observability_config",
    "fact_observability_report",
    "unavailable_fact_observability",
]
