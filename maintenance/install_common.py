"""Types and primitives shared by the install module family.

``install.py`` is the plan/apply entry.  ``install_codex.py``,
``install_claude_code.py``, ``install_hermes.py``, ``install_workbuddy.py`` and ``install_dsh.py``
each render one host's wrapper files (WorkBuddy's and dsh's: entries merged into their own
settings) and bind its instance behind the same function names, so the
entry picks a host module instead of branching on the host; ``install_client.py`` holds the functions
the four clients share.  ``install_receipt.py`` signs and verifies
the receipt; ``install_purge.py`` inventories what an explicit purge may
delete.
"""

from __future__ import annotations

import codecs
import json
import os
import re
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path
from typing import Any, Literal

import yaml
from scope_recall._version import __version__

from .backup import first_link

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_SHORT_DESCRIPTION = "Recall and manage shared, source-backed memory in Codex."
CODEX_LONG_DESCRIPTION = (
    "Scope Recall recalls relevant memories before a Codex turn and captures prompts and replies through turn "
    "and session hooks. MCP tools search memories, inspect their sources and history, and correct or forget them."
)
# Claude Code cannot tie a tool call to its conversation, so its MCP server offers no tool that changes the store.
CLAUDE_CODE_DESCRIPTION = (
    "In Claude Code, recall runs before a turn and prompts and replies are captured through "
    "turn and session hooks. MCP tools search memories and inspect their sources and history."
)
#: Appended by the plugins that reach a store on another machine.
REMOTE_STORE_NOTE = " The store runs on another machine and is reached through its remote entry server."
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
HostChoice = Literal["hermes", "codex", "claude-code", "workbuddy", "dsh"]
RECEIPT_FILENAME = ".scope-recall-install-receipt.json"
BACKUP_DIRNAME = ".scope-recall-backups"
#: What a store's runtime-config.json may weigh, for the shared commands that write it and the doctor that
#: reads it.  The shared worker's lists every scope of the store twice (its binding and its allowed scopes),
#: about 120 bytes a scope: 221 scopes make 58 KB, so a 64 KB limit would refuse the store's own worker config
#: an entry or two later.  At MAX_SHARED_SCOPES, 1024 scopes, that is about 250 KB; the limit leaves room for
#: longer scope ids.
RUNTIME_CONFIG_LIMIT = 1024 * 1024
_MAX_IDENTIFIER_LEN = 240
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class InstallError(RuntimeError):
    """Raised when install planning or apply cannot proceed safely."""


@cache
def plugin_metadata() -> tuple[str, str]:
    """Read and cache the declared author and product description for manifest builders.

    Other maintenance entry points can be imported without distribution metadata or a readable manifest. Only
    successful reads are cached, so an installation can retry after its package inputs have been restored.
    """
    distribution = "hermes-scope-recall"
    try:
        author = metadata(distribution)["Author"]
    except (PackageNotFoundError, KeyError, OSError, ValueError) as exc:
        raise InstallError(f"could not read plugin author from installed distribution {distribution}") from exc
    if not isinstance(author, str) or not author.strip():
        raise InstallError(f"could not read plugin author from installed distribution {distribution}")
    manifest_path = REPO_ROOT / "distribution" / "hermes" / "plugin.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InstallError(f"could not read plugin description from {manifest_path}") from exc
    description = manifest.get("description") if isinstance(manifest, dict) else None
    if not isinstance(description, str) or not description.strip():
        raise InstallError(f"could not read plugin description from {manifest_path}")
    return author, description


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
    #: Dashboard logins approved as the owner's own, each ``<platform>=<login>`` on one local surface.
    owner_logins: tuple[str, ...] = ()
    changes: list[PlannedChange] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    reuse_instance: bool = False
    #: Skill files an agent edited whose packaged copy has not changed, left as they are: each with the digest the
    #: receipt keeps for it, the package's (``install.plan_install``).
    kept: dict[str, str] = field(default_factory=dict)

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
            "owner_logins": list(self.owner_logins),
            "reuse_instance": self.reuse_instance,
            "conflicts": list(self.conflicts),
            "kept": sorted(self.kept),
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
    #: The host's own files this install merged its entries into (WorkBuddy's settings); never in the receipt.
    files_merged: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_written": list(self.files_written),
            "host_registration_pending": self.host_registration_pending,
            "hook_trust_pending": self.hook_trust_pending,
            "full_mode_unverified": self.full_mode_unverified,
            "receipt_path": self.receipt_path,
            "installation_id": self.installation_id,
            "backups": list(self.backups),
            "files_merged": list(self.files_merged),
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
    #: The host's own files this entry's entries are taken out of, everything else in them kept (WorkBuddy's).
    unmerged_files: list[str] = field(default_factory=list)

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
            "unmerged_files": list(self.unmerged_files),
        }


@dataclass
class UninstallResult:
    files_removed: list[str]
    memory_retained: bool
    purged: bool
    edited_files: list[str] = field(default_factory=list)
    purged_paths: list[str] = field(default_factory=list)
    retained_backups: list[str] = field(default_factory=list)
    unmerged_files: list[str] = field(default_factory=list)
    #: Copies of the host's own files, taken before this entry's entries came out of them.
    backups: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_removed": list(self.files_removed),
            "memory_retained": self.memory_retained,
            "purged": self.purged,
            "edited_files": list(self.edited_files),
            "purged_paths": list(self.purged_paths),
            "retained_backups": list(self.retained_backups),
            "unmerged_files": list(self.unmerged_files),
            "backups": list(self.backups),
        }


def normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path.resolve())))


def within(norm: str, root_norm: str) -> bool:
    """Whether a normalized path is the root itself or lies below it."""
    return norm == root_norm or norm.startswith(root_norm + os.sep)


def reject_symlink_chain(path: Path) -> None:
    link = first_link(path)
    if link is not None:
        raise InstallError(f"symlink or reparse paths are not allowed: {link}")


def read_host_file(path: Path) -> bytes | None:
    """What one of the host's own files that an install merges into holds; None when there is no such file yet."""
    reject_symlink_chain(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > RUNTIME_CONFIG_LIMIT:
        raise InstallError(f"{path} is not a file of at most {RUNTIME_CONFIG_LIMIT} bytes")
    return path.read_bytes()


def like_original(text: str, original: bytes | None) -> bytes:
    """``text`` written back as the host's file was: in its line endings (CRLF when it had any) and with its byte order
    mark if it had one."""
    if original is not None and b"\r\n" in original:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    return codecs.BOM_UTF8 + data if original is not None and original.startswith(codecs.BOM_UTF8) else data


def absolute(value: str | Path, field: str, *, error: type[BaseException] = InstallError) -> Path:
    """Expand ``~`` and refuse a relative path; the caller resolves after its own link checks."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise error(f"{field} must be absolute")
    return path


def require_absolute(path: Path, field: str) -> Path:
    expanded = absolute(path, field)
    reject_symlink_chain(expanded)
    resolved = expanded.resolve()
    if resolved.parent == resolved:
        raise InstallError(f"{field} must not be a filesystem root")
    return resolved


def require_interpreter(path: Path, field: str) -> Path:
    """Validate an interpreter through its real target, but keep the path as given.

    Managed interpreter layouts (hostedtoolcache, pyenv, homebrew) and every
    POSIX venv expose ``python`` as a symlink; the chain check inspects the
    destination rather than rejecting every launcher link, and a redirect
    inside the resolved chain is still refused.  What is recorded and later
    executed -- hooks, MCP launchers, the autostart task, the worker -- is the
    link itself: a venv's ``bin/python`` started by its resolved target runs
    without the venv on ``sys.path`` and cannot import this package.
    """

    expanded = absolute(path, field)
    resolved = expanded.resolve()
    reject_symlink_chain(resolved)
    if not resolved.is_file():
        raise InstallError(f"{field} must reference an existing file")
    return expanded


def safe_interpreter(path: Path, *, error_type=InstallError) -> Path:
    """``safe_path`` for interpreter executables: resolve, then verify the chain.

    Managed interpreter layouts (hostedtoolcache, pyenv, homebrew) expose
    ``python`` as a symlink into a versioned directory; the chain check must
    inspect the resolved destination instead of rejecting every launcher
    link. Redirects deeper than that single hop are still refused.
    """

    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    resolved = expanded.resolve(strict=True)
    reject_symlink_chain(resolved)
    if not resolved.is_file():
        raise error_type(f"{resolved} must reference an existing file")
    return resolved


def require_file(path: Path, field: str) -> Path:
    resolved = require_absolute(path, field)
    if not resolved.is_file():
        raise InstallError(f"{field} must reference an existing file")
    return resolved


def validate_roots(*paths: tuple[Path, str]) -> None:
    seen: list[tuple[str, str]] = []
    for path, label in paths:
        norm = normalized_path(path)
        for other, other_label in seen:
            if within(norm, other) or within(other, norm):
                raise InstallError(f"{label} overlaps {other_label}")
        seen.append((norm, label))


def validate_identifier(value: str, field: str) -> str:
    if len(value) > _MAX_IDENTIFIER_LEN:
        raise InstallError(f"{field} exceeds bounded length")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise InstallError(f"{field} format is invalid")
    return value


def validate_agent_id(agent_id: str) -> str:
    agent = agent_id.strip()
    if not agent:
        raise InstallError("agent_id is required")
    return validate_identifier(agent, "agent_id")


def validate_plugin_name(name: str) -> str:
    if not _PLUGIN_NAME_RE.fullmatch(name):
        raise InstallError("plugin directory name format is invalid")
    return name


def validate_host(host: str) -> HostChoice:
    if host == "hermes":
        return "hermes"
    if host == "codex":
        return "codex"
    if host == "claude-code":
        return "claude-code"
    if host == "workbuddy":
        return "workbuddy"
    if host == "dsh":
        return "dsh"
    raise InstallError("host must be 'hermes', 'codex', 'claude-code', 'workbuddy' or 'dsh'")


def manifest_version(version: str = PACKAGE_VERSION) -> str:
    """Semver spelling of the PEP 440 package version for host plugin manifests."""
    if ".dev" in version:
        return version.replace(".dev", "-dev.", 1)
    # Unanchored on purpose: with a trailing ``$`` a post-release suffix
    # (``X.Y.ZrcN.postM``) would stop the match and reach a plugin manifest
    # verbatim.  The X.Y.Z prefix keeps the pattern specific enough.
    return re.sub(r"(\d+\.\d+\.\d+)rc(\d+)", r"\1-rc.\2", version)


def json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
