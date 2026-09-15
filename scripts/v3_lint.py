"""Run static checks only over the current v3 release allowlist."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "packaging" / "v11-module-allowlist.json"


def allowed_python_files() -> list[Path]:
    payload = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    entries = payload.get("python_modules")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("v3 allowlist has no python_modules")
    files: list[Path] = []
    for entry in entries:
        if type(entry) is not str or not entry.endswith(".py"):
            raise RuntimeError(f"invalid allowlist module: {entry!r}")
        path = (ROOT / entry).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise RuntimeError(f"allowlist module is missing or escapes root: {entry!r}")
        files.append(path)
    return files


def _pyright_snapshot(files: list[Path]) -> tuple[Path, list[Path], tempfile.TemporaryDirectory[str]]:
    """Build a clean package-dir snapshot for Pyright's import graph.

    The repository uses setuptools' ``package-dir = {"scope_recall" = "."}``
    layout.  Running Pyright directly against the checkout makes imports such
    as ``scope_recall.core`` resolve as a second top-level module graph.  A
    temporary package snapshot models the installed layout without mutating
    the checkout or relying on a stale build directory.
    """
    execution = ROOT / ".execution"
    execution.mkdir(exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix="TEST-v3-pyright-", dir=execution)
    work = Path(temp.name)
    package = work / "scope_recall"
    staged: list[Path] = []
    for source in files:
        relative = source.relative_to(ROOT)
        destination = package / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        staged.append(destination)
    config = {
        "include": ["scope_recall"],
        "exclude": ["**/__pycache__", "**/.pytest_cache"],
        "pythonVersion": "3.11",
        "typeCheckingMode": "basic",
        "reportMissingImports": "error",
        "reportMissingTypeStubs": "none",
    }
    (work / "pyrightconfig.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return work, staged, temp


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint only the current v3 package allowlist")
    parser.add_argument("--tool", choices=("ruff", "pyright"), required=True)
    parser.add_argument("--json", action="store_true", help="Ask Pyright for structured JSON output")
    args = parser.parse_args()
    files = allowed_python_files()
    if args.tool == "ruff":
        # Ruff 0.12+ requires an explicit ``check`` subcommand.
        command = [sys.executable, "-m", "ruff", "check", *map(str, files)]
        return subprocess.run(command, cwd=ROOT).returncode

    work, staged, temp = _pyright_snapshot(files)
    try:
        # Pyright accepts file paths directly; the temporary package root and
        # config make every import resolve through one current module graph.
        command = [sys.executable, "-m", "pyright"]
        if args.json:
            command.append("--outputjson")
        command.extend(map(str, staged))
        return subprocess.run(command, cwd=work).returncode
    finally:
        temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
