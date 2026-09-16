"""Hermes host: the distribution templates and setup skill it installs, plus the
manifest-bound instance a receipt-backed uninstall verifies."""
from __future__ import annotations

from pathlib import Path

from scope_recall.adapters.hermes.installation import install_hermes_scope_recall, load_installation_manifest

from .install_common import (
    REPO_ROOT,
    SETUP_SKILL,
    InstallError,
    InstallPlan,
    _reject_symlink_chain,
    _validate_identifier,
)

# Hermes 0.21+ ``_memory_provider_init_kwargs`` hardcodes agent_workspace="hermes".
# The public installer must bind that exact host value; identifiers are not aliased.
DEFAULT_AGENT_WORKSPACE = "hermes"
DIST_HERMES = REPO_ROOT / "distribution" / "hermes"


def data_dir(instance_root: Path) -> Path:
    return instance_root / "scope-recall"


def config_path(instance_root: Path) -> Path:
    return data_dir(instance_root) / "installation.json"


def instance_wrapper_files(instance_root: Path) -> tuple[Path, ...]:
    """The setup skill lives under the Hermes HOME, outside the plugin directory."""
    return (instance_root / "skills" / "scope-recall-setup" / "SKILL.md",)


def validate_options(agent_workspace: str | None, env_file: Path | str | None) -> tuple[str, Path | None]:
    """Hermes processes inherit the gateway environment and must not carry a
    second credential path; the audience workspace defaults to the host value."""
    workspace = "" if agent_workspace is None else str(agent_workspace).strip()
    workspace = _validate_identifier(workspace or DEFAULT_AGENT_WORKSPACE, "agent_workspace")
    if env_file is not None and str(env_file).strip() != "":
        raise InstallError("env_file is only used for Codex installation")
    return workspace, None


def planned_files(plan: InstallPlan) -> dict[Path, str | bytes]:
    files: dict[Path, str | bytes] = {}
    for name in ("__init__.py", "plugin.yaml"):
        source = DIST_HERMES / name
        if not source.is_file():
            raise InstallError(f"distribution template missing: {source}")
        files[plan.target_plugin_dir / name] = source.read_text(encoding="utf-8")
    for path in instance_wrapper_files(plan.instance_root):
        files[path] = SETUP_SKILL.read_text(encoding="utf-8")
    return files


def foreign_instance_entries(instance_root: Path) -> list[str]:
    """A real Hermes HOME already holds host config, sessions and other plugins;
    only an existing scope-recall namespace that no receipt explains is foreign."""
    namespace = data_dir(instance_root)
    return [str(namespace)] if namespace.exists() else []


def initialize_instance(plan: InstallPlan) -> str:
    binding, _core = install_hermes_scope_recall(
        plan.instance_root,
        agent_id=plan.agent_id,
        platform="cli",
        user_id="local",
        agent_workspace=plan.agent_workspace,
        test_mode=plan.test_mode,
    )
    return binding.installation_id


def installation_id(instance_root: Path) -> str:
    return load_installation_manifest(instance_root).installation_id


def _bound_workspace(manifest) -> str:
    # Bind installer reuse to the explicit owner workspace. Conversation rows
    # may preserve old, independently authorized workspaces during migration.
    values = {
        str(row.get("agent_workspace") or "").strip()
        for row in manifest.audiences
        if row.get("kind") == "owner_private"
    }
    values.discard("")
    if len(values) != 1:
        raise InstallError("existing Hermes installation agent_workspace is ambiguous")
    return next(iter(values))


def validate_reuse(plan: InstallPlan) -> None:
    manifest = load_installation_manifest(plan.instance_root)
    if manifest.agent_id != plan.agent_id:
        raise InstallError("existing Hermes installation agent_id mismatch")
    if _bound_workspace(manifest) != plan.agent_workspace:
        raise InstallError("existing Hermes installation agent_workspace mismatch")
    if manifest.test_mode != plan.test_mode:
        raise InstallError(
            "existing Hermes installation test_mode mismatch: "
            f"stored={manifest.test_mode}, requested={plan.test_mode}"
        )
    if not (manifest.data_directory / "memory.sqlite3").is_file():
        raise InstallError("existing Hermes installation database is missing")


def purge_identity(instance_root: Path) -> tuple[Path, str, str, Path]:
    """Data directory, installation id, agent id and manifest path from the signed manifest."""
    manifest = load_installation_manifest(instance_root)
    _reject_symlink_chain(manifest.data_directory)
    data_directory = manifest.data_directory.resolve()
    return data_directory, manifest.installation_id, manifest.agent_id, data_directory / "installation.json"
