"""Shared lifecycle visibility policy for Scope Recall.

Ordinary recall is intentionally stricter than operator/profile review surfaces:
provisional and durable-target scratch rows must not become normal model
context. Chat-local ``general`` scratch remains visible only through the
ordinary same-scope policy, while review surfaces may expose candidates.

Python and SQL gates share one strip/case contract. Historical rows are not
rewritten; absent and unknown lifecycle tokens stay visible.
"""

from __future__ import annotations

# Characters removed by ``str.strip()`` on this interpreter family. Keep the
# SQL TRIM set identical so durable and ordinary predicates stay equivalent.
LIFECYCLE_STRIP_CHARACTERS = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0"
    "\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)

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


def normalize_lifecycle_token(raw: object) -> str:
    """Normalize one lifecycle or target token with Python ``str.strip()`` rules."""

    return str(raw or "").strip().lower()


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _normalized_sql_token(expr: str) -> str:
    """SQL equivalent of ``normalize_lifecycle_token`` for one expression."""

    return f"LOWER(TRIM(COALESCE({expr}, ''), {_sql_quote(LIFECYCLE_STRIP_CHARACTERS)}))"


def _metadata_lifecycle_expr(alias: str) -> str:
    return (
        f"CASE WHEN json_valid({alias}.metadata) "
        f"THEN json_extract({alias}.metadata, '$.lifecycle') END"
    )


def ordinary_recall_lifecycle_visible(*, lifecycle: str, target: str) -> bool:
    """Apply the ordinary recall lifecycle rule to one decoded row."""

    normalized_lifecycle = normalize_lifecycle_token(lifecycle)
    normalized_target = normalize_lifecycle_token(target)
    return normalized_lifecycle not in ORDINARY_RECALL_HIDDEN_LIFECYCLES or (
        normalized_target == "general" and normalized_lifecycle == "scratch"
    )


def durable_lifecycle_visible(*, lifecycle: str) -> bool:
    """Apply the durable-surface lifecycle rule to one decoded token."""

    return normalize_lifecycle_token(lifecycle) not in DURABLE_HIDDEN_LIFECYCLES


def durable_lifecycle_visible_sql(alias: str) -> str:
    """Return SQL for rows not in durable terminal lifecycle states."""

    lifecycle_expr = _normalized_sql_token(_metadata_lifecycle_expr(alias))
    hidden_values = ",".join(f"'{value}'" for value in sorted(DURABLE_HIDDEN_LIFECYCLES))
    return f"{lifecycle_expr} NOT IN ({hidden_values})"


def ordinary_recall_lifecycle_visible_sql(alias: str) -> str:
    """Return the shared SQL predicate for ordinary recall-visible rows."""

    lifecycle_expr = _normalized_sql_token(_metadata_lifecycle_expr(alias))
    target_expr = _normalized_sql_token(f"{alias}.target")
    hidden_values = ",".join(f"'{value}'" for value in ORDINARY_RECALL_HIDDEN_LIFECYCLE_VALUES)
    return (
        f"({lifecycle_expr} NOT IN ({hidden_values}) "
        f"OR ({target_expr} = 'general' AND {lifecycle_expr} = 'scratch'))"
    )
