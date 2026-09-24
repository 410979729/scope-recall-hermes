"""Types and primitives shared by the install module family.

``install.py`` is the plan/apply entry.  ``install_codex.py``,
``install_claude_code.py`` and ``install_hermes.py`` each render one host's
wrapper files and bind its instance behind the same function names, so the
entry picks a host module instead of branching on the host.  ``install_receipt.py`` signs and verifies
the receipt; ``install_purge.py`` inventories what an explicit purge may
delete.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from scope_recall._version import __version__

from .backup import _first_link

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SKILL = Path(__file__).with_name("skills") / "scope-recall-setup" / "SKILL.md"
#: Every skill an install carries, under the folder name the host discovers it by.
#: ``scope-recall-setup`` is for installing and upgrading.  ``scope-recall-memory``
#: is for what a person asks about their own memory (what is remembered, where it
#: came from, whether it still holds) and for correcting, muting and deleting, with
#: what a deletion takes with it said before it is done.
SKILLS: dict[str, Path] = {
    "scope-recall-setup": SETUP_SKILL,
    "scope-recall-memory": Path(__file__).with_name("skills") / "scope-recall-memory" / "SKILL.md",
}
PACKAGE_VERSION = __version__
HostChoice = Literal["hermes", "codex", "claude-code"]
RECEIPT_FILENAME = ".scope-recall-install-receipt.json"
BACKUP_DIRNAME = ".scope-recall-backups"
_MAX_IDENTIFIER_LEN = 240
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class InstallError(RuntimeError):
    """Raised when install planning or apply cannot proceed safely."""


@dataclass(frozen=True)
class PlannedChange:
    action: str
    path: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {"action": self.action, "path": self.path}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class InstallPlan:
    host: HostChoice
    target_plugin_dir: Path
    instance_root: Path
    #: The Codex workspace a local Codex installation maps; a host that maps none has ``None``.
    project_root: Path | None
    agent_id: str
    python_executable: Path
    test_mode: bool = False
    agent_workspace: str = ""
    env_file: Path | None = None
    local_platforms: tuple[str, ...] = ()
    changes: list[PlannedChange] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    reuse_instance: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "target_plugin_dir": str(self.target_plugin_dir),
            "instance_root": str(self.instance_root),
            "project_root": str(self.project_root) if self.project_root is not None else None,
            "agent_id": self.agent_id,
            "python_executable": str(self.python_executable),
            "test_mode": self.test_mode,
            "agent_workspace": self.agent_workspace,
            "env_file": str(self.env_file) if self.env_file is not None else None,
            "local_platforms": list(self.local_platforms),
            "reuse_instance": self.reuse_instance,
            "conflicts": list(self.conflicts),
            "changes": [item.to_dict() for item in self.changes],
        }


@dataclass
class InstallResult:
    files_written: list[str]
    host_registration_pending: bool
    hook_trust_pending: bool
    full_mode_unverified: bool
    receipt_path: str
    installation_id: str
    backups: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_written": list(self.files_written),
            "host_registration_pending": self.host_registration_pending,
            "hook_trust_pending": self.hook_trust_pending,
            "full_mode_unverified": self.full_mode_unverified,
            "receipt_path": self.receipt_path,
            "installation_id": self.installation_id,
            "backups": list(self.backups),
        }


@dataclass
class UninstallPlan:
    host: HostChoice
    instance_root: Path
    target_plugin_dir: Path
    files_to_remove: list[str] = field(default_factory=list)
    edited_files: list[str] = field(default_factory=list)
    retain_memory: bool = True
    purge_allowed: bool = False
    purge_paths: list[str] = field(default_factory=list)
    retained_backups: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "instance_root": str(self.instance_root),
            "target_plugin_dir": str(self.target_plugin_dir),
            "files_to_remove": list(self.files_to_remove),
            "edited_files": list(self.edited_files),
            "retain_memory": self.retain_memory,
            "purge_allowed": self.purge_allowed,
            "purge_paths": list(self.purge_paths),
            "retained_backups": list(self.retained_backups),
            "conflicts": list(self.conflicts),
        }


@dataclass
class UninstallResult:
    files_removed: list[str]
    memory_retained: bool
    purged: bool
    edited_files: list[str] = field(default_factory=list)
    purged_paths: list[str] = field(default_factory=list)
    retained_backups: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_removed": list(self.files_removed),
            "memory_retained": self.memory_retained,
            "purged": self.purged,
            "edited_files": list(self.edited_files),
            "purged_paths": list(self.purged_paths),
            "retained_backups": list(self.retained_backups),
        }


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path.resolve())))


def _within(norm: str, root_norm: str) -> bool:
    """Whether a normalized path is the root itself or lies below it."""
    return norm == root_norm or norm.startswith(root_norm + os.sep)


def _reject_symlink_chain(path: Path) -> None:
    link = _first_link(path)
    if link is not None:
        raise InstallError(f"symlink or reparse paths are not allowed: {link}")


def _absolute(value: str | Path, field: str, *, error: type[BaseException] = InstallError) -> Path:
    """Expand ``~`` and refuse a relative path; the caller resolves after its own link checks."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise error(f"{field} must be absolute")
    return path


def _require_absolute(path: Path, field: str) -> Path:
    expanded = _absolute(path, field)
    _reject_symlink_chain(expanded)
    resolved = expanded.resolve()
    if resolved.parent == resolved:
        raise InstallError(f"{field} must not be a filesystem root")
    return resolved


def _require_interpreter(path: Path, field: str) -> Path:
    """Validate an interpreter through its real target, but keep the path as given.

    Managed interpreter layouts (hostedtoolcache, pyenv, homebrew) and every
    POSIX venv expose ``python`` as a symlink; the chain check inspects the
    destination rather than rejecting every launcher link, and a redirect
    inside the resolved chain is still refused.  What is recorded and later
    executed -- hooks, MCP launchers, the autostart task, the worker -- is the
    link itself: a venv's ``bin/python`` started by its resolved target runs
    without the venv on ``sys.path`` and cannot import this package (#87).
    """

    expanded = _absolute(path, field)
    resolved = expanded.resolve()
    _reject_symlink_chain(resolved)
    if not resolved.is_file():
        raise InstallError(f"{field} must reference an existing file")
    return expanded


def _safe_interpreter(path: Path, *, error_type=InstallError) -> Path:
    """``_safe_path`` for interpreter executables: resolve, then verify the chain.

    Managed interpreter layouts (hostedtoolcache, pyenv, homebrew) expose
    ``python`` as a symlink into a versioned directory; the chain check must
    inspect the resolved destination instead of rejecting every launcher
    link. Redirects deeper than that single hop are still refused.
    """

    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    resolved = expanded.resolve(strict=True)
    _reject_symlink_chain(resolved)
    if not resolved.is_file():
        raise error_type(f"{resolved} must reference an existing file")
    return resolved


def _require_file(path: Path, field: str) -> Path:
    resolved = _require_absolute(path, field)
    if not resolved.is_file():
        raise InstallError(f"{field} must reference an existing file")
    return resolved


def _validate_roots(*paths: tuple[Path, str]) -> None:
    seen: list[tuple[str, str]] = []
    for path, label in paths:
        norm = _norm(path)
        for other, other_label in seen:
            if _within(norm, other) or _within(other, norm):
                raise InstallError(f"{label} overlaps {other_label}")
        seen.append((norm, label))


def _validate_identifier(value: str, field: str) -> str:
    if len(value) > _MAX_IDENTIFIER_LEN:
        raise InstallError(f"{field} exceeds bounded length")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise InstallError(f"{field} format is invalid")
    return value


def _validate_agent_id(agent_id: str) -> str:
    agent = agent_id.strip()
    if not agent:
        raise InstallError("agent_id is required")
    return _validate_identifier(agent, "agent_id")


def _validate_plugin_name(name: str) -> str:
    if not _PLUGIN_NAME_RE.fullmatch(name):
        raise InstallError("plugin directory name format is invalid")
    return name


def _validate_host(host: str) -> HostChoice:
    if host == "hermes":
        return "hermes"
    if host == "codex":
        return "codex"
    if host == "claude-code":
        return "claude-code"
    raise InstallError("host must be 'hermes', 'codex' or 'claude-code'")


def _manifest_version(version: str = PACKAGE_VERSION) -> str:
    """Semver spelling of the PEP 440 package version for host plugin manifests."""
    if ".dev" in version:
        return version.replace(".dev", "-dev.", 1)
    # Unanchored on purpose: a trailing ``$`` stopped matching the moment a
    # post-release suffix appeared, and ``3.1.0rc10.post16`` reached a plugin
    # manifest verbatim.  The X.Y.Z prefix keeps the pattern specific enough.
    return re.sub(r"(\d+\.\d+\.\d+)rc(\d+)", r"\1-rc.\2", version)


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
