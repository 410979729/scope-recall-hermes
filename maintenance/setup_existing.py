"""Locate an existing entry through structured host configuration without executing host commands."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ..adapters.shared_entries import attachment_path, read_attachment
from .install_common import RUNTIME_CONFIG_LIMIT, InstallError, require_absolute
from .install_receipt import receipt_file


def _read(path: Path, *, yaml_file: bool = False):
    if not path.is_file():
        return None
    if path.stat().st_size > RUNTIME_CONFIG_LIMIT:
        raise InstallError("host registration exceeds the supported size")
    text = path.read_text(encoding="utf-8-sig")
    return yaml.safe_load(text) if yaml_file else json.loads(text)


def _argument(args: list[str], name: str) -> str:
    if name not in args or args.index(name) + 1 >= len(args):
        raise InstallError("existing Scope Recall server has an incomplete binding argument")
    return args[args.index(name) + 1]


def _mcp_home(server: dict, host: str) -> Path:
    args = server.get("args")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise InstallError("existing Scope Recall connector is remote or unsupported; preserve its configuration")
    modules = {"scope_recall.adapters.codex.mcp_entry", "scope_recall.adapters.clients.mcp_entry"}
    if _argument(args, "-m") not in modules:
        raise InstallError("existing Scope Recall server has an unsupported module; preserve its configuration")
    if "--home" in args:
        if _argument(args, "--host") != host:
            raise InstallError("existing Scope Recall server host differs from the selected host")
        return require_absolute(Path(_argument(args, "--home")), "registered entry home")
    if host == "codex" and "--config" in args:
        config = require_absolute(Path(_argument(args, "--config")), "registered config")
        if config.name == "codex-installation.json":
            return config.parent
    raise InstallError("existing Scope Recall server does not declare its entry home")


def _catalog_target(target: Path, user_root: Path) -> Path:
    """Resolve only local catalog paths within the marketplace root; never fetch a catalog source."""
    if not (target / ".mcp.json").is_file():
        catalog = _read(user_root / ".agents" / "plugins" / "marketplace.json") or {}
        for entry in catalog.get("plugins", []):
            if entry.get("name") == "scope-recall":
                source = entry.get("source") or {}
                relative = source.get("path") if isinstance(source, dict) else source
                if not isinstance(relative, str):
                    raise InstallError("existing marketplace connector is not a local plugin")
                candidate = (user_root / relative).resolve()
                if not candidate.is_relative_to(user_root):
                    raise InstallError("local marketplace plugin is outside its declared root")
                target = candidate
    return target


def existing_entry_home(host: str, home: Path, target: Path, *, user_root: Path) -> Path:
    """Follow only this product's structured registration; its attachment must bind the same host."""
    if attachment_path(home).is_file() or receipt_file(home).is_file():
        return home
    if host == "codex":
        target = _catalog_target(target, user_root)
    if host == "dsh":
        layers = _read(target / "cordis.patch.yml", yaml_file=True) or []
        if not isinstance(layers, list):
            raise InstallError("existing dsh patch is not a layer list")
        rows = [
            row
            for layer in layers
            if isinstance(layer, dict)
            for row in layer.get("insert", [])
            if isinstance(row, dict) and row.get("id") == "scope-recall"
        ]
        if not rows:
            return home
        declared = (rows[0].get("config") or {}).get("home")
        if not isinstance(declared, str):
            raise InstallError("existing Scope Recall plugin has no local entry home; preserve its configuration")
        bound = require_absolute(Path(declared), "registered entry home")
    elif host in {"codex", "claude-code", "workbuddy"}:
        value = _read(target / ("mcp.json" if host == "workbuddy" else ".mcp.json")) or {}
        if not isinstance(value, dict) or not isinstance(value.get("mcpServers", {}), dict):
            raise InstallError("existing host MCP configuration is not a server mapping")
        server = (value.get("mcpServers") or {}).get("scope-recall")
        if server is not None and not isinstance(server, dict):
            raise InstallError("existing Scope Recall server is not an object")
        if server is None:
            return home
        bound = _mcp_home(server, host)
    else:
        return home
    pointer = read_attachment(bound)
    if pointer is None and not (bound / "codex-installation.json").is_file():
        raise InstallError("registered entry binding is missing; preserve the existing host registration")
    if pointer is not None and pointer.host != host:
        raise InstallError("registered entry binding belongs to a different host")
    return bound
