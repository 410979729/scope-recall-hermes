"""What the installers of the hook-and-MCP clients share: Codex, Claude Code, WorkBuddy and dsh.

``install.py`` calls the same functions on every host's module.  A client takes an env file and none of Hermes's
options (an agent workspace, local platforms, owner logins), and keeps no wrapper or plugin directory inside its home:
the functions for those are here, and each client's module imports the ones it uses under the same names.

Claude Code, WorkBuddy and dsh have no store of their own: ``scope-recall attach --host <host>`` makes the home an
entry of a shared store first, and their installers only write what runs the entry.  The functions for such an entry
are here too, and those that name the client are on ``AttachedEntry``.  A Codex home is an entry or an installation of
its own, and ``install_codex.py`` binds either.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scope_recall.adapters.clients.config import CodexConfigError, load_shared_client
from scope_recall.adapters.hermes.shared_entries import attachment_path

from .install_common import InstallError, InstallPlan, require_file

# -- every client ---------------------------------------------------------------------------------------------------


def validate_options(agent_workspace: str | None, env_file: Path | str | None, label: str) -> tuple[str, Path | None]:
    """A client starts its hooks and MCP server with an environment of its own, so the installer may hand them a
    credential file; an agent workspace is a Hermes audience, refused for the client ``label`` names."""
    if agent_workspace is not None and str(agent_workspace).strip():
        raise InstallError(f"agent_workspace is not used for {label} installation")
    if env_file is None or str(env_file).strip() == "":
        return "", None
    return "", require_file(Path(env_file), "env_file")


def validate_local_platforms(values: object) -> tuple[str, ...]:
    """A client's installation has one local user and no audiences to approve."""
    if values:
        raise InstallError("local_platform is only used for Hermes installation")
    return ()


def validate_owner_logins(values: object) -> tuple[str, ...]:
    if values:
        raise InstallError("owner_login is only used for Hermes installation")
    return ()


def unapproved_owner_logins(plan: InstallPlan) -> tuple[tuple[str, str], ...]:
    return ()


def unapproved_local_platforms(plan: InstallPlan) -> tuple[str, ...]:
    return ()


def approve_local_platforms(plan: InstallPlan) -> None:
    return None


def instance_wrapper_files(instance_root: Path) -> tuple[Path, ...]:
    """A client keeps every wrapper in its plugin directory or in the host's own files."""
    return ()


def home_plugin_dir(instance_root: Path) -> None:
    """A client's host finds the plugin by its own configuration; no plugin directory sits inside the home."""
    return None


def host_config_files(target_plugin_dir: Path) -> tuple[Path, ...]:
    """The plugin is the installer's own; no file of the host's configuration is changed."""
    return ()


# -- a client whose home is only ever an entry of a shared store ----------------------------------------------------


def data_dir(instance_root: Path) -> Path:
    return attachment_path(instance_root).parent


def config_path(instance_root: Path) -> Path:
    return attachment_path(instance_root)


def purge_identity(instance_root: Path) -> tuple[Path, str, str, Path]:
    raise InstallError("an entry of a shared store is never purged from its home; detach it instead")


@dataclass(frozen=True)
class AttachedEntry:
    """The functions of such a client's installer that name it: ``host`` as its ``--host``, ``label`` in messages."""

    host: str
    label: str

    def validate_options(self, agent_workspace: str | None, env_file: Path | str | None) -> tuple[str, Path | None]:
        return validate_options(agent_workspace, env_file, self.label)

    def foreign_instance_entries(self, instance_root: Path) -> list[str]:
        """A home this installer is asked to create: the client's is only ever an attached one."""
        return [f"{instance_root} is not attached to a shared store; run scope-recall attach --host {self.host} first"]

    def initialize_instance(self, plan: InstallPlan) -> str:
        raise InstallError(
            f"{self.label} joins a shared store: attach its home first (scope-recall attach --host {self.host})"
        )

    def _bound(self, instance_root: Path):
        try:
            return load_shared_client(instance_root, self.host)
        except CodexConfigError as exc:
            raise InstallError(f"existing {self.label} binding is unusable: {exc}") from exc

    def installation_id(self, instance_root: Path) -> str:
        return self._bound(instance_root).installation_id

    def validate_reuse(self, plan: InstallPlan) -> None:
        config = self._bound(plan.instance_root)
        if config.agent_id != plan.agent_id:
            raise InstallError(f"existing {self.label} entry agent_id mismatch: the store's is " + config.agent_id)
        if config.test_mode != plan.test_mode:
            raise InstallError(
                f"existing {self.label} entry test_mode mismatch: stored={config.test_mode}, requested={plan.test_mode}"
            )
