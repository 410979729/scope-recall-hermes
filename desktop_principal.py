"""Profile-local opaque principal fallback for Hermes Desktop sessions.

Desktop is a single-operator surface and often omits ``user_id``. This module
mints a stable opaque principal under the active Hermes profile home so Scope
Recall can activate without leaking host usernames or filesystem paths. Explicit
configuration always wins. Non-Desktop platforms must not use this path.
"""

from __future__ import annotations

from pathlib import Path
import re
import secrets
from typing import Any

from .file_lock import advisory_file_lock

DESKTOP_PLATFORMS = frozenset({"desktop"})
_PRINCIPAL_DIRNAME = "scope-recall"
_PRINCIPAL_FILENAME = "desktop-principal.id"
_PRINCIPAL_LOCK_FILENAME = ".desktop-principal.lock"
_PRINCIPAL_PATTERN = re.compile(r"^srdesk_[0-9a-f]{32}$")
_EXPLICIT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")


def is_desktop_platform(platform: str | None) -> bool:
    """Return whether the runtime platform is Hermes Desktop."""

    return str(platform or "").strip().lower() in DESKTOP_PLATFORMS


def _storage_dir(hermes_home: Path) -> Path:
    return Path(hermes_home).expanduser() / _PRINCIPAL_DIRNAME


def _principal_path(hermes_home: Path) -> Path:
    return _storage_dir(hermes_home) / _PRINCIPAL_FILENAME


def _lock_path(hermes_home: Path) -> Path:
    return _storage_dir(hermes_home) / _PRINCIPAL_LOCK_FILENAME


def _normalize_explicit(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _EXPLICIT_PATTERN.fullmatch(text):
        raise ValueError("identity.desktop_principal must be a compact opaque token")
    lowered = text.lower()
    if any(token in lowered for token in ("\\", "/", ":")):
        raise ValueError("identity.desktop_principal must not contain path characters")
    return text


def _read_principal_file(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if _PRINCIPAL_PATTERN.fullmatch(raw) or _EXPLICIT_PATTERN.fullmatch(raw):
        return raw
    return ""


def _mint_opaque_principal() -> str:
    return f"srdesk_{secrets.token_hex(16)}"


def resolve_desktop_principal(
    *,
    hermes_home: str | Path,
    explicit: Any = "",
) -> str:
    """Return a profile-isolated Desktop principal.

    Resolution order:
    1. Non-empty explicit config value (``identity.desktop_principal``)
    2. Existing durable principal file under the profile's ``scope-recall/``
    3. Create a new opaque principal and persist it under an advisory lock
    """

    home = Path(hermes_home).expanduser()
    configured = _normalize_explicit(explicit)
    if configured:
        return configured

    storage = _storage_dir(home)
    storage.mkdir(parents=True, exist_ok=True)
    path = _principal_path(home)
    existing = _read_principal_file(path)
    if existing:
        return existing

    with advisory_file_lock(_lock_path(home)):
        existing = _read_principal_file(path)
        if existing:
            return existing
        principal = _mint_opaque_principal()
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(principal + "\n", encoding="utf-8")
        temporary.replace(path)
        return principal


def desktop_principal_from_config(config: dict[str, Any] | None) -> str:
    """Extract the optional explicit Desktop principal override from config."""

    identity = (config or {}).get("identity")
    if not isinstance(identity, dict):
        return ""
    return _normalize_explicit(identity.get("desktop_principal"))


__all__ = [
    "DESKTOP_PLATFORMS",
    "desktop_principal_from_config",
    "is_desktop_platform",
    "resolve_desktop_principal",
]
