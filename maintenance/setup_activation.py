"""Register the local plugin catalog while leaving host hook trust to its owner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .install_common import InstallError, validate_write_path
from .setup_configuration import write_configuration


def codex_catalog(target: Path, user: Path) -> tuple[Path, str]:
    """Expose a plugin in the personal catalog, preserving all other entries and metadata."""
    try:
        relative = target.relative_to(user).as_posix()
    except ValueError as exc:
        raise InstallError("the Codex plugin must be inside the personal marketplace root") from exc
    path = user / ".agents" / "plugins" / "marketplace.json"
    validate_write_path(path)
    catalog: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8")) if path.is_file() else dict(name="local", plugins=[])
    )
    if not isinstance(catalog, dict) or not isinstance(catalog.get("plugins"), list) or not catalog.get("name"):
        raise InstallError("personal marketplace is not a named catalog with a plugin list")
    entry: dict[str, Any] = dict(
        name="scope-recall",
        source=dict(source="local", path="./" + relative),
        policy=dict(installation="INSTALLED_BY_DEFAULT", authentication="ON_INSTALL"),
        category="Productivity",
    )
    old = [item for item in catalog["plugins"] if item.get("name") == "scope-recall"]
    if old and any(item.get("source") != entry["source"] for item in old):
        raise InstallError("personal marketplace already names another scope-recall plugin")
    if old:
        for key, value in entry.items():
            old[0].setdefault(key, value)
    else:
        catalog["plugins"].append(entry)
    return path, json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"


def register_codex(home: Path, target: Path, user: Path, backups: list[str]) -> None:
    """Publish the same checked catalog used by planning, without changing existing policy."""
    path, content = codex_catalog(target, user)
    write_configuration(path, content, backups)
    # Codex itself records approval of the current hook definitions. A catalog entry
    # makes them discoverable; an installer cannot assert that a person reviewed them.
