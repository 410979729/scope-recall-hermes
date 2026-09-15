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
from scope_recall_rollout_runtime.windows_filesystem import (  # noqa: E402
    atomic_write_text,
    copy_file,
    copy_tree,
    list_directory_paths,
    make_dirs,
    move_path,
    path_exists,
    path_is_dir,
    path_is_file,
    path_is_symlink,
    public_path,
    read_text,
    real_path,
    remove_path,
)

PLUGIN_NAME = "scope-recall"


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d.%H%M%S.%f")


def read_manifest_name(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if not path_is_file(manifest):
        return ""
    for raw_line in read_text(manifest, errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def read_manifest_version(plugin_dir: Path) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if not path_is_file(manifest):
        return ""
    for raw_line in read_text(manifest, errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def read_config_summary(profile_home: Path) -> dict[str, Any]:
    config = profile_home / "config.yaml"
    if not path_is_file(config):
        return {"exists": False, "memory_provider": ""}
    text = read_text(config, errors="replace")[:100_000]
    provider = "scope-recall" if "scope-recall" in text else ""
    return {"exists": True, "memory_provider": provider}


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        real_path(path).relative_to(real_path(root))
        return True
    except (OSError, ValueError):
        return False


def validate_plugin_backup(backup_path: Path) -> str:
    try:
        backup = public_path(backup_path)
    except OSError as exc:
        return f"rollback backup cannot be resolved: {exc}"
    if not path_exists(backup):
        return f"rollback backup missing: {backup_path}"
    if not path_is_dir(backup):
        return f"rollback backup is not a directory: {backup_path}"
    if read_manifest_name(backup) != PLUGIN_NAME:
        return f"rollback backup plugin.yaml is not {PLUGIN_NAME}: {backup_path}"
    for required in ("__init__.py", "provider.py", "config.json"):
        if not path_is_file(backup / required):
            return f"rollback backup missing required file {required}: {backup_path}"
    return ""


def profile_homes(profiles_root: Path, selected: list[str] | None = None) -> list[Path]:
    selected_set = {item for item in selected or [] if item}
    if not path_is_dir(profiles_root):
        return []
    homes = [
        path
        for path in list_directory_paths(profiles_root)
        if path_is_dir(path)
    ]
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
        "plugin_exists": path_exists(plugin_dir),
        "plugin_version": read_manifest_version(plugin_dir),
        "config": read_config_summary(profile_home),
        "verify": verify,
    }


def _backup_root(profile_home: Path, lane: str) -> Path:
    stamp = now_stamp().replace(".", "")[:14]
    return profile_home / "backups" / "sr" / lane / f"{stamp}.{uuid.uuid4().hex[:8]}"


def _copy_plugin(source: Path, destination: Path) -> None:
    if path_is_dir(source) and not path_is_symlink(source):
        copy_tree(source, destination, symlinks=True)
    else:
        copy_file(source, destination, follow_symlinks=False)


def backup_plugin(profile_home: Path) -> str:
    plugin_dir = profile_home / "plugins" / PLUGIN_NAME
    if not path_exists(plugin_dir) and not path_is_symlink(plugin_dir):
        return ""
    backup_root = _backup_root(profile_home, "o")
    backup_path = backup_root / PLUGIN_NAME
    try:
        _copy_plugin(plugin_dir, backup_path)
    except Exception:
        remove_path(backup_root, missing_ok=True, ignore_errors=True)
        raise
    return str(backup_path)


def backup_current_for_rollback(profile_home: Path) -> str:
    plugin_dir = profile_home / "plugins" / PLUGIN_NAME
    if not path_exists(plugin_dir) and not path_is_symlink(plugin_dir):
        return ""
    backup_root = _backup_root(profile_home, "r")
    backup_path = backup_root / PLUGIN_NAME
    try:
        _copy_plugin(plugin_dir, backup_path)
    except Exception:
        remove_path(backup_root, missing_ok=True, ignore_errors=True)
        raise
    return str(backup_path)


def remove_plugin(plugin_dir: Path) -> None:
    remove_path(plugin_dir, missing_ok=True)


def restore_plugin(profile_home: Path, backup_path: str, *, previous_plugin_existed: bool) -> str:
    plugin_dir = profile_home / "plugins" / PLUGIN_NAME
    backup = Path(backup_path).expanduser() if backup_path else Path()
    if previous_plugin_existed:
        error = validate_plugin_backup(backup)
        if error:
            raise FileNotFoundError(error)
    staging = plugin_dir.parent / f".sr-rb-{uuid.uuid4().hex[:8]}"
    current_backup = ""
    try:
        if previous_plugin_existed:
            copy_tree(public_path(backup), staging, symlinks=True)
        current_backup = backup_current_for_rollback(profile_home)
        remove_plugin(plugin_dir)
        if previous_plugin_existed:
            make_dirs(plugin_dir.parent, exist_ok=True)
            move_path(staging, plugin_dir)
    except Exception as original_exc:
        if current_backup and not path_exists(plugin_dir):
            try:
                _copy_plugin(public_path(current_backup), plugin_dir)
            except Exception as compensation_exc:
                raise RuntimeError(
                    "rollback replacement failed and automatic current-plugin compensation failed"
                ) from compensation_exc
        raise original_exc
    finally:
        remove_path(staging, missing_ok=True, ignore_errors=True)
    return current_backup


def _publish_receipt(report: dict[str, Any], receipt_path: Path) -> None:
    """Atomically publish one durable rollout/rollback capability receipt."""

    report["receipt_path"] = str(receipt_path)
    atomic_write_text(
        receipt_path,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _compensate_action(
    profile_home: Path,
    action: dict[str, Any],
) -> bool:
    """Restore the exact pre-rollout plugin state for one failed action."""

    try:
        action["compensation_backup_path"] = restore_plugin(
            profile_home,
            str(action.get("backup_path") or ""),
            previous_plugin_existed=bool(action.get("previous_plugin_existed")),
        )
    except Exception as exc:
        action["compensated"] = False
        action["compensation_error"] = str(exc)
        return False
    action["compensated"] = True
    action["applied"] = False
    action["mutation_started"] = False
    return True


def _rollout_ok(
    actions: list[dict[str, Any]],
    *,
    apply: bool,
    selection_error: bool,
) -> bool:
    planned = [action for action in actions if action.get("planned")]
    if selection_error or (apply and not planned):
        return False
    if not apply:
        return True
    return all(
        action.get("ok") is True and action.get("applied") is True
        for action in planned
    )


def rollout_profiles(
    *,
    profiles_root: Path,
    selected_profiles: list[str] | None = None,
    canary: str = "",
    apply: bool = False,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Plan or durably apply cross-profile plugin rollout actions."""

    profiles_root = public_path(profiles_root.expanduser())
    selected_set = {item for item in selected_profiles or [] if item}
    all_homes = profile_homes(profiles_root)
    available_names = {home.name for home in all_homes}
    missing_profiles = sorted(selected_set - available_names)
    selected_homes = [
        home for home in all_homes if not selected_set or home.name in selected_set
    ]
    profiles = [inventory_profile(home) for home in selected_homes]
    profile_names = {str(profile["name"]) for profile in profiles}
    missing_canary = canary if canary and canary not in profile_names else ""
    source_version = read_manifest_version(installer.source_root())
    actions: list[dict[str, Any]] = []
    for profile in profiles:
        name = str(profile["name"])
        action: dict[str, Any] = {
            "profile": name,
            "hermes_home": str(profile["hermes_home"]),
            "planned": not bool(canary and name != canary),
            "applied": False,
            "mutation_started": False,
            "compensated": False,
            "reason": "not_canary" if canary and name != canary else "",
            "previous_plugin_existed": bool(profile["plugin_exists"]),
            "previous_version": str(profile["plugin_version"]),
            "target_version": source_version,
            "backup_path": "",
            "compensation_backup_path": "",
            "verify": {},
            "error": "",
        }
        actions.append(action)

    selection_error = bool(missing_profiles or missing_canary)
    if apply and selection_error:
        for action in actions:
            if action.get("planned"):
                action["planned"] = False
                action["reason"] = "selection_error"
    report: dict[str, Any] = {
        "ok": _rollout_ok(
            actions,
            apply=apply,
            selection_error=selection_error,
        ),
        "dry_run": not apply,
        "plan": not apply,
        "rollback": False,
        "profiles_root": str(profiles_root),
        "missing_profiles": missing_profiles,
        "missing_canary": missing_canary,
        "source_dir": str(installer.source_root()),
        "source_version": source_version,
        "profiles": profiles,
        "actions": actions,
    }
    planned_actions = [action for action in actions if action.get("planned")]
    if apply and not selection_error and planned_actions and receipt_path is None:
        raise ValueError("--apply requires a durable --receipt path")
    if not apply or selection_error or not planned_actions:
        if receipt_path is not None:
            _publish_receipt(report, receipt_path)
        return report

    assert receipt_path is not None
    for action in planned_actions:
        home = public_path(Path(str(action["hermes_home"])))
        target_mutation_possible = False
        try:
            action["backup_path"] = backup_plugin(home)
            action["mutation_started"] = True
            _publish_receipt(report, receipt_path)
            target_mutation_possible = True
            install_result = installer.install(home, force=True)
            action["applied"] = bool(
                install_result.get("installed")
                or install_result.get("mode") == "already-installed"
            )
            action["verify"] = install_result.get("verify", {})
            action["ok"] = bool(install_result.get("ok"))
            if not action["ok"]:
                action["reason"] = "install_not_ok"
                _compensate_action(home, action)
        except Exception as exc:
            action["ok"] = False
            action["reason"] = "install_error"
            action["error"] = str(exc)
            if target_mutation_possible:
                _compensate_action(home, action)

        report["ok"] = _rollout_ok(
            actions,
            apply=True,
            selection_error=False,
        )
        try:
            _publish_receipt(report, receipt_path)
        except Exception as exc:
            if target_mutation_possible:
                _compensate_action(home, action)
            action["ok"] = False
            action["reason"] = "receipt_error"
            action["error"] = str(exc)
            report["ok"] = False
            try:
                _publish_receipt(report, receipt_path)
            except Exception:
                pass
            break
        if action.get("ok") is not True:
            break
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
        expected_backup_roots = (
            profile_home / "backups" / "sr" / "o",
            profile_home / "backups" / "scope-recall-rollout",
        )
        if not any(is_relative_to(backup, root) for root in expected_backup_roots):
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
    receipt = json.loads(read_text(receipt_path))
    actions: list[dict[str, Any]] = []
    valid_homes: list[tuple[dict[str, Any], Path]] = []
    for original in receipt.get("actions", []):
        if not (original.get("applied") or original.get("mutation_started")):
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
    parser.add_argument("--plan", action="store_true", help="Explicit dry-run/inventory mode. This is the default and cannot be combined with --apply")
    parser.add_argument("--apply", action="store_true", help="Mutate profile plugin directories. Default is dry-run")
    parser.add_argument("--rollback", action="store_true", help="Rollback from a prior rollout receipt. Requires --receipt; use --apply to mutate")
    parser.add_argument("--receipt", default="", help="Receipt JSON path to write on rollout or read on rollback")
    parser.add_argument("--json", action="store_true", help="Print JSON output (accepted for product CLI consistency; JSON is always emitted)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.plan and args.apply:
            raise ValueError("--plan cannot be combined with --apply")
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
