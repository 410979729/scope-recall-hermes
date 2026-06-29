from __future__ import annotations

"""Stable public JSON response schema-version identifiers.

These constants version the top-level shape of operator-facing reports. They are
not full JSON Schema documents; they are lightweight contract names that let
callers branch safely when report fields evolve.
"""

DOCTOR_RESPONSE_SCHEMA_VERSION = "doctor_report.v1"
DASHBOARD_RESPONSE_SCHEMA_VERSION = "dashboard_report.v1"
GOLDEN_BENCHMARK_RESPONSE_SCHEMA_VERSION = "golden_benchmark_report.v1"
EXPERIENCE_REPLAY_RESPONSE_SCHEMA_VERSION = "experience_replay_report.v1"
FORGETTING_REPORT_SCHEMA_VERSION = "forgetting_report.v1"
FORGETTING_RUN_SCHEMA_VERSION = "forgetting_run.v1"

PUBLIC_RESPONSE_SCHEMA_VERSIONS = {
    "doctor": DOCTOR_RESPONSE_SCHEMA_VERSION,
    "dashboard": DASHBOARD_RESPONSE_SCHEMA_VERSION,
    "golden_benchmark": GOLDEN_BENCHMARK_RESPONSE_SCHEMA_VERSION,
    "experience_replay": EXPERIENCE_REPLAY_RESPONSE_SCHEMA_VERSION,
    "forgetting_report": FORGETTING_REPORT_SCHEMA_VERSION,
    "forgetting_run": FORGETTING_RUN_SCHEMA_VERSION,
}


def response_schema_version(surface: str) -> str:
    """Return the stable top-level schema version for a public report surface."""

    return PUBLIC_RESPONSE_SCHEMA_VERSIONS[str(surface)]
