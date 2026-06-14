from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

PLUGIN_NAME = "scope-recall"
PROVIDER_CONFIG_COMMAND = f"hermes config set memory.provider {PLUGIN_NAME}"
REQUIRED_PLUGIN_FILES = (
    "__init__.py",
    "provider.py",
    "plugin.yaml",
    "config.json",
)
_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hermes-agent-src",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "tests",
    "venv",
    "lancedb",
    "lancepro",
    "scope-recall",
    "backups",
    "htmlcov",
}
_EXCLUDED_FILE_GLOBS = (
    "*.pyc",
    "*.pyo",
    "*.sqlite3",
    "*.sqlite3-*",
    "*.egg-info",
    ".coverage",
    ".env",
    ".env.*",
    "review-report.*.md",
)


class InstallError(RuntimeError):
    """Raised when the scope-recall installer cannot safely proceed."""


def _platform_default_hermes_home() -> Path:
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def resolve_hermes_home(hermes_home: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the target Hermes home without importing Hermes runtime code."""
    raw = str(hermes_home or os.environ.get("HERMES_HOME") or _platform_default_hermes_home())
    return Path(raw).expanduser().resolve()


def source_root() -> Path:
    """Return the package/plugin source tree copied into Hermes plugins."""
    return Path(__file__).resolve().parent


def plugin_dir_for(hermes_home: str | os.PathLike[str] | None = None) -> Path:
    return resolve_hermes_home(hermes_home) / "plugins" / PLUGIN_NAME


def _read_manifest_name(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if not manifest.exists():
        return ""
    for raw_line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def _read_manifest_version(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if not manifest.exists():
        return ""
    for raw_line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def _has_discovery_marker(plugin_dir: Path) -> bool:
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        return False
    source = init_file.read_text(encoding="utf-8", errors="replace")[:8192]
    return "register_memory_provider" in source or "MemoryProvider" in source


def _is_same_tree(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _should_skip_entry(directory: str, name: str) -> bool:
    candidate = Path(directory) / name
    if candidate.is_symlink():
        return True
    if name in _EXCLUDED_DIR_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in _EXCLUDED_FILE_GLOBS)


def _copy_tree(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {name for name in names if _should_skip_entry(directory, name)}

    shutil.copytree(source, destination, ignore=ignore, symlinks=False)
    for pyc in destination.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)
    for cache in destination.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _shell_quote_path(path: Path) -> str:
    return shlex.quote(str(path))


def _next_steps(home: Path) -> list[str]:
    return [
        PROVIDER_CONFIG_COMMAND,
        "hermes memory setup",
        f"hermes-scope-recall verify --hermes-home {_shell_quote_path(home)}",
    ]


def verify(hermes_home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    home = resolve_hermes_home(hermes_home)
    plugin_dir = plugin_dir_for(home)
    missing = [rel for rel in REQUIRED_PLUGIN_FILES if not (plugin_dir / rel).is_file()]
    manifest_name = _read_manifest_name(plugin_dir)
    manifest_version = _read_manifest_version(plugin_dir)
    failures: list[str] = []
    if manifest_name and manifest_name != PLUGIN_NAME:
        failures.append(f"plugin.yaml name is {manifest_name!r}, expected {PLUGIN_NAME!r}")
    if not missing and not _has_discovery_marker(plugin_dir):
        failures.append("__init__.py discovery marker")
    ok = not missing and manifest_name == PLUGIN_NAME and not failures
    return {
        "ok": ok,
        "hermes_home": str(home),
        "plugin_dir": str(plugin_dir),
        "missing": missing,
        "failures": failures,
        "manifest_name": manifest_name,
        "manifest_version": manifest_version,
        "next_steps": [] if ok else [f"hermes-scope-recall install --hermes-home {_shell_quote_path(home)}"],
    }


def install(
    hermes_home: str | os.PathLike[str] | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    home = resolve_hermes_home(hermes_home)
    source = source_root()
    target = plugin_dir_for(home)
    if not all((source / rel).is_file() for rel in REQUIRED_PLUGIN_FILES):
        missing = [rel for rel in REQUIRED_PLUGIN_FILES if not (source / rel).is_file()]
        raise InstallError(f"source tree is missing required plugin files: {', '.join(missing)}")

    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "installed": False,
        "mode": "dry-run" if dry_run else "copy",
        "source_dir": str(source),
        "hermes_home": str(home),
        "plugin_dir": str(target),
        "manifest_version": _read_manifest_version(source),
        "next_steps": _next_steps(home),
    }
    if dry_run:
        return result

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if _is_same_tree(source, target):
            result["mode"] = "already-installed"
            result["verify"] = verify(home)
            result["ok"] = bool(result["verify"]["ok"])
            return result
        existing_name = _read_manifest_name(target)
        if not force and existing_name != PLUGIN_NAME:
            detail = f"manifest name: {existing_name!r}" if existing_name else "missing or unreadable scope-recall manifest"
            raise InstallError(
                f"refusing to overwrite existing target at {target} ({detail}); "
                "pass --force to replace it"
            )

    staging_root = Path(tempfile.mkdtemp(prefix="scope.recall.install.", dir=str(target.parent)))
    staging = staging_root / PLUGIN_NAME
    try:
        _copy_tree(source, staging)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
        staging.rename(target)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)

    result["installed"] = True
    result["verify"] = verify(home)
    result["ok"] = bool(result["verify"]["ok"])
    return result


def _print_payload(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    status = "ok" if payload.get("ok") else "error"
    print(f"scope-recall {status}")
    for key in ("hermes_home", "plugin_dir", "manifest_version", "mode"):
        if key in payload and payload[key]:
            print(f"{key}: {payload[key]}")
    missing = payload.get("missing") or []
    if missing:
        print("missing:")
        for item in missing:
            print(f"- {item}")
    next_steps = payload.get("next_steps") or []
    if next_steps:
        print("next steps:")
        for item in next_steps:
            print(f"- {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-scope-recall",
        description="Install or verify the scope-recall Hermes memory provider plugin.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    install_parser = sub.add_parser("install", help="copy scope-recall into a Hermes home plugins directory")
    install_parser.add_argument("--hermes-home", help="target Hermes home; defaults to HERMES_HOME or ~/.hermes")
    install_parser.add_argument("--dry-run", action="store_true", help="show what would be installed without mutating files")
    install_parser.add_argument("--force", action="store_true", help="replace an existing non-scope-recall directory")
    install_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")

    verify_parser = sub.add_parser("verify", help="verify scope-recall is installed in a Hermes home")
    verify_parser.add_argument("--hermes-home", help="target Hermes home; defaults to HERMES_HOME or ~/.hermes")
    verify_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            payload = install(args.hermes_home, dry_run=args.dry_run, force=args.force)
            _print_payload(payload, as_json=args.json)
            return 0 if payload["ok"] else 1
        if args.command == "verify":
            payload = verify(args.hermes_home)
            _print_payload(payload, as_json=args.json)
            return 0 if payload["ok"] else 1
    except InstallError as exc:
        payload = {"ok": False, "error": str(exc)}
        _print_payload(payload, as_json=getattr(args, "json", False))
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
