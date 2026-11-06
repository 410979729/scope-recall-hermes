"""Validate planned host writes and expose explicit configuration changes before any mutation."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..core.secret_patterns import structure_contains_secret_like_text
from ..runtime.instance_config import RuntimeInstanceConfig, configuration_value
from .doctor import load_binding
from .hermes_config import provider_configuration
from .install import plan_installation
from .install_common import InstallError, InstallPlan, validate_write_path
from .install_receipt import setup_receipts_directory
from .setup_activation import codex_catalog
from .setup_configuration import runtime_payload


def installation_plan(row: dict, plan, *, pending: bool = False):
    """Run the existing installer planner against an existing or explicitly pending attachment."""
    home = Path(row["home"])
    agent, options = "scope-recall", {}
    if not pending:
        binding, _directory = load_binding(row["host"], home)
        agent = binding.agent_id
        if row["host"] == "hermes":
            from ..adapters.hermes.shared_entries import load_binding_for_home
            from .install_hermes import bound_workspace

            options["agent_workspace"] = bound_workspace(load_binding_for_home(home))
    elif (plan.store / "installation.json").is_file():
        agent = json.loads((plan.store / "installation.json").read_text("utf-8"))["agent_id"]
    options["agent_workspace"] = options.get("agent_workspace", "")
    return plan_installation(
        InstallPlan(
            host=row["host"],
            instance_root=home,
            target_plugin_dir=Path(row["target"]),
            project_root=plan.project if row["state"] == "local" and row["host"] == "codex" else None,
            agent_id=agent,
            python_executable=Path(row["python"]),
            test_mode=plan.test_mode,
            env_file=Path(row["env_file"]) if row.get("env_file") else None,
            pending_attachment=pending,
            **options,
        )
    )


def _visible(field: str, value):
    """Preview declared settings while masking any credential-bearing value already present."""
    if field.endswith("credential_env"):
        return value
    if isinstance(value, str) and "://" in value:
        url = urlsplit(value)
        if url.username or url.password or url.query or url.fragment:
            return "credential-bearing URL omitted"
    return "<redacted>" if structure_contains_secret_like_text({field.rsplit(".", 1)[-1]: value}) else value


def _changes(before: dict, after: dict, prefix: str = "") -> list[dict]:
    out = []
    for key in sorted(before.keys() | after.keys()):
        field, old, value = prefix + key, before.get(key), after.get(key)
        if isinstance(value, dict) and value:
            out.extend(_changes(old if isinstance(old, dict) else {}, value, field + "."))
        elif value != old or key not in before or key not in after:
            out.append(
                dict(
                    field=field,
                    before=_visible(field, old),
                    after=_visible(field, value),
                    action="set" if key in after else "remove",
                )
            )
    return out


def configuration_changes(plan) -> list[dict]:
    """Preview supplied fields only; the plan never includes unrelated host configuration."""
    paths = {plan.store / "runtime-config.json"}
    for row in plan.hosts:
        home = Path(row["home"])
        directory = home / ("data" if row["host"] == "codex" and row["state"] == "local" else "scope-recall")
        paths.add(directory / "runtime-config.json")
    patch = plan.models or {}
    if plan.worker_explicit:
        patch = dict(patch, supervisor_enabled=plan.worker == "on-demand")
    result = []
    for path in sorted(paths):
        before = json.loads(path.read_text("utf-8")) if path.is_file() else {}
        if before:
            prior = RuntimeInstanceConfig.from_mapping(before)
            updated = runtime_payload(
                prior.binding, models=plan.models, existing=before, worker=plan.worker if plan.worker_explicit else None
            )
            # Compare actual accepted values, including removed route fields. Unknown
            # host input keys never become printable configuration data.
            before = configuration_value(prior)
            after = configuration_value(RuntimeInstanceConfig.from_mapping(updated))
        else:
            after = patch
        selected_before = {key: before[key] for key in patch if key in before}
        selected_after = {key: after[key] for key in patch if key in after}
        for change in _changes(selected_before, selected_after):
            result.append(dict(path=str(path), **change))
    return result


def check_plan(plan) -> list[str]:
    """Reject installer, catalog and provider conflicts while all selected homes remain untouched."""
    conflicts = []
    for row in plan.hosts:
        try:
            install = installation_plan(row, plan, pending=row["state"] == "fresh")
            conflicts.extend(install.conflicts)
            directory = Path(row["home"]) / (
                "data" if row["host"] == "codex" and row["state"] == "local" else "scope-recall"
            )
            for path in (
                directory / "runtime-config.json",
                directory / "worker-command.json",
                directory / "attachment.json",
                setup_receipts_directory(Path(row["home"])) / "receipt.json",
                plan.store / "runtime-config.json",
                plan.store / "worker-command.json",
            ):
                validate_write_path(path)
            if row["host"] == "codex":
                codex_catalog(Path(row["target"]), plan.user_root)
            elif row["host"] == "hermes":
                from .setup_bindings import profile_repair

                profile_repair(Path(row["home"]))
                path = Path(row["home"]) / "config.yaml"
                validate_write_path(path)
                provider_configuration(path)
        except (InstallError, OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            conflicts.append(
                "host preflight rejected configuration: "
                + (str(exc) if isinstance(exc, InstallError) else type(exc).__name__)
            )
    return conflicts
