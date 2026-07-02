#!/usr/bin/env python3
"""Inspect scope-recall source metadata and runtime storage health.

The doctor is intentionally read-only. The CLI stays in ``scripts/doctor.py``
for operator compatibility; implementation lives in ``doctor_*`` modules so new
health checks can be maintained without growing this wrapper.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_SOURCE_ROOT))

try:  # installed package / pytest package-alias path
    from scope_recall.doctor_common import (
        expected_embedder_from_config,
        load_runtime_config,
        redact_secret_like_text,
        vector_backend_from_config,
        vector_enabled_from_config,
        vector_fallback_backend_from_config,
    )
    from scope_recall.doctor_experience import experience_config_summary, experience_report, nightly_digest_report
    from scope_recall.doctor_journal import journal_enabled_from_config, journal_report
    from scope_recall.doctor_source import source_report
    from scope_recall.doctor_sqlite import memory_candidate_debt_report, memory_quality_lint_report, memory_secret_report, sqlite_report
    from scope_recall.doctor_vector import disabled_vector_report, sqlite_vector_report, vector_report
    from scope_recall.response_schemas import DOCTOR_RESPONSE_SCHEMA_VERSION
except ImportError:  # pragma: no cover - direct source checkout execution fallback
    from doctor_common import expected_embedder_from_config, load_runtime_config, redact_secret_like_text, vector_backend_from_config, vector_enabled_from_config, vector_fallback_backend_from_config
    from doctor_experience import experience_config_summary, experience_report, nightly_digest_report
    from doctor_journal import journal_enabled_from_config, journal_report
    from doctor_source import source_report
    from doctor_sqlite import memory_candidate_debt_report, memory_quality_lint_report, memory_secret_report, sqlite_report
    from doctor_vector import disabled_vector_report, sqlite_vector_report, vector_report
    from response_schemas import DOCTOR_RESPONSE_SCHEMA_VERSION

__all__ = [
    "disabled_vector_report",
    "experience_config_summary",
    "experience_report",
    "expected_embedder_from_config",
    "journal_enabled_from_config",
    "journal_report",
    "load_runtime_config",
    "main",
    "memory_candidate_debt_report",
    "memory_quality_lint_report",
    "memory_secret_report",
    "nightly_digest_report",
    "parse_args",
    "redact_secret_like_text",
    "source_report",
    "sqlite_report",
    "sqlite_vector_report",
    "vector_backend_from_config",
    "vector_enabled_from_config",
    "vector_fallback_backend_from_config",
    "vector_report",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect scope-recall source/runtime health")
    parser.add_argument("--json", action="store_true", help="emit JSON output (default; accepted for operator convenience)")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT), help="scope-recall source checkout")
    parser.add_argument("--hermes-home", default="", help="Hermes home/profile path to inspect")
    return parser.parse_args()


def _index_general_enabled(runtime_config: dict[str, Any]) -> bool:
    raw_vector_config = runtime_config.get("vector")
    vector_config = raw_vector_config if isinstance(raw_vector_config, dict) else {}
    raw_index_general = vector_config.get("index_general", False)
    if isinstance(raw_index_general, str):
        return raw_index_general.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw_index_general)


def main() -> int:
    """Parse doctor CLI flags, combine source/runtime checks, and emit a stable report.

    The wrapper should stay thin so focused doctor modules can evolve without breaking operator commands."""
    args = parse_args()
    source_root = Path(args.source_root).expanduser().resolve()
    source, source_check, recommendations = source_report(source_root)
    checks: dict[str, Any] = {"source_metadata": source_check}
    payload: dict[str, Any] = {
        "schema_version": DOCTOR_RESPONSE_SCHEMA_VERSION,
        "source": source,
        "checks": checks,
        "recommendations": recommendations,
        "runtime": {},
    }

    if args.hermes_home:
        hermes_home = Path(args.hermes_home).expanduser().resolve()
        runtime_config = load_runtime_config(source_root, hermes_home)
        expected_embedder = expected_embedder_from_config(runtime_config)
        sqlite_payload, sqlite_check, sqlite_recommendations = sqlite_report(hermes_home)
        candidate_payload, candidate_check, candidate_recommendations = memory_candidate_debt_report(hermes_home)
        quality_payload, quality_check, quality_recommendations = memory_quality_lint_report(hermes_home)
        secret_payload, secret_check, secret_recommendations = memory_secret_report(hermes_home)
        raw_journal = runtime_config.get("journal")
        journal_config = raw_journal if isinstance(raw_journal, dict) else {}
        journal_payload, journal_check, journal_recommendations = journal_report(
            hermes_home,
            enabled=journal_enabled_from_config(runtime_config),
            journal_config=journal_config,
        )
        experience_payload, experience_check, experience_recommendations = experience_report(hermes_home)
        experience_payload["config"] = experience_config_summary(runtime_config)
        nightly_payload, nightly_check, nightly_recommendations = nightly_digest_report(hermes_home)
        if vector_enabled_from_config(runtime_config):
            backend = vector_backend_from_config(runtime_config)
            fallback_backend = vector_fallback_backend_from_config(runtime_config)
            vector_payload, vector_check, vector_recommendations = vector_report(
                hermes_home,
                expected_embedder=expected_embedder,
                backend=backend,
                fallback_backend=fallback_backend,
                index_general=_index_general_enabled(runtime_config),
            )
        else:
            backend = "disabled"
            vector_payload, vector_check, vector_recommendations = disabled_vector_report()
        vector_payload.setdefault("backend", backend)
        payload["runtime"] = {
            "hermes_home": str(hermes_home),
            "expected_embedder": expected_embedder,
            "vector_backend": backend,
            "sqlite": sqlite_payload,
            "memory_candidate_debt": candidate_payload,
            "memory_quality_lint": quality_payload,
            "memory_secret_scan": secret_payload,
            "journal": journal_payload,
            "experience": experience_payload,
            "nightly_digest": nightly_payload,
            "vector": vector_payload,
        }
        checks["sqlite_truth"] = sqlite_check
        checks["memory_candidate_debt"] = candidate_check
        checks["memory_quality_lint"] = quality_check
        checks["memory_secret_scan"] = secret_check
        checks["journal_provenance"] = journal_check
        checks["experience_kernel"] = experience_check
        checks["nightly_digest"] = nightly_check
        checks["vector_companion"] = vector_check
        recommendations.extend(sqlite_recommendations)
        recommendations.extend(candidate_recommendations)
        recommendations.extend(quality_recommendations)
        recommendations.extend(secret_recommendations)
        recommendations.extend(journal_recommendations)
        recommendations.extend(experience_recommendations)
        recommendations.extend(nightly_recommendations)
        recommendations.extend(vector_recommendations)

    payload["ok"] = all(bool(check.get("ok")) for check in checks.values())
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
