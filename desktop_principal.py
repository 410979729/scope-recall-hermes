"""Profile-local opaque principal fallback for Hermes Desktop sessions.

Desktop is a single-operator surface and often omits ``user_id``. This module
mints a stable opaque principal under the active Hermes profile home so Scope
Recall can activate without leaking host usernames or filesystem paths. Explicit
configuration always wins. Non-Desktop platforms must not use this path.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import tempfile
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
    raw = path.read_text(encoding="utf-8")
    if raw.endswith(chr(13) + chr(10)):
        raw = raw[:-2]
    elif raw.endswith("\n"):
        raw = raw[:-1]
    if _PRINCIPAL_PATTERN.fullmatch(raw):
        return raw
    raise ValueError("persistent Desktop principal file is empty or invalid")


def _existing_principal(path: Path) -> str:
    try:
        return _read_principal_file(path)
    except FileNotFoundError:
        return ""


def _optimistic_existing_principal(path: Path) -> str:
    """Read before locking, deferring transient sharing denial to a locked retry."""

    try:
        return _existing_principal(path)
    except PermissionError:
        # A concurrent Windows replace can expose the final path before another
        # thread can open it. The locked read below remains strict: persistent
        # ACL errors or non-file paths still propagate and are never overwritten.
        return ""


def _mint_opaque_principal() -> str:
    return f"srdesk_{secrets.token_hex(16)}"


def _sync_directory(path: Path) -> None:
    """Flush the parent directory using the strongest available OS primitive."""

    if os.name == "nt":
        import ctypes

        # Windows has no portable directory fd/fsync. Flush a directory handle
        # opened with backup semantics and write-through so rename metadata is
        # pushed through the filesystem before this function returns.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        kernel32.FlushFileBuffers.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateFileW(
            str(path),
            0x40000000,
            0x00000007,
            None,
            3,
            0x02000000 | 0x80000000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.FlushFileBuffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)
        return

    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _persist_principal(path: Path, principal: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(principal + chr(10))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        _sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            temporary.unlink(missing_ok=True)


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
    existing = _optimistic_existing_principal(path)
    if existing:
        return existing

    with advisory_file_lock(_lock_path(home)):
        existing = _existing_principal(path)
        if existing:
            return existing
        principal = _mint_opaque_principal()
        _persist_principal(path, principal)
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
