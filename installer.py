"""Install, upgrade, verify, and rollback helpers for copying Scope Recall into a Hermes home.

Installer operations are designed around dry-run evidence, backups, and explicit rollback commands."""

from __future__ import annotations

import argparse
import fnmatch
import importlib
import importlib.util
import json
import os
import shutil
import shlex
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
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


def _clear_runtime_verify_modules(package_name: str) -> None:
    for name in list(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            sys.modules.pop(name, None)


def _load_installed_package(plugin_dir: Path, *, package_name: str = "_scope_recall_runtime_verify") -> Any:
    init_file = plugin_dir / "__init__.py"
    if not init_file.is_file():
        raise InstallError(f"installed plugin is missing __init__.py: {plugin_dir}")
    _clear_runtime_verify_modules(package_name)
    spec = importlib.util.spec_from_file_location(package_name, init_file, submodule_search_locations=[str(plugin_dir)])
    if spec is None or spec.loader is None:
        raise InstallError(f"cannot build import spec for installed plugin: {plugin_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_verify(home: Path, plugin_dir: Path) -> dict[str, Any]:
    """Verify an installed Scope Recall copy against a Hermes home.

    Runtime verification checks importability and basic commands without performing upgrade or repair mutations."""
    payload: dict[str, Any] = {
        "requested": True,
        "provider_loaded": False,
        "config_schema_keys": [],
        "tool_schema_names": [],
        "sqlite_schema_current": False,
        "failures": [],
    }
    failures: list[str] = []
    package_name = "_scope_recall_runtime_verify"
    try:
        try:
            _load_installed_package(plugin_dir, package_name=package_name)
            provider_module = importlib.import_module(f"{package_name}.provider")
            provider = provider_module.ScopeRecallMemoryProvider()
            setattr(provider, "_hermes_home", home)
            payload["provider_loaded"] = True
            payload["config_schema_keys"] = [str(item.get("key") or "") for item in provider.get_config_schema()]
            tool_names = [str(schema.get("name") or "") for schema in provider.get_tool_schemas()]
            payload["tool_schema_names"] = tool_names
            required_tools = {
                "scope_recall_store",
                "scope_recall_search",
                "scope_recall_context",
                "scope_recall_profile",
                "scope_recall_memory",
                "scope_recall_entity",
            }
            missing_tools = sorted(required_tools - set(tool_names))
            if missing_tools:
                failures.append(f"runtime tool schemas missing compact defaults: {', '.join(missing_tools)}")
        except Exception as exc:
            failures.append(f"provider runtime load failed: {exc}")

        db_path = home / "scope-recall" / "memory.sqlite3"
        payload["sqlite_path"] = str(db_path)
        if not db_path.is_file():
            failures.append(f"SQLite truth DB missing: {db_path}; run `hermes memory setup` first")
        else:
            try:
                sql_store = importlib.import_module(f"{package_name}.sql_store")
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                try:
                    schema_status = sql_store.schema_migration_status(conn)
                finally:
                    conn.close()
                payload["schema_migrations"] = schema_status
                payload["sqlite_schema_current"] = bool(schema_status.get("current"))
                if not payload["sqlite_schema_current"]:
                    failures.append("SQLite schema migration ledger is not current")
            except Exception as exc:
                failures.append(f"SQLite runtime schema check failed: {exc}")
        payload["failures"] = failures
        payload["ok"] = not failures
        return payload
    finally:
        _clear_runtime_verify_modules(package_name)


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


def _copy_existing_plugin(source: Path, destination: Path) -> None:
    if source.is_symlink() or source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        shutil.copytree(source, destination, symlinks=True)


def _remove_existing_plugin(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _backup_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d.%H%M%S.%f")


def _backup_existing_plugin(home: Path, plugin_dir: Path, *, category: str) -> Path:
    backup_root = home / "backups" / category / f"{_backup_stamp()}.{uuid.uuid4().hex[:8]}"
    backup_root.mkdir(parents=True, exist_ok=False)
    backup_path = backup_root / PLUGIN_NAME
    _copy_existing_plugin(plugin_dir, backup_path)
    return backup_path


def _validate_backup_dir(backup_dir: Path) -> str:
    if not backup_dir.exists():
        return f"rollback backup missing: {backup_dir}"
    if not backup_dir.is_dir():
        return f"rollback backup is not a directory: {backup_dir}"
    if _read_manifest_name(backup_dir) != PLUGIN_NAME:
        return f"rollback backup plugin.yaml is not {PLUGIN_NAME}: {backup_dir}"
    missing = [rel for rel in REQUIRED_PLUGIN_FILES if not (backup_dir / rel).is_file()]
    if missing:
        return f"rollback backup missing required files: {', '.join(missing)}"
    return ""


def _rollback_command(home: Path, backup_path: str) -> str:
    if not backup_path:
        return ""
    return f"hermes-scope-recall rollback --hermes-home {_shell_quote_path(home)} --backup-dir {_shell_quote_path(Path(backup_path))}"


def _shell_quote_path(path: Path) -> str:
    return shlex.quote(str(path))


def _next_steps(home: Path) -> list[str]:
    quoted_home = _shell_quote_path(home)
    return [
        PROVIDER_CONFIG_COMMAND,
        "hermes memory setup",
        f"hermes-scope-recall verify --hermes-home {quoted_home}",
        "restart Hermes gateway/service to load the installed plugin copy",
        f"hermes-scope-recall doctor --hermes-home {quoted_home} --json",
    ]


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _verify_next_steps(
    home: Path,
    *,
    structural_ok: bool,
    runtime: bool,
    runtime_payload: dict[str, Any] | None,
) -> list[str]:
    steps: list[str] = []
    quoted_home = _shell_quote_path(home)
    if not structural_ok:
        _append_unique(steps, f"hermes-scope-recall install --hermes-home {quoted_home}")
        return steps
    if runtime and runtime_payload is not None:
        schema_status = runtime_payload.get("schema_migrations") if isinstance(runtime_payload.get("schema_migrations"), dict) else {}
        db_missing = not bool(runtime_payload.get("sqlite_path")) or not Path(str(runtime_payload.get("sqlite_path") or "")).is_file()
        if schema_status and not bool(schema_status.get("current")):
            _append_unique(steps, f"hermes-scope-recall migrate status --hermes-home {quoted_home}")
        if db_missing or not bool(runtime_payload.get("sqlite_schema_current")):
            _append_unique(steps, "hermes memory setup")
        if steps:
            _append_unique(steps, f"hermes-scope-recall verify --runtime --hermes-home {quoted_home}")
    return steps


def verify(hermes_home: str | os.PathLike[str] | None = None, *, runtime: bool = False) -> dict[str, Any]:
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
    runtime_payload: dict[str, Any] | None = None
    if runtime and not missing and manifest_name == PLUGIN_NAME and not failures:
        runtime_payload = _runtime_verify(home, plugin_dir)
        failures.extend(str(item) for item in runtime_payload.get("failures", []))
    ok = not missing and manifest_name == PLUGIN_NAME and not failures
    structural_ok = not missing and manifest_name == PLUGIN_NAME and not (failures and runtime_payload is None)
    next_steps = _verify_next_steps(home, structural_ok=structural_ok, runtime=runtime, runtime_payload=runtime_payload) if not ok else []
    payload = {
        "ok": ok,
        "hermes_home": str(home),
        "plugin_dir": str(plugin_dir),
        "missing": missing,
        "failures": failures,
        "manifest_name": manifest_name,
        "manifest_version": manifest_version,
        "runtime": runtime_payload or {"requested": bool(runtime)},
        "next_steps": next_steps,
    }
    return payload


def install(
    hermes_home: str | os.PathLike[str] | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Install or upgrade the plugin copy in a Hermes home with backup and rollback metadata.

    Writes are explicit, source paths are validated, and operator output includes enough evidence to reverse a bad copy."""
    home = resolve_hermes_home(hermes_home)
    source = source_root()
    target = plugin_dir_for(home)
    if not all((source / rel).is_file() for rel in REQUIRED_PLUGIN_FILES):
        missing = [rel for rel in REQUIRED_PLUGIN_FILES if not (source / rel).is_file()]
        raise InstallError(f"source tree is missing required plugin files: {', '.join(missing)}")

    previous_plugin_existed = target.exists() or target.is_symlink()
    previous_version = _read_manifest_version(target) if previous_plugin_existed else ""
    new_version = _read_manifest_version(source)
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "installed": False,
        "mode": "dry-run" if dry_run else "copy",
        "source_dir": str(source),
        "hermes_home": str(home),
        "plugin_dir": str(target),
        "manifest_version": new_version,
        "new_version": new_version,
        "previous_plugin_existed": previous_plugin_existed,
        "previous_version": previous_version,
        "backup_path": "",
        "rollback_command": "",
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
    backup_path = ""
    try:
        _copy_tree(source, staging)
        if target.exists() or target.is_symlink():
            backup = _backup_existing_plugin(home, target, category="scope-recall-installer")
            backup_path = str(backup)
            result["backup_path"] = backup_path
            result["rollback_command"] = _rollback_command(home, backup_path)
            _remove_existing_plugin(target)
        try:
            staging.rename(target)
        except Exception:
            if backup_path and not target.exists():
                _copy_existing_plugin(Path(backup_path), target)
            raise
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)

    result["installed"] = True
    result["verify"] = verify(home)
    result["ok"] = bool(result["verify"]["ok"])
    return result


def rollback(
    hermes_home: str | os.PathLike[str] | None = None,
    *,
    backup_dir: str | os.PathLike[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    home = resolve_hermes_home(hermes_home)
    target = plugin_dir_for(home)
    backup = Path(backup_dir).expanduser().resolve()
    error = _validate_backup_dir(backup)
    if error:
        raise InstallError(error)
    replaced_version = _read_manifest_version(target) if target.exists() or target.is_symlink() else ""
    restored_version = _read_manifest_version(backup)
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "restored": False,
        "hermes_home": str(home),
        "plugin_dir": str(target),
        "backup_dir": str(backup),
        "replaced_version": replaced_version,
        "restored_version": restored_version,
        "current_backup_path": "",
        "next_steps": _next_steps(home),
    }
    if dry_run:
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        current_backup = _backup_existing_plugin(home, target, category="scope-recall-rollback-current")
        result["current_backup_path"] = str(current_backup)
        _remove_existing_plugin(target)
    try:
        _copy_existing_plugin(backup, target)
    except Exception:
        current_backup_path = str(result.get("current_backup_path") or "")
        if current_backup_path and not target.exists():
            _copy_existing_plugin(Path(current_backup_path), target)
        raise
    result["restored"] = True
    result["verify"] = verify(home)
    result["ok"] = bool(result["verify"].get("ok"))
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

    upgrade_parser = sub.add_parser("upgrade", help="upgrade scope-recall and back up the existing plugin copy first")
    upgrade_parser.add_argument("--hermes-home", help="target Hermes home; defaults to HERMES_HOME or ~/.hermes")
    upgrade_parser.add_argument("--dry-run", action="store_true", help="show what would be upgraded without mutating files")
    upgrade_parser.add_argument("--force", action="store_true", help="replace an existing non-scope-recall directory")
    upgrade_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")

    rollback_parser = sub.add_parser("rollback", help="restore a previous scope-recall plugin backup")
    rollback_parser.add_argument("--hermes-home", help="target Hermes home; defaults to HERMES_HOME or ~/.hermes")
    rollback_parser.add_argument("--backup-dir", required=True, help="Backup plugin directory returned by install/upgrade")
    rollback_parser.add_argument("--dry-run", action="store_true", help="validate rollback without mutating files")
    rollback_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")

    verify_parser = sub.add_parser("verify", help="verify scope-recall is installed in a Hermes home")
    verify_parser.add_argument("--hermes-home", help="target Hermes home; defaults to HERMES_HOME or ~/.hermes")
    verify_parser.add_argument("--runtime", action="store_true", help="also load the installed provider and read the SQLite schema ledger")
    verify_parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"install", "upgrade"}:
            payload = install(args.hermes_home, dry_run=args.dry_run, force=args.force)
            if args.command == "upgrade":
                payload["mode"] = "upgrade-dry-run" if args.dry_run else ("already-installed" if payload.get("mode") == "already-installed" else "upgrade")
            _print_payload(payload, as_json=args.json)
            return 0 if payload["ok"] else 1
        if args.command == "rollback":
            payload = rollback(args.hermes_home, backup_dir=args.backup_dir, dry_run=args.dry_run)
            _print_payload(payload, as_json=args.json)
            return 0 if payload["ok"] else 1
        if args.command == "verify":
            payload = verify(args.hermes_home, runtime=args.runtime)
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
