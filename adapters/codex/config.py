"""Immutable Codex installation config; hooks never infer identity from cwd.

The same adapter serves a local client that is an entry of a shared store
(``SharedClientConfig``): Codex, or Claude Code, whose hooks and MCP stdio
server speak the protocol Codex's were modelled on.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from scope_recall.adapters.hermes.audiences import LOCAL_USER_ID
from scope_recall.adapters.hermes.identity import HermesRuntimeScope, RuntimeAudience, resolve_runtime_audience
from scope_recall.adapters.hermes.installation import (
    CLIENT_HOSTS,
    HermesIdentityError,
    InstallationManifest,
    attachment_path,
    bounded_text,
    read_attachment,
    shared_entry_manifest,
)
from scope_recall.adapters.runtime_wiring import RUNTIME_CONFIG_FILENAME
from scope_recall.contracts import InstanceBinding
from scope_recall.core import CoreConfig, MemoryCore


class CodexConfigError(RuntimeError):
    """Raised when installation config is missing, invalid, or unbound."""


SCHEMA_VERSION = "scope-recall.codex-installation.v1"
CONFIG_FILENAME = "codex-installation.json"
_MAX_FIELD_LEN = 240
_bounded_text = partial(bounded_text, error=CodexConfigError)


@dataclass(frozen=True)
class CodexInstallationConfig:
    schema_version: str
    installation_id: str
    agent_id: str
    data_directory: Path
    scope_ids: frozenset[str]
    audience_scopes: Mapping[str, str]
    allow_owner_private: bool
    project_roots: Mapping[str, str]
    test_mode: bool
    config_path: Path

    def to_binding(self) -> InstanceBinding:
        return InstanceBinding(
            agent_id=self.agent_id,
            installation_id=self.installation_id,
            data_directory=self.data_directory,
            scope_ids=self.scope_ids,
            test_mode=self.test_mode,
        )


def _strict_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise CodexConfigError(f"{field} must be boolean")
    return value


def _norm_root(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path.resolve())))


def _installation_id(config_path: Path) -> str:
    digest = hashlib.sha256(str(config_path.resolve()).encode("utf-8")).hexdigest()
    return f"codex-install:{digest[:32]}"


def _normalize_project_roots(raw: object, *, scope_ids: frozenset[str]) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise CodexConfigError("project_roots invalid")
    roots: dict[str, str] = {}
    for key, scope_id in raw.items():
        if type(key) is not str or type(scope_id) is not str:
            raise CodexConfigError("project_roots invalid")
        root = Path(key).expanduser()
        if not root.is_absolute():
            raise CodexConfigError("project_roots must use absolute paths")
        normalized = _norm_root(root)
        if scope_id not in scope_ids:
            raise CodexConfigError("project_roots scope binding invalid")
        roots[normalized] = scope_id
    return roots


def load_codex_config(config_path: Path | str) -> CodexInstallationConfig:
    path = Path(config_path).expanduser()
    if not path.is_absolute():
        raise CodexConfigError("config path must be absolute")
    if not path.is_file():
        raise CodexConfigError("config file is required")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CodexConfigError("config decode failed") from exc
    if not isinstance(payload, dict):
        raise CodexConfigError("config root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CodexConfigError("config schema_version mismatch")
    data_directory = Path(_bounded_text(payload.get("data_directory"), field="data_directory")).expanduser()
    if not data_directory.is_absolute():
        raise CodexConfigError("data_directory must be absolute")
    scope_ids_raw = payload.get("scope_ids")
    if not isinstance(scope_ids_raw, list) or not scope_ids_raw:
        raise CodexConfigError("scope_ids invalid")
    if any(type(item) is not str or not item.strip() or len(item) > _MAX_FIELD_LEN for item in scope_ids_raw):
        raise CodexConfigError("scope_ids must contain bounded strings")
    scope_ids = frozenset(scope_ids_raw)
    audience_scopes = payload.get("audience_scopes")
    if not isinstance(audience_scopes, dict) or not audience_scopes:
        raise CodexConfigError("audience_scopes invalid")
    required = frozenset({"owner_private", "project", "shared"})
    if not required <= audience_scopes.keys():
        raise CodexConfigError("audience_scopes incomplete")
    if any(type(key) is not str or type(value) is not str or not key.strip() or not value.strip() for key, value in audience_scopes.items()):
        raise CodexConfigError("audience_scopes must contain strings")
    if frozenset(audience_scopes.values()) != scope_ids:
        raise CodexConfigError("scope binding mismatch")
    config = CodexInstallationConfig(
        schema_version=SCHEMA_VERSION,
        installation_id=_bounded_text(payload.get("installation_id"), field="installation_id"),
        agent_id=_bounded_text(payload.get("agent_id"), field="agent_id"),
        data_directory=data_directory.resolve(),
        scope_ids=scope_ids,
        audience_scopes=MappingProxyType(dict(audience_scopes)),
        allow_owner_private=_strict_bool(payload.get("allow_owner_private"), field="allow_owner_private"),
        project_roots=MappingProxyType(_normalize_project_roots(payload.get("project_roots"), scope_ids=scope_ids)),
        test_mode=_strict_bool(payload.get("test_mode"), field="test_mode"),
        config_path=path.resolve(),
    )
    if config.installation_id != _installation_id(path):
        raise CodexConfigError("installation_id mismatch")
    db_path = config.data_directory / "memory.sqlite3"
    if not db_path.is_file():
        raise CodexConfigError("verified core database is required")
    return config


@dataclass(frozen=True)
class SharedClientConfig:
    """A local client attached to a shared store as an entry (``attach --host codex|claude-code``).

    The grants are the entry's, in the store's manifest; the home keeps the
    pointer and the entry's runtime config.  Whoever types into the client here
    is the owner, in whatever directory it runs, so the audience is fixed when
    the config loads and a session's cwd plays no part in it.
    """

    host: str
    home: Path
    manifest: InstallationManifest
    audience: RuntimeAudience
    #: The route every capture records, re-checked against the entry's grants when it is replayed.
    scope: HermesRuntimeScope

    @property
    def installation_id(self) -> str:
        return self.manifest.installation_id

    @property
    def agent_id(self) -> str:
        return self.manifest.agent_id

    @property
    def data_directory(self) -> Path:
        return self.manifest.data_directory

    @property
    def scope_ids(self) -> frozenset[str]:
        return self.manifest.scope_ids

    @property
    def test_mode(self) -> bool:
        return self.manifest.test_mode

    @property
    def entry_id(self) -> str:
        return str(self.manifest.entry_id)

    @property
    def entry_name(self) -> str:
        return self.manifest.entry_name

    @property
    def runtime_config_path(self) -> Path:
        """The entry's own runtime config beside its pointer; the store's directory holds the worker's."""
        return attachment_path(self.home).parent / RUNTIME_CONFIG_FILENAME

    def to_binding(self) -> InstanceBinding:
        return self.manifest.to_binding()


def load_shared_client(home: Path | str, host: str) -> SharedClientConfig:
    """The entry ``home`` is of a shared store, for the client ``host``."""
    if host not in CLIENT_HOSTS:
        raise CodexConfigError(f"host must be one of {', '.join(CLIENT_HOSTS)}")
    path = Path(home).expanduser()
    if not path.is_absolute():
        raise CodexConfigError("home must be absolute")
    path = path.resolve()
    try:
        attachment = read_attachment(path)
        if attachment is None:
            raise CodexConfigError("home is not attached to a shared store")
        manifest = shared_entry_manifest(attachment.root, attachment.entry_id, hermes_home=path)
    except HermesIdentityError as exc:
        raise CodexConfigError(str(exc)) from exc
    if attachment.host != host or manifest.entry_host != host:
        raise CodexConfigError(f"home is attached as a {attachment.host} entry, not {host}")
    scope = HermesRuntimeScope(
        platform=host, user_id=LOCAL_USER_ID, chat_type="private", chat_id=LOCAL_USER_ID, thread_id="main",
        agent_identity=manifest.agent_id, agent_workspace="default", agent_context="primary",
        entry_id=str(manifest.entry_id),
    )
    audience = resolve_runtime_audience(manifest, scope)
    if not audience.allowed_scope_ids or audience.capture_scope_id is None:
        raise CodexConfigError("the entry's grants give this client nothing to read or no scope to capture into")
    if not (manifest.data_directory / "memory.sqlite3").is_file():
        raise CodexConfigError("verified core database is required")
    return SharedClientConfig(host, path, manifest, audience, scope)


def write_codex_config(config: CodexInstallationConfig) -> None:
    config.data_directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": config.schema_version,
        "installation_id": config.installation_id,
        "agent_id": config.agent_id,
        "data_directory": str(config.data_directory),
        "scope_ids": sorted(config.scope_ids),
        "audience_scopes": dict(config.audience_scopes),
        "allow_owner_private": config.allow_owner_private,
        "project_roots": {path: scope for path, scope in config.project_roots.items()},
        "test_mode": config.test_mode,
    }
    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def install_codex_scope_recall(
    root: Path | str,
    *,
    project_root: Path | str,
    agent_id: str = "TEST-agent",
    allow_owner_private: bool = True,
    test_mode: bool = True,
    clock: Any | None = None,
) -> tuple[CodexInstallationConfig, MemoryCore]:
    """Explicit trusted install for isolated tests; production uses an installer."""

    base = Path(root).resolve()
    project = Path(project_root).resolve()
    if type(allow_owner_private) is not bool or type(test_mode) is not bool:
        raise CodexConfigError("installation boolean options must be boolean")
    config_path = base / CONFIG_FILENAME
    data_directory = base / "data"
    audience = {
        "owner_private": f"audience:owner_private:{agent_id}",
        "shared": f"audience:shared:{agent_id}",
        "project": f"audience:project:{agent_id}",
    }
    scope_ids = frozenset(audience.values())
    config = CodexInstallationConfig(
        schema_version=SCHEMA_VERSION,
        installation_id=_installation_id(config_path),
        agent_id=agent_id,
        data_directory=data_directory,
        scope_ids=scope_ids,
        audience_scopes=MappingProxyType(dict(audience)),
        allow_owner_private=allow_owner_private,
        project_roots=MappingProxyType({_norm_root(project): audience["project"]}),
        test_mode=test_mode,
        config_path=config_path,
    )
    write_codex_config(config)
    binding = config.to_binding()
    core = MemoryCore(CoreConfig(binding), clock=clock)
    core.initialize()
    return config, core
