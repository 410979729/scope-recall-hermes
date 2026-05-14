#!/usr/bin/env python3
from __future__ import annotations

"""Release-readiness checks for scope-recall.

This script runs local checks that are useful immediately before committing or
publishing the plugin. It deliberately avoids reading secrets from the user's
Hermes runtime environment; it scans only this source tree.
"""

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
GENERATED_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
SECRET_PATTERNS = {
    "api_key_assignment": re.compile(r"(api_key|secret|password|passwd|token)\s*=\s*['\"][A-Za-z0-9._\-+/=]{12,}['\"]", re.I),
    "bearer_literal": re.compile(r"bearer\s+[A-Za-z0-9._\-~+/=]{16,}", re.I),
    "github_pat": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "openai_style": re.compile(r"sk-[A-Za-z0-9]{20,}"),
}
REQUIRED_WHEEL = {
    "scope_recall/__init__.py",
    "scope_recall/provider.py",
    "scope_recall-0.2.0.data/data/plugin.yaml",
    "scope_recall-0.2.0.data/data/config.json",
    "scope_recall-0.2.0.data/data/README.md",
    "scope_recall-0.2.0.data/data/DESIGN.md",
    "scope_recall-0.2.0.data/data/CHANGELOG.md",
    "scope_recall-0.2.0.data/data/.env.example",
    "scope_recall-0.2.0.data/data/docs/migration.md",
    "scope_recall-0.2.0.data/data/docs/differences-from-memory-lancedb-pro.md",
    "scope_recall-0.2.0.data/data/scripts/import.openclaw.memory_lancedb_pro.py",
    "scope_recall-0.2.0.data/data/scripts/repair.vector_index.py",
}


def run(cmd: list[str], *, cwd: pathlib.Path = ROOT, env: dict[str, str] | None = None) -> dict[str, object]:
    proc = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
    return {"cmd": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def fail_if_bad(result: dict[str, object]) -> None:
    if result["returncode"] != 0:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(int(result["returncode"]))


def scan_tree() -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {"generated_artifacts": [], "secrets": [], "private_paths": []}
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in GENERATED_DIRS for part in rel.parts):
            continue
        if rel.match("review-report.*.md") or rel.name == ".env":
            continue
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name, rx in SECRET_PATTERNS.items():
            for match in rx.finditer(text):
                findings["secrets"].append(f"{rel}: {name}: {match.group(0)[:80]}")
        private_markers = ("".join(("/home/", "a/", ".hermes-yuheng")), "".join(("/home/", "a/")))
        if any(marker in text for marker in private_markers):
            findings["private_paths"].append(str(rel))
    findings["generated_artifacts"] = sorted(set(findings["generated_artifacts"]))
    return findings


def wheel_check() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="scope.recall.dist.") as tmp:
        dist = pathlib.Path(tmp)
        result = run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(dist)])
        fail_if_bad(result)
        wheels = list(dist.glob("scope_recall-*.whl"))
        if len(wheels) != 1:
            raise SystemExit(f"expected one wheel, found {wheels}")
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
        return {"wheel": wheels[0].name, "file_count": len(names), "import_stdout": result["stdout"].strip()}


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
    cleanup_generated()
    for cmd in ([sys.executable, "-m", "pytest", "-q"], [sys.executable, "-m", "compileall", "-q", "."]):
        fail_if_bad(run(cmd))
    wheel = wheel_check()
    scan = scan_tree()
    blocking_scan = {key: value for key, value in scan.items() if value}
    cleanup_generated()
    if blocking_scan:
        print(json.dumps({"ok": False, "scan": blocking_scan, "wheel": wheel}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "wheel": wheel, "scan": scan}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
