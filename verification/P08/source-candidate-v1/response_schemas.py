"""Public response-schema version constants for operator-facing JSON reports.

Update these identifiers only when top-level response meaning or required fields change."""

from __future__ import annotations

from .vector_status import VECTOR_STATUS_SCHEMA_VERSION

"""Stable public JSON response schema-version identifiers.

These constants version the top-level shape of operator-facing reports. They are
not full JSON Schema documents; they are lightweight contract names that let
callers branch safely when report fields evolve.
"""

DOCTOR_RESPONSE_SCHEMA_VERSION = "doctor_report.v1"
DOCTOR_REQUIRED_CHECK_NAMES = (
    "config_load",
    "endpoint_policy",
    "event_digest",
    "experience_kernel",
    "extensions",
    "journal_provenance",
    "memory_candidate_debt",
    "memory_quality_lint",
    "memory_secret_scan",
    "nightly_digest",
    "runtime_pipelines",
    "source_metadata",
    "sqlite_truth",
    "temporal_evolution",
    "vector_companion",
)

# Managed upgrades must not make ordinary users adjudicate historical memory
# quality before new code can start.  This registry is the explicit, reviewed
# activation contract: safety failures block and roll back; advisory failures
# are preserved as maintenance debt without mutating semantic memory rows.
# Keep the two sets a complete, disjoint partition of
# ``DOCTOR_REQUIRED_CHECK_NAMES`` so callers never infer severity from names or
# human-readable Doctor text.
DOCTOR_ACTIVATION_SAFETY_CHECK_NAMES = (
    "config_load",
    "endpoint_policy",
    "extensions",
    "runtime_pipelines",
    "source_metadata",
    "sqlite_truth",
    "temporal_evolution",
)
DOCTOR_ACTIVATION_ADVISORY_CHECK_NAMES = (
    "event_digest",
    "experience_kernel",
    "journal_provenance",
    "memory_candidate_debt",
    "memory_quality_lint",
    "memory_secret_scan",
    "nightly_digest",
    "vector_companion",
)

if (
    set(DOCTOR_ACTIVATION_SAFETY_CHECK_NAMES)
    | set(DOCTOR_ACTIVATION_ADVISORY_CHECK_NAMES)
) != set(DOCTOR_REQUIRED_CHECK_NAMES):
    raise RuntimeError("Doctor activation policy does not cover every required check")
if set(DOCTOR_ACTIVATION_SAFETY_CHECK_NAMES) & set(
    DOCTOR_ACTIVATION_ADVISORY_CHECK_NAMES
):
    raise RuntimeError("Doctor activation safety and advisory checks overlap")
DASHBOARD_RESPONSE_SCHEMA_VERSION = "dashboard_report.v1"
GOLDEN_BENCHMARK_RESPONSE_SCHEMA_VERSION = "golden_benchmark_report.v1"
EXPERIENCE_REPLAY_RESPONSE_SCHEMA_VERSION = "experience_replay_report.v1"
FORGETTING_REPORT_SCHEMA_VERSION = "forgetting_report.v1"
FORGETTING_RUN_SCHEMA_VERSION = "forgetting_run.v1"
RETENTION_RESPONSE_SCHEMA_VERSION = "retention_response.v1"
RETENTION_MODES = frozenset({"archive", "hard_delete", "privacy_purge"})

PUBLIC_RESPONSE_SCHEMA_VERSIONS = {
    "doctor": DOCTOR_RESPONSE_SCHEMA_VERSION,
    "dashboard": DASHBOARD_RESPONSE_SCHEMA_VERSION,
    "golden_benchmark": GOLDEN_BENCHMARK_RESPONSE_SCHEMA_VERSION,
    "experience_replay": EXPERIENCE_REPLAY_RESPONSE_SCHEMA_VERSION,
    "forgetting_report": FORGETTING_REPORT_SCHEMA_VERSION,
    "forgetting_run": FORGETTING_RUN_SCHEMA_VERSION,
    "retention": RETENTION_RESPONSE_SCHEMA_VERSION,
    "vector_status": VECTOR_STATUS_SCHEMA_VERSION,
}


def retention_response_contract(
    *,
    mode: str,
    data_retained: bool,
    mutation_applied: bool,
    companion_erasure_pending: bool = False,
) -> dict[str, object]:
    """Return the stable retention and reversibility semantics for a mutation.

    The legacy count/receipt fields remain surface-specific.  These fields let
    callers determine whether authoritative user data remains without guessing
    from those counts or from rebuildable companion-layer progress.
    """

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in RETENTION_MODES:
        raise ValueError(f"unsupported retention mode: {normalized_mode or '<empty>'}")
    return {
        "retention_schema_version": RETENTION_RESPONSE_SCHEMA_VERSION,
        "mode": normalized_mode,
        "data_retained": bool(data_retained),
        "reversible": normalized_mode == "archive",
        "privacy_purge": normalized_mode == "privacy_purge",
        "mutation_applied": bool(mutation_applied),
        "companion_erasure_pending": bool(companion_erasure_pending),
    }


def response_schema_version(surface: str) -> str:
    """Return the stable top-level schema version for a public report surface."""

    return PUBLIC_RESPONSE_SCHEMA_VERSIONS[str(surface)]
