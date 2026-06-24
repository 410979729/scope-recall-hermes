#!/usr/bin/env python3
"""Release-readiness checks for scope-recall.

This script runs local checks that are useful immediately before committing or
publishing the plugin. It deliberately avoids reading secrets from the user's
Hermes runtime environment; it scans only this source tree.
"""

from __future__ import annotations

import argparse
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

PACKAGE_VERSION = "1.5.1"
WHEEL_DIST_PREFIX = f"hermes_scope_recall-{PACKAGE_VERSION}"
GENERATED_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist", ".venv"}
LOCAL_ONLY_DIRS = {".hermes"}
EXTERNAL_TEST_DIRS = {".hermes-agent-src"}
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
    "benchmarks/golden_recall_cases.json",
    "scripts/import.openclaw.memory_lancedb_pro.py",
    "scripts/nightly-digest.py",
    "scripts/journal-digest.py",
    "scripts/repair.vector_index.py",
    "scripts/report.hygiene.py",
    "scripts/migrate.legacy_hygiene.py",
    "scripts/doctor.py",
    "scripts/experience-replay.py",
    "scripts/benchmark.golden.py",
    "scripts/governance.cleanup.py",
    "scripts/journal.recovery.py",
    "scripts/report.dashboard.py",
    "experience_models.py",
    "experience_store.py",
    "experience_preflight.py",
    "experience_replay.py",
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
    "scope_recall/experience_models.py",
    "scope_recall/experience_store.py",
    "scope_recall/experience_preflight.py",
    "scope_recall/experience_replay.py",
    "scope_recall/experience_promotion.py",
    "scope_recall/forgetting.py",
    "scope_recall/governance_cleanup.py",
    "scope_recall/journal_recovery.py",
    "scope_recall/hygiene.py",
    "scope_recall/journal.py",
    "scope_recall/nightly_digest.py",
    "scope_recall/sqlite_vector_store.py",
    "scope_recall/py.typed",
    "scope_recall/pyproject.toml",
    "scope_recall/plugin.yaml",
    "scope_recall/config.json",
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
    "scope_recall/benchmarks/golden_recall_cases.json",
    "scope_recall/scripts/import.openclaw.memory_lancedb_pro.py",
    "scope_recall/scripts/nightly-digest.py",
    "scope_recall/scripts/journal-digest.py",
    "scope_recall/scripts/repair.vector_index.py",
    "scope_recall/scripts/report.hygiene.py",
    "scope_recall/scripts/migrate.legacy_hygiene.py",
    "scope_recall/scripts/doctor.py",
    "scope_recall/scripts/experience-replay.py",
    "scope_recall/scripts/benchmark.golden.py",
    "scope_recall/scripts/governance.cleanup.py",
    "scope_recall/scripts/journal.recovery.py",
    "scope_recall/scripts/report.dashboard.py",
}
STABLE_TOOL_NAMES = {
    "scope_recall_store",
    "scope_recall_store_secret_index",
    "scope_recall_search",
    "scope_recall_context",
    "scope_recall_profile",
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
        "ok": bool(payload.get("passed")),
        "golden_name": payload.get("golden_name"),
        "query_count": payload.get("query_count"),
        "failures": payload.get("failures"),
    }


def read_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


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


def metadata_check() -> dict[str, object]:
    pyproject = read_text("pyproject.toml")
    plugin = read_text("plugin.yaml")
    readme = read_text("README.md")
    changelog = read_text("CHANGELOG.md")
    stability = read_text("docs/stability.md")
    schemas = read_text("schemas.py")

    missing_source = sorted(rel for rel in REQUIRED_SOURCE_FILES if not (ROOT / rel).is_file())
    failures: list[str] = []
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
    for tool_name in STABLE_TOOL_NAMES:
        if tool_name not in stability:
            failures.append(f"stable tool missing from stability doc: {tool_name}")
        if tool_name.upper() not in schemas.upper():
            failures.append(f"stable tool missing from schemas.py: {tool_name}")
    return {"ok": not missing_source and not failures, "missing_source": missing_source, "failures": failures}


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
        if not doctor_payload.get("ok") or doctor_payload.get("source", {}).get("pyproject_version") != PACKAGE_VERSION:
            raise SystemExit(json.dumps({"doctor": doctor_payload}, ensure_ascii=False, indent=2))
        return {
            "wheel": wheels[0].name,
            "file_count": len(names),
            "import_stdout": str(result["stdout"]).strip(),
            "install_smoke": str(install_smoke["stdout"]).strip(),
            "doctor_smoke": json.dumps(
                {
                    "ok": doctor_payload.get("ok"),
                    "pyproject_version": doctor_payload.get("source", {}).get("pyproject_version"),
                    "plugin_version": doctor_payload.get("source", {}).get("plugin_version"),
                },
                sort_keys=True,
            ),
        }


def cleanup_generated() -> None:
    for pattern in ["__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist", "*.egg-info"]:
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
    git_tree = git_tree_check(allow_dirty=bool(args.allow_dirty))
    metadata = metadata_check()
    for cmd in (
        [sys.executable, "-m", "ruff", "check", "."],
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
    if blocking_scan:
        failures["scan"] = blocking_scan
    if failures:
        print(json.dumps({"ok": False, "failures": failures, "benchmark": benchmark, "wheel": wheel}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "git_tree": git_tree, "metadata": metadata, "benchmark": benchmark, "wheel": wheel, "scan": scan}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
