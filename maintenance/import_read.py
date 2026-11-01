"""Bounded local-file access and timestamps shared by import readers."""

from __future__ import annotations

import json
import lzma
import math
import stat
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from scope_recall.contracts import ContractError

MAX_FILE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class SkippedImport:
    """One validated text-free occurrence, counted without retaining its payload."""


def timestamp(value, *, milliseconds=False) -> str | None:
    """Normalize an original ISO instant or epoch; missing values stay unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        result = datetime.fromtimestamp(value / (1000 if milliseconds else 1), UTC)
    elif isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ContractError("INPUT_INVALID", "import_time")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ContractError("INPUT_INVALID", "import_time_zone")
    return result.astimezone(UTC).isoformat().replace("+00:00", "Z")


def read_bytes(path: Path) -> bytes:
    """Read a bounded ordinary file; never follow a source symlink."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ContractError("INPUT_INVALID", "import_file")
    return path.read_bytes()


def json_documents(path: Path, *, prefixes=("conversations",)):
    """Read conversations JSON in place, including split ZIP members, without extraction."""
    if path.is_dir():
        files = sorted({candidate for prefix in prefixes for candidate in path.glob(prefix + "*.json")})
        if not files:
            raise ContractError("INPUT_INVALID", "import_conversations")
        for candidate in files:
            yield json.loads(read_bytes(candidate))
    elif path.suffix.lower() == ".zip":
        yield from _archive_documents(path, prefixes)
    else:
        yield json.loads(read_bytes(path))


def _archive_members(archive, prefixes):
    seen, selected = set(), []
    for member in archive.infolist():
        name = member.orig_filename
        pieces = name.rstrip("/").split("/")
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or unicodedata.normalize("NFC", name) != name
            or any(ch in '<>:"|?*' or ord(ch) < 32 for ch in name)
            or any(part in {"", ".", ".."} or part.rstrip(" .") != part for part in pieces)
            or name.casefold() in seen
            or stat.S_ISLNK(member.external_attr >> 16)
        ):
            raise ContractError("INPUT_INVALID", "import_archive")
        seen.add(name.casefold())
        if pieces[-1].startswith(prefixes) and name.endswith(".json") and not member.is_dir():
            selected.append(member)
    if not selected or sum(member.file_size for member in selected) > MAX_FILE_BYTES:
        raise ContractError("INPUT_INVALID", "import_archive")
    return sorted(selected, key=lambda item: item.filename)


def _archive_documents(path, prefixes):
    # Keep one bounded byte snapshot so validation and member reads cannot
    # observe different files, and never interpolate archive diagnostics.
    try:
        with zipfile.ZipFile(BytesIO(read_bytes(path))) as archive:
            for member in _archive_members(archive, prefixes):
                yield json.loads(archive.read(member))
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        RuntimeError,
        NotImplementedError,
        ValueError,
        OSError,
        EOFError,
        zlib.error,
        lzma.LZMAError,
    ):
        # bzip2 reports invalid streams as OSError; the other decompressors
        # have separate exception types. None may escape with archive data.
        raise ContractError("INPUT_INVALID", "import_archive") from None
