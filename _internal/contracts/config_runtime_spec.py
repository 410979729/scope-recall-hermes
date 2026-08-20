"""Independent config runtime facts. Does not replace DEFAULT_CONFIG."""

from __future__ import annotations

import json
from importlib import import_module, resources
from pathlib import Path
from typing import Any


def _resolve_plugin_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        if (parent / "config.json").is_file() and (
            (parent / "pyproject.toml").is_file() or (parent / "plugin.yaml").is_file()
        ):
            return parent
    try:
        packaged = resources.files("scope_recall")
        candidate = Path(str(packaged))
        if (candidate / "config.json").is_file():
            return candidate
    except (FileNotFoundError, ModuleNotFoundError, TypeError, ValueError):
        pass
    raise RuntimeError("scope_recall plugin root with config.json was not found")


PLUGIN_ROOT = _resolve_plugin_root()


def walk_leaves(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and path not in {"identity_aliases", "scope_aliases"}:
            out.update(walk_leaves(value, path))
        else:
            out[path] = value
    return out


def _load_default_config() -> dict[str, Any]:
    return dict(import_module("scope_recall.config").DEFAULT_CONFIG)


def _load_packaged_config() -> dict[str, Any]:
    try:
        text = resources.files("scope_recall").joinpath("config.json").read_text(encoding="utf-8")
        return json.loads(text)
    except Exception:
        return json.loads((PLUGIN_ROOT / "config.json").read_text(encoding="utf-8"))


def config_leaf_kinds() -> list[dict[str, str]]:
    runtime = walk_leaves(_load_default_config())
    packed = walk_leaves(_load_packaged_config())
    rows = []
    for path in sorted(set(runtime) | set(packed)):
        if path in runtime and path in packed:
            kind = "both"
        elif path in runtime:
            kind = "runtime_only"
        else:
            kind = "packaged_only"
        rows.append({"path": path, "kind": kind})
    return rows
