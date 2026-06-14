#!/usr/bin/env python3
"""Release-readiness checks for scope-recall.

This script runs local checks that are useful immediately before committing or
publishing the plugin. It deliberately avoids reading secrets from the user's
Hermes runtime environment; it scans only this source tree.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PACKAGE_VERSION = "1.3.0"
WHEEL_DIST_PREFIX = f"hermes_scope_recall-{PACKAGE_VERSION}"
GENERATED_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist", ".venv"}
EXTERNAL_TEST_DIRS = {".hermes-agent-src"}
SECRET_PATTERNS = {
    "api_key_assignment": re.compile(r"(api_key|secret|password|passwd|token)\s*=\s*['\"][A-Za-z0-9._\-+/=]{12,}['\"]", re.I),
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
    "docs/hermes-upstream-recommendation-plan.md",
    "scripts/import.openclaw.memory_lancedb_pro.py",
    "scripts/nightly-digest.py",
    "scripts/journal-digest.py",
    "scripts/repair.vector_index.py",
    "scripts/report.hygiene.py",
    "scripts/migrate.legacy_hygiene.py",
    "scripts/doctor.py",
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
    "scope_recall/docs/hermes-upstream-recommendation-plan.md",
    "scope_recall/scripts/import.openclaw.memory_lancedb_pro.py",
    "scope_recall/scripts/nightly-digest.py",
    "scope_recall/scripts/journal-digest.py",
    "scope_recall/scripts/repair.vector_index.py",
    "scope_recall/scripts/report.hygiene.py",
    "scope_recall/scripts/migrate.legacy_hygiene.py",
    "scope_recall/scripts/doctor.py",
}
STABLE_TOOL_NAMES = {
    "scope_recall_store",
    "scope_recall_store_secret_index",
    "scope_recall_search",
    "scope_recall_context",
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
}


def run(cmd: list[str], *, cwd: pathlib.Path = ROOT, env: dict[str, str] | None = None) -> dict[str, object]:
    proc = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
    return {"cmd": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def fail_if_bad(result: dict[str, object]) -> None:
    if result["returncode"] != 0:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(int(result["returncode"]))


def read_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def redact_sensitive(text: object) -> str:
    from scope_recall.http_utils import redact_sensitive as _redact_sensitive

    return _redact_sensitive(text)


def scan_tree() -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {"generated_artifacts": [], "secrets": [], "private_paths": []}
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if ".git" in rel.parts:
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
        for name, rx in SECRET_PATTERNS.items():
            for match in rx.finditer(text):
                line_no = text[: match.start()].count("\n") + 1
                findings["secrets"].append(f"{rel}:{line_no}: {name}: {redact_sensitive(match.group(0))}")
        private_markers = ("".join(("/home/", "a/", ".hermes-yuheng")), "".join(("/home/", "a/")))
        if any(marker in text for marker in private_markers):
            findings["private_paths"].append(str(rel))
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
    }
    searchable = "\n".join([pyproject, plugin, readme, changelog, stability])
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
    cleanup_generated()
    metadata = metadata_check()
    for cmd in (
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "pytest", "-q"],
        [sys.executable, "-m", "compileall", "-q", "."],
    ):
        fail_if_bad(run(cmd))
    wheel = wheel_check()
    cleanup_generated()
    scan = scan_tree()
    blocking_scan = {key: value for key, value in scan.items() if value}
    failures: dict[str, object] = {}
    if not metadata["ok"]:
        failures["metadata"] = metadata
    if blocking_scan:
        failures["scan"] = blocking_scan
    if failures:
        print(json.dumps({"ok": False, "failures": failures, "wheel": wheel}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "metadata": metadata, "wheel": wheel, "scan": scan}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
