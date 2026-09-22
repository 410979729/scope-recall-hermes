"""Explicit plan/apply install and receipt-backed uninstall for v1.1 host wrappers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from types import ModuleType
import uuid

from . import install_codex, install_hermes
from .backup import _atomic_write, _sha256
from .doctor import _host_registration_status
from .install_common import (
    BACKUP_DIRNAME,
    PACKAGE_VERSION,
    InstallError,
    InstallPlan,
    InstallResult,
    PlannedChange,
    UninstallPlan,
    UninstallResult,
    _norm,
    _require_absolute,
    _require_interpreter,
    _validate_agent_id,
    _validate_host,
    _validate_plugin_name,
    _validate_roots,
    _within,
)
from .install_purge import _purge_guard, _purge_identity, _purge_inventory, _purge_owned_data
from .install_receipt import _load_receipt, _receipt_path, _validate_receipt_binding, _write_receipt

__all__ = [
    "PACKAGE_VERSION",
    "InstallError",
    "InstallPlan",
    "InstallResult",
    "UninstallPlan",
    "UninstallResult",
    "apply_install",
    "apply_uninstall",
    "plan_install",
    "plan_uninstall",
]

# Both host modules expose the same functions; the entry picks one instead of branching.
_HOSTS: dict[str, ModuleType] = {"codex": install_codex, "hermes": install_hermes}


def _instance_files(host: ModuleType, instance_root: Path) -> tuple[Path, Path]:
    """The adapter-owned manifest and truth database that the receipt also tracks."""
    return host.config_path(instance_root), host.data_dir(instance_root) / "memory.sqlite3"


def _foreign_plugin_entries(target: Path, keep: set[str]) -> list[str]:
    if not target.exists():
        return []
    return [str(path) for path in sorted(target.rglob("*")) if not path.is_dir() and _norm(path) not in keep]


def _backup_copy(path: Path, backup_root: Path, plan: InstallPlan) -> Path:
    """Copy a file the install is about to overwrite under plugin/, instance/ or other/."""
    norm = _norm(path)
    if norm.startswith(_norm(plan.target_plugin_dir) + os.sep):
        rel = Path("plugin") / path.relative_to(plan.target_plugin_dir)
    elif norm.startswith(_norm(plan.instance_root) + os.sep):
        rel = Path("instance") / path.relative_to(plan.instance_root)
    else:
        rel = Path("other") / path.name
    backup_path = backup_root / rel
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    return backup_path


def plan_install(
    *,
    target_plugin_dir: Path | str,
    instance_root: Path | str,
    project_root: Path | str,
    agent_id: str,
    python_executable: Path | str,
    host: str,
    test_mode: bool = False,
    agent_workspace: str | None = None,
    env_file: Path | str | None = None,
    local_platforms: tuple[str, ...] | list[str] = (),
) -> InstallPlan:
    host_choice = _validate_host(host)
    adapter = _HOSTS[host_choice]
    target = _require_absolute(Path(target_plugin_dir), "target_plugin_dir")
    instance = _require_absolute(Path(instance_root), "instance_root")
    project = _require_absolute(Path(project_root), "project_root")
    python = _require_interpreter(Path(python_executable), "python_executable")
    agent = _validate_agent_id(agent_id)
    workspace, credentials = adapter.validate_options(agent_workspace, env_file)
    approvals = adapter.validate_local_platforms(local_platforms)
    if type(test_mode) is not bool:
        raise InstallError("test_mode must be a boolean")
    _validate_plugin_name(target.name)
    _validate_roots((target, "target_plugin_dir"), (instance, "instance_root"), (project, "project_root"))

    plan = InstallPlan(
        host=host_choice,
        target_plugin_dir=target,
        instance_root=instance,
        project_root=project,
        agent_id=agent,
        python_executable=python,
        test_mode=test_mode,
        agent_workspace=workspace,
        env_file=credentials,
        local_platforms=approvals,
    )
    receipt = _load_receipt(instance)
    owned: dict[str, str] = {}
    if receipt is not None:
        try:
            owned = _validate_receipt_binding(receipt, host=host_choice, instance_root=instance, target_plugin_dir=target)
        except InstallError as exc:
            plan.conflicts.append(str(exc))
        stored_workspace = receipt.get("agent_workspace")
        if type(stored_workspace) is str and stored_workspace.strip():
            if host_choice != "hermes" or stored_workspace.strip() != workspace:
                plan.conflicts.append("existing receipt agent_workspace mismatch")

    planned = adapter.planned_files(plan)
    target_norm = _norm(target)
    keep = {_norm(path) for path in planned} | {norm for norm in owned if _within(norm, target_norm)}
    for path in _foreign_plugin_entries(target, keep):
        plan.conflicts.append(f"unrelated plugin file: {path}")

    plan.reuse_instance = adapter.config_path(instance).is_file()
    if plan.reuse_instance:
        try:
            adapter.validate_reuse(plan)
        except Exception as exc:
            plan.conflicts.append(str(exc))
        else:
            plan.changes.append(PlannedChange("validate", str(instance), "reuse initialized instance binding"))
            for platform in adapter.unapproved_local_platforms(plan):
                plan.changes.append(PlannedChange(
                    "write", str(adapter.config_path(instance)),
                    f"approve local platform {platform}: a session there that names no user binds as the local owner"))
    else:
        for path in adapter.foreign_instance_entries(instance):
            plan.conflicts.append(f"foreign instance content: {path}")
        plan.changes.append(PlannedChange("initialize", str(instance), "create empty instance via adapter helper"))

    for path in sorted(planned, key=str):
        norm = _norm(path)
        if path.is_file():
            if norm not in owned:
                plan.conflicts.append(f"no-receipt collision: {path}")
            elif _sha256(path) != owned[norm]:
                plan.conflicts.append(f"edited prior file: {path}")
        plan.changes.append(PlannedChange("write", str(path), "install host wrapper artifact"))
    plan.changes.append(PlannedChange("write", str(_receipt_path(instance)), "install receipt with digest"))

    if receipt is not None and receipt.get("host") not in {None, host_choice}:
        plan.conflicts.append("existing receipt host mismatch")
    return plan


def apply_install(plan: InstallPlan) -> InstallResult:
    plan = plan_install(
        target_plugin_dir=plan.target_plugin_dir,
        instance_root=plan.instance_root,
        project_root=plan.project_root,
        agent_id=plan.agent_id,
        python_executable=plan.python_executable,
        host=plan.host,
        test_mode=plan.test_mode,
        agent_workspace=plan.agent_workspace or None,
        env_file=plan.env_file,
        local_platforms=plan.local_platforms,
    )
    if plan.conflicts:
        raise InstallError("; ".join(plan.conflicts))
    adapter = _HOSTS[plan.host]
    planned = adapter.planned_files(plan)
    tracked = _instance_files(adapter, plan.instance_root)
    backup_root = plan.instance_root / BACKUP_DIRNAME / uuid.uuid4().hex
    backups: list[str] = []
    written: list[str] = []
    touched: list[tuple[Path, Path | None]] = []
    installation_id = ""
    instance_initialized = False
    try:
        if plan.reuse_instance:
            installation_id = adapter.installation_id(plan.instance_root)
            if adapter.unapproved_local_platforms(plan):
                # The manifest is the adapter's, not a wrapper: it is rewritten in
                # place, with the copy the rollback below restores, and the receipt
                # tracks its new digest like any other state of it.
                manifest_path = adapter.config_path(plan.instance_root)
                manifest_backup = _backup_copy(manifest_path, backup_root, plan)
                backups.append(str(manifest_backup))
                touched.append((manifest_path, manifest_backup))
                adapter.approve_local_platforms(plan)
        else:
            installation_id = adapter.initialize_instance(plan)
            instance_initialized = True

        prior_receipt = _receipt_path(plan.instance_root)
        if prior_receipt.is_file():
            backups.append(str(_backup_copy(prior_receipt, backup_root, plan)))
        for path, content in planned.items():
            backup_path = _backup_copy(path, backup_root, plan) if path.is_file() else None
            if backup_path is not None:
                backups.append(str(backup_path))
            _atomic_write(path, content)
            written.append(str(path))
            touched.append((path, backup_path))

        receipt_path = _write_receipt(plan, installation_id=installation_id, written=written, tracked=tracked)
        written.append(str(receipt_path))
    except Exception:
        for path, backup_path in reversed(touched):
            if backup_path is not None and backup_path.is_file():
                shutil.copy2(backup_path, path)
            elif path.is_file():
                path.unlink()
        if instance_initialized:
            # The adapter already created the instance; leave a receipt that
            # names it so a later uninstall still recognizes those files.
            partial = [str(path) for path in tracked if path.is_file()]
            if partial and installation_id:
                _write_receipt(plan, installation_id=installation_id, written=partial, tracked=tracked)
        raise

    return InstallResult(
        files_written=written,
        # Registration is what the doctor can actually observe; hook trust is a
        # Codex operator step and full mode is never verified by an install.
        host_registration_pending=_host_registration_status(plan.host, plan.instance_root, plan.python_executable) != "registered",
        hook_trust_pending=plan.host == "codex",
        full_mode_unverified=True,
        receipt_path=str(receipt_path),
        installation_id=installation_id,
        backups=backups,
    )


def plan_uninstall(
    *,
    instance_root: Path | str,
    target_plugin_dir: Path | str | None = None,
    purge: bool = False,
) -> UninstallPlan:
    instance = _require_absolute(Path(instance_root), "instance_root")
    receipt = _load_receipt(instance)
    if receipt is None:
        raise InstallError("install receipt is required for uninstall")

    host = _validate_host(str(receipt.get("host") or ""))
    adapter = _HOSTS[host]
    if target_plugin_dir is None:
        target_plugin_dir = str(receipt.get("target_plugin_dir") or "")
    target = _require_absolute(Path(target_plugin_dir), "target_plugin_dir")
    plan = UninstallPlan(host=host, instance_root=instance, target_plugin_dir=target, retain_memory=True)

    try:
        owned = _validate_receipt_binding(receipt, host=host, instance_root=instance, target_plugin_dir=target)
    except InstallError as exc:
        plan.conflicts.append(str(exc))
        return plan

    target_norm = _norm(target)
    wrapper_norms = {_norm(path) for path in adapter.instance_wrapper_files(instance)}
    for norm, expected in owned.items():
        if not (_within(norm, target_norm) or norm in wrapper_norms):
            continue
        path = Path(norm)
        if not path.is_file():
            continue
        if _sha256(path) != expected:
            plan.edited_files.append(norm)
        else:
            plan.files_to_remove.append(norm)

    if purge:
        if plan.edited_files:
            plan.conflicts.append("purge_refused: edited plugin files")
        else:
            try:
                data_directory, _installation_id, _agent_id, _config_path = _purge_identity(adapter, plan, receipt)
                with _purge_guard(data_directory):
                    inventory = _purge_inventory(adapter, plan, receipt)
            except InstallError as exc:
                plan.conflicts.append(str(exc))
            else:
                plan.purge_allowed = True
                plan.purge_paths = [str(path) for path in inventory.files]
                plan.retained_backups = [str(path) for path in inventory.retained_backups]
                plan.retain_memory = False
    return plan


def _disable_autostart(data_dir: Path) -> None:
    """Remove the Windows wake task bound to this instance before its wrappers go."""
    registration_path = data_dir / "runtime-autostart.json"
    if not registration_path.is_file():
        return
    from .autostart import disable
    from ..runtime.worker_entry import load_config

    registration = json.loads(registration_path.read_text(encoding="utf-8"))
    config = load_config(registration["config_path"])
    if config.binding.data_directory.resolve() != data_dir.resolve():
        raise InstallError("autostart_binding_mismatch")
    disable(registration["config_path"], remove=True)


def apply_uninstall(plan: UninstallPlan, *, purge: bool = False) -> UninstallResult:
    plan = plan_uninstall(instance_root=plan.instance_root, target_plugin_dir=plan.target_plugin_dir, purge=purge)
    if plan.conflicts:
        raise InstallError("; ".join(plan.conflicts))
    adapter = _HOSTS[plan.host]
    data_dir = adapter.data_dir(plan.instance_root)
    _disable_autostart(data_dir)

    purged_paths: list[str] = []
    retained_backups = list(plan.retained_backups)
    if purge:
        if not plan.purge_allowed:
            raise InstallError("purge_refused:plan_not_authorized")
        receipt = _load_receipt(plan.instance_root)
        if receipt is None:
            raise InstallError("install receipt is required for uninstall")
        purged_paths, retained_backups = _purge_owned_data(adapter, plan, receipt)

    removed: list[str] = []
    for path_text in plan.files_to_remove:
        path = Path(path_text)
        if not path.is_file():
            continue
        path.unlink()
        removed.append(path_text)
        parent = path.parent
        while parent != plan.target_plugin_dir and parent != plan.instance_root:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
            else:
                break

    return UninstallResult(
        files_removed=removed,
        # An entry of a shared store keeps its memory in the store, which its pointer still names.
        memory_retained=False if purge else any((data_dir / name).is_file() for name in ("memory.sqlite3", "attachment.json")),
        purged=bool(purge and purged_paths),
        edited_files=list(plan.edited_files),
        purged_paths=purged_paths,
        retained_backups=retained_backups,
    )
