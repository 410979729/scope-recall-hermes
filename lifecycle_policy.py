"""Shared lifecycle visibility policy for Scope Recall.

Ordinary recall is intentionally stricter than operator/profile review surfaces:
provisional and durable-target scratch rows must not become normal model
context. Chat-local ``general`` scratch remains visible only through the
ordinary same-scope policy, while review surfaces may expose candidates.
"""

from __future__ import annotations

# Durable terminal states hidden from every non-audit memory surface.
DURABLE_HIDDEN_LIFECYCLES = frozenset(
    {"archived", "obsolete", "rejected", "superseded"}
)

# Profile/quality views default to promoted memory, but their explicit
# ``include_candidates`` path may opt selected provisional states back in.
PROFILE_HIDDEN_LIFECYCLES = frozenset(
    {*DURABLE_HIDDEN_LIFECYCLES, "candidate", "scratch"}
)

# Ordinary retrieval has no opt-in provisional mode. The recall layer makes the
# narrow exception for same-scope ``general`` scratch after applying this set.
ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES = tuple(
    sorted({*PROFILE_HIDDEN_LIFECYCLES, "in_progress"})
)
ORDINARY_RECALL_HIDDEN_LIFECYCLES = frozenset(
    ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES
)


def ordinary_recall_lifecycle_visible(*, lifecycle: str, target: str) -> bool:
    """Apply the ordinary recall lifecycle rule to one decoded row."""

    normalized_lifecycle = str(lifecycle or "").strip().lower()
    normalized_target = str(target or "").strip().lower()
    return normalized_lifecycle not in ORDINARY_RECALL_HIDDEN_LIFECYCLES or (
        normalized_target == "general" and normalized_lifecycle == "scratch"
    )


def durable_lifecycle_visible_sql(alias: str) -> str:
    """Return SQL for rows not in durable terminal lifecycle states."""

    lifecycle_expr = f"LOWER(COALESCE(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.lifecycle') ELSE '' END, ''))"
    hidden_values = ",".join(f"'{value}'" for value in sorted(DURABLE_HIDDEN_LIFECYCLES))
    return f"{lifecycle_expr} NOT IN ({hidden_values})"


def ordinary_recall_lifecycle_visible_sql(alias: str) -> str:
    """Return the shared SQL predicate for ordinary recall-visible rows."""

    lifecycle_expr = f"LOWER(COALESCE(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.lifecycle') ELSE '' END, ''))"
    hidden_values = ",".join(f"'{value}'" for value in ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES)
    return (
        f"({lifecycle_expr} NOT IN ({hidden_values}) "
        f"OR (LOWER(COALESCE({alias}.target, '')) = 'general' AND {lifecycle_expr} = 'scratch'))"
    )
