#!/usr/bin/env python3
"""Release-readiness checks for scope-recall.

This script runs local checks that are useful immediately before committing or
publishing the plugin. It deliberately avoids reading secrets from the user's
Hermes runtime environment; it scans only this source tree.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

try:
    import yaml
except ImportError:  # pragma: no cover - release environment check reports this cleanly
    yaml = None

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PACKAGE_VERSION = "1.7.2"
WHEEL_DIST_PREFIX = f"hermes_scope_recall-{PACKAGE_VERSION}"
RELEASE_READINESS_DOC = f"docs/release-readiness.{PACKAGE_VERSION}.md"
GENERATED_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist", ".venv"}
LOCAL_ONLY_DIRS = {".hermes"}
EXTERNAL_TEST_DIRS = {".hermes-agent-src"}
RELEASE_REQUIRED_MODULES = ("build", "pytest", "ruff", "wheel", "pyright", "yaml", "lancedb", "pyarrow")
SECRET_PATTERNS = {
    "api_key_assignment": re.compile(
        r"[\"']?\b(?:api[_ -]?key|secret|password|passwd|token)\b[\"']?\s*(?:=|:)\s*[\"']?[A-Za-z0-9._\-+/=]{12,}[\"']?",
        re.I,
    ),
    "bearer_literal": re.compile(r"bearer\s+[A-Za-z0-9._\-~+/=]{16,}", re.I),
    "github_pat": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "openai_style": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # Telegram supergroup/channel IDs use a signed ``-100...`` form and may
    # appear in Python, JSON, YAML, or list fixtures without a ``telegram:``
    # prefix. Match the shape everywhere in public source/artifacts, but never
    # echo the identifier in scanner output.
    "telegram_group_id": re.compile(r"(?P<telegram_id>(?<!\d)-100\d{8,12})(?!\d)"),
}

_POSITIVE_TELEGRAM_ID = r"(?P<telegram_id>\d{8,12})(?!\d)"
POSITIVE_TELEGRAM_ID_CONTEXT_PATTERNS = (
    # Python/env assignments, including camelCase and compound
    # ``telegram_chat_id`` keys. Case-insensitive matching covers env keys.
    re.compile(
        rf"\b(?:telegram[_-]?)?(?:chat|user)[_-]?id\b\s*=\s*[\"']?{_POSITIVE_TELEGRAM_ID}",
        re.I,
    ),
    # JSON/mapping keys and Python subscript assignments such as
    # ``payload[\"chat_id\"] = ...``.
    re.compile(
        rf"[\"'](?:telegram[_-]?)?(?:chat|user)[_-]?id[\"']\s*"
        rf"(?:\]\s*=|:|,)\s*[\"']?{_POSITIVE_TELEGRAM_ID}",
        re.I,
    ),
    # Unquoted YAML/TOML-like key/value mappings.
    re.compile(
        rf"(?<![\"'\w-])(?:telegram[_-]?)?(?:chat|user)[_-]?id\s*:\s*"
        rf"[\"']?{_POSITIVE_TELEGRAM_ID}",
        re.I,
    ),
    # CLI/config forms: ``--chat-id VALUE`` and ``--user-id=VALUE``.
    re.compile(
        rf"--(?:telegram-)?(?:chat|user)-id(?:\s+|=)\s*[\"']?{_POSITIVE_TELEGRAM_ID}",
        re.I,
    ),
    # Preserve the legacy explicit Telegram label/tuple forms.
    re.compile(rf"\btelegram\s*:\s*[\"']?{_POSITIVE_TELEGRAM_ID}", re.I),
    re.compile(rf"[\"']telegram[\"']\s*,\s*[\"']?{_POSITIVE_TELEGRAM_ID}", re.I),
)
_POSITIVE_TELEGRAM_ID_LITERAL = re.compile(_POSITIVE_TELEGRAM_ID)
_IDENTIFIER_CONTEXT_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,80}")
_IDENTIFIER_CONTEXT_WINDOW_CHARS = 160
_RESERVED_POSITIVE_IDENTIFIER = "9000000001"
_RESERVED_IDENTIFIER_FIXTURE_PATHS = frozenset(
    {
        "tests/test_journal_digest.py",
        "tests/test_journal_extractors.py",
        "tests/test_nightly_digest.py",
        "tests/test_provider.py",
        "tests/test_v1015_audit_regressions.py",
    }
)
FORBIDDEN_PUBLIC_DOC_MARKERS = {
    "personal_name_joy": re.compile(r"\bJoy\b"),
    "agent_persona_yuheng": re.compile(r"玉衡"),
    "manual_review_private_context": re.compile(r"人工复审"),
    "private_product_promise": re.compile(r"product promise Joy cares about", re.I),
}
FORBIDDEN_DISTRIBUTION_PATH_FRAGMENTS = ("/docs/plans/",)
FORBIDDEN_DISTRIBUTION_BASENAMES = {
    "hermes-upstream-recommendation-plan.md",
    "source_isolation.py",
}
REQUIRED_SOURCE_FILES = {
    "README.md",
    "DESIGN.md",
    "CHANGELOG.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "MANIFEST.in",
    "pyproject.toml",
    "plugin.yaml",
    "config.json",
    "cli.py",
    "config_schema.py",
    "candidate_extraction.py",
    "candidate_review.py",
    "candidate_store.py",
    "capture_filters.py",
    "doctor_event_digest.py",
    "event_digest.py",
    "external_bridge.py",
    "memory_browser.py",
    "pgvector_store.py",
    "postgres_bridge.py",
    "secret_index.py",
    "skill_bridge.py",
    "vector_generation.py",
    "vector_generation_preflight.py",
    "vector_migration.py",
    "vector_repair.py",
    "vector_runtime.py",
    "vector_store.py",
    "experience_replay_generation.py",
    "digest_quality.py",
    "digest_run_results.py",
    "doctor_common.py",
    "doctor_experience.py",
    "doctor_journal.py",
    "doctor_source.py",
    "doctor_sqlite.py",
    "doctor_vector.py",
    "freshness.py",
    "lifecycle_policy.py",
    "lifecycle_service.py",
    "governance_scheduler.py",
    "graph_relations.py",
    "graph_hygiene.py",
    "maintenance_ops.py",
    "memory_quality.py",
    "task_boundary.py",
    "experience_evidence.py",
    "experience_quality.py",
    "experience_synthesis.py",
    "migration_openclaw.py",
    "nightly_llm.py",
    "journal_llm.py",
    "journal_store.py",
    "journal_candidates.py",
    "journal_extractors.py",
    "provider_schemas.py",
    "recall_pipeline.py",
    "relation_extraction.py",
    "response_schemas.py",
    ".env.example",
    "docs/migration.md",
    "docs/differences-from-memory-lancedb-pro.md",
    "docs/external-shared-memory.md",
    "docs/stability.md",
    "docs/naming.md",
    "docs/experience.kernel.md",
    "docs/contract.matrix.md",
    "docs/upstream-recommendation.md",
    "docs/benchmark.golden.md",
    "docs/governance.cleanup.md",
    "docs/memory-quality-kernel.md",
    "docs/configuration.md",
    "docs/event-digest.md",
    "docs/governance-ui.md",
    "docs/install.md",
    "docs/skill-bridge.md",
    "docs/vector-backends.md",
    "docs/operator-runbook.md",
    "docs/cross-profile-rollout.md",
    "docs/response-contracts.md",
    RELEASE_READINESS_DOC,
    "benchmarks/golden_recall_cases.json",
    "benchmarks/golden_recall_hybrid_cases.json",
    "benchmarks/experience_replay_cases.json",
    "examples/external_bridge/import.jsonl",
    "examples/external_bridge/export.jsonl",
    "examples/external_bridge/conflict_resolution.jsonl",
    "examples/external_bridge/postgres_schema.sql",
    "scripts/import.openclaw.memory_lancedb_pro.py",
    "scripts/nightly-digest.py",
    "scripts/journal-digest.py",
    "scripts/repair.vector_index.py",
    "scripts/repair.hidden_vector_companions.py",
    "scripts/migrate.vector_generation.py",
    "scripts/report.hygiene.py",
    "scripts/migrate.legacy_hygiene.py",
    "scripts/migrate.status.py",
    "scripts/doctor.py",
    "scripts/experience-replay.py",
    "scripts/benchmark.golden.py",
    "scripts/benchmark.graph_relations.py",
    "scripts/benchmark.retrieval_regression.py",
    "scripts/backfill.graph_relations.py",
    "scripts/governance.cleanup.py",
    "scripts/governance.audit_coverage.py",
    "scripts/governance.scheduler.py",
    "scripts/journal.recovery.py",
    "scripts/playbook.bootstrap.py",
    "scripts/playbooks.py",
    "scripts/report.dashboard.py",
    "scripts/rollout.profiles.py",
    "scripts/candidate.review.py",
    "scripts/memory.browser.py",
    "scripts/skill.bridge.py",
    "experience_bootstrap.py",
    "experience_classification.py",
    "experience_replay.py",
    "experience_models.py",
    "experience_store.py",
    "experience_preflight.py",
    "experience_promotion.py",
    "forgetting.py",
    "governance_cleanup.py",
    "journal_recovery.py",
    "installer.py",
    "py.typed",
}
REQUIRED_WHEEL = {
    "scope_recall/__init__.py",
    "scope_recall/artifacts.py",
    "scope_recall/provider.py",
    "scope_recall/cli.py",
    "scope_recall/config_schema.py",
    "scope_recall/candidate_extraction.py",
    "scope_recall/candidate_review.py",
    "scope_recall/candidate_store.py",
    "scope_recall/doctor_event_digest.py",
    "scope_recall/event_digest.py",
    "scope_recall/external_bridge.py",
    "scope_recall/memory_browser.py",
    "scope_recall/pgvector_store.py",
    "scope_recall/postgres_bridge.py",
    "scope_recall/skill_bridge.py",
    "scope_recall/vector_generation.py",
    "scope_recall/vector_generation_preflight.py",
    "scope_recall/vector_migration.py",
    "scope_recall/vector_repair.py",
    "scope_recall/vector_runtime.py",
    "scope_recall/vector_store.py",
    "scope_recall/experience_replay_generation.py",
    "scope_recall/installer.py",
    "scope_recall/capture_llm.py",
    "scope_recall/capture_filters.py",
    "scope_recall/memory_ops.py",
    "scope_recall/tooling.py",
    "scope_recall/governance.py",
    "scope_recall/http_utils.py",
    "scope_recall/prompting.py",
    "scope_recall/schemas.py",
    "scope_recall/secret_index.py",
    "scope_recall/experience_bootstrap.py",
    "scope_recall/experience_classification.py",
    "scope_recall/experience_replay.py",
    "scope_recall/experience_models.py",
    "scope_recall/experience_store.py",
    "scope_recall/experience_preflight.py",
    "scope_recall/experience_promotion.py",
    "scope_recall/forgetting.py",
    "scope_recall/governance_cleanup.py",
    "scope_recall/journal_recovery.py",
    "scope_recall/hygiene.py",
    "scope_recall/journal.py",
    "scope_recall/nightly_digest.py",
    "scope_recall/nightly_llm.py",
    "scope_recall/journal_llm.py",
    "scope_recall/journal_store.py",
    "scope_recall/journal_candidates.py",
    "scope_recall/journal_extractors.py",
    "scope_recall/sqlite_vector_store.py",
    "scope_recall/py.typed",
    "scope_recall/pyproject.toml",
    "scope_recall/plugin.yaml",
    "scope_recall/config.json",
    "scope_recall/digest_quality.py",
    "scope_recall/digest_run_results.py",
    "scope_recall/doctor_common.py",
    "scope_recall/doctor_experience.py",
    "scope_recall/doctor_journal.py",
    "scope_recall/doctor_source.py",
    "scope_recall/doctor_sqlite.py",
    "scope_recall/doctor_vector.py",
    "scope_recall/freshness.py",
    "scope_recall/lifecycle_policy.py",
    "scope_recall/lifecycle_service.py",
    "scope_recall/governance_scheduler.py",
    "scope_recall/graph_relations.py",
    "scope_recall/graph_hygiene.py",
    "scope_recall/maintenance_ops.py",
    "scope_recall/memory_quality.py",
    "scope_recall/task_boundary.py",
    "scope_recall/experience_evidence.py",
    "scope_recall/experience_quality.py",
    "scope_recall/experience_synthesis.py",
    "scope_recall/migration_openclaw.py",
    "scope_recall/provider_schemas.py",
    "scope_recall/recall_pipeline.py",
    "scope_recall/relation_extraction.py",
    "scope_recall/response_schemas.py",
    "scope_recall/README.md",
    "scope_recall/DESIGN.md",
    "scope_recall/CHANGELOG.md",
    "scope_recall/CONTRIBUTING.md",
    "scope_recall/LICENSE",
    "scope_recall/SECURITY.md",
    "scope_recall/MANIFEST.in",
    "scope_recall/.env.example",
    "scope_recall/docs/migration.md",
    "scope_recall/docs/differences-from-memory-lancedb-pro.md",
    "scope_recall/docs/external-shared-memory.md",
    "scope_recall/docs/stability.md",
    "scope_recall/docs/naming.md",
    "scope_recall/docs/experience.kernel.md",
    "scope_recall/docs/contract.matrix.md",
    "scope_recall/docs/upstream-recommendation.md",
    "scope_recall/docs/benchmark.golden.md",
    "scope_recall/docs/governance.cleanup.md",
    "scope_recall/docs/memory-quality-kernel.md",
    "scope_recall/docs/configuration.md",
    "scope_recall/docs/event-digest.md",
    "scope_recall/docs/governance-ui.md",
    "scope_recall/docs/install.md",
    "scope_recall/docs/skill-bridge.md",
    "scope_recall/docs/vector-backends.md",
    "scope_recall/docs/operator-runbook.md",
    "scope_recall/docs/cross-profile-rollout.md",
    "scope_recall/docs/response-contracts.md",
    f"scope_recall/{RELEASE_READINESS_DOC}",
    "scope_recall/benchmarks/golden_recall_cases.json",
    "scope_recall/benchmarks/golden_recall_hybrid_cases.json",
    "scope_recall/benchmarks/experience_replay_cases.json",
    "scope_recall/examples/external_bridge/import.jsonl",
    "scope_recall/examples/external_bridge/export.jsonl",
    "scope_recall/examples/external_bridge/conflict_resolution.jsonl",
    "scope_recall/examples/external_bridge/postgres_schema.sql",
    "scope_recall/scripts/import.openclaw.memory_lancedb_pro.py",
    "scope_recall/scripts/nightly-digest.py",
    "scope_recall/scripts/journal-digest.py",
    "scope_recall/scripts/repair.vector_index.py",
    "scope_recall/scripts/repair.hidden_vector_companions.py",
    "scope_recall/scripts/migrate.vector_generation.py",
    "scope_recall/scripts/report.hygiene.py",
    "scope_recall/scripts/migrate.legacy_hygiene.py",
    "scope_recall/scripts/migrate.status.py",
    "scope_recall/scripts/doctor.py",
    "scope_recall/scripts/experience-replay.py",
    "scope_recall/scripts/benchmark.golden.py",
    "scope_recall/scripts/benchmark.graph_relations.py",
    "scope_recall/scripts/benchmark.retrieval_regression.py",
    "scope_recall/scripts/backfill.graph_relations.py",
    "scope_recall/scripts/governance.cleanup.py",
    "scope_recall/scripts/governance.audit_coverage.py",
    "scope_recall/scripts/governance.scheduler.py",
    "scope_recall/scripts/journal.recovery.py",
    "scope_recall/scripts/playbook.bootstrap.py",
    "scope_recall/scripts/playbooks.py",
    "scope_recall/scripts/report.dashboard.py",
    "scope_recall/scripts/rollout.profiles.py",
    "scope_recall/scripts/candidate.review.py",
    "scope_recall/scripts/memory.browser.py",
    "scope_recall/scripts/skill.bridge.py",
}
STABLE_TOOL_NAMES = {
    "scope_recall_store",
    "scope_recall_store_secret_index",
    "scope_recall_search",
    "scope_recall_context",
    "scope_recall_profile",
    "scope_recall_memory",
    "scope_recall_entity",
    "scope_recall_probe",
    "scope_recall_related",
    "scope_recall_feedback",
    "scope_recall_forget",
    "scope_recall_update",
    "scope_recall_dedupe",
    "scope_recall_merge",
    "scope_recall_export",
    "scope_recall_govern",
    "scope_recall_hygiene",
    "scope_recall_repair",
    "scope_recall_stats",
    "scope_recall_inspect",
    "scope_recall_explain",
    "scope_recall_benchmark",
    "scope_recall_playbook_create",
    "scope_recall_playbook_search",
    "scope_recall_playbook_inspect",
    "scope_recall_experience_preflight",
    "scope_recall_playbook_feedback",
    "scope_recall_playbook_review",
    "scope_recall_experience_stats",
    "scope_recall_experience_promote",
    "scope_recall_forgetting_report",
    "scope_recall_forgetting_run",
}
STABLE_LIFECYCLE_HOOKS = {
    "on_turn_start",
    "on_pre_compress",
    "on_memory_write",
    "on_session_end",
    "on_session_switch",
}
STABLE_PROVIDER_METHODS = STABLE_LIFECYCLE_HOOKS | {"get_config_schema", "get_tool_schemas"}
REQUIRED_CHANGELOG_TERMS = (
    "forgetting",
    "governance",
    "journal recovery",
    "dashboard",
    "experience replay",
    "installer rollback",
    "fact freshness",
    "relation extraction",
    "golden benchmark",
)
RELEASE_READINESS_LOCAL_STATE_PATTERNS = {
    "embedded_live_snapshot": re.compile(r"current read-only snapshot", re.I),
    "embedded_severity_counter": re.compile(r"\bseverity=(?:ok|degraded|blocked)\b", re.I),
    "embedded_journal_counter": re.compile(r"\bjournal_(?:unprocessed|dead_letter_replay_candidates|llm_quarantine_runs)=", re.I),
    "embedded_dead_letter_counter": re.compile(r"\bdead-letter:[a-z_-]+=", re.I),
    "embedded_private_path": re.compile(r"(?:^|[\s`'\"])(?:/home/|/Users/|/root/|[A-Za-z]:[\\/](?:Users|Documents)[\\/])"),
}


def run(cmd: list[str], *, cwd: pathlib.Path = ROOT, env: dict[str, str] | None = None) -> dict[str, object]:
    proc = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
    return {"cmd": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def fail_if_bad(result: dict[str, object]) -> None:
    if result["returncode"] != 0:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(int(result["returncode"]))


def progress(stage: str) -> None:
    """Emit machine-readable progress on stderr so long release gates are diagnosable."""
    print(
        json.dumps({"event": "release_gate_progress", "stage": stage, "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()}, ensure_ascii=False),
        file=sys.stderr,
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run scope-recall release readiness checks")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow a dirty/untracked working tree while running development verification. Strict release mode fails dirty trees.",
    )
    # Accepted for operator compatibility: live doctor checks use this, but the
    # release script intentionally avoids reading the live runtime by default.
    parser.add_argument("--hermes-home", default="", help=argparse.SUPPRESS)
    parser.add_argument(
        "--live-dashboard-json",
        default="",
        help=(
            "Optional path to a JSON payload from scripts/report.dashboard.py. "
            "When provided, the gate verifies that docs/release-readiness.<version>.md matches the live snapshot."
        ),
    )
    parser.add_argument(
        "--accept-stale-live-waiver",
        action="store_true",
        help="Allow --live-dashboard-json to report stale waiver fields without failing the release gate.",
    )
    return parser.parse_args()


def _git_status_path(line: str) -> str:
    if len(line) < 4:
        return ""
    return line[3:].strip()


def _is_ignorable_git_status_line(line: str) -> bool:
    path = _git_status_path(line)
    if not path:
        return False
    parts = pathlib.PurePosixPath(path).parts
    top_level = parts[0] if parts else ""
    return top_level in LOCAL_ONLY_DIRS or top_level in EXTERNAL_TEST_DIRS or top_level in GENERATED_DIRS


def git_tree_check(*, allow_dirty: bool) -> dict[str, object]:
    result = run(["git", "status", "--porcelain=v1"])
    if result["returncode"] != 0:
        return {"ok": False, "error": result}
    lines = [
        line
        for line in str(result["stdout"]).splitlines()
        if line.strip() and not _is_ignorable_git_status_line(line)
    ]
    untracked = [line for line in lines if line.startswith("?? ")]
    dirty = [line for line in lines if not line.startswith("?? ")]
    return {
        "ok": allow_dirty or not lines,
        "allow_dirty": bool(allow_dirty),
        "dirty": dirty,
        "untracked": untracked,
    }


def _run_golden_benchmark(cases_path: str) -> tuple[dict[str, object], dict[str, object] | None]:
    args = [sys.executable, "scripts/benchmark.golden.py"]
    if cases_path:
        args.extend(["--cases", cases_path])
    result = run(args)
    if result["returncode"] != 0:
        return result, None
    try:
        payload = json.loads(str(result["stdout"] or "{}"))
    except json.JSONDecodeError as exc:
        return {"returncode": result.get("returncode"), "error": f"invalid golden benchmark json: {exc}", "result": result}, None
    return result, payload if isinstance(payload, dict) else None


def _int_payload_value(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _payload_failures(payload: dict[str, object]) -> list[object]:
    failures = payload.get("failures")
    return list(failures) if isinstance(failures, list) else []


def benchmark_check() -> dict[str, object]:
    golden_payloads: list[dict[str, object]] = []
    for cases_path in ("", "benchmarks/golden_recall_hybrid_cases.json"):
        golden_result, golden_payload = _run_golden_benchmark(cases_path)
        if golden_payload is None:
            return {"ok": False, "golden_result": golden_result, "cases_path": cases_path or "benchmarks/golden_recall_cases.json"}
        golden_payloads.append(golden_payload)

    graph_result = run([sys.executable, "scripts/benchmark.graph_relations.py"])
    if graph_result["returncode"] != 0:
        return {"ok": False, "graph_result": graph_result}
    try:
        graph_payload = json.loads(str(graph_result["stdout"] or "{}"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid graph benchmark json: {exc}", "graph_result": graph_result}

    golden_ok = all(bool(payload.get("passed")) and payload.get("schema_version") == "golden_benchmark_report.v1" for payload in golden_payloads)
    return {
        "ok": golden_ok and bool(graph_payload.get("passed")),
        "schema_version": golden_payloads[0].get("schema_version"),
        "golden_profiles": [
            {
                "golden_name": payload.get("golden_name"),
                "query_count": _int_payload_value(payload, "query_count"),
                "failures": _payload_failures(payload),
                "metrics": payload.get("metrics"),
            }
            for payload in golden_payloads
        ],
        "golden_name": golden_payloads[0].get("golden_name"),
        "query_count": sum(_int_payload_value(payload, "query_count") for payload in golden_payloads),
        "failures": [failure for payload in golden_payloads for failure in _payload_failures(payload)],
        "graph_name": graph_payload.get("benchmark_name"),
        "graph_metrics": graph_payload.get("metrics"),
    }


def read_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def pyright_include_check() -> dict[str, object]:
    """Ensure release-critical top-level source modules are covered by Pyright.

    Required-file and wheel checks prove packaging coverage, but not type-check
    coverage. This gate prevents newly added modules from silently bypassing the
    explicit Pyright include list used for release verification.
    """
    try:
        pyproject = tomllib.loads(read_text("pyproject.toml"))
    except Exception as exc:
        return {"ok": False, "error": f"invalid pyproject.toml: {exc}", "required_source_py": [], "missing_pyright_include": []}
    tool_config = pyproject.get("tool", {}) if isinstance(pyproject, dict) else {}
    pyright_config = tool_config.get("pyright", {}) if isinstance(tool_config, dict) else {}
    raw_include = pyright_config.get("include", []) if isinstance(pyright_config, dict) else []
    includes = {str(item).replace("\\", "/") for item in raw_include if str(item).strip()}
    required_source_py = sorted(rel for rel in REQUIRED_SOURCE_FILES if rel.endswith(".py") and "/" not in rel)
    missing = sorted(rel for rel in required_source_py if rel not in includes)
    return {"ok": not missing, "required_source_py": required_source_py, "missing_pyright_include": missing}


def changelog_section(changelog: str, version: str) -> str:
    marker = f"## [{version}]"
    start = changelog.find(marker)
    if start < 0:
        return ""
    next_match = re.search(r"(?m)^## \[[^\]]+\]", changelog[start + len(marker) :])
    if next_match is None:
        return changelog[start:]
    end = start + len(marker) + next_match.start()
    return changelog[start:end]


def changelog_completeness_check(changelog: str, *, version: str = PACKAGE_VERSION) -> dict[str, object]:
    section = changelog_section(changelog, version)
    if not section:
        return {"ok": False, "version": version, "missing_terms": list(REQUIRED_CHANGELOG_TERMS), "section_found": False}
    lower = section.lower()
    missing_terms = [term for term in REQUIRED_CHANGELOG_TERMS if term.lower() not in lower]
    return {"ok": not missing_terms, "version": version, "missing_terms": missing_terms, "section_found": True}


def release_readiness_public_hygiene_check(readiness_text: str) -> dict[str, object]:
    """Keep versioned public readiness notes free of deployment-local runtime state."""
    findings = [
        {"marker": name, "match": match.group(0).strip()}
        for name, pattern in RELEASE_READINESS_LOCAL_STATE_PATTERNS.items()
        if (match := pattern.search(readiness_text)) is not None
    ]
    return {"ok": not findings, "findings": findings}


def release_readiness_tree_hygiene_check() -> dict[str, object]:
    """Apply public-state hygiene to every readiness note shipped by package globs."""
    findings: list[dict[str, str]] = []
    for path in sorted((ROOT / "docs").glob("release-readiness.*.md")):
        result = release_readiness_public_hygiene_check(path.read_text(encoding="utf-8", errors="ignore"))
        result_findings = result.get("findings", [])
        if not isinstance(result_findings, list):
            continue
        for item in result_findings:
            if isinstance(item, dict):
                findings.append(
                    {
                        "path": str(path.relative_to(ROOT)),
                        "marker": str(item.get("marker", "")),
                        "match": str(item.get("match", "")),
                    }
                )
    return {"ok": not findings, "findings": findings}


LIVE_DASHBOARD_WAIVER_FIELDS = (
    "ok",
    "severity",
    "journal_unprocessed",
    "journal_dead_letter_replay_candidates",
    "journal_llm_quarantine_runs",
    "journal_digest_status",
    "experience_duplicate_groups",
    "experience_needs_review",
    "memory_quality_active_hits",
    "memory_secret_active",
    "vector_status",
    "schema_migration_current",
)


def _normalize_snapshot_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def release_readiness_snapshot_values(readiness_text: str) -> dict[str, str]:
    marker = "Current read-only snapshot"
    snapshot_text = readiness_text
    marker_index = readiness_text.find(marker)
    if marker_index >= 0:
        snapshot_text = readiness_text[marker_index:]
        end_match = re.search(r"(?m)^Reason:\s*$", snapshot_text)
        if end_match is not None:
            snapshot_text = snapshot_text[: end_match.start()]
    values: dict[str, str] = {}
    for match in re.finditer(r"`([^`=]+)=([^`]+)`", snapshot_text):
        values[match.group(1).strip()] = match.group(2).strip()
    return values


def release_readiness_snapshot_age_days(readiness_text: str) -> int | None:
    match = re.search(r"(?m)^Date:\s*(\d{4}-\d{2}-\d{2})\s*$", readiness_text)
    if not match:
        return None
    try:
        snapshot_date = dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None
    return (dt.date.today() - snapshot_date).days


def dashboard_snapshot_values(dashboard_payload: dict[str, object]) -> dict[str, str]:
    summary_obj = dashboard_payload.get("summary", {})
    summary = summary_obj if isinstance(summary_obj, dict) else {}
    output: dict[str, str] = {
        "ok": _normalize_snapshot_value(dashboard_payload.get("ok")),
        "severity": _normalize_snapshot_value(dashboard_payload.get("severity")),
    }
    for key in LIVE_DASHBOARD_WAIVER_FIELDS:
        if key in output:
            continue
        if key in summary:
            output[key] = _normalize_snapshot_value(summary.get(key))
    return output


def live_dashboard_waiver_check(
    dashboard_payload: dict[str, object],
    readiness_text: str,
    *,
    accept_stale: bool = False,
) -> dict[str, object]:
    readiness_values = release_readiness_snapshot_values(readiness_text)
    dashboard_values = dashboard_snapshot_values(dashboard_payload)
    mismatches: list[dict[str, str]] = []
    missing_fields: list[str] = []
    for field in LIVE_DASHBOARD_WAIVER_FIELDS:
        current = dashboard_values.get(field, "")
        if not current:
            continue
        recorded = readiness_values.get(field)
        if recorded is None:
            missing_fields.append(field)
            continue
        if recorded != current:
            mismatches.append({"field": field, "recorded": recorded, "current": current})
    live_ok = dashboard_values.get("severity") == "OK"
    waiver_used = not live_ok
    stale = bool(mismatches or missing_fields)
    return {
        "ok": not stale or bool(accept_stale),
        "enabled": True,
        "live_ok": live_ok,
        "waiver_used": waiver_used,
        "accept_stale": bool(accept_stale),
        "snapshot_age_days": release_readiness_snapshot_age_days(readiness_text),
        "mismatches": mismatches,
        "missing_fields": missing_fields,
        "current": dashboard_values,
    }


def live_dashboard_file_check(path: str, *, accept_stale: bool = False) -> dict[str, object]:
    if not path:
        return {"ok": True, "enabled": False}
    dashboard_path = pathlib.Path(path)
    try:
        dashboard_payload = json.loads(dashboard_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "enabled": True, "error": f"invalid live dashboard json: {exc}", "path": str(dashboard_path)}
    if not isinstance(dashboard_payload, dict):
        return {"ok": False, "enabled": True, "error": "live dashboard json must be an object", "path": str(dashboard_path)}
    readiness_path = ROOT / RELEASE_READINESS_DOC
    readiness_text = readiness_path.read_text(encoding="utf-8") if readiness_path.is_file() else ""
    result = live_dashboard_waiver_check(dashboard_payload, readiness_text, accept_stale=accept_stale)
    result["path"] = str(dashboard_path)
    result["release_readiness_doc"] = RELEASE_READINESS_DOC
    return result


def parse_plugin_manifest_hooks(plugin_text: str) -> list[str]:
    hooks: list[str] = []
    in_hooks = False
    hooks_indent = 0
    for raw_line in plugin_text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not in_hooks:
            if stripped == "hooks:" or stripped.startswith("hooks: "):
                in_hooks = True
                hooks_indent = indent
            continue
        if indent <= hooks_indent and not stripped.startswith("-"):
            break
        if stripped.startswith("- "):
            hooks.append(stripped[2:].strip().strip("'\""))
    return hooks


def provider_class_method_names() -> list[str]:
    tree = ast.parse(read_text("provider.py"), filename="provider.py")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ScopeRecallMemoryProvider":
            return sorted(
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return []


def provider_lifecycle_hook_methods() -> list[str]:
    return sorted(name for name in provider_class_method_names() if name.startswith("on_"))


def schema_constant_tool_names() -> dict[str, str]:
    tree = ast.parse(read_text("schemas.py"), filename="schemas.py")
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (TypeError, ValueError, SyntaxError):
            continue
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("SCOPE_RECALL_") and target.id.endswith("_SCHEMA"):
                names[target.id] = str(value["name"])
    return names


def provider_tool_schema_names_by_surface() -> dict[str, list[str]]:
    schema_names = schema_constant_tool_names()
    tree = ast.parse(read_text("provider_schemas.py"), filename="provider_schemas.py")
    surfaces: dict[str, list[str]] = {}
    variable_to_surface = {
        "compact_schemas": "compact",
        "standard_schemas": "standard",
        "experience_schemas": "experience",
        "maintenance_schemas": "maintenance",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        surface = ""
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in variable_to_surface:
                surface = variable_to_surface[target.id]
                break
        if not surface or not isinstance(node.value, ast.List):
            continue
        values: list[str] = []
        for item in node.value.elts:
            if isinstance(item, ast.Name) and item.id in schema_names:
                values.append(schema_names[item.id])
        surfaces[surface] = values
    referenced = {
        schema_names[node.id]
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in schema_names
    }
    surfaces["all_referenced"] = sorted(referenced)
    return surfaces


def tool_dispatcher_names() -> list[str]:
    tree = ast.parse(read_text("tooling.py"), filename="tooling.py")
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.startswith("scope_recall_"):
                names.add(key.value)
    return sorted(names)


def response_schema_versions() -> dict[str, str]:
    path = ROOT / "response_schemas.py"
    if not path.is_file():
        return {}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename="response_schemas.py")
    string_constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    string_constants[target.id] = str(node.value.value)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "PUBLIC_RESPONSE_SCHEMA_VERSIONS" for target in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            return {}
        versions: dict[str, str] = {}
        for key_node, value_node in zip(node.value.keys, node.value.values, strict=False):
            if key_node is None:
                continue
            try:
                key = ast.literal_eval(key_node)
            except (TypeError, ValueError, SyntaxError):
                continue
            if not isinstance(key, str):
                continue
            if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                versions[key] = str(value_node.value)
            elif isinstance(value_node, ast.Name) and value_node.id in string_constants:
                versions[key] = string_constants[value_node.id]
        return versions
    return {}


def pypi_workflow_gate_check() -> dict[str, object]:
    pypi_path = ROOT / ".github" / "workflows" / "pypi.yml"
    release_path = ROOT / ".github" / "workflows" / "release.yml"
    failures: list[str] = []
    if not pypi_path.is_file():
        failures.append("missing .github/workflows/pypi.yml")
        pypi_text = ""
    else:
        pypi_text = pypi_path.read_text(encoding="utf-8")
    if not release_path.is_file():
        failures.append("missing .github/workflows/release.yml")
        release_text = ""
    else:
        release_text = release_path.read_text(encoding="utf-8")

    gate_marker = "scripts/check.release.py"
    publish_marker = "pypa/gh-action-pypi-publish"
    if gate_marker not in pypi_text:
        failures.append("manual PyPI workflow does not invoke scripts/check.release.py")
    if publish_marker in pypi_text and gate_marker in pypi_text and pypi_text.index(gate_marker) > pypi_text.index(publish_marker):
        failures.append("manual PyPI workflow invokes release gate after the publish step")
    if re.search(r"(?m)^\s+release:\s*$", pypi_text):
        failures.append("manual PyPI workflow must not listen to release: published; tag release workflow publishes PyPI directly")

    for workflow_name, workflow_text in (("manual PyPI", pypi_text), ("tag release", release_text)):
        if "Invalid release tag" not in workflow_text:
            failures.append(f"{workflow_name} workflow does not validate release tag format")
        if "Verify tag matches package version" not in workflow_text:
            failures.append(f"{workflow_name} workflow does not verify tag/package version consistency")

    if gate_marker not in release_text:
        failures.append("tag release workflow does not invoke scripts/check.release.py")
    if publish_marker not in release_text:
        failures.append("tag release workflow does not publish to PyPI")
    if publish_marker in release_text and gate_marker in release_text and release_text.index(gate_marker) > release_text.index(publish_marker):
        failures.append("tag release workflow invokes release gate after the PyPI publish step")
    return {"ok": not failures, "failures": failures}


def product_contract_check() -> dict[str, object]:
    """Check stable product contracts that should not drift during refactors.

    These assertions protect public tool names, lifecycle hooks, release workflows, and response surfaces from accidental breakage."""
    failures: list[str] = []
    provider_methods = set(provider_class_method_names())
    provider_hooks = set(provider_lifecycle_hook_methods())
    manifest_hooks = set(parse_plugin_manifest_hooks(read_text("plugin.yaml")))
    missing_provider_methods = sorted(STABLE_PROVIDER_METHODS - provider_methods)
    if missing_provider_methods:
        failures.append(f"provider missing stable methods: {', '.join(missing_provider_methods)}")
    if provider_hooks != manifest_hooks:
        missing_manifest_hooks = sorted(provider_hooks - manifest_hooks)
        extra_manifest_hooks = sorted(manifest_hooks - provider_hooks)
        if missing_manifest_hooks:
            failures.append(f"manifest missing provider hooks: {', '.join(missing_manifest_hooks)}")
        if extra_manifest_hooks:
            failures.append(f"manifest lists hooks not implemented by provider: {', '.join(extra_manifest_hooks)}")
    missing_stable_hooks = sorted(STABLE_LIFECYCLE_HOOKS - manifest_hooks)
    if missing_stable_hooks:
        failures.append(f"manifest missing stable lifecycle hooks: {', '.join(missing_stable_hooks)}")

    schema_surfaces = provider_tool_schema_names_by_surface()
    referenced_tools = set(schema_surfaces.get("all_referenced", []))
    missing_stable_tools = sorted(referenced_tools - STABLE_TOOL_NAMES)
    if missing_stable_tools:
        failures.append(f"STABLE_TOOL_NAMES missing provider schema tools: {', '.join(missing_stable_tools)}")

    dispatch_tools = set(tool_dispatcher_names())
    missing_dispatch = sorted(STABLE_TOOL_NAMES - dispatch_tools)
    if missing_dispatch:
        failures.append(f"tool dispatcher missing stable tool handlers: {', '.join(missing_dispatch)}")

    required_response_surfaces = {
        "doctor",
        "dashboard",
        "golden_benchmark",
        "experience_replay",
        "forgetting_report",
        "forgetting_run",
    }
    response_versions = response_schema_versions()
    missing_response_surfaces = sorted(required_response_surfaces - set(response_versions))
    if missing_response_surfaces:
        failures.append(f"response schema registry missing surfaces: {', '.join(missing_response_surfaces)}")
    response_doc_path = ROOT / "docs" / "response-contracts.md"
    response_doc = response_doc_path.read_text(encoding="utf-8") if response_doc_path.is_file() else ""
    for surface, version in sorted(response_versions.items()):
        if version not in response_doc:
            failures.append(f"response contract doc missing {surface} schema version: {version}")

    workflow_gate = pypi_workflow_gate_check()
    workflow_failures = workflow_gate.get("failures", [])
    if isinstance(workflow_failures, list):
        failures.extend(str(item) for item in workflow_failures)
    return {
        "ok": not failures,
        "failures": failures,
        "manifest_hooks": sorted(manifest_hooks),
        "provider_hooks": sorted(provider_hooks),
        "stable_tool_names": sorted(STABLE_TOOL_NAMES),
        "schema_surfaces": schema_surfaces,
        "response_schema_versions": response_versions,
        "workflow_gate": workflow_gate,
    }


def redact_sensitive(text: object) -> str:
    from scope_recall.http_utils import redact_sensitive as _redact_sensitive

    redacted = _redact_sensitive(text)
    redacted = re.sub(
        r"(?i)([\"']?\b(?:api[_ -]?key|secret|password|passwd|token)\b[\"']?\s*(?:=|:)\s*[\"']?)[A-Za-z0-9._\-+/=]{4,}([\"']?)",
        r"\1[REDACTED]\2",
        redacted,
    )
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{4,}", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"gh[pousr]_[A-Za-z0-9_]{8,}", "[REDACTED]", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9]{8,}", "[REDACTED]", redacted)
    return redacted


SYNTHETIC_HOME_PRIVATE_FIXTURE = "/home/" + "a/private"

SYNTHETIC_TEST_FIXTURE_MARKERS = (
    "fake",
    "fixture",
    "legacy_",
    "example_",
    "notareal",
    "not_a_real",
    "public-test-token",
    "secret1234567890",
    "abcdef1234567890",
    "test-key",
    "test_token",
    "token-without",
    "without-jwt",
    "sk-secret",
    "[redacted",
    "redacted_",
    "private/output.log",
    SYNTHETIC_HOME_PRIVATE_FIXTURE,
)


def _is_synthetic_test_fixture_line(rel: pathlib.Path, line: str) -> bool:
    if rel.parts[:1] != ("tests",):
        return False
    lowered = line.lower()
    return any(marker in lowered for marker in SYNTHETIC_TEST_FIXTURE_MARKERS)


def _is_reserved_identifier_fixture_context(rel: pathlib.Path, context: str) -> bool:
    """Allow one reserved value only in named historical fixture files."""

    if rel.as_posix() not in _RESERVED_IDENTIFIER_FIXTURE_PATHS:
        return False
    identifiers = {match.group("telegram_id") for match in _POSITIVE_TELEGRAM_ID_LITERAL.finditer(context)}
    return bool(identifiers) and identifiers <= {_RESERVED_POSITIVE_IDENTIFIER}


def _looks_like_release_secret(match_text: str) -> bool:
    """Return true only for likely plaintext secret literals.

    The release scanner should catch real JSON/YAML/Python secret assignments,
    while ignoring ordinary source variables such as
    ``api_key = _resolve_api_key(...)`` and sanitizer fixtures that already use
    ``[REDACTED]`` or ``***``.
    """
    parts = re.split(r"=|:", match_text, maxsplit=1)
    raw_value = parts[1] if len(parts) == 2 else match_text
    value = raw_value.strip().strip("'\"").strip()
    value_lower = value.lower()
    if not value or value.startswith("_") or "(" in value or "[" in value:
        return False
    if value_lower in {"none", "null", "true", "false", "api_key", "token", "secret", "password"}:
        return False
    if "redacted" in value_lower or set(value) <= {"*"}:
        return False
    if value.startswith(("sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_")):
        return True
    has_alpha = any(ch.isalpha() for ch in value)
    has_digit = any(ch.isdigit() for ch in value)
    return len(value) >= 16 and has_alpha and has_digit


def _is_identifier_context_name(value: str) -> bool:
    """Recognize singular/plural Telegram identifier names after normalization."""

    return _normalized_identifier_context_name(value).endswith(("chatid", "chatids", "userid", "userids"))


def _normalized_identifier_context_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _ast_has_identifier_context(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Name):
        return _is_identifier_context_name(node.id)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _is_identifier_context_name(node.value)
    if isinstance(node, ast.Attribute):
        return _is_identifier_context_name(node.attr)
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and isinstance(key.value, str) and _is_identifier_context_name(key.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(_ast_has_identifier_context(item) for item in node.elts)
    return False


def _static_scalar(node: ast.AST, aliases: dict[str, str | int]) -> str | int | None:
    """Resolve only side-effect-free scalar syntax used by release fixtures."""

    if isinstance(node, ast.Constant) and not isinstance(node.value, bool) and isinstance(node.value, (int, str)):
        return node.value
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_scalar(node.left, aliases)
        right = _static_scalar(node.right, aliases)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, int) and isinstance(right, int):
            return left + right
        return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"int", "str"}
        and len(node.args) == 1
        and not node.keywords
    ):
        value = _static_scalar(node.args[0], aliases)
        if value is None:
            return None
        if node.func.id == "int":
            return int(value) if str(value).isdigit() else None
        return str(value)
    return None


def _positive_identifier_nodes(node: ast.AST, aliases: dict[str, str | int]) -> list[ast.AST]:
    matches: list[ast.AST] = []
    for child in ast.walk(node):
        value = _static_scalar(child, aliases)
        rendered = str(value) if value is not None else ""
        if not rendered.isdigit() or not 8 <= len(rendered) <= 12:
            continue
        matches.append(child)
    return matches


def _update_scalar_aliases(
    targets: list[ast.expr],
    value: ast.AST | None,
    aliases: dict[str, str | int],
) -> None:
    """Track simple aliases without executing imports, calls, or arbitrary code."""

    rendered = _static_scalar(value, aliases) if value is not None else None
    for target in targets:
        if not isinstance(target, ast.Name) or _is_identifier_context_name(target.id):
            continue
        if rendered is None:
            aliases.pop(target.id, None)
        else:
            aliases[target.id] = rendered


def _python_identifier_lines(text: str) -> set[int] | None:
    """Find positive identifier literals in valid Python using AST context."""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None

    lines: set[int] = set()
    aliases: dict[str, str | int] = {}
    ordered_nodes = sorted(
        ast.walk(tree),
        key=lambda item: (int(getattr(item, "lineno", 0)), int(getattr(item, "col_offset", 0))),
    )
    for node in ordered_nodes:
        values: list[ast.AST] = []
        if isinstance(node, ast.Assign) and any(_ast_has_identifier_context(target) for target in node.targets):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and _ast_has_identifier_context(node.target):
            if node.value is not None:
                values.append(node.value)
            values.append(node.annotation)
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for index, operand in enumerate(operands):
                if _ast_has_identifier_context(operand):
                    values.extend(item for offset, item in enumerate(operands) if offset != index)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if _ast_has_identifier_context(key):
                    values.append(value)
        for value in values:
            lines.update(
                int(getattr(item, "lineno", getattr(value, "lineno", 1)))
                for item in _positive_identifier_nodes(value, aliases)
            )
        if isinstance(node, ast.Assign):
            _update_scalar_aliases(node.targets, node.value, aliases)
        elif isinstance(node, ast.AnnAssign):
            _update_scalar_aliases([node.target], node.value, aliases)
    return lines


class _JSONObjectPairs(list[tuple[str, object]]):
    """Preserve duplicate structured-config keys so policy scans cannot be bypassed."""


def _yaml_load_preserving_pairs(text: str) -> object:
    """Load safe YAML while preserving duplicate mapping keys for policy scans."""

    if yaml is None:
        raise RuntimeError("PyYAML is unavailable")

    class PairLoader(yaml.SafeLoader):
        pass

    def construct_pairs(loader, node, deep=False):
        return _JSONObjectPairs(
            [
                (loader.construct_object(key, deep=deep), loader.construct_object(value, deep=deep))
                for key, value in node.value
            ]
        )

    PairLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_pairs)
    return yaml.load(text, Loader=PairLoader)


def _value_has_positive_identifier(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, str)):
        rendered = str(value)
        return rendered.isdigit() and 8 <= len(rendered) <= 12
    if isinstance(value, _JSONObjectPairs):
        return any(_value_has_positive_identifier(item) for _key, item in value)
    if isinstance(value, dict):
        return any(_value_has_positive_identifier(item) for item in value.values())
    if isinstance(value, list):
        return any(_value_has_positive_identifier(item) for item in value)
    return False


def _collect_structured_identifier_names(value: object, found: set[str]) -> None:
    if isinstance(value, _JSONObjectPairs):
        items = value
    elif isinstance(value, dict):
        items = list(value.items())
    else:
        items = []
    for key, item in items:
        if _is_identifier_context_name(key) and _value_has_positive_identifier(item):
            found.add(_normalized_identifier_context_name(key))
        _collect_structured_identifier_names(item, found)
    if isinstance(value, list) and not isinstance(value, _JSONObjectPairs):
        for item in value:
            _collect_structured_identifier_names(item, found)


def _structured_config_identifier_lines(rel: pathlib.Path, text: str) -> set[int] | None:
    """Parse JSON/YAML/TOML and return context-key lines; malformed input falls back."""

    suffix = rel.suffix.lower()
    try:
        if suffix == ".json":
            value = json.loads(text, object_pairs_hook=_JSONObjectPairs)
        elif suffix in {".yaml", ".yml"}:
            if yaml is None:
                return None
            value = _yaml_load_preserving_pairs(text)
        elif suffix == ".toml":
            value = tomllib.loads(text)
        else:
            return None
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, yaml.YAMLError if yaml is not None else RuntimeError):
        return None
    names: set[str] = set()
    _collect_structured_identifier_names(value, names)
    return {
        text[: match.start()].count("\n") + 1
        for match in _IDENTIFIER_CONTEXT_TOKEN.finditer(text)
        if _normalized_identifier_context_name(match.group(0)) in names
    }


_YAML_KEY_LINE = re.compile(
    r"^(?P<indent>[ \t]*)(?:-\s+)?(?P<key>[\"']?[A-Za-z][A-Za-z0-9_-]{0,80}[\"']?)\s*:\s*(?P<rest>.*)$"
)


def _yaml_identifier_lines(text: str) -> set[int]:
    """Fallback for malformed YAML or environments missing the declared parser."""

    lines = text.splitlines()
    found: set[int] = set()
    for index, line in enumerate(lines):
        match = _YAML_KEY_LINE.match(line)
        if match is None or not _is_identifier_context_name(match.group("key").strip("'\"")):
            continue
        rest = match.group("rest")
        chunks = [rest]
        meaningful_rest = rest.split("#", 1)[0].strip()
        if (
            not meaningful_rest
            or meaningful_rest in {"|", ">", "|-", ">-", "|+", ">+"}
            or (meaningful_rest.startswith("[") and "]" not in meaningful_rest)
        ):
            base_indent = len(match.group("indent").expandtabs(8))
            for following in lines[index + 1 :]:
                stripped = following.strip()
                if not stripped or stripped.startswith("#"):
                    chunks.append(following)
                    continue
                indent = len(following) - len(following.lstrip(" \t"))
                if indent <= base_indent and not stripped.startswith("-"):
                    break
                chunks.append(following)
        if _POSITIVE_TELEGRAM_ID_LITERAL.search("\n".join(chunks)):
            found.add(index + 1)
    return found


def _text_identifier_lines(text: str) -> set[int]:
    """Scan bounded cross-line key windows in docs or malformed config text."""

    found: set[int] = set()
    for key_match in _IDENTIFIER_CONTEXT_TOKEN.finditer(text):
        if not _is_identifier_context_name(key_match.group(0)):
            continue
        window = text[key_match.start() : key_match.end() + _IDENTIFIER_CONTEXT_WINDOW_CHARS]
        if _POSITIVE_TELEGRAM_ID_LITERAL.search(window):
            found.add(text[: key_match.start()].count("\n") + 1)
    return found


def _non_python_identifier_lines(rel: pathlib.Path, text: str) -> set[int]:
    structured = _structured_config_identifier_lines(rel, text)
    if structured is not None:
        return structured
    if rel.suffix.lower() in {".yaml", ".yml"}:
        return _yaml_identifier_lines(text)
    return _text_identifier_lines(text)


def _scan_sensitive_text(rel: pathlib.Path, text: str, *, display_path: str = "") -> dict[str, list[str]]:
    """Scan one decoded source/artifact member without echoing sensitive values."""

    findings: dict[str, list[str]] = {"secrets": [], "private_paths": []}
    label = display_path or str(rel)
    lines = text.splitlines()

    # Regex preserves support for syntax-invalid snippets and existing textual
    # forms. Valid Python additionally uses AST context; other text uses one
    # bounded normalized key window instead of syntax-by-syntax regex growth.
    positive_lines: set[int] = set()
    for rx in POSITIVE_TELEGRAM_ID_CONTEXT_PATTERNS:
        for match in rx.finditer(text):
            positive_lines.add(text[: match.start()].count("\n") + 1)
    if rel.suffix.lower() == ".py":
        python_lines = _python_identifier_lines(text)
        positive_lines.update(python_lines if python_lines is not None else _non_python_identifier_lines(rel, text))
    else:
        positive_lines.update(_non_python_identifier_lines(rel, text))
    positive_lines = {
        line_no
        for line_no in positive_lines
        if not (
            _is_synthetic_test_fixture_line(
                rel,
                "\n".join(lines[line_no - 1 :])[:_IDENTIFIER_CONTEXT_WINDOW_CHARS]
                if 0 < line_no <= len(lines)
                else "",
            )
            or _is_reserved_identifier_fixture_context(
                rel,
                "\n".join(lines[line_no - 1 :])[:_IDENTIFIER_CONTEXT_WINDOW_CHARS]
                if 0 < line_no <= len(lines)
                else "",
            )
        )
    }
    findings["secrets"].extend(
        f"{label}:{line_no}: personal_numeric_id: [REDACTED_ID]" for line_no in sorted(positive_lines)
    )

    for name, rx in SECRET_PATTERNS.items():
        for match in rx.finditer(text):
            if name == "api_key_assignment" and not _looks_like_release_secret(match.group(0)):
                continue
            line_no = text[: match.start()].count("\n") + 1
            line = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else match.group(0)
            if name == "telegram_group_id":
                findings["secrets"].append(f"{label}:{line_no}: personal_numeric_id: [REDACTED_ID]")
                continue
            if _is_synthetic_test_fixture_line(rel, line):
                continue
            findings["secrets"].append(f"{label}:{line_no}: {name}: {redact_sensitive(match.group(0))}")

    home = pathlib.Path.home()
    private_markers = tuple(
        marker
        for marker in {
            str(home / ".hermes-yuheng"),
            str(home) + os.sep,
        }
        if marker and marker != os.sep
    )
    private_path_lines = [
        line_no
        for line_no, line in enumerate(lines, 1)
        if any(marker in line for marker in private_markers) and not _is_synthetic_test_fixture_line(rel, line)
    ]
    if private_path_lines:
        findings["private_paths"].append(f"{label}:{private_path_lines[0]}")
    return findings


def scan_tree() -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {"generated_artifacts": [], "secrets": [], "private_paths": []}
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if ".git" in rel.parts:
            continue
        if any(part in LOCAL_ONLY_DIRS for part in rel.parts):
            continue
        if any(part in EXTERNAL_TEST_DIRS for part in rel.parts):
            continue
        if any(part in GENERATED_DIRS for part in rel.parts):
            if path.exists():
                findings["generated_artifacts"].append(str(rel))
            continue
        if rel.match("review-report.*.md") or rel.name == ".env":
            continue
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        scanned = _scan_sensitive_text(rel, text)
        findings["secrets"].extend(scanned["secrets"])
        findings["private_paths"].extend(scanned["private_paths"])
    for key in findings:
        findings[key] = sorted(set(findings[key]))
    return findings


_DISTRIBUTION_TEXT_SUFFIXES = {
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_DISTRIBUTION_TEXT_NAMES = {"METADATA", "PKG-INFO", "entry_points.txt", "top_level.txt"}


def _distribution_member_is_text(name: str) -> bool:
    member = pathlib.PurePosixPath(str(name).replace("\\", "/"))
    return member.suffix.lower() in _DISTRIBUTION_TEXT_SUFFIXES or member.name in _DISTRIBUTION_TEXT_NAMES


def _distribution_member_source_path(name: str) -> pathlib.Path:
    """Normalize an archive member to its source-tree path for fixture policy."""

    parts = pathlib.PurePosixPath(str(name).replace("\\", "/")).parts
    if len(parts) >= 2 and parts[1] == "tests":
        parts = parts[1:]
    return pathlib.Path(*parts)


def scan_distribution_artifact(path: pathlib.Path) -> dict[str, list[str]]:
    """Scan decoded wheel/sdist members so packaging cannot bypass source hygiene."""

    artifact = pathlib.Path(path)
    members: list[tuple[str, bytes]] = []
    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            members = [
                (info.filename, archive.read(info))
                for info in archive.infolist()
                if not info.is_dir() and _distribution_member_is_text(info.filename)
            ]
    elif tarfile.is_tarfile(artifact):
        with tarfile.open(artifact, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not _distribution_member_is_text(member.name):
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    members.append((member.name, handle.read()))
    else:
        raise ValueError(f"unsupported distribution artifact: {artifact.name}")

    findings: dict[str, list[str]] = {"secrets": [], "private_paths": []}
    for name, raw in members:
        rel = _distribution_member_source_path(name)
        scanned = _scan_sensitive_text(
            rel,
            raw.decode("utf-8", errors="ignore"),
            display_path=f"{artifact.name}:{name}",
        )
        findings["secrets"].extend(scanned["secrets"])
        findings["private_paths"].extend(scanned["private_paths"])
    return {key: sorted(set(value)) for key, value in findings.items()}


def release_environment_check() -> dict[str, object]:
    """Report and enforce the Python environment expected by release gates."""

    modules = {name: importlib.util.find_spec(name) is not None for name in RELEASE_REQUIRED_MODULES}
    missing = sorted(name for name, present in modules.items() if not present)
    return {
        "ok": not missing,
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "prefix": sys.prefix,
        "required_modules": modules,
        "missing_modules": missing,
        "install_command": "python -m pip install -e '.[dev,all]'",
    }


def metadata_check() -> dict[str, object]:
    """Validate package and plugin metadata for a release candidate.

    Version, entry point, manifest, and package names must agree before publishing wheels or tags."""
    pyproject = read_text("pyproject.toml")
    plugin = read_text("plugin.yaml")
    readme = read_text("README.md")
    changelog = read_text("CHANGELOG.md")
    stability = read_text("docs/stability.md")
    schemas = read_text("schemas.py")
    release_readiness = read_text(RELEASE_READINESS_DOC) if (ROOT / RELEASE_READINESS_DOC).is_file() else ""

    missing_source = sorted(rel for rel in REQUIRED_SOURCE_FILES if not (ROOT / rel).is_file())
    failures: list[str] = []
    product_contract = product_contract_check()
    public_docs_hygiene = public_doc_hygiene_check()
    release_readiness_hygiene = release_readiness_tree_hygiene_check()
    pyright_coverage = pyright_include_check()
    required_snippets = {
        "pyproject version": f'version = "{PACKAGE_VERSION}"',
        "plugin version": f"version: {PACKAGE_VERSION}",
        "stable classifier": "Development Status :: 4 - Beta",
        "public contributors": "scope-recall contributors",
        "changelog v1": f"## [{PACKAGE_VERSION}]",
        "readme v1": "stable V1 release line",
        "stability truth source": "SQLite is the truth source",
        "stability tools": "scope_recall_stats",
        "contract matrix": "Scope Recall Contract Matrix",
        "contract matrix truth source": "SQLite is the truth source. Vector stores, summaries, and derived indexes are",
        "contract matrix stable tools": "Stable `scope_recall_*` tool names remain registered.",
    }
    searchable = "\n".join([pyproject, plugin, readme, changelog, stability, read_text("docs/contract.matrix.md")])
    for label, snippet in required_snippets.items():
        if snippet not in searchable:
            failures.append(f"missing {label}: {snippet}")
    if "Development Status :: 5 - Production/Stable" in searchable:
        failures.append("production-stable classifier still present; V1 should remain release-candidate/beta until broader field use")
    if 'version = "0.' in pyproject or "version: 0." in plugin:
        failures.append("0.x package/plugin version still present")
    changelog_gate = changelog_completeness_check(changelog)
    if not changelog_gate["ok"]:
        missing_obj = changelog_gate.get("missing_terms", [])
        missing_terms = [str(term) for term in missing_obj] if isinstance(missing_obj, list) else []
        failures.append(f"changelog {PACKAGE_VERSION} missing release-note terms: {', '.join(missing_terms)}")
    for label, snippet in {
        "release readiness title": f"Scope Recall {PACKAGE_VERSION} Release Readiness",
        "runtime evidence policy": "Runtime evidence policy",
        "release owner": "Owner: maintainers.",
        "release clearance": "Clearance condition:",
    }.items():
        if snippet not in release_readiness:
            failures.append(f"missing {label} in {RELEASE_READINESS_DOC}: {snippet}")
    for tool_name in STABLE_TOOL_NAMES:
        if tool_name not in stability:
            failures.append(f"stable tool missing from stability doc: {tool_name}")
        if tool_name.upper() not in schemas.upper():
            failures.append(f"stable tool missing from schemas.py: {tool_name}")
    product_failures = product_contract.get("failures", [])
    if not product_contract["ok"] and isinstance(product_failures, list):
        failures.extend(f"product contract: {failure}" for failure in product_failures)
    if not public_docs_hygiene["ok"]:
        failures.append(f"public docs hygiene: {json.dumps(public_docs_hygiene, ensure_ascii=False, sort_keys=True)}")
    if not release_readiness_hygiene["ok"]:
        failures.append(
            f"release readiness public hygiene: {json.dumps(release_readiness_hygiene, ensure_ascii=False, sort_keys=True)}"
        )
    if not pyright_coverage["ok"]:
        missing_pyright = pyright_coverage.get("missing_pyright_include", [])
        missing_pyright_list = missing_pyright if isinstance(missing_pyright, list) else []
        failures.append(f"pyright include missing required source files: {', '.join(str(item) for item in missing_pyright_list)}")
    return {
        "ok": not missing_source and not failures,
        "missing_source": missing_source,
        "failures": failures,
        "product_contract": product_contract,
        "public_docs_hygiene": public_docs_hygiene,
        "release_readiness_hygiene": release_readiness_hygiene,
        "pyright_coverage": pyright_coverage,
    }


def public_doc_hygiene_check() -> dict[str, object]:
    """Reject private planning notes and personal collaboration markers from public docs.

    Implementation plans may exist in private operator notes, but release-tagged
    repository docs and package artifacts should read as public product material.
    """
    doc_paths: set[pathlib.Path] = set()
    for rel in ("README.md", "DESIGN.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md"):
        path = ROOT / rel
        if path.is_file():
            doc_paths.add(path)
    docs_dir = ROOT / "docs"
    if docs_dir.is_dir():
        doc_paths.update(path for path in docs_dir.rglob("*.md") if path.is_file())

    forbidden_paths = sorted(
        str(path.relative_to(ROOT)).replace(os.sep, "/")
        for path in doc_paths
        if "plans" in path.relative_to(ROOT).parts
    )
    findings: list[dict[str, object]] = []
    for path in sorted(doc_paths):
        rel = str(path.relative_to(ROOT)).replace(os.sep, "/")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            findings.append({"path": rel, "marker": "decode_error", "line": 0})
            continue
        for line_no, line in enumerate(lines, 1):
            for label, pattern in FORBIDDEN_PUBLIC_DOC_MARKERS.items():
                if pattern.search(line):
                    findings.append({"path": rel, "marker": label, "line": line_no})
    return {"ok": not forbidden_paths and not findings, "forbidden_paths": forbidden_paths, "findings": findings}


def forbidden_distribution_entries(names: set[str]) -> list[str]:
    """Return packaged paths that should never ship in public artifacts."""
    forbidden: list[str] = []
    for name in sorted(names):
        normalized = name.replace("\\", "/")
        wrapped = f"/{normalized}/"
        if any(fragment in wrapped for fragment in FORBIDDEN_DISTRIBUTION_PATH_FRAGMENTS):
            forbidden.append(name)
            continue
        if pathlib.PurePosixPath(normalized).name in FORBIDDEN_DISTRIBUTION_BASENAMES:
            forbidden.append(name)
    return forbidden


def wheel_check() -> dict[str, object]:
    """Build and inspect the wheel artifact for release-critical package contents.

    The check catches missing modules, docs, entry points, and metadata before a tag can publish a broken package."""
    with tempfile.TemporaryDirectory(prefix="scope.recall.dist.") as tmp:
        dist = pathlib.Path(tmp)
        result = run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(dist)])
        fail_if_bad(result)
        wheels = list(dist.glob("hermes_scope_recall-*.whl"))
        if len(wheels) != 1:
            raise SystemExit(f"expected one wheel, found {wheels}")
        expected_name = f"{WHEEL_DIST_PREFIX}-py3-none-any.whl"
        if wheels[0].name != expected_name:
            raise SystemExit(f"expected wheel {expected_name}, got {wheels[0].name}")
        with zipfile.ZipFile(wheels[0]) as zf:
            names = set(zf.namelist())
        wheel_scan = scan_distribution_artifact(wheels[0])
        missing = sorted(item for item in REQUIRED_WHEEL if item not in names)
        pycache = sorted(name for name in names if "__pycache__" in name or name.endswith(".pyc"))
        wheel_forbidden = forbidden_distribution_entries(names)
        if missing or pycache or wheel_forbidden:
            raise SystemExit(json.dumps({"missing": missing, "pycache": pycache, "forbidden": wheel_forbidden}, ensure_ascii=False, indent=2))

        sdist_result = run([sys.executable, "-m", "build", "--sdist", "--outdir", str(dist)])
        fail_if_bad(sdist_result)
        sdists = list(dist.glob("hermes_scope_recall-*.tar.gz"))
        expected_sdist = f"hermes_scope_recall-{PACKAGE_VERSION}.tar.gz"
        if len(sdists) != 1 or sdists[0].name != expected_sdist:
            raise SystemExit(f"expected sdist {expected_sdist}, found {sdists}")
        with tarfile.open(sdists[0], "r:gz") as tf:
            sdist_names = set(tf.getnames())
        sdist_scan = scan_distribution_artifact(sdists[0])
        sdist_forbidden = forbidden_distribution_entries(sdist_names)
        artifact_scan = {
            "wheel": wheel_scan,
            "sdist": sdist_scan,
        }
        blocking_artifact_scan = {
            artifact_type: {key: value for key, value in scan.items() if value}
            for artifact_type, scan in artifact_scan.items()
            if any(scan.values())
        }
        if sdist_forbidden or blocking_artifact_scan:
            raise SystemExit(
                json.dumps(
                    {
                        "sdist_forbidden": sdist_forbidden,
                        "artifact_scan": blocking_artifact_scan,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

        install_dir = dist / "install"
        install_dir.mkdir()
        result = run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(install_dir), str(wheels[0])])
        fail_if_bad(result)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(install_dir)
        result = run([sys.executable, "-c", "import scope_recall; print(scope_recall.__all__)"], cwd=dist, env=env)
        fail_if_bad(result)
        hermes_home = dist / "hermes-home"
        smoke = """
import json
from pathlib import Path
from scope_recall import installer
home = Path(__import__('os').environ['SCOPE_RECALL_TEST_HOME'])
installed = installer.install(home)
verified = installer.verify(home)
assert installed['ok'] is True, installed
assert installed['installed'] is True, installed
assert verified['ok'] is True, verified
plugin_dir = home / 'plugins' / 'scope-recall'
assert (plugin_dir / 'plugin.yaml').is_file(), plugin_dir
assert (plugin_dir / 'provider.py').is_file(), plugin_dir
print(json.dumps({'plugin_dir': str(plugin_dir), 'version': verified['manifest_version']}, sort_keys=True))
"""
        env["SCOPE_RECALL_TEST_HOME"] = str(hermes_home)
        install_smoke = run([sys.executable, "-c", smoke], cwd=dist, env=env)
        fail_if_bad(install_smoke)
        install_payload = json.loads(str(install_smoke["stdout"]))
        plugin_dir = pathlib.Path(str(install_payload["plugin_dir"]))
        doctor = run(
            [
                sys.executable,
                str(plugin_dir / "scripts" / "doctor.py"),
                "--json",
                "--source-root",
                str(plugin_dir),
            ],
            cwd=dist,
            env=env,
        )
        fail_if_bad(doctor)
        doctor_payload = json.loads(str(doctor["stdout"]))
        if (
            not doctor_payload.get("ok")
            or doctor_payload.get("schema_version") != "doctor_report.v1"
            or doctor_payload.get("source", {}).get("pyproject_version") != PACKAGE_VERSION
        ):
            raise SystemExit(json.dumps({"doctor": doctor_payload}, ensure_ascii=False, indent=2))
        return {
            "wheel": wheels[0].name,
            "sdist": sdists[0].name,
            "file_count": len(names),
            "artifact_scan": artifact_scan,
            "import_stdout": str(result["stdout"]).strip(),
            "install_smoke": str(install_smoke["stdout"]).strip(),
            "doctor_smoke": json.dumps(
                {
                    "ok": doctor_payload.get("ok"),
                    "schema_version": doctor_payload.get("schema_version"),
                    "pyproject_version": doctor_payload.get("source", {}).get("pyproject_version"),
                    "plugin_version": doctor_payload.get("source", {}).get("plugin_version"),
                },
                sort_keys=True,
            ),
        }


def cleanup_generated() -> None:
    for pattern in ["__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist", "*.egg-info"]:
        for path in sorted(ROOT.rglob(pattern), key=lambda item: len(item.parts), reverse=True):
            if not path.exists():
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
    for path in ROOT.rglob("*.pyc"):
        path.unlink(missing_ok=True)


def main() -> int:
    """Run the full release gate and exit nonzero when any contract fails.

    The command is intentionally strict because it is used by CI, tag release workflows, and local pre-publish checks."""
    args = parse_args()
    progress("cleanup_generated:start")
    cleanup_generated()
    progress("environment:start")
    environment = release_environment_check()
    if not environment["ok"]:
        print(json.dumps({"ok": False, "environment": environment}, ensure_ascii=False, indent=2))
        return 1
    progress("git_tree:start")
    git_tree = git_tree_check(allow_dirty=bool(args.allow_dirty))
    progress("metadata:start")
    metadata = metadata_check()
    progress("live_dashboard:start")
    live_dashboard = live_dashboard_file_check(str(args.live_dashboard_json or ""), accept_stale=bool(args.accept_stale_live_waiver))
    # Scan release source before the multi-minute test/build stages. A known
    # sensitive identifier or private path should fail cheaply and must never be
    # hidden behind a later green wheel/install result.
    progress("scan:start")
    scan = scan_tree()
    blocking_scan = {key: value for key, value in scan.items() if value}
    preflight_failures: dict[str, object] = {}
    if not git_tree["ok"]:
        preflight_failures["git_tree"] = git_tree
    if not metadata["ok"]:
        preflight_failures["metadata"] = metadata
    if not live_dashboard["ok"]:
        preflight_failures["live_dashboard"] = live_dashboard
    if blocking_scan:
        preflight_failures["scan"] = blocking_scan
    if preflight_failures:
        progress("release_gate:failed")
        print(
            json.dumps(
                {
                    "ok": False,
                    "environment": environment,
                    "failures": preflight_failures,
                    "live_dashboard": live_dashboard,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    for stage, cmd in (
        ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
        ("pyright", [sys.executable, "-m", "pyright"]),
        ("pytest", [sys.executable, "-m", "pytest", "-q"]),
        ("compileall", [sys.executable, "-m", "compileall", "-q", "."]),
    ):
        progress(f"{stage}:start")
        fail_if_bad(run(cmd))
        progress(f"{stage}:done")
    progress("benchmark:start")
    benchmark = benchmark_check()
    if not benchmark["ok"]:
        print(json.dumps({"ok": False, "benchmark": benchmark}, ensure_ascii=False, indent=2))
        return 1
    progress("wheel:start")
    wheel = wheel_check()
    progress("cleanup_generated:final")
    cleanup_generated()
    progress("release_gate:done")
    print(
        json.dumps(
            {
                "ok": True,
                "environment": environment,
                "git_tree": git_tree,
                "metadata": metadata,
                "benchmark": benchmark,
                "wheel": wheel,
                "scan": scan,
                "live_dashboard": live_dashboard,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
