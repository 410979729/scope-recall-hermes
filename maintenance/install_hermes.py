"""Hermes host: the distribution templates and setup skill it installs, plus the
manifest-bound instance a receipt-backed uninstall verifies."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml
from packaging.version import Version
from scope_recall._version import __version__
from scope_recall.adapters.hermes.audiences import (
    LOCAL_PLATFORMS,
    HermesIdentityError,
    normalize_local_platforms,
    normalize_owner_logins,
)
from scope_recall.adapters.hermes.installation import (
    approve_local_platforms as _approve_local_platforms,
)
from scope_recall.adapters.hermes.installation import (
    install_hermes_scope_recall,
    load_installation_manifest,
    write_installation_manifest,
)
from scope_recall.adapters.hermes.installation import (
    unapproved_local_platforms as _unapproved_local_platforms,
)
from scope_recall.adapters.hermes.installation import (
    unapproved_owner_logins as _unapproved_owner_logins,
)
from scope_recall.adapters.hermes.shared_entries import attachment_path, load_binding_for_home

from .install_common import (
    REPO_ROOT,
    SKILLS,
    InstallError,
    InstallPlan,
    normalized_path,
    reject_symlink_chain,
    validate_identifier,
)

# Hermes 0.21+ ``_memory_provider_init_kwargs`` hardcodes agent_workspace="hermes".
# The public installer must bind that exact host value; identifiers are not aliased.
DEFAULT_AGENT_WORKSPACE = "hermes"
DIST_HERMES = REPO_ROOT / "distribution" / "hermes"


def profile_identity(home: Path) -> str:
    """Infer the host's profile name from its native or custom root without importing Hermes."""
    resolved = home.resolve()
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    native = (
        Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / ("hermes" + suffix)
        if sys.platform == "win32"
        else Path.home() / (".hermes" + suffix)
    ).resolve()
    if resolved.is_relative_to(native):
        root = native
    else:
        root = resolved.parent.parent if resolved.parent.name == "profiles" else resolved
    if resolved == root:
        return "default"
    parts = resolved.relative_to(root).parts
    if len(parts) == 2 and parts[0] == "profiles" and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", parts[1]):
        return parts[1]
    return "custom"


def host_owned_plugin(home: Path, target: Path) -> bool:
    """Recognize only the host's installed plugin namespace, using its external install record."""
    if normalized_path(target) != normalized_path(home_plugin_dir(home)):
        return False
    metadata = home / "plugins" / ".install-metadata.json"
    try:
        if metadata.stat().st_size > 1024 * 1024:
            return False
        records = json.loads(metadata.read_text(encoding="utf-8"))
        manifest = yaml.safe_load((target / "plugin.yaml").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, yaml.YAMLError):
        return False
    record = records.get("scope-recall") if isinstance(records, dict) else None
    return bool(
        isinstance(record, dict)
        and record.get("source")
        and record.get("revision")
        and isinstance(manifest, dict)
        and manifest.get("name") == "scope-recall"
    )


def data_dir(instance_root: Path) -> Path:
    return instance_root / "scope-recall"


def config_path(instance_root: Path) -> Path:
    """What the home binds with: the pointer of a shared store's entry, or its own manifest."""
    pointer = attachment_path(instance_root)
    return pointer if pointer.is_file() else data_dir(instance_root) / "installation.json"


def instance_wrapper_files(instance_root: Path) -> tuple[Path, ...]:
    """The skills live under the Hermes HOME, outside the plugin directory."""
    return tuple(instance_root / "skills" / name / "SKILL.md" for name in SKILLS)


def home_plugin_dir(instance_root: Path) -> Path:
    """The one plugin directory that may sit inside the home: where Hermes looks a memory provider up by name.

    Hermes reads a provider's ``plugin.yaml``, and with it the core the wrapper declares, from
    ``<home>/plugins/<name>/`` or from the installed core's own directory.  Once the environment Hermes runs
    has lost the core, only this one is left.
    """
    return instance_root / "plugins" / "scope-recall"


def host_config_files(target_plugin_dir: Path) -> tuple[Path, ...]:
    """The wrapper is the installer's own; Hermes's config.yaml is the operator's to change."""
    return ()


def validate_options(agent_workspace: str | None, env_file: Path | str | None) -> tuple[str, Path | None]:
    """Hermes processes inherit the gateway environment and must not carry a
    second credential path; the audience workspace defaults to the host value."""
    workspace = "" if agent_workspace is None else str(agent_workspace).strip()
    workspace = validate_identifier(workspace or DEFAULT_AGENT_WORKSPACE, "agent_workspace")
    if env_file is not None and str(env_file).strip() != "":
        raise InstallError("env_file is only used for Codex installation")
    return workspace, None


#: What ``--local-platform`` accepts.
LOCAL_PLATFORM_CHOICES = tuple(sorted(LOCAL_PLATFORMS))


def validate_local_platforms(values: object) -> tuple[str, ...]:
    """Host surfaces that name no user, approved here as the owner's own: the
    Desktop chat panel and ``hermes --tui``.  Anything else keeps failing closed."""
    try:
        return normalize_local_platforms(list(values or ()))
    except (HermesIdentityError, TypeError) as exc:
        raise InstallError(str(exc)) from exc


def validate_owner_logins(values: object) -> tuple[str, ...]:
    """Dashboard logins approved as the owner's own, each ``<platform>=<login>`` on one local surface.
    The host passes a login there as the session's user, so it is never the local owner."""
    try:
        return tuple(f"{platform}={login}" for platform, login in normalize_owner_logins(list(values or ())))
    except (HermesIdentityError, TypeError) as exc:
        raise InstallError(str(exc)) from exc


def _non_pypi_version(version: str) -> bool:
    release = Version(version)
    return release.is_prerelease or release.is_devrelease or release.local is not None


def environment_notes(plan: InstallPlan, home_plugin: Path | None) -> tuple[str, ...]:
    """Plan notes for the plugin environment Hermes builds from the home plugin's declaration.

    Only a build that is not on PyPI needs one: the pinned version resolves only from a wheel source the operator
    supplies, and every later rebuild needs that source again.
    """
    if home_plugin is None or normalized_path(plan.target_plugin_dir) != normalized_path(home_plugin):
        return ()
    if not _non_pypi_version(__version__):
        return ()
    return (
        "Rebuild the Hermes plugin environment with hermes_cli.venv_sync and "
        "UV_FIND_LINKS=<directory containing this wheel>; repeat after every hermes update, "
        "or the rebuild will fail and the plugin will be disabled.",
    )


def wrapper_pyproject(version: str = __version__) -> str:
    """The ``pyproject.toml`` beside the wrapper in ``home_plugin_dir``: the same core, vector extra included.

    Hermes's package manager builds the environment it runs plugins in from each plugin's declaration, and a
    ``pyproject.toml`` there wins over ``plugin.yaml``. The vector extra stays in the declaration for installation
    compatibility; NumPy retrieval is part of the base dependency set. Development, pre-release and local versions
    are pinned too; Hermes must be given a wheel source when rebuilding their environment.
    """
    return (
        '[project]\nname = "hermes-scope-recall-plugin"\nversion = "0.0.0"\nrequires-python = ">=3.11"\n'
        f'dependencies = ["hermes-scope-recall[vectors]=={version}"]\n\n[tool.uv]\npackage = false\n'
    )


def adoptable_wrapper_files(target: Path) -> set[Path]:
    """Files of a Scope Recall wrapper someone put into ``home_plugin_dir`` by hand, which an install there replaces,
    copied to the backups first: its ``plugin.yaml`` names this plugin, and only the wrapper's own file names count.
    Anything else in the directory stays a conflict."""
    try:
        manifest = yaml.safe_load((target / "plugin.yaml").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return set()
    if not isinstance(manifest, dict) or manifest.get("name") != "scope-recall":
        return set()
    return {target / name for name in ("plugin.yaml", "__init__.py", "pyproject.toml") if (target / name).is_file()}


def wrapper_manifest(template: str, version: str = __version__) -> str:
    """The wrapper's ``plugin.yaml``: the template, plus the core it runs on when that is a release on PyPI.

    Hermes Desktop rebuilds the Python environment it runs plugins in on updates, dropping a core installed
    there by hand, and Hermes installs what a memory provider's manifest declares
    (``pip_dependencies``) when the provider is set up; it reads the manifest from ``home_plugin_dir`` once
    the core is gone.  The pin is exact, as the wrapper and the core are one release.  A
    pre-release, development or local build declares no ``pip_dependencies`` here; its home plugin's
    ``pyproject.toml`` pins the build and its wheel source must be supplied during environment sync.
    """
    if _non_pypi_version(version):
        return re.sub(r"(?m)^pip_dependencies:\r?\n(?:[ \t]+[^\n]*\n?)*", "", template)
    return template


def planned_files(plan: InstallPlan) -> dict[Path, str | bytes]:
    files: dict[Path, str | bytes] = {}
    host_owned = host_owned_plugin(plan.instance_root, plan.target_plugin_dir)
    for name in () if host_owned else ("__init__.py", "plugin.yaml"):
        source = DIST_HERMES / name
        if not source.is_file():
            raise InstallError(f"distribution template missing: {source}")
        text = source.read_text(encoding="utf-8")
        files[plan.target_plugin_dir / name] = wrapper_manifest(text) if name == "plugin.yaml" else text
    if not host_owned and normalized_path(plan.target_plugin_dir) == normalized_path(
        home_plugin_dir(plan.instance_root)
    ):
        files[plan.target_plugin_dir / "pyproject.toml"] = wrapper_pyproject()
    for name, source in SKILLS.items():
        files[plan.instance_root / "skills" / name / "SKILL.md"] = source.read_text(encoding="utf-8")
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
        local_platforms=plan.local_platforms,
        owner_logins=plan.owner_logins,
        test_mode=plan.test_mode,
    )
    return binding.installation_id


def unapproved_local_platforms(plan: InstallPlan) -> tuple[str, ...]:
    """The requested local surfaces an existing installation has not approved yet."""
    if not plan.local_platforms:
        return ()
    manifest = load_binding_for_home(plan.instance_root)
    return _unapproved_local_platforms(manifest, plan.local_platforms, agent_workspace=bound_workspace(manifest))


def unapproved_owner_logins(plan: InstallPlan) -> tuple[tuple[str, str], ...]:
    """The requested owner logins, as ``(platform, login)``, an existing installation has not approved yet."""
    if not plan.owner_logins:
        return ()
    manifest = load_binding_for_home(plan.instance_root)
    return _unapproved_owner_logins(manifest, plan.owner_logins, agent_workspace=bound_workspace(manifest))


def approve_local_platforms(plan: InstallPlan) -> None:
    """Write the approvals, surfaces and logins, into an existing installation's manifest; the store is not touched."""
    manifest = load_installation_manifest(plan.instance_root)
    write_installation_manifest(
        _approve_local_platforms(
            manifest, plan.local_platforms, agent_workspace=bound_workspace(manifest), logins=plan.owner_logins
        )
    )


def installation_id(instance_root: Path) -> str:
    return load_binding_for_home(instance_root).installation_id


def bound_workspace(manifest) -> str:
    """The unique owner workspace that installation reuse must preserve."""
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
    manifest = load_binding_for_home(plan.instance_root)
    if manifest.agent_id != plan.agent_id:
        raise InstallError("existing Hermes installation agent_id mismatch")
    if bound_workspace(manifest) != plan.agent_workspace:
        raise InstallError("existing Hermes installation agent_workspace mismatch")
    if manifest.test_mode != plan.test_mode:
        raise InstallError(
            f"existing Hermes installation test_mode mismatch: stored={manifest.test_mode}, requested={plan.test_mode}"
        )
    if not (manifest.data_directory / "memory.sqlite3").is_file():
        raise InstallError("existing Hermes installation database is missing")
    if manifest.entry_id is not None and (
        _unapproved_local_platforms(manifest, plan.local_platforms, agent_workspace=bound_workspace(manifest))
        or _unapproved_owner_logins(manifest, plan.owner_logins, agent_workspace=bound_workspace(manifest))
    ):
        # Its grants live in the shared store's manifest, which this installer does not write.
        raise InstallError(
            "a shared store entry's grants are in the store's manifest, which this installer does not write: "
            "add the surface's or the login's owner row with scope-recall grants"
        )


def purge_identity(instance_root: Path) -> tuple[Path, str, str, Path]:
    """Data directory, installation id, agent id and manifest path from the signed manifest."""
    if attachment_path(instance_root).exists():
        raise InstallError("an entry of a shared store is never purged from its home; detach it instead")
    manifest = load_installation_manifest(instance_root)
    reject_symlink_chain(manifest.data_directory)
    data_directory = manifest.data_directory.resolve()
    return data_directory, manifest.installation_id, manifest.agent_id, data_directory / "installation.json"
