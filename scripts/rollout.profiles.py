#!/usr/bin/env python3
"""Cross-profile rollout helper for scope-recall.

Default mode is dry-run/inventory only.  Mutating rollout and rollback both
require ``--apply``.  The script operates on Hermes profile homes under a
profiles root (default: ``~/.hermes/profiles``), backs up an existing
``plugins/scope-recall`` directory before installing, and writes a receipt that
can be used for rollback.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "scope_recall_rollout_runtime"
if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load scope-recall package from {PLUGIN_ROOT}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)

from scope_recall_rollout_runtime import installer  # noqa: E402

PLUGIN_NAME = "scope-recall"


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d.%H%M%S.%f")


def read_manifest_name(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if not manifest.is_file():
        return ""
    for raw_line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def read_manifest_version(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if not manifest.is_file():
        return ""
    for raw_line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def read_config_summary(profile_home: Path) -> dict[str, Any]:
    config = profile_home / "config.yaml"
    if not config.is_file():
        return {"exists": False, "memory_provider": ""}
    text = config.read_text(encoding="utf-8", errors="replace")[:100_000]
    provider = "scope-recall" if "scope-recall" in text else ""
    return {"exists": True, "memory_provider": provider}


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def validate_plugin_backup(backup_path: Path) -> str:
    try:
        backup = backup_path.resolve()
    except OSError as exc:
        return f"rollback backup cannot be resolved: {exc}"
    if not backup.exists():
        return f"rollback backup missing: {backup_path}"
    if not backup.is_dir():
        return f"rollback backup is not a directory: {backup_path}"
    if read_manifest_name(backup) != PLUGIN_NAME:
        return f"rollback backup plugin.yaml is not {PLUGIN_NAME}: {backup_path}"
    for required in ("__init__.py", "provider.py", "config.json"):
        if not (backup / required).is_file():
            return f"rollback backup missing required file {required}: {backup_path}"
    return ""


def profile_homes(profiles_root: Path, selected: list[str] | None = None) -> list[Path]:
    selected_set = {item for item in selected or [] if item}
    if not profiles_root.exists():
        return []
    homes = [path for path in sorted(profiles_root.iterdir(), key=lambda item: item.name) if path.is_dir()]
    if selected_set:
        homes = [path for path in homes if path.name in selected_set]
    return homes


def inventory_profile(profile_home: Path) -> dict[str, Any]:
    plugin_dir = profile_home / "plugins" / PLUGIN_NAME
    verify = installer.verify(profile_home, runtime=False)
    return {
        "name": profile_home.name,
        "hermes_home": str(profile_home),
        "plugin_dir": str(plugin_dir),
        "plugin_exists": plugin_dir.exists(),
        "plugin_version": read_manifest_version(plugin_dir),
        "config": read_config_summary(profile_home),
        "verify": verify,
    }


def backup_plugin(profile_home: Path) -> str:
    plugin_dir = profile_home / "plugins" / PLUGIN_NAME
    if not plugin_dir.exists() and not plugin_dir.is_symlink():
        return ""
    backup_root = profile_home / "backups" / "scope-recall-rollout" / f"{now_stamp()}.{uuid.uuid4().hex[:8]}"
    backup_root.mkdir(parents=True, exist_ok=False)
    backup_path = backup_root / PLUGIN_NAME
    if plugin_dir.is_dir() and not plugin_dir.is_symlink():
        shutil.copytree(plugin_dir, backup_path, symlinks=True)
    else:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plugin_dir, backup_path, follow_symlinks=False)
    return str(backup_path)


def backup_current_for_rollback(profile_home: Path) -> str:
    plugin_dir = profile_home / "plugins" / PLUGIN_NAME
    if not plugin_dir.exists() and not plugin_dir.is_symlink():
        return ""
    backup_root = profile_home / "backups" / "scope-recall-rollback-current" / f"{now_stamp()}.{uuid.uuid4().hex[:8]}"
    backup_root.mkdir(parents=True, exist_ok=False)
    backup_path = backup_root / PLUGIN_NAME
    if plugin_dir.is_dir() and not plugin_dir.is_symlink():
        shutil.copytree(plugin_dir, backup_path, symlinks=True)
    else:
        shutil.copy2(plugin_dir, backup_path, follow_symlinks=False)
    return str(backup_path)


def remove_plugin(plugin_dir: Path) -> None:
    if plugin_dir.is_symlink() or plugin_dir.is_file():
        plugin_dir.unlink()
    elif plugin_dir.exists():
        shutil.rmtree(plugin_dir)


def restore_plugin(profile_home: Path, backup_path: str, *, previous_plugin_existed: bool) -> str:
    plugin_dir = profile_home / "plugins" / PLUGIN_NAME
    backup = Path(backup_path).expanduser() if backup_path else Path()
    if previous_plugin_existed:
        error = validate_plugin_backup(backup)
        if error:
            raise FileNotFoundError(error)
    staging = plugin_dir.parent / f".scope-recall-rollback-staging-{now_stamp()}.{uuid.uuid4().hex[:8]}"
    try:
        if previous_plugin_existed:
            shutil.copytree(backup.resolve(), staging, symlinks=True)
        current_backup = backup_current_for_rollback(profile_home)
        remove_plugin(plugin_dir)
        if previous_plugin_existed:
            plugin_dir.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(plugin_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return current_backup


def rollout_profiles(
    *,
    profiles_root: Path,
    selected_profiles: list[str] | None = None,
    canary: str = "",
    apply: bool = False,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Plan or apply cross-profile plugin rollout actions.

    The function reports every target and backup path explicitly because cross-profile writes can affect other Hermes sessions."""
    profiles_root = profiles_root.expanduser()
    selected_set = {item for item in selected_profiles or [] if item}
    all_homes = profile_homes(profiles_root)
    available_names = {home.name for home in all_homes}
    missing_profiles = sorted(selected_set - available_names)
    selected_homes = [home for home in all_homes if not selected_set or home.name in selected_set]
    profiles = [inventory_profile(home) for home in selected_homes]
    profile_names = {str(profile["name"]) for profile in profiles}
    missing_canary = canary if canary and canary not in profile_names else ""
    source_version = read_manifest_version(installer.source_root())
    actions: list[dict[str, Any]] = []
    for profile in profiles:
        name = str(profile["name"])
        home = Path(str(profile["hermes_home"]))
        action: dict[str, Any] = {
            "profile": name,
            "hermes_home": str(home),
            "planned": True,
            "applied": False,
            "reason": "",
            "previous_plugin_existed": bool(profile["plugin_exists"]),
            "previous_version": str(profile["plugin_version"]),
            "target_version": source_version,
            "backup_path": "",
            "verify": {},
            "error": "",
        }
        if canary and name != canary:
            action["planned"] = False
            action["reason"] = "not_canary"
            actions.append(action)
            continue
        if apply and not (missing_profiles or missing_canary):
            try:
                action["backup_path"] = backup_plugin(home)
                install_result = installer.install(home, force=True)
                action["applied"] = bool(install_result.get("installed") or install_result.get("mode") == "already-installed")
                action["verify"] = install_result.get("verify", {})
                action["ok"] = bool(install_result.get("ok"))
            except Exception as exc:  # pragma: no cover - exact exception type depends on installer/runtime path
                action["ok"] = False
                action["applied"] = False
                action["reason"] = "install_error"
                action["error"] = str(exc)
        elif apply and (missing_profiles or missing_canary):
            action["planned"] = False
            action["reason"] = "selection_error"
        actions.append(action)
    applied_actions = [action for action in actions if action.get("applied")]
    action_error = any(action.get("ok") is False or bool(action.get("error")) for action in actions)
    selection_error = bool(missing_profiles or missing_canary)
    no_apply_target = bool(apply and not selection_error and not any(action.get("planned") for action in actions))
    report = {
        "ok": False if selection_error or no_apply_target or action_error else (all(bool(action.get("ok", True)) for action in applied_actions) if apply else True),
        "dry_run": not apply,
        "rollback": False,
        "profiles_root": str(profiles_root),
        "missing_profiles": missing_profiles,
        "missing_canary": missing_canary,
        "source_dir": str(installer.source_root()),
        "source_version": source_version,
        "profiles": profiles,
        "actions": actions,
    }
    if receipt_path is not None:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        report["receipt_path"] = str(receipt_path)
        receipt_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return report


def validate_rollback_action(original: dict[str, Any], profiles_root: Path) -> tuple[dict[str, Any], Path]:
    profile_home = Path(str(original.get("hermes_home") or "")).expanduser()
    action = {
        "profile": str(original.get("profile") or profile_home.name),
        "hermes_home": str(profile_home),
        "planned": True,
        "applied": False,
        "backup_path": str(original.get("backup_path") or ""),
        "current_backup_path": "",
        "previous_plugin_existed": bool(original.get("previous_plugin_existed")),
        "error": "",
    }
    if not is_relative_to(profile_home, profiles_root):
        action["error"] = f"profile home outside profiles root: {profile_home}"
        action["planned"] = False
        return action, profile_home
    if action["previous_plugin_existed"]:
        backup = Path(action["backup_path"]).expanduser()
        expected_backup_root = profile_home / "backups" / "scope-recall-rollout"
        if not is_relative_to(backup, expected_backup_root):
            action["error"] = f"rollback backup outside profile rollout backup root: {backup}"
            action["planned"] = False
            return action, profile_home
        backup_error = validate_plugin_backup(backup)
        if backup_error:
            action["error"] = backup_error
            action["planned"] = False
    return action, profile_home


def rollback_profiles(*, profiles_root: Path, receipt_path: Path, apply: bool = False) -> dict[str, Any]:
    profiles_root = profiles_root.expanduser()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    actions: list[dict[str, Any]] = []
    valid_homes: list[tuple[dict[str, Any], Path]] = []
    for original in receipt.get("actions", []):
        if not original.get("applied"):
            continue
        action, profile_home = validate_rollback_action(original, profiles_root)
        actions.append(action)
        if not action.get("error"):
            valid_homes.append((action, profile_home))
    has_errors = any(bool(action.get("error")) for action in actions)
    restored = 0
    if apply and not has_errors:
        for action, profile_home in valid_homes:
            action["current_backup_path"] = restore_plugin(
                profile_home,
                str(action["backup_path"]),
                previous_plugin_existed=bool(action["previous_plugin_existed"]),
            )
            action["applied"] = True
            restored += 1
    return {
        "ok": not has_errors,
        "dry_run": not apply,
        "rollback": True,
        "profiles_root": str(profiles_root),
        "receipt_path": str(receipt_path),
        "rollback_restored": restored,
        "actions": actions,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Roll out scope-recall across Hermes profiles with dry-run, canary, backup, and rollback support")
    parser.add_argument("--profiles-root", default=str(Path.home() / ".hermes" / "profiles"), help="Directory containing Hermes profile homes")
    parser.add_argument("--profile", action="append", default=[], help="Specific profile name to include; repeatable")
    parser.add_argument("--canary", default="", help="Only apply rollout to this profile name; other profiles are inventoried/skipped")
    parser.add_argument("--apply", action="store_true", help="Mutate profile plugin directories. Default is dry-run")
    parser.add_argument("--rollback", action="store_true", help="Rollback from a prior rollout receipt. Requires --receipt; use --apply to mutate")
    parser.add_argument("--receipt", default="", help="Receipt JSON path to write on rollout or read on rollback")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        receipt_path = Path(args.receipt).expanduser() if args.receipt else None
        if args.rollback:
            if receipt_path is None:
                raise ValueError("--rollback requires --receipt")
            report = rollback_profiles(profiles_root=Path(args.profiles_root).expanduser(), receipt_path=receipt_path, apply=bool(args.apply))
        else:
            report = rollout_profiles(
                profiles_root=Path(args.profiles_root).expanduser(),
                selected_profiles=list(args.profile or []),
                canary=str(args.canary or ""),
                apply=bool(args.apply),
                receipt_path=receipt_path,
            )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
