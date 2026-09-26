"""Hermes host: the distribution templates and setup skill it installs, plus the
manifest-bound instance a receipt-backed uninstall verifies."""
from __future__ import annotations

from pathlib import Path

from packaging.version import Version

from scope_recall._version import __version__
from scope_recall.adapters.hermes.audiences import LOCAL_PLATFORMS, HermesIdentityError, normalize_local_platforms
from scope_recall.adapters.hermes.installation import (
    approve_local_platforms as _approve_local_platforms,
    attachment_path,
    install_hermes_scope_recall,
    load_binding_for_home,
    load_installation_manifest,
    unapproved_local_platforms as _unapproved_local_platforms,
    write_installation_manifest,
)

from .install_common import (
    REPO_ROOT,
    SKILLS,
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
    has lost the core, only this one is left (#135).
    """
    return instance_root / "plugins" / "scope-recall"


def validate_options(agent_workspace: str | None, env_file: Path | str | None) -> tuple[str, Path | None]:
    """Hermes processes inherit the gateway environment and must not carry a
    second credential path; the audience workspace defaults to the host value."""
    workspace = "" if agent_workspace is None else str(agent_workspace).strip()
    workspace = _validate_identifier(workspace or DEFAULT_AGENT_WORKSPACE, "agent_workspace")
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


def wrapper_manifest(template: str, version: str = __version__) -> str:
    """The wrapper's ``plugin.yaml``: the template, plus the core it runs on when that is a release on PyPI.

    Hermes Desktop rebuilds the Python environment it runs plugins in on updates, dropping a core installed
    there by hand (#135), and Hermes installs what a memory provider's manifest declares
    (``pip_dependencies``) when the provider is set up; it reads the manifest from ``home_plugin_dir`` once
    the core is gone.  The pin is exact, as the wrapper and the core are one release.  A
    pre-release, development or local build is not on PyPI, and a requirement that cannot be resolved fails
    Hermes' whole build, so such a build declares nothing and is installed by hand.
    """
    release = Version(version)
    if release.is_prerelease or release.is_devrelease or release.local is not None:
        return template
    return template.rstrip("\n") + f'\npip_dependencies:\n  - "hermes-scope-recall[lancedb]=={version}"\n'


def planned_files(plan: InstallPlan) -> dict[Path, str | bytes]:
    files: dict[Path, str | bytes] = {}
    for name in ("__init__.py", "plugin.yaml"):
        source = DIST_HERMES / name
        if not source.is_file():
            raise InstallError(f"distribution template missing: {source}")
        text = source.read_text(encoding="utf-8")
        files[plan.target_plugin_dir / name] = wrapper_manifest(text) if name == "plugin.yaml" else text
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
        test_mode=plan.test_mode,
    )
    return binding.installation_id


def unapproved_local_platforms(plan: InstallPlan) -> tuple[str, ...]:
    """The requested local surfaces an existing installation has not approved yet."""
    if not plan.local_platforms:
        return ()
    manifest = load_binding_for_home(plan.instance_root)
    return _unapproved_local_platforms(manifest, plan.local_platforms, agent_workspace=_bound_workspace(manifest))


def approve_local_platforms(plan: InstallPlan) -> None:
    """Write the approvals into an existing installation's manifest; the store is not touched."""
    manifest = load_installation_manifest(plan.instance_root)
    write_installation_manifest(
        _approve_local_platforms(manifest, plan.local_platforms, agent_workspace=_bound_workspace(manifest)))


def installation_id(instance_root: Path) -> str:
    return load_binding_for_home(instance_root).installation_id


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
    manifest = load_binding_for_home(plan.instance_root)
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
    if manifest.entry_id is not None and _unapproved_local_platforms(
            manifest, plan.local_platforms, agent_workspace=_bound_workspace(manifest)):
        # Its grants live in the shared store's manifest, which this installer does not write.
        raise InstallError("a shared store entry keeps the grants it was attached with; approve a local "
                           "surface in the home's own installation before attaching it")


def purge_identity(instance_root: Path) -> tuple[Path, str, str, Path]:
    """Data directory, installation id, agent id and manifest path from the signed manifest."""
    if attachment_path(instance_root).exists():
        raise InstallError("an entry of a shared store is never purged from its home; detach it instead")
    manifest = load_installation_manifest(instance_root)
    _reject_symlink_chain(manifest.data_directory)
    data_directory = manifest.data_directory.resolve()
    return data_directory, manifest.installation_id, manifest.agent_id, data_directory / "installation.json"
