"""Small, content-addressed store for explicitly authorized visual artifacts.

This module deliberately owns no database state and never searches a directory.
The caller is responsible for deciding that a source is authorized and for
placing database deletion fences before calling :func:`erase_retained`.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from ..contracts import ContractError, InstanceBinding
from .secret_patterns import contains_secret_like_text


MAX_RETAINED_BYTES = 16 * 1024 * 1024
ALLOWED_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/svg+xml", "text/plain"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _bad(field: str, code: str = "INPUT_INVALID") -> None:
    raise ContractError(code, field)


def _io_path(path: Path) -> Path:
    """Use an extended Windows path only at filesystem I/O boundaries."""
    if os.name != "nt":
        return path
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def _reject_links(path: Path, field: str) -> None:
    """Reject symlink/junction components without resolving the path."""
    current = path
    while True:
        io_current = _io_path(current)
        try:
            attributes = getattr(io_current.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        if io_current.is_symlink() or attributes & 0x400 or getattr(io_current, "is_junction", lambda: False)():
            _bad(field, "IDENTITY_UNBOUND")
        if current.parent == current:
            break
        current = current.parent


def _root(binding: InstanceBinding, *, create: bool) -> Path:
    if not isinstance(binding, InstanceBinding):
        _bad("binding", "IDENTITY_UNBOUND")
    root = binding.data_directory / "retained"
    _reject_links(binding.data_directory, "data_directory")
    if create:
        _io_path(root).mkdir(parents=True, exist_ok=True)
    elif not _io_path(root).is_dir():
        _bad("retained_directory", "SOURCE_MISSING")
    _reject_links(root, "retained_directory")
    if root.resolve(strict=False) != root:
        _bad("retained_directory", "IDENTITY_UNBOUND")
    return root


def _validate_media(media_type: str) -> None:
    if type(media_type) is not str or media_type not in ALLOWED_MEDIA_TYPES:
        _bad("media_type")


def _validate_digest(value: str, field: str = "sha256") -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _bad(field)


@dataclass(frozen=True)
class ArtifactGrant:
    path: Path
    sha256: str
    media_type: str
    max_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            _bad("path")
        _validate_digest(self.sha256)
        _validate_media(self.media_type)
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_RETAINED_BYTES:
            _bad("max_bytes")


@dataclass(frozen=True)
class RetainedBlob:
    sha256: str
    size_bytes: int
    media_type: str
    relative_path: str
    installation_id: str | None = None
    agent_id: str | None = None

    def __post_init__(self) -> None:
        _validate_digest(self.sha256)
        _validate_media(self.media_type)
        if type(self.size_bytes) is not int or not 0 <= self.size_bytes <= MAX_RETAINED_BYTES:
            _bad("size_bytes")
        relative = Path(self.relative_path)
        if relative.is_absolute() or relative.parts != ("retained", self.sha256):
            _bad("relative_path")
        for value, field in ((self.installation_id, "installation_id"), (self.agent_id, "agent_id")):
            if value is not None and (type(value) is not str or not value.strip() or len(value) > 240):
                _bad(field, "IDENTITY_UNBOUND")


def _check_secret(data: bytes, field: str) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    if contains_secret_like_text(text):
        _bad(field, "SENSITIVE_CONTENT")


def _read_bounded(path: Path, limit: int, field: str) -> bytes:
    try:
        io_path = _io_path(path)
        before = io_path.stat()
        with io_path.open("rb") as stream:
            data = stream.read(limit + 1)
        after = io_path.stat()
    except OSError as exc:
        raise ContractError("SOURCE_MISSING", field) from exc
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or len(data) > limit:
        _bad(field, "INTEGRITY_ERROR" if len(data) <= limit else "INPUT_TOO_LARGE")
    return data


def _validate_magic(data: bytes, media_type: str) -> None:
    valid = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
        "image/svg+xml": True,
        "text/plain": b"\x00" not in data,
    }[media_type]
    if not valid:
        _bad("media_type", "INTEGRITY_ERROR")


def _read_grant(grant: ArtifactGrant) -> bytes:
    _reject_links(grant.path, "artifact_path")
    if not _io_path(grant.path).is_file():
        _bad("artifact_path", "SOURCE_MISSING")
    try:
        size = _io_path(grant.path).stat().st_size
        if size > grant.max_bytes or size > MAX_RETAINED_BYTES:
            _bad("artifact_size", "INPUT_TOO_LARGE")
        data = _read_bounded(grant.path, grant.max_bytes, "artifact_path")
    except OSError as exc:
        raise ContractError("SOURCE_MISSING", "artifact_path") from exc
    if len(data) != size or len(data) > grant.max_bytes:
        _bad("artifact_size", "INPUT_TOO_LARGE")
    if hashlib.sha256(data).hexdigest() != grant.sha256:
        _bad("sha256", "INTEGRITY_ERROR")
    _validate_magic(data, grant.media_type)
    _check_secret(data, "artifact_content")
    if grant.media_type == "image/svg+xml":
        _validate_svg(data)
    return data


def _validate_svg(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("INPUT_INVALID", "svg_encoding") from exc
    lowered = text.lower()
    if any(token in lowered for token in ("<!doctype", "<!entity", "javascript:", "data:", "@import")):
        _bad("svg_content", "INPUT_INVALID")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ContractError("INPUT_INVALID", "svg_content") from exc
    allowed = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
               "defs", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern", "use"}
    svg_ns = "http://www.w3.org/2000/svg"
    for element in root.iter():
        if element.tag.startswith("{"):
            namespace, local = element.tag[1:].split("}", 1)
            if namespace != svg_ns:
                _bad("svg_content", "INPUT_INVALID")
        else:
            local = element.tag
        if local not in allowed:
            _bad("svg_content", "INPUT_INVALID")
        for attr, value in element.attrib.items():
            name = attr.rsplit("}", 1)[-1].lower()
            value = value.strip()
            if name.startswith("on") or name in {"href", "src"} and not value.startswith("#"):
                _bad("svg_content", "INPUT_INVALID")
            if "url(" in value.lower() or "\\" in value or "@" in value or "entity" in value.lower():
                _bad("svg_content", "INPUT_INVALID")


def retain(binding: InstanceBinding, grant: ArtifactGrant) -> RetainedBlob:
    if not isinstance(grant, ArtifactGrant):
        _bad("grant")
    data = _read_grant(grant)
    root = _root(binding, create=True)
    destination = root / grant.sha256
    _reject_links(destination, "retained_path")
    io_destination = _io_path(destination)
    if io_destination.exists():
        if io_destination.is_symlink() or not io_destination.is_file():
            _bad("retained_path", "IDENTITY_UNBOUND")
        existing = _read_bounded(destination, MAX_RETAINED_BYTES, "retained_path")
        if existing != data:
            _bad("retained_path", "INTEGRITY_ERROR")
    else:
        created = False
        try:
            with io_destination.open("xb") as stream:
                created = True
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            existing = _read_bounded(destination, MAX_RETAINED_BYTES, "retained_path")
            if existing != data:
                _bad("retained_path", "INTEGRITY_ERROR")
        except BaseException as original:
            if created:
                try:
                    io_destination.unlink()
                except OSError as cleanup:
                    original.add_note(f'retained artifact cleanup failed: {type(cleanup).__name__}')
                    original.__cause__=cleanup
            raise
    return RetainedBlob(grant.sha256, len(data), grant.media_type, f"retained/{grant.sha256}",
                        binding.installation_id, binding.agent_id)


def _blob_path(binding: InstanceBinding, blob: RetainedBlob) -> Path:
    if not isinstance(blob, RetainedBlob):
        _bad("blob")
    root = _root(binding, create=False)
    if blob.installation_id != binding.installation_id:
        _bad("installation_id", "ACCESS_DENIED")
    if blob.agent_id != binding.agent_id:
        _bad("agent_id", "ACCESS_DENIED")
    path = root / blob.sha256
    _reject_links(path, "retained_path")
    if path.relative_to(binding.data_directory) != Path(blob.relative_path):
        _bad("relative_path", "IDENTITY_UNBOUND")
    return path


def read_retained(binding: InstanceBinding, blob: RetainedBlob) -> bytes:
    path = _blob_path(binding, blob)
    try:
        data = _read_bounded(path, MAX_RETAINED_BYTES, "retained_path")
    except OSError as exc:
        raise ContractError("SOURCE_MISSING", "retained_path") from exc
    if len(data) != blob.size_bytes or len(data) > MAX_RETAINED_BYTES:
        _bad("size_bytes", "INTEGRITY_ERROR")
    if hashlib.sha256(data).hexdigest() != blob.sha256:
        _bad("sha256", "INTEGRITY_ERROR")
    _validate_magic(data, blob.media_type)
    _check_secret(data, "retained_content")
    if blob.media_type == "image/svg+xml":
        _validate_svg(data)
    return data


def erase_retained(binding: InstanceBinding, blob: RetainedBlob) -> None:
    try:
        path = _blob_path(binding, blob)
    except ContractError as exc:
        if exc.code == "SOURCE_MISSING":
            return
        raise
    try:
        _io_path(path).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ContractError("STORAGE_UNAVAILABLE", "retained_path") from exc
