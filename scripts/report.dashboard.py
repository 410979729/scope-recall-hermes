#!/usr/bin/env python3
"""Render a compact operator dashboard from scope-recall doctor data."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # package import path when installed or pytest aliases scope_recall
    from scope_recall.response_schemas import DASHBOARD_RESPONSE_SCHEMA_VERSION
except ImportError:  # pragma: no cover - direct source checkout execution fallback
    from response_schemas import DASHBOARD_RESPONSE_SCHEMA_VERSION

DOCTOR = ROOT / "scripts" / "doctor.py"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render scope-recall operator dashboard")
    parser.add_argument("--hermes-home", required=True, help="Hermes home/profile path to inspect")
    parser.add_argument("--source-root", default=str(ROOT), help="scope-recall source checkout")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--output", help="write dashboard output to this path instead of stdout")
    parser.add_argument("--previous", help="optional previous dashboard JSON for trend deltas")
    return parser.parse_args(argv)


def _load_doctor():
    spec = importlib.util.spec_from_file_location("scope_recall_dashboard_doctor", DOCTOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load doctor module: {DOCTOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _severity(ok: bool, summary: dict[str, Any], recommendations: list[str]) -> str:
    if not ok:
        return "FAIL"
    degraded_values = {"degraded", "warn", "warning"}
    if str(summary.get("journal_digest_status") or "").lower() in degraded_values:
        return "DEGRADED"
    if str(summary.get("nightly_status") or "").lower() in degraded_values:
        return "DEGRADED"
    if recommendations:
        return "WARN"
    return "OK"


def _load_previous_summary(previous_path: Path | None) -> dict[str, Any]:
    if previous_path is None or not previous_path.is_file():
        return {}
    try:
        payload = json.loads(previous_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    return dict(summary) if isinstance(summary, dict) else {}


def _trend(summary: dict[str, Any], previous_summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    trend: dict[str, dict[str, Any]] = {}
    for key, current in summary.items():
        previous = previous_summary.get(key)
        if isinstance(current, (int, float)) and isinstance(previous, (int, float)):
            trend[key] = {"current": current, "previous": previous, "delta": current - previous}
    return trend


def build_dashboard(source_root: Path, hermes_home: Path, *, previous_path: Path | None = None) -> dict[str, Any]:
    doctor = _load_doctor()
    runtime_config = doctor.load_runtime_config(source_root, hermes_home)
    source, source_check, source_recommendations = doctor.source_report(source_root)
    sqlite_payload, sqlite_check, sqlite_recommendations = doctor.sqlite_report(hermes_home)
    if hasattr(doctor, "memory_candidate_debt_report"):
        candidate_debt_payload, candidate_debt_check, candidate_debt_recommendations = doctor.memory_candidate_debt_report(hermes_home)
    else:
        candidate_debt_payload, candidate_debt_check, candidate_debt_recommendations = ({}, {"ok": True}, [])
    if hasattr(doctor, "memory_quality_lint_report"):
        memory_quality_payload, memory_quality_check, memory_quality_recommendations = doctor.memory_quality_lint_report(hermes_home)
    else:
        memory_quality_payload, memory_quality_check, memory_quality_recommendations = ({}, {"ok": True}, [])
    secret_payload, secret_check, secret_recommendations = doctor.memory_secret_report(hermes_home)
    raw_journal = runtime_config.get("journal")
    journal_config = raw_journal if isinstance(raw_journal, dict) else {}
    journal_payload, journal_check, journal_recommendations = doctor.journal_report(
        hermes_home,
        enabled=doctor.journal_enabled_from_config(runtime_config),
        journal_config=journal_config,
    )
    experience_payload, experience_check, experience_recommendations = doctor.experience_report(hermes_home)
    nightly_payload, nightly_check, nightly_recommendations = doctor.nightly_digest_report(hermes_home)
    expected_embedder = doctor.expected_embedder_from_config(runtime_config)
    if doctor.vector_enabled_from_config(runtime_config):
        backend = doctor.vector_backend_from_config(runtime_config)
        fallback_backend = doctor.vector_fallback_backend_from_config(runtime_config)
        vector_payload, vector_check, vector_recommendations = doctor.vector_report(
            hermes_home,
            expected_embedder=expected_embedder,
            backend=backend,
            fallback_backend=fallback_backend,
            index_general=doctor._index_general_enabled(runtime_config),
        )
    else:
        backend = "disabled"
        vector_payload, vector_check, vector_recommendations = doctor.disabled_vector_report()
    checks = {
        "source_metadata": source_check,
        "sqlite_truth": sqlite_check,
        "memory_candidate_debt": candidate_debt_check,
        "memory_quality_lint": memory_quality_check,
        "memory_secret_scan": secret_check,
        "journal_provenance": journal_check,
        "experience_kernel": experience_check,
        "nightly_digest": nightly_check,
        "vector_companion": vector_check,
    }
    recommendations = [
        *source_recommendations,
        *sqlite_recommendations,
        *candidate_debt_recommendations,
        *memory_quality_recommendations,
        *secret_recommendations,
        *journal_recommendations,
        *experience_recommendations,
        *nightly_recommendations,
        *vector_recommendations,
    ]
    journal_health = journal_payload.get("digest_health") or {}
    recovery_queue = journal_health.get("recovery_queue") or {}
    experience_funnel = experience_payload.get("promotion_funnel") or {}
    candidate_debt = dict(candidate_debt_payload or {})
    if not candidate_debt and isinstance(sqlite_payload.get("candidate_debt"), dict):
        candidate_debt = dict(sqlite_payload.get("candidate_debt") or {})
    memory_quality_lint = dict(memory_quality_payload or {})
    if not memory_quality_lint and isinstance(sqlite_payload.get("memory_quality_lint"), dict):
        memory_quality_lint = dict(sqlite_payload.get("memory_quality_lint") or {})
    schema_migration = sqlite_payload.get("schema_migrations") if isinstance(sqlite_payload.get("schema_migrations"), dict) else {}
    freshness = experience_payload.get("fact_freshness") if isinstance(experience_payload.get("fact_freshness"), dict) else {}
    ok = all(bool(check.get("ok")) for check in checks.values())
    summary = {
        "sqlite_memories": sqlite_payload.get("memory_count", 0),
        "journal_unprocessed": (journal_payload.get("entries") or {}).get("unprocessed", 0),
        "journal_digest_status": journal_health.get("status", journal_payload.get("status")),
        "journal_retry_exhausted_rejections": journal_health.get("retry_exhausted_rejections", 0),
        "journal_retry_replay_candidates": recovery_queue.get("retry_exhausted_candidates", 0),
        "journal_dead_letter_replay_candidates": recovery_queue.get("dead_letter_candidates", 0),
        "journal_llm_quarantine_runs": journal_health.get("llm_quarantine_runs", 0),
        "memory_secret_active": secret_payload.get("active_secret_like_count", 0),
        "candidate_debt_count": candidate_debt.get("candidate_count", candidate_debt.get("count", 0)),
        "candidate_debt_oldest_age_hours": candidate_debt.get("oldest_age_hours", 0),
        "memory_quality_active_hits": memory_quality_lint.get("active_lint_hits", memory_quality_lint.get("active_hits", 0)),
        "memory_quality_high_severity": memory_quality_lint.get("high_severity", memory_quality_lint.get("high", 0)),
        "fact_freshness_needs_live_check": freshness.get("needs_live_check", 0),
        "fact_freshness_expired": freshness.get("expired", (freshness.get("by_status") or {}).get("expired", 0)),
        "fact_freshness_total": freshness.get("total", freshness.get("tracked_facts", 0)),
        "schema_migration_current": bool(schema_migration.get("current")),
        "vector_status": vector_payload.get("status"),
        "vector_backend": vector_payload.get("backend", backend),
        "experience_needs_review": experience_funnel.get("needs_review", 0),
        "experience_promoted": experience_funnel.get("promoted", 0),
        "experience_duplicate_groups": len(experience_funnel.get("duplicate_groups") or []),
        "nightly_status": nightly_payload.get("status"),
    }
    return {
        "schema_version": DASHBOARD_RESPONSE_SCHEMA_VERSION,
        "ok": ok,
        "severity": _severity(ok, summary, recommendations),
        "source_version": source.get("pyproject_version") or source.get("plugin_yaml_version") or "",
        "hermes_home": str(hermes_home),
        "checks": checks,
        "summary": summary,
        "trend": _trend(summary, _load_previous_summary(previous_path)),
        "sections": {
            "journal": journal_payload,
            "candidate_debt": candidate_debt,
            "memory_quality_lint": memory_quality_lint,
            "schema_migration": schema_migration,
            "freshness": freshness,
            "memory_secret_scan": secret_payload,
            "experience": experience_payload,
            "nightly_digest": nightly_payload,
            "vector": vector_payload,
        },
        "recommendations": recommendations,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = ["# Scope Recall Dashboard", ""]
    lines.append(f"- ok: `{payload.get('ok')}`")
    lines.append(f"- severity: `{payload.get('severity', '')}`")
    lines.append(f"- version: `{payload.get('source_version', '')}`")
    lines.append(f"- hermes_home: `{payload.get('hermes_home', '')}`")
    lines.append("")
    lines.append("## 核心指标")
    labels = [
        ("SQLite memories", "sqlite_memories"),
        ("Journal unprocessed", "journal_unprocessed"),
        ("Journal digest status", "journal_digest_status"),
        ("Retry-exhausted rejections", "journal_retry_exhausted_rejections"),
        ("Retry replay candidates", "journal_retry_replay_candidates"),
        ("Dead-letter replay candidates", "journal_dead_letter_replay_candidates"),
        ("LLM quarantine runs", "journal_llm_quarantine_runs"),
        ("Active secret-like memories", "memory_secret_active"),
        ("Candidate debt", "candidate_debt_count"),
        ("Memory quality active hits", "memory_quality_active_hits"),
        ("Fact freshness needs live check", "fact_freshness_needs_live_check"),
        ("Schema migration current", "schema_migration_current"),
        ("Vector status", "vector_status"),
        ("Vector backend", "vector_backend"),
        ("Experience needs_review", "experience_needs_review"),
        ("Experience promoted", "experience_promoted"),
        ("Experience duplicate groups", "experience_duplicate_groups"),
        ("Nightly status", "nightly_status"),
    ]
    for label, key in labels:
        lines.append(f"- {label}: `{summary.get(key)}`")
    recommendations = payload.get("recommendations") or []
    if recommendations:
        lines.append("")
        lines.append("## 建议")
        for item in recommendations[:20]:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    previous_path = Path(args.previous).expanduser().resolve() if args.previous else None
    payload = build_dashboard(Path(args.source_root).expanduser().resolve(), Path(args.hermes_home).expanduser().resolve(), previous_path=previous_path)
    if args.format == "json":
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        rendered = render_markdown(payload)
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
