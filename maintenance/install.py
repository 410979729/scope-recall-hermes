"""Explicit plan/apply install and receipt-backed uninstall for v1.1 host wrappers."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import shlex
import sqlite3
import shutil
import uuid
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, Literal

from scope_recall.maintenance.doctor import _host_registration_status

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_package_version() -> str:
    path = REPO_ROOT / "_version.py"
    spec = spec_from_file_location("scope_recall._version", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"missing package version source: {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.__version__)


PACKAGE_VERSION = _load_package_version()

HostChoice = Literal["hermes", "codex"]
RECEIPT_SCHEMA = "scope-recall.install-receipt.v1"
RECEIPT_FILENAME = ".scope-recall-install-receipt.json"
BACKUP_DIRNAME = ".scope-recall-backups"
CODEX_HOOK_EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
        "Interrupt",
        "SessionEnd",
    }
)
_MAX_AGENT_ID_LEN = 240
_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Hermes 0.21+ `_memory_provider_init_kwargs` hardcodes agent_workspace="hermes".
# The public installer must bind that exact host value; identifiers are not aliased.
HERMES_DEFAULT_AGENT_WORKSPACE = "hermes"
DIST_HERMES = REPO_ROOT / "distribution" / "hermes"
DIST_CODEX = REPO_ROOT / "distribution" / "codex" / "scope-recall"


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
    project_root: Path
    agent_id: str
    python_executable: Path
    test_mode: bool = False
    agent_workspace: str = ""
    env_file: Path | None = None
    changes: list[PlannedChange] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    reuse_instance: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "target_plugin_dir": str(self.target_plugin_dir),
            "instance_root": str(self.instance_root),
            "project_root": str(self.project_root),
            "agent_id": self.agent_id,
            "python_executable": str(self.python_executable),
            "test_mode": self.test_mode,
            "agent_workspace": self.agent_workspace,
            "env_file": str(self.env_file) if self.env_file is not None else None,
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


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _reject_symlink_chain(path: Path) -> None:
    current = path
    while True:
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        if current.is_symlink() or attributes & 0x400:
            raise InstallError(f"symlink or reparse paths are not allowed: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _is_filesystem_root(path: Path) -> bool:
    return path.parent == path


def _require_absolute(path: Path, field: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise InstallError(f"{field} must be absolute")
    _reject_symlink_chain(expanded)
    resolved = expanded.resolve()
    if _is_filesystem_root(resolved):
        raise InstallError(f"{field} must not be a filesystem root")
    return resolved


def _require_file(path: Path, field: str) -> Path:
    resolved = _require_absolute(path, field)
    if not resolved.is_file():
        raise InstallError(f"{field} must reference an existing file")
    return resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    a = _norm(left)
    b = _norm(right)
    if a == b:
        return True
    sep = os.sep
    return a.startswith(b + sep) or b.startswith(a + sep)


def _validate_roots(*paths: tuple[Path, str]) -> None:
    seen: list[tuple[Path, str]] = []
    for path, label in paths:
        for other, other_label in seen:
            if _paths_overlap(path, other):
                raise InstallError(f"{label} overlaps {other_label}")
        seen.append((path, label))


def _validate_agent_id(agent_id: str) -> str:
    agent = agent_id.strip()
    if not agent:
        raise InstallError("agent_id is required")
    if len(agent) > _MAX_AGENT_ID_LEN:
        raise InstallError("agent_id exceeds bounded length")
    if not _AGENT_ID_RE.fullmatch(agent):
        raise InstallError("agent_id format is invalid")
    return agent


def _validate_env_file(env_file: Path | str | None, host: HostChoice) -> Path | None:
    """Codex starts the MCP server and hooks with its own environment, so the
    installer may hand them a credential file; Hermes processes inherit the
    gateway environment and must not carry a second credential path."""
    if env_file is None or str(env_file).strip() == "":
        return None
    if host != "codex":
        raise InstallError("env_file is only used for Codex installation")
    return _require_file(Path(env_file), "env_file")


def _validate_agent_workspace(agent_workspace: str | None, host: HostChoice) -> str:
    raw = "" if agent_workspace is None else str(agent_workspace).strip()
    if host == "codex":
        if raw:
            raise InstallError("agent_workspace is not used for Codex installation")
        return ""
    workspace = raw or HERMES_DEFAULT_AGENT_WORKSPACE
    if len(workspace) > _MAX_AGENT_ID_LEN:
        raise InstallError("agent_workspace exceeds bounded length")
    if not _AGENT_ID_RE.fullmatch(workspace):
        raise InstallError("agent_workspace format is invalid")
    return workspace


def _hermes_bound_workspace(manifest: Any) -> str:
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


def _validate_plugin_name(name: str) -> str:
    if not _PLUGIN_NAME_RE.fullmatch(name):
        raise InstallError("plugin directory name format is invalid")
    return name


def _manifest_version(version: str = PACKAGE_VERSION) -> str:
    if ".dev" in version:
        return version.replace(".dev", "-dev.", 1)
    # Unanchored on purpose.  With a trailing ``$`` the rule silently stopped
    # applying the moment a post-release suffix appeared, so 3.1.0rc10.post16
    # was written into a plugin manifest verbatim -- not valid semver at all --
    # while plain 3.1.0rc10 normalised correctly.  The X.Y.Z prefix is what
    # makes the pattern specific enough without the anchor.
    return re.sub(r"(\d+\.\d+\.\d+)rc(\d+)", r"\1-rc.\2", version)


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _receipt_path(instance_root: Path) -> Path:
    return instance_root / RECEIPT_FILENAME


def _digest_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body.pop("receipt_sha256", None)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body["receipt_sha256"] = _sha256_text(encoded)
    return body


def _verify_receipt_digest(payload: dict[str, Any]) -> None:
    digest = payload.get("receipt_sha256")
    if type(digest) is not str or not digest:
        raise InstallError("receipt digest missing")
    body = dict(payload)
    body.pop("receipt_sha256", None)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if _sha256_text(encoded) != digest:
        raise InstallError("receipt digest mismatch")


def _validated_receipt_path(
    path_text: object,
    *,
    plugin_dir: Path,
    instance_root: Path,
    role: object,
) -> tuple[str, str, str]:
    if type(path_text) is not str or not path_text:
        raise InstallError("receipt file path invalid")
    path = Path(path_text)
    if not path.is_absolute():
        raise InstallError("receipt file path must be absolute")
    _reject_symlink_chain(path)
    resolved = path.resolve()
    norm = _norm(resolved)
    plugin_norm = _norm(plugin_dir)
    instance_norm = _norm(instance_root)
    if role == "plugin":
        if not (norm == plugin_norm or norm.startswith(plugin_norm + os.sep)):
            raise InstallError("receipt plugin path outside target_plugin_dir")
    elif role == "instance":
        if not (norm == instance_norm or norm.startswith(instance_norm + os.sep)):
            raise InstallError("receipt instance path outside instance_root")
    else:
        raise InstallError("receipt file role invalid")
    return norm, str(role), norm


def _owned_files_from_receipt(
    receipt: dict[str, Any],
    *,
    plugin_dir: Path,
    instance_root: Path,
) -> dict[str, str]:
    owned: dict[str, str] = {}
    files = receipt.get("files")
    if not isinstance(files, list):
        raise InstallError("receipt files invalid")
    for item in files:
        if not isinstance(item, dict):
            raise InstallError("receipt files invalid")
        path_text = item.get("path")
        sha = item.get("sha256")
        role = item.get("role")
        if type(sha) is not str or len(sha) != 64:
            raise InstallError("receipt file hash invalid")
        norm, _, _ = _validated_receipt_path(
            path_text,
            plugin_dir=plugin_dir,
            instance_root=instance_root,
            role=role,
        )
        if norm in owned and owned[norm] != sha:
            raise InstallError("receipt duplicate path")
        owned[norm] = sha
    return owned


def _load_receipt(instance_root: Path) -> dict[str, Any] | None:
    path = _receipt_path(instance_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError("existing install receipt is unreadable") from exc
    if not isinstance(payload, dict):
        raise InstallError("existing install receipt is invalid")
    if payload.get("schema_version") != RECEIPT_SCHEMA:
        raise InstallError("existing install receipt schema mismatch")
    _verify_receipt_digest(payload)
    return payload


def _validate_receipt_binding(
    receipt: dict[str, Any],
    *,
    host: HostChoice,
    instance_root: Path,
    target_plugin_dir: Path,
) -> dict[str, str]:
    if _validate_host(str(receipt.get("host") or "")) != host:
        raise InstallError("existing receipt host mismatch")
    if _norm(instance_root) != _norm(Path(str(receipt.get("instance_root") or ""))):
        raise InstallError("existing receipt instance_root mismatch")
    if _norm(target_plugin_dir) != _norm(Path(str(receipt.get("target_plugin_dir") or ""))):
        raise InstallError("existing receipt target_plugin_dir mismatch")
    if not str(receipt.get("installation_id") or "").strip():
        raise InstallError("existing receipt installation_id missing")
    return _owned_files_from_receipt(
        receipt,
        plugin_dir=target_plugin_dir,
        instance_root=instance_root,
    )


def _hook_argv(python_executable: Path, config_path: Path, *, env_file: Path | None = None) -> list[str]:
    argv = [
        str(python_executable),
        "-I",
        "-B",
        "-m",
        "scope_recall.adapters.codex.hook_entry",
        "--config",
        str(config_path),
    ]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
    return argv


WINDOWS_HOOK_LAUNCHER = "scope-recall-hook.cmd"


def _windows_hook_launcher_bytes(python_executable: Path, config_path: Path, *, env_file: Path | None = None) -> bytes:
    """UTF-8 .cmd so cmd.exe can start python without a PowerShell EncodedCommand tax."""
    argv = _hook_argv(python_executable, config_path, env_file=env_file)
    quoted = " ".join('"' + part.replace('"', "") + '"' for part in argv)
    text = "@echo off\r\nchcp 65001 >nul\r\n" + quoted + "\r\nexit /b %ERRORLEVEL%\r\n"
    return text.encode("utf-8")


def _write_windows_hook_launcher(path: Path, python_executable: Path, config_path: Path, *, env_file: Path | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_windows_hook_launcher_bytes(python_executable, config_path, env_file=env_file))
    return path


def _hook_command(
    python_executable: Path,
    config_path: Path,
    *,
    windows_launcher: Path | None = None,
    write_launcher: bool = True,
    env_file: Path | None = None,
) -> tuple[str, str]:
    argv = _hook_argv(python_executable, config_path, env_file=env_file)
    posix = shlex.join(argv)
    # Codex's Windows runner puts commandWindows inside cmd.exe /C. A
    # PowerShell EncodedCommand wrapper costs ~0.5-1.0s and pushes capture
    # past the frozen 2s hook timeout. A UTF-8 .cmd next to hooks.json keeps
    # Unicode/space paths literal without that tax.
    launcher = windows_launcher if windows_launcher is not None else Path(config_path).with_name(WINDOWS_HOOK_LAUNCHER)
    if write_launcher:
        _write_windows_hook_launcher(launcher, python_executable, config_path, env_file=env_file)
    return posix, str(launcher.resolve())


def _codex_hooks_json(
    python_executable: Path,
    config_path: Path,
    *,
    windows_launcher: Path | None = None,
    env_file: Path | None = None,
) -> dict[str, Any]:
    command, command_windows = _hook_command(
        python_executable,
        config_path,
        windows_launcher=windows_launcher,
        write_launcher=False,
        env_file=env_file,
    )
    hooks: dict[str, Any] = {}
    for event in sorted(CODEX_HOOK_EVENTS):
        hooks[event] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "commandWindows": command_windows,
                        "timeout": 2,
                    }
                ]
            }
        ]
    return {"hooks": hooks}


def _codex_mcp_json(
    python_executable: Path, config_path: Path, workspace: Path, *, env_file: Path | None = None
) -> dict[str, Any]:
    args = [
        "-I",
        "-B",
        "-m",
        "scope_recall.adapters.codex.mcp_entry",
        "--config",
        str(config_path),
        "--workspace",
        str(workspace),
    ]
    if env_file is not None:
        # Codex starts the server with its own environment; the key names the
        # runtime config declares are read from this file by the entry itself.
        args += ["--env-file", str(env_file)]
    return {"mcpServers": {"scope-recall": {"command": str(python_executable), "args": args}}}


def _codex_plugin_json(plugin_name: str) -> dict[str, Any]:
    return {
        "name": plugin_name,
        "version": _manifest_version(),
        "description": "Scope Recall local Codex plugin",
        "author": {"name": "Local developer"},
        "interface": {
            "displayName": "Scope Recall",
            "shortDescription": "Use Scope Recall in Codex.",
            "longDescription": "Scope Recall adds a local Codex plugin over the installed core.",
            "developerName": "Local developer",
            "category": "Productivity",
            "capabilities": [],
            "defaultPrompt": "Help me use Scope Recall.",
        },
        "mcpServers": "./.mcp.json",
    }


def _planned_codex_files(
    *,
    target_plugin_dir: Path,
    instance_root: Path,
    project_root: Path,
    python_executable: Path,
    plugin_name: str,
    env_file: Path | None = None,
) -> dict[Path, str | bytes]:
    config_path = instance_root / "codex-installation.json"
    launcher = target_plugin_dir / "hooks" / WINDOWS_HOOK_LAUNCHER
    return {
        target_plugin_dir / ".codex-plugin" / "plugin.json": _json_dump(_codex_plugin_json(plugin_name)),
        launcher: _windows_hook_launcher_bytes(python_executable, config_path, env_file=env_file),
        target_plugin_dir / "hooks" / "hooks.json": _json_dump(
            _codex_hooks_json(python_executable, config_path, windows_launcher=launcher, env_file=env_file)
        ),
        target_plugin_dir / "skills" / "scope-recall-setup" / "SKILL.md": (REPO_ROOT / "maintenance" / "skills" / "scope-recall-setup" / "SKILL.md").read_text(encoding="utf-8"),
        target_plugin_dir / ".mcp.json": _json_dump(
            _codex_mcp_json(python_executable, config_path, project_root, env_file=env_file)
        ),
    }


def _planned_hermes_files(target_plugin_dir: Path, instance_root: Path) -> dict[Path, str]:
    files: dict[Path, str] = {}
    for name in ("__init__.py", "plugin.yaml"):
        source = DIST_HERMES / name
        if not source.is_file():
            raise InstallError(f"distribution template missing: {source}")
        files[target_plugin_dir / name] = source.read_text(encoding="utf-8")
    files[instance_root / "skills" / "scope-recall-setup" / "SKILL.md"] = (REPO_ROOT / "maintenance" / "skills" / "scope-recall-setup" / "SKILL.md").read_text(encoding="utf-8")
    return files


def _foreign_plugin_entries(target: Path, allowed: set[str], owned: set[str]) -> list[str]:
    if not target.exists():
        return []
    foreign: list[str] = []
    for path in sorted(target.rglob("*")):
        if path.is_dir():
            continue
        norm = _norm(path)
        if norm in allowed or norm in owned:
            continue
        foreign.append(str(path))
    return foreign


def _instance_foreign_entries(instance_root: Path, host: HostChoice, *, initialized: bool) -> list[str]:
    if not instance_root.exists():
        return []
    if host == "hermes":
        # A real Hermes HOME already contains host config, sessions, and other
        # plugins. Hermes maintenance only owns the scope-recall namespace and
        # receipt-backed files; host siblings are not foreign. An existing
        # unknown/unbound/conflicting managed namespace is still refused.
        namespace = instance_root / "scope-recall"
        if initialized:
            return []
        if namespace.exists():
            return [str(namespace)]
        return []
    allowed = {RECEIPT_FILENAME, BACKUP_DIRNAME}
    if initialized:
        allowed.update({"codex-installation.json", "data"})
    foreign: list[str] = []
    for child in instance_root.iterdir():
        if child.name in allowed:
            continue
        foreign.append(str(child))
    return foreign


def _validate_host(host: str) -> HostChoice:
    if host == "hermes":
        return "hermes"
    if host == "codex":
        return "codex"
    raise InstallError("host must be 'hermes' or 'codex'")


def _hermes_data_dir(instance_root: Path) -> Path:
    return instance_root / "scope-recall"


def _codex_config_path(instance_root: Path) -> Path:
    return instance_root / "codex-installation.json"


def _instance_initialized(host: HostChoice, instance_root: Path) -> bool:
    if host == "hermes":
        return (_hermes_data_dir(instance_root) / "installation.json").is_file()
    return _codex_config_path(instance_root).is_file()


def _validate_reuse_binding(
    host: HostChoice,
    instance_root: Path,
    agent_id: str,
    test_mode: bool,
    agent_workspace: str,
) -> None:
    if host == "hermes":
        from scope_recall.adapters.hermes.installation import load_installation_manifest

        manifest = load_installation_manifest(instance_root)
        if manifest.agent_id != agent_id:
            raise InstallError("existing Hermes installation agent_id mismatch")
        bound_workspace = _hermes_bound_workspace(manifest)
        if bound_workspace != agent_workspace:
            raise InstallError("existing Hermes installation agent_workspace mismatch")
        if manifest.test_mode != test_mode:
            raise InstallError(
                "existing Hermes installation test_mode mismatch: "
                f"stored={manifest.test_mode}, requested={test_mode}"
            )
        db_path = manifest.data_directory / "memory.sqlite3"
        if not db_path.is_file():
            raise InstallError("existing Hermes installation database is missing")
        return
    from scope_recall.adapters.codex.config import load_codex_config

    config = load_codex_config(_codex_config_path(instance_root))
    if config.agent_id != agent_id:
        raise InstallError("existing Codex installation agent_id mismatch")
    if config.test_mode != test_mode:
        raise InstallError(
            "existing Codex installation test_mode mismatch: "
            f"stored={config.test_mode}, requested={test_mode}"
        )


def _initialize_instance(
    host: HostChoice,
    *,
    instance_root: Path,
    project_root: Path,
    agent_id: str,
    test_mode: bool,
    agent_workspace: str,
) -> str:
    if host == "hermes":
        from scope_recall.adapters.hermes.installation import install_hermes_scope_recall

        binding, _core = install_hermes_scope_recall(
            instance_root,
            agent_id=agent_id,
            platform="cli",
            user_id="local",
            agent_workspace=agent_workspace,
            test_mode=test_mode,
        )
        return binding.installation_id
    from scope_recall.adapters.codex.config import install_codex_scope_recall

    config, _core = install_codex_scope_recall(
        instance_root,
        project_root=project_root,
        agent_id=agent_id,
        allow_owner_private=True,
        test_mode=test_mode,
    )
    return config.installation_id


def _installation_id_from_instance(host: HostChoice, instance_root: Path) -> str:
    if host == "hermes":
        from scope_recall.adapters.hermes.installation import load_installation_manifest

        return load_installation_manifest(instance_root).installation_id
    from scope_recall.adapters.codex.config import load_codex_config

    return load_codex_config(_codex_config_path(instance_root)).installation_id


@dataclass(frozen=True)
class _PurgeInventory:
    """Exact, receipt-bound files that an explicit purge may remove."""

    data_directory: Path
    installation_id: str
    agent_id: str
    files: tuple[Path, ...]
    retained_files: tuple[Path, ...]
    vector_files: tuple[Path, ...]
    retained_backups: tuple[Path, ...]


def _purge_guard(data_directory: Path):
    """Hold the cooperative writer and physical-retained locks for purge."""

    from scope_recall.file_lock import advisory_file_lock
    from scope_recall.writer_lease import holding_truth_writer_lease, truth_writer_process_snapshot

    @contextmanager
    def guarded():
        try:
            snapshot = truth_writer_process_snapshot(data_directory)
            if snapshot.get("same_process_holder_count", 0) or snapshot.get("connection_pin_count", 0):
                raise InstallError("purge_busy:truth_writer")
            # ``save_config`` is the existing exclusive maintenance role.  It
            # uses the same OS lease as all Core writers and therefore makes a
            # purge fail closed when a provider process still owns the store.
            with holding_truth_writer_lease(data_directory, role="save_config"):
                with advisory_file_lock(
                    data_directory / "scope-recall-retained.lock",
                    timeout_seconds=0,
                ):
                    with advisory_file_lock(
                        data_directory / "runtime-worker.lock",
                        timeout_seconds=0,
                    ):
                        yield
        except TimeoutError as exc:
            raise InstallError("purge_busy:owned_lock") from exc
        except sqlite3.OperationalError as exc:
            if "busy" in str(exc).casefold() or "locked" in str(exc).casefold():
                raise InstallError("purge_busy:truth_database") from exc
            raise
        except RuntimeError as exc:
            if "truth_writer_busy" in str(exc):
                raise InstallError("purge_busy:truth_writer") from exc
            raise

    return guarded()


def _purge_identity(plan: UninstallPlan, receipt: dict[str, Any]) -> tuple[Path, str, str, Path]:
    """Resolve the data directory only through the signed install identity."""

    if plan.host == "hermes":
        from scope_recall.adapters.hermes.installation import load_installation_manifest

        manifest = load_installation_manifest(plan.instance_root)
        _reject_symlink_chain(manifest.data_directory)
        data_directory = manifest.data_directory.resolve()
        installation_id = manifest.installation_id
        agent_id = manifest.agent_id
        config_path = data_directory / "installation.json"
        expected = _hermes_data_dir(plan.instance_root).resolve()
    else:
        from scope_recall.adapters.codex.config import load_codex_config

        config = load_codex_config(_codex_config_path(plan.instance_root))
        _reject_symlink_chain(config.data_directory)
        _reject_symlink_chain(_codex_config_path(plan.instance_root))
        data_directory = config.data_directory.resolve()
        installation_id = config.installation_id
        agent_id = config.agent_id
        config_path = _codex_config_path(plan.instance_root).resolve()
        expected = (plan.instance_root / "data").resolve()

    if _norm(data_directory) != _norm(expected):
        raise InstallError("purge_refused:data_directory_binding")
    if str(receipt.get("installation_id") or "") != installation_id:
        raise InstallError("purge_refused:installation_binding")
    if str(receipt.get("agent_id") or "") != agent_id:
        raise InstallError("purge_refused:agent_binding")
    _reject_symlink_chain(plan.instance_root)
    _reject_symlink_chain(data_directory)
    if config_path.is_symlink() or not config_path.is_file():
        raise InstallError("purge_refused:installation_manifest")
    return data_directory, installation_id, agent_id, config_path


def _safe_owned_files(root: Path, *, label: str) -> tuple[Path, ...]:
    """Return regular files below an owned directory, rejecting links."""

    _reject_symlink_chain(root)
    if not root.exists():
        return ()
    if not root.is_dir():
        raise InstallError(f"purge_refused:{label}_not_directory")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        _reject_symlink_chain(path)
        if path.is_dir():
            continue
        if not path.is_file():
            raise InstallError(f"purge_refused:{label}_special_file")
        files.append(path.resolve())
    return tuple(files)


def _purge_inventory(plan: UninstallPlan, receipt: dict[str, Any]) -> _PurgeInventory:
    data_directory, installation_id, agent_id, config_path = _purge_identity(plan, receipt)
    db_path = data_directory / "memory.sqlite3"
    if db_path.is_symlink() or not db_path.is_file():
        raise InstallError("purge_refused:database_missing")
    _reject_symlink_chain(db_path)
    if (data_directory / "restore-required.json").exists():
        raise InstallError("purge_refused:restore_pending")

    retained_root = data_directory / "retained"
    retained_files = _safe_owned_files(retained_root, label="retained")
    vector_root = data_directory / "vectors"
    vector_files = _safe_owned_files(vector_root, label="vectors")

    expected_retained: set[Path] = set()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        meta = conn.execute(
            "SELECT agent_id,installation_id,data_directory FROM instance_meta WHERE singleton=1"
        ).fetchone()
        if meta is None or (
            str(meta["agent_id"]) != agent_id
            or str(meta["installation_id"]) != installation_id
            or _norm(Path(str(meta["data_directory"]))) != _norm(data_directory)
        ):
            raise InstallError("purge_refused:database_identity")
        rows = conn.execute(
            "SELECT blob_json FROM artifact_versions WHERE blob_json IS NOT NULL"
        ).fetchall()
        for row in rows:
            try:
                blob = json.loads(str(row["blob_json"]))
            except (TypeError, ValueError) as exc:
                raise InstallError("purge_refused:attachment_metadata") from exc
            if not isinstance(blob, dict):
                raise InstallError("purge_refused:attachment_metadata")
            if blob.get("installation_id") != installation_id or blob.get("agent_id") != agent_id:
                raise InstallError("purge_refused:attachment_identity")
            relative = Path(str(blob.get("relative_path") or ""))
            sha = str(blob.get("sha256") or "")
            if (
                len(sha) != 64
                or any(char not in "0123456789abcdef" for char in sha)
                or relative.parts != ("retained", sha)
            ):
                raise InstallError("purge_refused:attachment_path")
            expected_retained.add((data_directory / relative).resolve())
        conn.rollback()
        conn.close()
    except InstallError:
        try:
            if conn is not None:
                conn.rollback()
                conn.close()
        except Exception:
            pass
        raise
    except sqlite3.OperationalError as exc:
        try:
            if conn is not None:
                conn.rollback()
                conn.close()
        except Exception:
            pass
        if "busy" in str(exc).casefold() or "locked" in str(exc).casefold():
            raise InstallError("purge_busy:truth_database") from exc
        raise InstallError("purge_refused:database_read") from exc

    retained_set = set(retained_files)
    if retained_set - expected_retained:
        raise InstallError("purge_refused:unknown_retained_file")
    missing = expected_retained - retained_set
    if missing:
        # A missing retained blob is already physically gone; it is safe to
        # repeat the purge, but it is not evidence of an external file.
        pass
    files = [config_path.resolve(), db_path.resolve()]
    for sidecar in (db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        if sidecar.exists():
            _reject_symlink_chain(sidecar)
            if not sidecar.is_file():
                raise InstallError("purge_refused:database_sidecar")
            files.append(sidecar.resolve())
    files.extend(retained_files)
    files.extend(vector_files)
    backups = ()
    backup_root = plan.instance_root / BACKUP_DIRNAME
    if backup_root.exists():
        _reject_symlink_chain(backup_root)
        backups = (backup_root.resolve(),)
    return _PurgeInventory(
        data_directory=data_directory,
        installation_id=installation_id,
        agent_id=agent_id,
        files=tuple(dict.fromkeys(files)),
        retained_files=retained_files,
        vector_files=vector_files,
        retained_backups=backups,
    )


def _purge_owned_data(plan: UninstallPlan, receipt: dict[str, Any]) -> tuple[list[str], list[str]]:
    data_directory, _installation_id, _agent_id, _config_path = _purge_identity(plan, receipt)
    with _purge_guard(data_directory):
        inventory = _purge_inventory(plan, receipt)
        removed: list[str] = []
        # Delete only the inventory under the verified Core data directory;
        # instance_root siblings (host sessions/config/backups) are untouched.
        for path in inventory.files:
            _reject_symlink_chain(path)
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        for directory in (data_directory / "retained", data_directory / "vectors"):
            _reject_symlink_chain(directory)
            if directory.is_dir() and not directory.is_symlink():
                for child in sorted(directory.rglob("*"), reverse=True):
                    _reject_symlink_chain(child)
                    if child.is_dir() and not child.is_symlink():
                        try:
                            child.rmdir()
                        except OSError:
                            pass
                try:
                    directory.rmdir()
                except OSError:
                    pass
        return removed, [str(path) for path in inventory.retained_backups]


def _atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    if isinstance(content, bytes):
        temp_path.write_bytes(content)
    else:
        temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def _backup_rel_path(path: Path, plan: InstallPlan) -> Path:
    target_norm = _norm(plan.target_plugin_dir)
    instance_norm = _norm(plan.instance_root)
    path_norm = _norm(path)
    if path_norm.startswith(target_norm + os.sep):
        return Path("plugin") / path.relative_to(plan.target_plugin_dir)
    if path_norm.startswith(instance_norm + os.sep):
        return Path("instance") / path.relative_to(plan.instance_root)
    return Path("other") / path.name


def _write_receipt(
    plan: InstallPlan,
    *,
    installation_id: str,
    written: list[str],
) -> Path:
    receipt_files: list[dict[str, str]] = []
    for path_text in written:
        path = Path(path_text)
        role = "plugin"
        if _norm(path).startswith(_norm(plan.instance_root) + os.sep):
            role = "instance"
        receipt_files.append(
            {
                "path": _norm(path),
                "sha256": _sha256_file(path),
                "role": role,
            }
        )

    if plan.host == "codex":
        config_path = _codex_config_path(plan.instance_root)
        data_path = plan.instance_root / "data" / "memory.sqlite3"
    else:
        config_path = _hermes_data_dir(plan.instance_root) / "installation.json"
        data_path = _hermes_data_dir(plan.instance_root) / "memory.sqlite3"

    for tracked in (config_path, data_path):
        norm = _norm(tracked)
        if tracked.is_file() and norm not in {item["path"] for item in receipt_files}:
            receipt_files.append(
                {
                    "path": norm,
                    "sha256": _sha256_file(tracked),
                    "role": "instance",
                }
            )

    receipt_body: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "package_version": PACKAGE_VERSION,
        "host": plan.host,
        "installation_id": installation_id,
        "agent_id": plan.agent_id,
        "target_plugin_dir": _norm(plan.target_plugin_dir),
        "instance_root": _norm(plan.instance_root),
        "project_root": _norm(plan.project_root),
        "python_executable": _norm(plan.python_executable),
        "files": receipt_files,
    }
    if plan.host == "hermes":
        receipt_body["agent_workspace"] = plan.agent_workspace
    if plan.env_file is not None:
        receipt_body["env_file"] = _norm(plan.env_file)
    receipt_payload = _digest_receipt(receipt_body)
    receipt_path = _receipt_path(plan.instance_root)
    _atomic_write(receipt_path, _json_dump(receipt_payload))
    return receipt_path


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
) -> InstallPlan:
    host_choice = _validate_host(host)
    target = _require_absolute(Path(target_plugin_dir), "target_plugin_dir")
    instance = _require_absolute(Path(instance_root), "instance_root")
    project = _require_absolute(Path(project_root), "project_root")
    python = _require_file(Path(python_executable), "python_executable")
    agent = _validate_agent_id(agent_id)
    workspace = _validate_agent_workspace(agent_workspace, host_choice)
    credentials = _validate_env_file(env_file, host_choice)
    if type(test_mode) is not bool:
        raise InstallError("test_mode must be a boolean")
    plugin_name = _validate_plugin_name(target.name)
    _validate_roots(
        (target, "target_plugin_dir"),
        (instance, "instance_root"),
        (project, "project_root"),
    )

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
    )
    receipt = _load_receipt(instance)
    owned: dict[str, str] = {}
    if receipt is not None:
        try:
            owned = _validate_receipt_binding(
                receipt,
                host=host_choice,
                instance_root=instance,
                target_plugin_dir=target,
            )
        except InstallError as exc:
            plan.conflicts.append(str(exc))
        stored_workspace = receipt.get("agent_workspace")
        if type(stored_workspace) is str and stored_workspace.strip():
            if host_choice != "hermes" or stored_workspace.strip() != workspace:
                plan.conflicts.append("existing receipt agent_workspace mismatch")

    if host_choice == "codex":
        planned = _planned_codex_files(
            target_plugin_dir=target,
            instance_root=instance,
            project_root=project,
            python_executable=python,
            plugin_name=plugin_name,
            env_file=credentials,
        )
    else:
        planned = _planned_hermes_files(target, instance)

    allowed_plugin = {_norm(path) for path in planned}
    target_norm = _norm(target)
    owned_plugin = {path for path in owned if path.startswith(target_norm + os.sep) or path == target_norm}
    for path in _foreign_plugin_entries(target, allowed_plugin, owned_plugin):
        plan.conflicts.append(f"unrelated plugin file: {path}")

    initialized = _instance_initialized(host_choice, instance)
    plan.reuse_instance = initialized
    if initialized:
        try:
            _validate_reuse_binding(host_choice, instance, agent, test_mode, workspace)
        except Exception as exc:
            plan.conflicts.append(str(exc))
        else:
            plan.changes.append(
                PlannedChange("validate", str(instance), "reuse initialized instance binding")
            )
    else:
        for path in _instance_foreign_entries(instance, host_choice, initialized=False):
            plan.conflicts.append(f"foreign instance content: {path}")
        plan.changes.append(
            PlannedChange("initialize", str(instance), "create empty instance via adapter helper")
        )

    for path, _content in sorted(planned.items(), key=lambda item: str(item[0])):
        norm = _norm(path)
        if path.is_file():
            if not owned:
                plan.conflicts.append(f"no-receipt collision: {path}")
            elif norm not in owned:
                plan.conflicts.append(f"no-receipt collision: {path}")
            elif _sha256_file(path) != owned[norm]:
                plan.conflicts.append(f"edited prior file: {path}")
        plan.changes.append(PlannedChange("write", str(path), "install host wrapper artifact"))

    receipt_target = _receipt_path(instance)
    plan.changes.append(PlannedChange("write", str(receipt_target), "install receipt with digest"))

    if receipt is not None and receipt.get("host") not in {None, host_choice}:
        plan.conflicts.append("existing receipt host mismatch")

    return plan


def apply_install(plan: InstallPlan) -> InstallResult:
    fresh = plan_install(
        target_plugin_dir=plan.target_plugin_dir,
        instance_root=plan.instance_root,
        project_root=plan.project_root,
        agent_id=plan.agent_id,
        python_executable=plan.python_executable,
        host=plan.host,
        test_mode=plan.test_mode,
        agent_workspace=plan.agent_workspace or None,
        env_file=plan.env_file,
    )
    if fresh.conflicts:
        raise InstallError("; ".join(fresh.conflicts))
    plan = fresh

    backups: list[str] = []
    written: list[str] = []
    backup_root = plan.instance_root / BACKUP_DIRNAME / uuid.uuid4().hex
    touched: list[tuple[Path, Path | None]] = []
    instance_initialized = False

    if plan.host == "codex":
        planned_files = _planned_codex_files(
            target_plugin_dir=plan.target_plugin_dir,
            instance_root=plan.instance_root,
            project_root=plan.project_root,
            python_executable=plan.python_executable,
            plugin_name=_validate_plugin_name(plan.target_plugin_dir.name),
            env_file=plan.env_file,
        )
    else:
        planned_files = _planned_hermes_files(plan.target_plugin_dir, plan.instance_root)

    installation_id = ""
    try:
        if not plan.reuse_instance:
            installation_id = _initialize_instance(
                plan.host,
                instance_root=plan.instance_root,
                project_root=plan.project_root,
                agent_id=plan.agent_id,
                test_mode=plan.test_mode,
                agent_workspace=plan.agent_workspace,
            )
            instance_initialized = True
        else:
            installation_id = _installation_id_from_instance(plan.host, plan.instance_root)

        prior_receipt = _receipt_path(plan.instance_root)
        if prior_receipt.is_file():
            rel = _backup_rel_path(prior_receipt, plan)
            backup_path = backup_root / rel
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(prior_receipt, backup_path)
            backups.append(str(backup_path))

        for path, content in planned_files.items():
            backup_path: Path | None = None
            if path.is_file():
                rel = _backup_rel_path(path, plan)
                backup_path = backup_root / rel
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup_path)
                backups.append(str(backup_path))
            _atomic_write(path, content)
            written.append(str(path))
            touched.append((path, backup_path))

        receipt_path = _write_receipt(plan, installation_id=installation_id, written=written)
        written.append(str(receipt_path))
    except Exception:
        for path, backup_path in reversed(touched):
            if backup_path is not None and backup_path.is_file():
                shutil.copy2(backup_path, path)
            elif path.is_file():
                path.unlink()
        if instance_initialized:
            partial_written = [
                str(path)
                for path in (
                    _codex_config_path(plan.instance_root)
                    if plan.host == "codex"
                    else _hermes_data_dir(plan.instance_root) / "installation.json",
                    plan.instance_root / "data" / "memory.sqlite3"
                    if plan.host == "codex"
                    else _hermes_data_dir(plan.instance_root) / "memory.sqlite3",
                )
                if path.is_file()
            ]
            if partial_written and installation_id:
                _write_receipt(plan, installation_id=installation_id, written=partial_written)
        raise

    return InstallResult(
        files_written=written,
        # Report what was actually checked instead of two constants. Both of
        # these were unconditionally True, so a completed install looked
        # identical to a broken one in every receipt ever written.
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
    target = (
        _require_absolute(Path(target_plugin_dir), "target_plugin_dir")
        if target_plugin_dir is not None
        else _require_absolute(Path(str(receipt.get("target_plugin_dir") or "")), "target_plugin_dir")
    )

    plan = UninstallPlan(
        host=host,
        instance_root=instance,
        target_plugin_dir=target,
        retain_memory=True,
    )

    try:
        owned = _validate_receipt_binding(
            receipt,
            host=host,
            instance_root=instance,
            target_plugin_dir=target,
        )
    except InstallError as exc:
        plan.conflicts.append(str(exc))
        return plan

    plugin_paths = [
        path
        for path, _sha in owned.items()
        if path.startswith(_norm(target) + os.sep) or path == _norm(target)
        or (host == "hermes" and path == _norm(instance / "skills" / "scope-recall-setup" / "SKILL.md"))
    ]

    for path_text in plugin_paths:
        path = Path(path_text)
        if not path.is_file():
            continue
        expected = owned[path_text]
        if _sha256_file(path) != expected:
            plan.edited_files.append(path_text)
            continue
        plan.files_to_remove.append(path_text)

    if purge:
        if plan.edited_files:
            plan.conflicts.append("purge_refused: edited plugin files")
        else:
            try:
                receipt = _load_receipt(instance)
                if receipt is None:
                    raise InstallError("install receipt is required for uninstall")
                data_directory, _installation_id, _agent_id, _config_path = _purge_identity(plan, receipt)
                with _purge_guard(data_directory):
                    inventory = _purge_inventory(plan, receipt)
            except InstallError as exc:
                plan.conflicts.append(str(exc))
            else:
                plan.purge_allowed = True
                plan.purge_paths = [str(path) for path in inventory.files]
                plan.retained_backups = [str(path) for path in inventory.retained_backups]
                plan.retain_memory = False

    return plan


def apply_uninstall(plan: UninstallPlan, *, purge: bool = False) -> UninstallResult:
    fresh = plan_uninstall(
        instance_root=plan.instance_root,
        target_plugin_dir=plan.target_plugin_dir,
        purge=purge,
    )
    if fresh.conflicts:
        raise InstallError("; ".join(fresh.conflicts))
    plan = fresh

    removed: list[str] = []
    purged_paths: list[str] = []
    retained_backups = list(plan.retained_backups)
    background_root = plan.instance_root / "data" if plan.host == "codex" else _hermes_data_dir(plan.instance_root)
    background_receipt = background_root / "runtime-autostart.json"
    if background_receipt.is_file():
        from .autostart import disable
        from ..runtime.worker_entry import load_config
        registration = json.loads(background_receipt.read_text(encoding="utf-8"))
        background_config = load_config(registration["config_path"])
        if background_config.binding.data_directory.resolve() != background_root.resolve():
            raise InstallError("autostart_binding_mismatch")
        disable(registration["config_path"], remove=True)
    if purge:
        if not plan.purge_allowed:
            raise InstallError("purge_refused:plan_not_authorized")
        receipt = _load_receipt(plan.instance_root)
        if receipt is None:
            raise InstallError("install receipt is required for uninstall")
        purged_paths, retained_backups = _purge_owned_data(plan, receipt)
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

    memory_path = (
        plan.instance_root / "data" / "memory.sqlite3"
        if plan.host == "codex"
        else _hermes_data_dir(plan.instance_root) / "memory.sqlite3"
    )
    return UninstallResult(
        files_removed=removed,
        memory_retained=False if purge else memory_path.is_file(),
        purged=bool(purge and purged_paths),
        edited_files=list(plan.edited_files),
        purged_paths=purged_paths,
        retained_backups=retained_backups,
    )
