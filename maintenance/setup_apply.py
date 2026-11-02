"""Execute an approved setup plan through existing binding and install boundaries."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import yaml

from ..adapters.hermes.shared_entries import attachment_path, read_shared_payload
from ..core.schema import SCHEMA_VERSION
from ..runtime.instance_config import RuntimeInstanceConfig
from ..runtime.model_budget import initialize_auxiliary_budget_ledger
from .backup import atomic_write
from .doctor import load_binding, run_doctor
from .install import apply_install, plan_install
from .install_common import InstallError
from .setup_activation import register_codex
from .setup_configuration import runtime_payload, write_configuration
from .setup_packages import install_package
from .setup_projects import register_project_copy
from .shared import attach, init_shared, store_binding


def _runtime(row: dict, plan, backups: list[str]) -> Path:
    home = Path(row["home"])
    binding, directory = load_binding(row["host"], home)
    path = (
        attachment_path(home).parent / "runtime-config.json"
        if binding.installation_kind == "shared"
        else directory / "runtime-config.json"
    )
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["supervisor_enabled"] = row["worker"] == "on-demand"
        if plan.models is not None:
            configured = runtime_payload(binding, models=plan.models, host=row["host"], worker=row["worker"])
            raw.update(
                {key: configured[key] for key in ("auxiliary", "vector", "vector_threshold") if key in configured}
            )
    else:
        raw = runtime_payload(binding, models=plan.models, host=row["host"], worker=row["worker"])
    config = RuntimeInstanceConfig.from_mapping(raw)
    write_configuration(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n", backups)
    if config.auxiliary is not None and config.auxiliary.ledger_path is not None:
        initialize_auxiliary_budget_ledger(config.auxiliary.ledger_path, config.auxiliary.budget)
    return path


def _attach(row: dict, plan) -> None:
    if row["state"] != "fresh":
        return
    root = plan.store
    if not (root / "installation.json").is_file():
        init_shared(root=root, agent_id="scope-recall", test_mode=plan.test_mode)
    payload = read_shared_payload(root)
    entry = row["host"]
    taken = {item["entry_id"] for item in payload["entries"]}
    if entry in taken:
        raise ValueError("entry name already belongs to another home; choose that home or detach explicitly")
    source = row["scopes"]["source_entry"]
    kwargs: dict[str, Any] = dict(grants_like=(source,), capture_like=source) if source and entry != "hermes" else {}
    routes = root / "runtime-config.json"
    attach(
        host=entry,
        instance_root=Path(row["home"]),
        root=root,
        entry_id=entry,
        display_name=entry,
        runtime_config_from=routes if routes.is_file() else None,
        python_executable=Path(row["python"]),
        **kwargs,
    )


def _activate_hermes(home: Path, backups: list[str]) -> None:
    path = home / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(raw, dict) or not isinstance(raw.get("memory", {}), dict):
        raise ValueError("host memory configuration is not an object")
    raw.setdefault("memory", {})["provider"] = "scope-recall"
    write_configuration(path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), backups)


def _worker(plan, config_path: Path, python: str, backups: list[str], env_file: str | None = None) -> dict:
    command = [python, "-I", "-B", "-m", "scope_recall.runtime.worker_entry", "--config", str(config_path)]
    if env_file:
        command += ["--env-file", env_file]
    mode = (
        "on-demand" if json.loads(config_path.read_text(encoding="utf-8")).get("supervisor_enabled", True) else "manual"
    )
    value = dict(
        mode=mode,
        command=command,
        system_registration=False,
        explanation="Host hooks wake bounded worker passes."
        if mode == "on-demand"
        else "Run this command to process queued memory.",
    )
    write_configuration(config_path.parent / "worker-command.json", json.dumps(value, indent=2) + "\n", backups)
    return value


def _install(row: dict, plan, backups: list[str]) -> dict:
    home = Path(row["home"])
    binding, _directory = load_binding(row["host"], home)
    options = {}
    if row["host"] == "hermes":
        from ..adapters.hermes.shared_entries import load_binding_for_home
        from .install_hermes import bound_workspace

        options["agent_workspace"] = bound_workspace(load_binding_for_home(home))
    install = plan_install(
        host=row["host"],
        instance_root=home,
        target_plugin_dir=Path(row["target"]),
        project_root=plan.project if row["state"] == "local" and row["host"] == "codex" else None,
        agent_id=binding.agent_id,
        python_executable=Path(row["python"]),
        test_mode=binding.test_mode,
        env_file=row.get("env_file"),
        **options,
    )
    result = apply_install(install)
    backups.extend(result.backups)
    if row["host"] == "hermes":
        _activate_hermes(home, backups)
    elif row["host"] == "codex":
        register_codex(home, Path(row["target"]), plan.user_root, backups)
    return result.to_dict()


def _store_usable(report: dict) -> bool:
    """Require the selected truth store to pass its own checks before considering the isolated probe."""
    return bool(
        report["binding_ok"]
        and report["package_ok"]
        and report["database_present"]
        and report["sources"] is not None
        and report["schema_version"] == SCHEMA_VERSION
        and "capture_authorization_invalid" not in report["capability_gaps"]
    )


def apply_setup(plan, *, register_copies: bool = False) -> dict[str, Any]:
    """Apply one approved plan and preserve completed stages and rollback paths on failure."""
    from .setup_verify import verify_memory

    if plan.blockers:
        raise InstallError("setup plan is blocked; resolve the listed findings before applying")
    completed, backups, results = [], [], []
    receipt_home = Path(plan.hosts[0]["home"]) / "scope-recall" / "setup-receipts"
    receipt = receipt_home / (uuid.uuid4().hex + ".json")
    stage = "binding"
    try:
        if register_copies:
            for item in plan.project_copies:
                register_project_copy(
                    Path(item["home"]), {key: member for key, member in item.items() if key != "home"}
                )
        for row in plan.hosts:
            stage = row["host"] + ":package"
            install_package(row.get("package_command", []), Path(row["python"]))
            if row.get("package_command"):
                completed.append(stage)
            stage = row["host"] + ":binding"
            _attach(row, plan)
            completed.append(stage)
            stage = row["host"] + ":configuration"
            config_path = _runtime(row, plan, backups)
            completed.append(stage)
            stage = row["host"] + ":install"
            installed = _install(row, plan, backups)
            completed.append(stage)
            worker_path = config_path
            binding, _directory = load_binding(row["host"], Path(row["home"]))
            if binding.installation_kind == "shared":
                root = binding.data_directory
                worker_path = root / "runtime-config.json"
                payload = read_shared_payload(root)
                shared_binding = store_binding(payload, root, frozenset(payload["scope_ids"]))
                if not worker_path.is_file() or plan.models is not None:
                    raw = runtime_payload(shared_binding, models=plan.models, worker=plan.worker)
                    write_configuration(worker_path, json.dumps(raw, indent=2) + "\n", backups)
                elif plan.worker_explicit:
                    raw = json.loads(worker_path.read_text(encoding="utf-8"))
                    raw["supervisor_enabled"] = plan.worker == "on-demand"
                    RuntimeInstanceConfig.from_mapping(raw)
                    write_configuration(worker_path, json.dumps(raw, indent=2) + "\n", backups)
            worker = _worker(plan, worker_path, row["python"], backups, row.get("env_file"))
            stage = row["host"] + ":doctor"
            doctor = run_doctor(
                host=row["host"], instance_root=Path(row["home"]), python_executable=Path(row["python"])
            )
            completed.append(stage)
            stage = row["host"] + ":verification"
            configured = json.loads(config_path.read_text(encoding="utf-8"))
            check = (
                verify_memory(Path(row["home"]), row["host"], configured, env_file=row.get("env_file"))
                if _store_usable(doctor.to_dict())
                else dict(
                    cross_session_recall=False, models_verified=False, skipped="selected installation failed doctor"
                )
            )
            completed.append(stage)
            results.append(
                dict(
                    host=row["host"], installation=installed, worker=worker, doctor=doctor.to_dict(), verification=check
                )
            )
        full = bool(results) and all(item["verification"].get("models_verified") for item in results)
        ready = all(
            item["verification"].get("cross_session_recall") and _store_usable(item["doctor"]) for item in results
        )
        value = dict(
            status="ready" if ready else "verification_failed",
            state=("完整记忆功能可用" if full else "原文检索可用") if ready else None,
            missing=[]
            if full
            else [
                (
                    "Configure embedding and extraction routes, credentials and a nonzero budget; "
                    "rerun setup to verify them."
                )
            ],
            completed=completed,
            hosts=results,
            backups=backups,
            receipt=str(receipt),
            host_activation="Start a new host session; approve plugin hooks or MCP when the host requests it.",
        )
    except Exception as exc:  # noqa: BLE001 -- external failures must produce a sanitized recovery receipt.
        # Unexpected transport/parser errors may carry credentials. Persist only a type and the known stage.
        value = dict(
            status="failed",
            failed_step=stage,
            error_type=type(exc).__name__,
            completed=completed,
            backups=backups,
            receipt=str(receipt),
            retry="Rerun the original setup command; completed bindings and truth databases are reused.",
            rollback=(
                "Use the install receipt with apply-uninstall to remove wrappers while retaining memory; "
                "restore the listed configuration backups if needed."
            ),
        )
    atomic_write(receipt, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return value
