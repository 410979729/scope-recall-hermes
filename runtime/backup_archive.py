"""Archive member layout shared by backup verification and disaster restore.

The zip member set must be exactly the manifest plus ``manifest.json``. Each
member is a canonical POSIX relative path, and the restore mapping (database
snapshot and ``configs/`` prefix included) must land on one unused path inside
the destination. Callers write only the members this module returns, and only
after that check.
"""

from __future__ import annotations

import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backup_primitives import BackupError, first_link

_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_DEVICE_NAME = re.compile(r"^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.IGNORECASE)


class ArchiveLayoutError(BackupError):
    """Raised when an archive member set or its destination mapping is unsafe."""


@dataclass(frozen=True)
class ArchiveMember:
    """One manifest file and the relative path it is allowed to occupy."""

    archive_name: str
    destination_relative: str


def canonical_member_name(name: str) -> str:
    """Return ``name`` when it is one canonical POSIX relative path.

    Absolute paths, drive paths, backslashes, ``.``, ``..`` and empty segments
    are rejected so a later join cannot escape the destination or alias another
    member.
    """
    if type(name) is not str or name == "":
        raise ArchiveLayoutError("archive member name is empty")
    if "\\" in name:
        raise ArchiveLayoutError(f"archive member name uses a backslash: {name}")
    if "\x00" in name:
        raise ArchiveLayoutError("archive member name contains a null character")
    if name.startswith("/"):
        raise ArchiveLayoutError(f"archive member name is absolute: {name}")
    if _DRIVE_PATH.match(name):
        raise ArchiveLayoutError(f"archive member name is a drive path: {name}")
    parts = name.split("/")
    if any(part == "" for part in parts):
        raise ArchiveLayoutError(f"archive member name has an empty segment: {name}")
    if any(part in {".", ".."} for part in parts):
        raise ArchiveLayoutError(f"archive member name is not a canonical relative path: {name}")
    if any(
        part[-1:] in {".", " "} or _DEVICE_NAME.match(part) or any(c in '<>:"|?*' or ord(c) < 32 for c in part)
        for part in parts
    ):
        raise ArchiveLayoutError(f"archive member name is not portable: {name}")
    return name


def destination_relative(archive_name: str) -> str:
    """Map one archive path to its path relative to the restore root.

    ``database.sqlite3`` becomes the store file. A ``configs/`` member is placed
    at the store root under the remaining relative path. Any other member keeps
    its archive path.
    """
    name = canonical_member_name(archive_name)
    if name == "database.sqlite3":
        return "memory.sqlite3"
    prefix = "configs/"
    if name.startswith(prefix):
        return canonical_member_name(name[len(prefix) :])
    return name


def _is_link_member(info: zipfile.ZipInfo) -> bool:
    """Whether the zip entry is stored as a symbolic link.

    The Unix mode lives in the high 16 bits of ``external_attr``. The member
    name's extension is not evidence either way.
    """
    return stat.S_ISLNK((info.external_attr >> 16) & 0o170000)


def _declared_paths(manifest: dict[str, Any]) -> list[str]:
    files = manifest.get("files")
    if type(files) is not list or not files:
        raise ArchiveLayoutError("manifest lists no files")
    declared: list[str] = []
    seen: set[str] = set()
    for entry in files:
        if type(entry) is not dict or type(entry.get("path")) is not str:
            raise ArchiveLayoutError("manifest file entry is missing a path")
        path = canonical_member_name(entry["path"])
        if path == "manifest.json":
            raise ArchiveLayoutError("manifest.json is listed as a payload member")
        if path in seen:
            raise ArchiveLayoutError(f"duplicate manifest member: {path}")
        seen.add(path)
        declared.append(path)
    return declared


def verify_archive_layout(archive: zipfile.ZipFile, manifest: dict[str, Any]) -> tuple[ArchiveMember, ...]:
    """Return the only members that may be written, each with its destination.

    Does not read member bodies and does not write. A duplicate zip entry, a
    name that is not canonical, a link entry, a member outside the manifest, or
    two members that map to one destination fails the archive.
    """
    infos = archive.infolist()
    seen_names: set[str] = set()
    for info in infos:
        if info.filename in seen_names:
            raise ArchiveLayoutError(f"duplicate archive member: {info.filename}")
        seen_names.add(info.filename)
        canonical_member_name(info.filename)
        if _is_link_member(info):
            raise ArchiveLayoutError(f"archive member is a symlink: {info.filename}")
    if "manifest.json" not in seen_names:
        raise ArchiveLayoutError("manifest.json is missing from the archive")
    declared = _declared_paths(manifest)
    expected = set(declared) | {"manifest.json"}
    missing = sorted(expected - seen_names)
    extra = sorted(seen_names - expected)
    if missing:
        raise ArchiveLayoutError(f"manifest member is missing from the archive: {missing[0]}")
    if extra:
        raise ArchiveLayoutError(f"archive member is not listed in the manifest: {extra[0]}")
    destinations: dict[str, str] = {}
    members: list[ArchiveMember] = []
    for name in declared:
        dest = destination_relative(name)
        folded = dest.casefold()
        previous = destinations.get(folded)
        if previous is not None:
            raise ArchiveLayoutError(f"archive members map to the same destination {dest}: {previous} and {name}")
        destinations[folded] = name
        members.append(ArchiveMember(name, dest))
    for dest in destinations:
        parts = dest.split("/")
        if any("/".join(parts[:end]) in destinations for end in range(1, len(parts))):
            raise ArchiveLayoutError(f"archive member overlaps a directory: {dest}")
    return tuple(members)


def member_destination(root: Path, relative: str) -> Path:
    """Absolute path of one verified member inside ``root``.

    The path is lexical. Existing links are refused, and the result must stay
    on ``root`` after normalization. This does not create directories.
    """
    relative = canonical_member_name(relative)
    if first_link(root) is not None:
        raise ArchiveLayoutError("restore root is a symlink or reparse point")
    base = root if root.is_absolute() else Path.cwd() / root
    base_abs = Path(os.path.abspath(base))
    current = base_abs
    for part in relative.split("/"):
        current = current / part
        if first_link(current) is not None:
            raise ArchiveLayoutError(f"refusing to follow a link at {current}")
    base_key = os.path.normcase(str(base_abs))
    target_key = os.path.normcase(str(current))
    try:
        if os.path.commonpath((base_key, target_key)) != base_key:
            raise ArchiveLayoutError(f"member destination escapes the restore root: {relative}")
    except ValueError as exc:
        raise ArchiveLayoutError(f"member destination escapes the restore root: {relative}") from exc
    return current


def _ensure_parent(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts[:-1]:
        current = current / part
        if first_link(current) is not None:
            raise ArchiveLayoutError(f"refusing to follow a link at {current}")
        if current.exists() and not current.is_dir():
            raise ArchiveLayoutError(f"destination parent is not a directory: {current}")
        if not current.exists():
            current.mkdir()


def _write_one(archive: zipfile.ZipFile, root: Path, member: ArchiveMember, target: Path) -> None:
    checked = member_destination(root, member.destination_relative)
    if os.path.normcase(str(checked)) != os.path.normcase(str(target)):
        raise ArchiveLayoutError(f"member destination escapes the restore root: {member.destination_relative}")
    if first_link(target) is not None or target.exists():
        raise ArchiveLayoutError(f"refusing to overwrite {target.name}")
    _ensure_parent(root, target)
    info = archive.getinfo(member.archive_name)
    # Binary mode prevents the Windows CRT from translating newline bytes in
    # SQLite pages and attachments written through the file descriptor.
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        with archive.open(info, "r") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                view = chunk
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
    finally:
        os.close(descriptor)


def write_verified_members(archive: zipfile.ZipFile, root: Path, members: tuple[ArchiveMember, ...]) -> None:
    """Write ``members`` under ``root``.

    Every destination is checked before the root is created. Member bytes are
    read by zip entry, not by joining the archive name onto the root.
    """
    if first_link(root) is not None:
        raise ArchiveLayoutError("restore root is a symlink or reparse point")
    base = root if root.is_absolute() else Path.cwd() / root
    root = Path(os.path.abspath(base))
    planned = [(member, member_destination(root, member.destination_relative)) for member in members]
    root.mkdir(parents=True, exist_ok=True)
    for member, target in planned:
        _write_one(archive, root, member, target)


__all__ = [
    "ArchiveLayoutError",
    "ArchiveMember",
    "canonical_member_name",
    "destination_relative",
    "member_destination",
    "verify_archive_layout",
    "write_verified_members",
]
