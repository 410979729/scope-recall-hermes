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
import tempfile
import zipfile

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PACKAGE_VERSION = "1.6.0"
WHEEL_DIST_PREFIX = f"hermes_scope_recall-{PACKAGE_VERSION}"
RELEASE_READINESS_DOC = f"docs/release-readiness.{PACKAGE_VERSION}.md"
GENERATED_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist", ".venv"}
LOCAL_ONLY_DIRS = {".hermes"}
EXTERNAL_TEST_DIRS = {".hermes-agent-src"}
RELEASE_REQUIRED_MODULES = ("pytest", "ruff", "wheel", "pyright", "lancedb", "pyarrow")
SECRET_PATTERNS = {
    "api_key_assignment": re.compile(
        r"[\"']?\b(?:api[_ -]?key|secret|password|passwd|token)\b[\"']?\s*(?:=|:)\s*[\"']?[A-Za-z0-9._\-+/=]{12,}[\"']?",
        re.I,
    ),
    "bearer_literal": re.compile(r"bearer\s+[A-Za-z0-9._\-~+/=]{16,}", re.I),
    "github_pat": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "openai_style": re.compile(r"sk-[A-Za-z0-9]{20,}"),
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
    "digest_quality.py",
    "digest_run_results.py",
    "doctor_common.py",
    "doctor_experience.py",
    "doctor_journal.py",
    "doctor_source.py",
    "doctor_sqlite.py",
    "doctor_vector.py",
    "freshness.py",
    "graph_hygiene.py",
    "maintenance_ops.py",
    "memory_quality.py",
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
    "docs/hermes-upstream-recommendation-plan.md",
    "docs/benchmark.golden.md",
    "docs/governance.cleanup.md",
    "docs/configuration.md",
    "docs/operator-runbook.md",
    "docs/cross-profile-rollout.md",
    "docs/response-contracts.md",
    RELEASE_READINESS_DOC,
    "benchmarks/golden_recall_cases.json",
    "benchmarks/experience_replay_cases.json",
    "examples/external_bridge/import.jsonl",
    "examples/external_bridge/export.jsonl",
    "examples/external_bridge/conflict_resolution.jsonl",
    "scripts/import.openclaw.memory_lancedb_pro.py",
    "scripts/nightly-digest.py",
    "scripts/journal-digest.py",
    "scripts/repair.vector_index.py",
    "scripts/report.hygiene.py",
    "scripts/migrate.legacy_hygiene.py",
    "scripts/migrate.status.py",
    "scripts/doctor.py",
    "scripts/experience-replay.py",
    "scripts/benchmark.golden.py",
    "scripts/benchmark.retrieval_regression.py",
    "scripts/governance.cleanup.py",
    "scripts/governance.audit_coverage.py",
    "scripts/journal.recovery.py",
    "scripts/playbook.bootstrap.py",
    "scripts/playbooks.py",
    "scripts/report.dashboard.py",
    "scripts/rollout.profiles.py",
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
    "scope_recall/graph_hygiene.py",
    "scope_recall/maintenance_ops.py",
    "scope_recall/memory_quality.py",
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
    "scope_recall/docs/hermes-upstream-recommendation-plan.md",
    "scope_recall/docs/benchmark.golden.md",
    "scope_recall/docs/governance.cleanup.md",
    "scope_recall/docs/configuration.md",
    "scope_recall/docs/operator-runbook.md",
    "scope_recall/docs/cross-profile-rollout.md",
    "scope_recall/docs/response-contracts.md",
    f"scope_recall/{RELEASE_READINESS_DOC}",
    "scope_recall/benchmarks/golden_recall_cases.json",
    "scope_recall/benchmarks/experience_replay_cases.json",
    "scope_recall/examples/external_bridge/import.jsonl",
    "scope_recall/examples/external_bridge/export.jsonl",
    "scope_recall/examples/external_bridge/conflict_resolution.jsonl",
    "scope_recall/scripts/import.openclaw.memory_lancedb_pro.py",
    "scope_recall/scripts/nightly-digest.py",
    "scope_recall/scripts/journal-digest.py",
    "scope_recall/scripts/repair.vector_index.py",
    "scope_recall/scripts/report.hygiene.py",
    "scope_recall/scripts/migrate.legacy_hygiene.py",
    "scope_recall/scripts/migrate.status.py",
    "scope_recall/scripts/doctor.py",
    "scope_recall/scripts/experience-replay.py",
    "scope_recall/scripts/benchmark.golden.py",
    "scope_recall/scripts/benchmark.retrieval_regression.py",
    "scope_recall/scripts/governance.cleanup.py",
    "scope_recall/scripts/governance.audit_coverage.py",
    "scope_recall/scripts/journal.recovery.py",
    "scope_recall/scripts/playbook.bootstrap.py",
    "scope_recall/scripts/playbooks.py",
    "scope_recall/scripts/report.dashboard.py",
    "scope_recall/scripts/rollout.profiles.py",
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


def run(cmd: list[str], *, cwd: pathlib.Path = ROOT, env: dict[str, str] | None = None) -> dict[str, object]:
    proc = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
    return {"cmd": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def fail_if_bad(result: dict[str, object]) -> None:
    if result["returncode"] != 0:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(int(result["returncode"]))


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


def benchmark_check() -> dict[str, object]:
    result = run([sys.executable, "scripts/benchmark.golden.py"])
    if result["returncode"] != 0:
        return {"ok": False, "result": result}
    try:
        payload = json.loads(str(result["stdout"] or "{}"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid benchmark json: {exc}", "result": result}
    return {
        "ok": bool(payload.get("passed")) and payload.get("schema_version") == "golden_benchmark_report.v1",
        "schema_version": payload.get("schema_version"),
        "golden_name": payload.get("golden_name"),
        "query_count": payload.get("query_count"),
        "failures": payload.get("failures"),
    }


def read_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


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
        lines = text.splitlines()
        for name, rx in SECRET_PATTERNS.items():
            for match in rx.finditer(text):
                if name == "api_key_assignment" and not _looks_like_release_secret(match.group(0)):
                    continue
                line_no = text[: match.start()].count("\n") + 1
                line = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else match.group(0)
                if _is_synthetic_test_fixture_line(rel, line):
                    continue
                findings["secrets"].append(f"{rel}:{line_no}: {name}: {redact_sensitive(match.group(0))}")
        home = pathlib.Path.home()
        private_markers = tuple(
            marker
            for marker in {
                str(home / ".hermes-yuheng"),
                str(home) + os.sep,
            }
            if marker and marker != os.sep
        )
        private_path_lines: list[int] = []
        for line_no, line in enumerate(lines, 1):
            if any(marker in line for marker in private_markers) and not _is_synthetic_test_fixture_line(rel, line):
                private_path_lines.append(line_no)
        if private_path_lines:
            findings["private_paths"].append(f"{rel}:{private_path_lines[0]}")
    findings["generated_artifacts"] = sorted(set(findings["generated_artifacts"]))
    return findings


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
        "live dashboard waiver": "Live dashboard waiver",
        "dashboard degraded status": "severity=DEGRADED",
        "auth dead-letter evidence": "dead-letter:auth",
        "release owner": "Owner:",
        "waiver clearance": "Clearance condition:",
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
    return {
        "ok": not missing_source and not failures,
        "missing_source": missing_source,
        "failures": failures,
        "product_contract": product_contract,
    }


def wheel_check() -> dict[str, object]:
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
        missing = sorted(item for item in REQUIRED_WHEEL if item not in names)
        pycache = sorted(name for name in names if "__pycache__" in name or name.endswith(".pyc"))
        if missing or pycache:
            raise SystemExit(json.dumps({"missing": missing, "pycache": pycache}, ensure_ascii=False, indent=2))

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
            "file_count": len(names),
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
    args = parse_args()
    cleanup_generated()
    environment = release_environment_check()
    if not environment["ok"]:
        print(json.dumps({"ok": False, "environment": environment}, ensure_ascii=False, indent=2))
        return 1
    git_tree = git_tree_check(allow_dirty=bool(args.allow_dirty))
    metadata = metadata_check()
    live_dashboard = live_dashboard_file_check(str(args.live_dashboard_json or ""), accept_stale=bool(args.accept_stale_live_waiver))
    for cmd in (
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "pyright"],
        [sys.executable, "-m", "pytest", "-q"],
        [sys.executable, "-m", "compileall", "-q", "."],
    ):
        fail_if_bad(run(cmd))
    benchmark = benchmark_check()
    if not benchmark["ok"]:
        print(json.dumps({"ok": False, "benchmark": benchmark}, ensure_ascii=False, indent=2))
        return 1
    wheel = wheel_check()
    cleanup_generated()
    scan = scan_tree()
    blocking_scan = {key: value for key, value in scan.items() if value}
    failures: dict[str, object] = {}
    if not git_tree["ok"]:
        failures["git_tree"] = git_tree
    if not metadata["ok"]:
        failures["metadata"] = metadata
    if not live_dashboard["ok"]:
        failures["live_dashboard"] = live_dashboard
    if blocking_scan:
        failures["scan"] = blocking_scan
    if failures:
        print(
            json.dumps(
                {
                    "ok": False,
                    "environment": environment,
                    "failures": failures,
                    "benchmark": benchmark,
                    "wheel": wheel,
                    "live_dashboard": live_dashboard,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
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
