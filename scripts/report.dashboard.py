#!/usr/bin/env python3
"""Render a compact operator dashboard from scope-recall doctor data."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "scripts" / "doctor.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render scope-recall operator dashboard")
    parser.add_argument("--hermes-home", required=True, help="Hermes home/profile path to inspect")
    parser.add_argument("--source-root", default=str(ROOT), help="scope-recall source checkout")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    return parser.parse_args()


def _load_doctor():
    spec = importlib.util.spec_from_file_location("scope_recall_dashboard_doctor", DOCTOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load doctor module: {DOCTOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_dashboard(source_root: Path, hermes_home: Path) -> dict[str, Any]:
    doctor = _load_doctor()
    runtime_config = doctor.load_runtime_config(source_root, hermes_home)
    source, source_check, source_recommendations = doctor.source_report(source_root)
    sqlite_payload, sqlite_check, sqlite_recommendations = doctor.sqlite_report(hermes_home)
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
        vector_payload, vector_check, vector_recommendations = doctor.vector_report(hermes_home, expected_embedder=expected_embedder, backend=backend)
    else:
        backend = "disabled"
        vector_payload, vector_check, vector_recommendations = doctor.disabled_vector_report()
    checks = {
        "source_metadata": source_check,
        "sqlite_truth": sqlite_check,
        "memory_secret_scan": secret_check,
        "journal_provenance": journal_check,
        "experience_kernel": experience_check,
        "nightly_digest": nightly_check,
        "vector_companion": vector_check,
    }
    recommendations = [
        *source_recommendations,
        *sqlite_recommendations,
        *secret_recommendations,
        *journal_recommendations,
        *experience_recommendations,
        *nightly_recommendations,
        *vector_recommendations,
    ]
    journal_health = journal_payload.get("digest_health") or {}
    recovery_queue = journal_health.get("recovery_queue") or {}
    experience_funnel = experience_payload.get("promotion_funnel") or {}
    return {
        "ok": all(bool(check.get("ok")) for check in checks.values()),
        "source_version": source.get("pyproject_version") or source.get("plugin_yaml_version") or "",
        "hermes_home": str(hermes_home),
        "checks": checks,
        "summary": {
            "sqlite_memories": sqlite_payload.get("memory_count", 0),
            "journal_unprocessed": (journal_payload.get("entries") or {}).get("unprocessed", 0),
            "journal_digest_status": journal_health.get("status", journal_payload.get("status")),
            "journal_retry_exhausted_rejections": journal_health.get("retry_exhausted_rejections", 0),
            "journal_retry_replay_candidates": recovery_queue.get("retry_exhausted_candidates", 0),
            "journal_dead_letter_replay_candidates": recovery_queue.get("dead_letter_candidates", 0),
            "journal_llm_quarantine_runs": journal_health.get("llm_quarantine_runs", 0),
            "memory_secret_active": secret_payload.get("active_secret_like_count", 0),
            "vector_status": vector_payload.get("status"),
            "vector_backend": vector_payload.get("backend", backend),
            "experience_needs_review": experience_funnel.get("needs_review", 0),
            "experience_promoted": experience_funnel.get("promoted", 0),
            "experience_duplicate_groups": len(experience_funnel.get("duplicate_groups") or []),
            "nightly_status": nightly_payload.get("status"),
        },
        "sections": {
            "journal": journal_payload,
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


def main() -> int:
    args = parse_args()
    payload = build_dashboard(Path(args.source_root).expanduser().resolve(), Path(args.hermes_home).expanduser().resolve())
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(payload), end="")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
