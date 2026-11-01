"""Bounded local-file access and timestamps shared by import readers."""

from __future__ import annotations

import json
import math
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from scope_recall.contracts import ContractError

MAX_FILE_BYTES = 256 * 1024 * 1024


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
        read_bytes(path)
        with zipfile.ZipFile(path) as archive:
            members = [
                i
                for i in archive.infolist()
                if Path(i.filename).name.startswith(prefixes) and i.filename.endswith(".json") and not i.is_dir()
            ]
            if not members or sum(i.file_size for i in members) > MAX_FILE_BYTES:
                raise ContractError("INPUT_INVALID", "import_archive")
            for member in sorted(members, key=lambda i: i.filename):
                yield json.loads(archive.read(member))
    else:
        yield json.loads(read_bytes(path))
