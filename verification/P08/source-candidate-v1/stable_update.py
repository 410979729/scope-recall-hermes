"""Download and stage one verified candidate from the official stable release.

This module is intentionally independent of the active Scope Recall plugin and
uses only the Python standard library.  It never activates a candidate, opens a
database, or controls a service.  Network authority is fixed to the official
GitHub repository; callers cannot supply a repository or download URL.
"""

from __future__ import annotations

import hashlib
import gzip
from http.client import HTTPMessage
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import tomllib
from typing import Any, BinaryIO, Mapping, Protocol
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


OFFICIAL_REPOSITORY = "410979729/scope-recall-hermes"
LATEST_RELEASE_API = (
    "https://api.github.com/repos/410979729/scope-recall-hermes/releases/latest"
)
STABLE_MANIFEST_ASSET = "scope-recall-stable-update.json"
MANIFEST_SCHEMA = "scope-recall.stable-update.v1"
TREE_ALGORITHM = "scope-recall-canonical-tree-sha256-v1"
STAGE_RECEIPT_SCHEMA = "scope-recall.stable-stage.v1"

ALLOWED_HTTPS_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

MAX_RELEASE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_TREE_FILES = 8_000
MAX_RECEIPT_BYTES = 8 * 1024
MAX_ASSETS = 128
MAX_PATH_LENGTH = 512
MAX_SEGMENT_LENGTH = 180
NETWORK_TIMEOUT_SECONDS = 30

_SEMVER_RE = re.compile(
    r"(?P<major>0|[1-9][0-9]{0,5})\."
    r"(?P<minor>0|[1-9][0-9]{0,5})\."
    r"(?P<patch>0|[1-9][0-9]{0,5})"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ASSET_API_RE = re.compile(
    r"/repos/410979729/scope-recall-hermes/releases/assets/[1-9][0-9]{0,19}"
)
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_RETRYABLE_CODES = frozenset(
    {
        "NETWORK_ERROR",
        "HTTP_ERROR",
        "DOWNLOAD_TRUNCATED",
        "LOCAL_IO_ERROR",
    }
)


class StableUpdateError(RuntimeError):
    """A content-free, fail-closed stable-update failure."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


class _BinaryReader(Protocol):
    """Minimal byte-reader contract shared by files and ``gzip.GzipFile``."""

    def read(self, size: int = -1) -> bytes: ...


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Reject redirects outside the explicit GitHub HTTPS boundary."""

    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        _validate_https_url(newurl, allow_release_asset_redirect=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(
    path: Path,
    *,
    expected_size: int | None = None,
    limit: int | None = None,
) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            if limit is not None and total > limit:
                raise StableUpdateError("ARCHIVE_EXPANSION_LIMIT")
            digest.update(chunk)
    if expected_size is not None and total != expected_size:
        raise StableUpdateError("UNSAFE_CANDIDATE_TREE")
    return digest.hexdigest()


def _parse_semver(value: object, *, code: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) > 32:
        raise StableUpdateError(code)
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        raise StableUpdateError(code)
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _validate_https_url(
    value: object,
    *,
    exact_api: bool = False,
    asset_api: bool = False,
    allow_release_asset_redirect: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise StableUpdateError("UNTRUSTED_DOWNLOAD_URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise StableUpdateError("UNTRUSTED_DOWNLOAD_URL") from exc
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or host not in ALLOWED_HTTPS_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise StableUpdateError("UNTRUSTED_DOWNLOAD_URL")
    if exact_api and value != LATEST_RELEASE_API:
        raise StableUpdateError("UNTRUSTED_DOWNLOAD_URL")
    if asset_api:
        if (
            host != "api.github.com"
            or parsed.query
            or _ASSET_API_RE.fullmatch(parsed.path) is None
        ):
            raise StableUpdateError("UNTRUSTED_DOWNLOAD_URL")
    if allow_release_asset_redirect:
        if host == "api.github.com" and _ASSET_API_RE.fullmatch(parsed.path) is None:
            raise StableUpdateError("UNTRUSTED_DOWNLOAD_URL")
        if host == "github.com" and not parsed.path.startswith(
            "/410979729/scope-recall-hermes/releases/download/"
        ):
            raise StableUpdateError("UNTRUSTED_DOWNLOAD_URL")
    return value


def _open_request(request: Request, *, timeout: int) -> Any:
    """Open a request while honoring standard proxy environment variables."""

    opener = build_opener(ProxyHandler(), _SafeRedirectHandler())
    return opener.open(request, timeout=timeout)


def _request(url: str, *, accept: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "hermes-scope-recall-stable-update/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )


def _response_length(response: Any, *, limit: int) -> int | None:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    if status is not None and int(status) != 200:
        raise StableUpdateError("HTTP_ERROR")
    raw = response.headers.get("Content-Length") if response.headers is not None else None
    if raw in {None, ""}:
        return None
    if not isinstance(raw, str):
        raise StableUpdateError("INVALID_CONTENT_LENGTH")
    try:
        length = int(raw)
    except (TypeError, ValueError) as exc:
        raise StableUpdateError("INVALID_CONTENT_LENGTH") from exc
    if length < 0 or length > limit:
        raise StableUpdateError("DOWNLOAD_TOO_LARGE")
    return length


def _read_bounded(url: str, *, accept: str, limit: int) -> bytes:
    try:
        with _open_request(
            _request(url, accept=accept),
            timeout=NETWORK_TIMEOUT_SECONDS,
        ) as response:
            expected = _response_length(response, limit=limit)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(64 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise StableUpdateError("DOWNLOAD_TOO_LARGE")
                chunks.append(chunk)
    except StableUpdateError:
        raise
    except HTTPError as exc:
        raise StableUpdateError("HTTP_ERROR") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise StableUpdateError("NETWORK_ERROR") from exc
    if expected is not None and total != expected:
        raise StableUpdateError("DOWNLOAD_TRUNCATED")
    return b"".join(chunks)


def _download_bounded(
    url: str,
    destination: Path,
    *,
    expected_size: int,
) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with _open_request(
            _request(url, accept="application/octet-stream"),
            timeout=NETWORK_TIMEOUT_SECONDS,
        ) as response:
            declared = _response_length(response, limit=MAX_ARCHIVE_BYTES)
            if declared is not None and declared != expected_size:
                raise StableUpdateError("ARCHIVE_SIZE_MISMATCH")
            with destination.open("xb") as handle:
                while True:
                    chunk = response.read(
                        min(1024 * 1024, MAX_ARCHIVE_BYTES - total + 1)
                    )
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES or total > expected_size:
                        raise StableUpdateError("DOWNLOAD_TOO_LARGE")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
    except StableUpdateError:
        raise
    except HTTPError as exc:
        raise StableUpdateError("HTTP_ERROR") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise StableUpdateError("NETWORK_ERROR") from exc
    if total != expected_size:
        raise StableUpdateError("DOWNLOAD_TRUNCATED")
    return digest.hexdigest()


def _decode_json(raw: bytes, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StableUpdateError(code) from exc
    if not isinstance(value, dict):
        raise StableUpdateError(code)
    return value


def _validated_assets(release: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_ASSETS:
        raise StableUpdateError("INVALID_RELEASE_METADATA")
    assets: dict[str, dict[str, Any]] = {}
    for item in raw_assets:
        if not isinstance(item, dict):
            raise StableUpdateError("INVALID_RELEASE_METADATA")
        name = item.get("name")
        size = item.get("size")
        url = item.get("url")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 180
            or "/" in name
            or "\\" in name
            or any(ord(char) < 32 for char in name)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise StableUpdateError("INVALID_RELEASE_METADATA")
        _validate_https_url(url, asset_api=True)
        if name in assets:
            raise StableUpdateError("DUPLICATE_RELEASE_ASSET")
        assets[name] = {"name": name, "size": size, "url": url}
    return assets


def _release_metadata() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    _validate_https_url(LATEST_RELEASE_API, exact_api=True)
    release = _decode_json(
        _read_bounded(
            LATEST_RELEASE_API,
            accept="application/vnd.github+json",
            limit=MAX_RELEASE_BYTES,
        ),
        code="INVALID_RELEASE_METADATA",
    )
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise StableUpdateError("RELEASE_NOT_STABLE")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise StableUpdateError("INVALID_RELEASE_TAG")
    _parse_semver(tag[1:], code="INVALID_RELEASE_TAG")
    return release, _validated_assets(release)


def _require_exact_keys(value: Mapping[str, Any], keys: set[str], *, code: str) -> None:
    if set(value) != keys:
        raise StableUpdateError(code)


def _validated_manifest(
    raw: bytes,
    *,
    release_tag: str,
    installed_version: str,
    assets: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    payload = _decode_json(raw, code="INVALID_STABLE_MANIFEST")
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "repository",
            "tag",
            "version",
            "minimum_installed_version",
            "archive",
            "tree",
        },
        code="INVALID_STABLE_MANIFEST",
    )
    if (
        payload.get("schema_version") != MANIFEST_SCHEMA
        or payload.get("repository") != OFFICIAL_REPOSITORY
    ):
        raise StableUpdateError("INVALID_STABLE_MANIFEST")

    version = payload.get("version")
    tag = payload.get("tag")
    minimum = payload.get("minimum_installed_version")
    version_tuple = _parse_semver(version, code="INVALID_STABLE_MANIFEST")
    minimum_tuple = _parse_semver(minimum, code="INVALID_STABLE_MANIFEST")
    installed_tuple = _parse_semver(
        installed_version,
        code="INVALID_INSTALLED_VERSION",
    )
    if tag != release_tag or tag != f"v{version}":
        raise StableUpdateError("RELEASE_VERSION_MISMATCH")
    if minimum_tuple > version_tuple:
        raise StableUpdateError("INVALID_STABLE_MANIFEST")
    if installed_tuple < minimum_tuple:
        raise StableUpdateError("INSTALLED_VERSION_UNSUPPORTED")
    if version_tuple < installed_tuple:
        raise StableUpdateError("DOWNGRADE_REFUSED")

    archive = payload.get("archive")
    tree = payload.get("tree")
    if not isinstance(archive, dict) or not isinstance(tree, dict):
        raise StableUpdateError("INVALID_STABLE_MANIFEST")
    _require_exact_keys(
        archive,
        {"asset_name", "sha256", "size_bytes"},
        code="INVALID_STABLE_MANIFEST",
    )
    _require_exact_keys(
        tree,
        {"algorithm", "sha256", "file_count"},
        code="INVALID_STABLE_MANIFEST",
    )
    asset_name = archive.get("asset_name")
    # One producer-owned format keeps the consumer parser small and auditable.
    # The release workflow emits strict USTAR; ZIP and PAX/GNU TAR extensions
    # are intentionally outside the stable-update protocol.
    permitted_names = {f"scope-recall-hermes-{version}.tar.gz"}
    archive_sha256 = archive.get("sha256")
    archive_size = archive.get("size_bytes")
    tree_sha256 = tree.get("sha256")
    file_count = tree.get("file_count")
    if (
        asset_name not in permitted_names
        or not isinstance(archive_sha256, str)
        or _SHA256_RE.fullmatch(archive_sha256) is None
        or isinstance(archive_size, bool)
        or not isinstance(archive_size, int)
        or not 1 <= archive_size <= MAX_ARCHIVE_BYTES
        or tree.get("algorithm") != TREE_ALGORITHM
        or not isinstance(tree_sha256, str)
        or _SHA256_RE.fullmatch(tree_sha256) is None
        or isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or not 2 <= file_count <= MAX_TREE_FILES
    ):
        raise StableUpdateError("INVALID_STABLE_MANIFEST")
    asset = assets.get(str(asset_name))
    if asset is None:
        raise StableUpdateError("ARCHIVE_ASSET_MISSING")
    if asset.get("size") != archive_size:
        raise StableUpdateError("ARCHIVE_SIZE_MISMATCH")
    return payload


def _is_reparse(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError as exc:
        raise StableUpdateError("LOCAL_IO_ERROR") from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _member_name(raw: object, *, is_dir: bool) -> str:
    if not isinstance(raw, str) or "\\" in raw or "\x00" in raw:
        raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER")
    value = raw[:-1] if is_dir and raw.endswith("/") else raw
    if not value or len(value) > MAX_PATH_LENGTH:
        raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value != pure.as_posix() or ".." in pure.parts:
        raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER")
    for part in pure.parts:
        stem = part.split(".", 1)[0].upper()
        if (
            not part
            or part in {".", ".."}
            or len(part) > MAX_SEGMENT_LENGTH
            or ":" in part
            or part.endswith((" ", "."))
            or unicodedata.normalize("NFC", part) != part
            or stem in _WINDOWS_RESERVED
            or any(ord(char) < 32 for char in part)
        ):
            raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER")
    return value


def _safe_target(root: Path, relative: str) -> Path:
    target = root.joinpath(*PurePosixPath(relative).parts)
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER") from exc
    return target


def _copy_member(source: _BinaryReader, target: Path, *, size: int) -> None:
    if size < 0 or size > MAX_SINGLE_FILE_BYTES:
        raise StableUpdateError("ARCHIVE_EXPANSION_LIMIT")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            remaining = size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise StableUpdateError("CORRUPT_ARCHIVE")
                if len(chunk) > remaining:
                    raise StableUpdateError("CORRUPT_ARCHIVE")
                handle.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise StableUpdateError("CORRUPT_ARCHIVE")
            handle.flush()
            os.fsync(handle.fileno())
        status = target.lstat()
    except StableUpdateError:
        raise
    except OSError as exc:
        raise StableUpdateError("LOCAL_IO_ERROR") from exc
    if not stat.S_ISREG(status.st_mode) or _is_reparse(target):
        raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER")


def _tar_number(raw: bytes) -> int:
    """Decode one positive USTAR octal field; reject GNU base-256 values."""

    if raw and raw[0] & 0x80:
        raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER")
    value = raw.strip(b" \x00")
    if not value:
        return 0
    if any(byte not in b"01234567" for byte in value):
        raise StableUpdateError("CORRUPT_ARCHIVE")
    return int(value, 8)


def _tar_text(raw: bytes) -> str:
    value, separator, padding = raw.partition(b"\x00")
    if separator and padding.strip(b"\x00"):
        raise StableUpdateError("CORRUPT_ARCHIVE")
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER") from exc


def _read_exact(source: _BinaryReader, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = source.read(remaining)
        if not chunk:
            raise StableUpdateError("CORRUPT_ARCHIVE")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _TarMemberReader:
    """Expose exactly one member payload without owning the TAR stream."""

    def __init__(self, source: _BinaryReader, size: int) -> None:
        self._source = source
        self._remaining = size

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        if size < 0 or size > self._remaining:
            size = self._remaining
        payload = self._source.read(size)
        self._remaining -= len(payload)
        return payload


def _extract_tar(archive_path: Path, root: Path) -> None:
    """Extract the release workflow's strict USTAR subset.

    Parsing 512-byte headers directly is deliberate: Python's general TAR
    reader consumes PAX/GNU extension payloads before yielding a member, so a
    malicious metadata header can allocate far beyond a post-yield limit.
    This reader rejects every extension, link, device, sparse, and unknown
    type before consuming its payload.
    """

    seen: set[str] = set()
    total = 0
    member_count = 0
    try:
        with gzip.open(archive_path, "rb") as archive:
            while True:
                header = _read_exact(archive, 512)
                if header == b"\x00" * 512:
                    if _read_exact(archive, 512) != b"\x00" * 512:
                        raise StableUpdateError("CORRUPT_ARCHIVE")
                    break
                if header[257:263] != b"ustar\x00" or header[263:265] != b"00":
                    raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER")
                expected_checksum = _tar_number(header[148:156])
                actual_checksum = sum(header[:148]) + (32 * 8) + sum(header[156:])
                if expected_checksum != actual_checksum:
                    raise StableUpdateError("CORRUPT_ARCHIVE")
                member_count += 1
                if member_count > MAX_ARCHIVE_MEMBERS:
                    raise StableUpdateError("ARCHIVE_MEMBER_LIMIT")
                type_flag = header[156:157]
                is_dir = type_flag == b"5"
                if type_flag not in {b"\x00", b"0", b"5"}:
                    raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER")
                name = _tar_text(header[:100])
                prefix = _tar_text(header[345:500])
                raw_name = f"{prefix}/{name}" if prefix else name
                relative = _member_name(raw_name, is_dir=is_dir)
                folded = relative.casefold()
                if folded in seen:
                    raise StableUpdateError("DUPLICATE_ARCHIVE_MEMBER")
                seen.add(folded)
                size = _tar_number(header[124:136])
                if is_dir:
                    if size != 0:
                        raise StableUpdateError("UNSAFE_ARCHIVE_MEMBER")
                    _safe_target(root, relative).mkdir(parents=True, exist_ok=True)
                    continue
                if size > MAX_SINGLE_FILE_BYTES:
                    raise StableUpdateError("ARCHIVE_EXPANSION_LIMIT")
                total += size
                if total > MAX_UNPACKED_BYTES:
                    raise StableUpdateError("ARCHIVE_EXPANSION_LIMIT")
                source = _TarMemberReader(archive, size)
                _copy_member(
                    source,
                    _safe_target(root, relative),
                    size=size,
                )
                padding = (-size) % 512
                if padding and _read_exact(archive, padding) != b"\x00" * padding:
                    raise StableUpdateError("CORRUPT_ARCHIVE")
        if member_count == 0:
            raise StableUpdateError("ARCHIVE_MEMBER_LIMIT")
    except StableUpdateError:
        raise
    except (EOFError, gzip.BadGzipFile, OSError) as exc:
        raise StableUpdateError("CORRUPT_ARCHIVE") from exc


def _extract_archive(archive_path: Path, asset_name: str, root: Path) -> None:
    root.mkdir(parents=False, exist_ok=False)
    if asset_name.endswith(".tar.gz"):
        _extract_tar(archive_path, root)
    else:
        raise StableUpdateError("INVALID_STABLE_MANIFEST")


def _candidate_root(extracted: Path) -> Path:
    required = ("plugin.yaml", "pyproject.toml")
    if all((extracted / name).is_file() for name in required):
        return extracted
    try:
        children = list(extracted.iterdir())
    except OSError as exc:
        raise StableUpdateError("LOCAL_IO_ERROR") from exc
    if (
        len(children) == 1
        and children[0].is_dir()
        and not children[0].is_symlink()
        and not _is_reparse(children[0])
        and all((children[0] / name).is_file() for name in required)
    ):
        return children[0]
    raise StableUpdateError("INVALID_CANDIDATE_ROOT")


def canonical_tree_manifest(root: Path) -> dict[str, object]:
    """Return the canonical regular-file identity of an extracted candidate."""

    source = Path(root).expanduser()
    try:
        if source.is_symlink() or (source.exists() and _is_reparse(source)):
            raise StableUpdateError("UNSAFE_CANDIDATE_TREE")
        resolved = source.resolve(strict=True)
    except StableUpdateError:
        raise
    except OSError as exc:
        raise StableUpdateError("LOCAL_IO_ERROR") from exc
    if not resolved.is_dir() or resolved.is_symlink() or _is_reparse(resolved):
        raise StableUpdateError("UNSAFE_CANDIDATE_TREE")
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    total = 0
    stack = [resolved]
    while stack:
        current = stack.pop()
        try:
            children = list(os.scandir(current))
        except OSError as exc:
            raise StableUpdateError("LOCAL_IO_ERROR") from exc
        for child in children:
            path = Path(child.path)
            try:
                relative = path.relative_to(resolved).as_posix()
                normalized = _member_name(relative, is_dir=child.is_dir(follow_symlinks=False))
            except (OSError, ValueError) as exc:
                raise StableUpdateError("UNSAFE_CANDIDATE_TREE") from exc
            folded = normalized.casefold()
            if folded in seen:
                raise StableUpdateError("UNSAFE_CANDIDATE_TREE")
            seen.add(folded)
            if len(seen) > MAX_ARCHIVE_MEMBERS:
                raise StableUpdateError("ARCHIVE_MEMBER_LIMIT")
            if child.is_symlink() or _is_reparse(path):
                raise StableUpdateError("UNSAFE_CANDIDATE_TREE")
            if child.is_dir(follow_symlinks=False):
                stack.append(path)
                continue
            if not child.is_file(follow_symlinks=False):
                raise StableUpdateError("UNSAFE_CANDIDATE_TREE")
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise StableUpdateError("LOCAL_IO_ERROR") from exc
            if size < 0 or size > MAX_SINGLE_FILE_BYTES:
                raise StableUpdateError("UNSAFE_CANDIDATE_TREE")
            total += size
            if total > MAX_UNPACKED_BYTES:
                raise StableUpdateError("ARCHIVE_EXPANSION_LIMIT")
            entries.append(
                {
                    "path": normalized,
                    "sha256": _sha256_file(
                        path,
                        expected_size=size,
                        limit=MAX_SINGLE_FILE_BYTES,
                    ),
                    "size_bytes": size,
                }
            )
            if len(entries) > MAX_TREE_FILES:
                raise StableUpdateError("TREE_FILE_LIMIT")
    entries.sort(key=lambda item: str(item["path"]))
    return {
        "algorithm": TREE_ALGORITHM,
        "tree_sha256": _sha256_bytes(_canonical_json_bytes(entries)),
        "file_count": len(entries),
    }


def _plugin_version(root: Path) -> tuple[str, str]:
    try:
        lines = (root / "plugin.yaml").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise StableUpdateError("INVALID_CANDIDATE_METADATA") from exc
    names: list[str] = []
    versions: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("name:"):
            names.append(line.split(":", 1)[1].strip().strip("\"'"))
        if line.startswith("version:"):
            versions.append(line.split(":", 1)[1].strip().strip("\"'"))
    if len(names) != 1 or len(versions) != 1:
        raise StableUpdateError("INVALID_CANDIDATE_METADATA")
    return names[0], versions[0]


def _project_version(root: Path) -> tuple[str, str]:
    try:
        payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = payload["project"]
        name = project["name"]
        version = project["version"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise StableUpdateError("INVALID_CANDIDATE_METADATA") from exc
    if not isinstance(name, str) or not isinstance(version, str):
        raise StableUpdateError("INVALID_CANDIDATE_METADATA")
    return name, version


def _verify_candidate(root: Path, manifest: Mapping[str, Any]) -> dict[str, object]:
    expected_version = str(manifest["version"])
    plugin_name, plugin_version = _plugin_version(root)
    project_name, project_version = _project_version(root)
    if plugin_name != "scope-recall" or project_name != "hermes-scope-recall":
        raise StableUpdateError("INVALID_CANDIDATE_METADATA")
    if plugin_version != expected_version or project_version != expected_version:
        raise StableUpdateError("CANDIDATE_VERSION_MISMATCH")
    _parse_semver(plugin_version, code="CANDIDATE_VERSION_MISMATCH")
    actual = canonical_tree_manifest(root)
    expected_tree = manifest["tree"]
    if actual["file_count"] != expected_tree["file_count"]:
        raise StableUpdateError("TREE_FILE_COUNT_MISMATCH")
    if actual["tree_sha256"] != expected_tree["sha256"]:
        raise StableUpdateError("TREE_SHA256_MISMATCH")
    return actual


def _resolved_live_plugin_paths() -> tuple[Path, ...]:
    values: list[Path] = []
    explicit = str(os.environ.get("SCOPE_RECALL_PLUGIN_DIR") or "").strip()
    home = str(os.environ.get("HERMES_HOME") or "").strip()
    for raw in (explicit, str(Path(home) / "plugins" / "scope-recall") if home else ""):
        if not raw:
            continue
        try:
            values.append(Path(raw).expanduser().resolve(strict=False))
        except OSError:
            continue
    return tuple(values)


def _safe_cache_dir(cache_dir: Path) -> Path:
    raw = Path(cache_dir).expanduser()
    try:
        if raw.exists() and (raw.is_symlink() or _is_reparse(raw)):
            raise StableUpdateError("UNSAFE_CACHE_DIR")
        raw.mkdir(parents=True, exist_ok=True)
        resolved = raw.resolve(strict=True)
    except StableUpdateError:
        raise
    except OSError as exc:
        raise StableUpdateError("LOCAL_IO_ERROR") from exc
    if not resolved.is_dir() or resolved.is_symlink() or _is_reparse(resolved):
        raise StableUpdateError("UNSAFE_CACHE_DIR")
    for plugin_dir in _resolved_live_plugin_paths():
        if resolved == plugin_dir or plugin_dir in resolved.parents:
            raise StableUpdateError("CACHE_OVERLAPS_ACTIVE_PLUGIN")
    return resolved


def _safe_cleanup(path: Path, *, cache_dir: Path) -> bool:
    try:
        resolved_parent = path.parent.resolve(strict=True)
        if resolved_parent != cache_dir or not path.name.startswith(".stable-update-"):
            return False
        if path.is_symlink() or _is_reparse(path):
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)
        return not path.exists()
    except (OSError, StableUpdateError):
        return False


def _failure(code: str) -> dict[str, object]:
    return {
        "ok": False,
        "status": "failed",
        "error": {
            "code": code,
            "retryable": code in _RETRYABLE_CODES,
        },
    }


def _stage_receipt(manifest: Mapping[str, Any]) -> dict[str, object]:
    return {
        "schema_version": STAGE_RECEIPT_SCHEMA,
        "repository": OFFICIAL_REPOSITORY,
        "tag": manifest["tag"],
        "version": manifest["version"],
        "minimum_installed_version": manifest["minimum_installed_version"],
        "archive_sha256": manifest["archive"]["sha256"],
        "tree_sha256": manifest["tree"]["sha256"],
        "file_count": manifest["tree"]["file_count"],
    }


def _existing_stage(
    bundle: Path,
    *,
    manifest: Mapping[str, Any],
) -> Path | None:
    if not bundle.exists():
        return None
    if bundle.is_symlink() or _is_reparse(bundle) or not bundle.is_dir():
        raise StableUpdateError("CACHE_CONFLICT")
    candidate = bundle / "candidate"
    receipt_path = bundle / "stable-update.json"
    try:
        if (
            receipt_path.is_symlink()
            or not receipt_path.is_file()
            or _is_reparse(receipt_path)
            or receipt_path.stat().st_size > MAX_RECEIPT_BYTES
        ):
            raise StableUpdateError("CACHE_CONFLICT")
        receipt = _decode_json(
            receipt_path.read_bytes(),
            code="CACHE_CONFLICT",
        )
    except (OSError, StableUpdateError) as exc:
        raise StableUpdateError("CACHE_CONFLICT") from exc
    if receipt != _stage_receipt(manifest):
        raise StableUpdateError("CACHE_CONFLICT")
    _verify_candidate(candidate, manifest)
    return candidate


def stage_latest_stable_update(
    *,
    cache_dir: Path,
    installed_version: str,
) -> dict[str, object]:
    """Download, verify, and atomically cache the latest official stable source.

    Failures are returned as content-free reason codes.  The function has no
    activation authority and does not inspect or mutate the active plugin.
    """

    temporary: Path | None = None
    cleanup_failed = False
    result: dict[str, object]
    try:
        cache = _safe_cache_dir(cache_dir)
        release, assets = _release_metadata()
        release_tag = str(release["tag_name"])
        manifest_asset = assets.get(STABLE_MANIFEST_ASSET)
        if manifest_asset is None:
            raise StableUpdateError("STABLE_MANIFEST_ASSET_MISSING")
        if not 1 <= int(manifest_asset["size"]) <= MAX_MANIFEST_BYTES:
            raise StableUpdateError("INVALID_STABLE_MANIFEST")
        manifest_raw = _read_bounded(
            str(manifest_asset["url"]),
            accept="application/octet-stream",
            limit=MAX_MANIFEST_BYTES,
        )
        if len(manifest_raw) != int(manifest_asset["size"]):
            raise StableUpdateError("DOWNLOAD_TRUNCATED")
        manifest = _validated_manifest(
            manifest_raw,
            release_tag=release_tag,
            installed_version=installed_version,
            assets=assets,
        )
        tree_sha = str(manifest["tree"]["sha256"])
        version = str(manifest["version"])
        bundle = cache / f"scope-recall-{version}-{tree_sha[:16]}"
        existing = _existing_stage(bundle, manifest=manifest)
        if existing is not None:
            return {
                "ok": True,
                "status": "staged",
                "reused": True,
                "repository": OFFICIAL_REPOSITORY,
                "tag": release_tag,
                "version": version,
                "candidate_dir": str(existing),
                "archive_sha256": manifest["archive"]["sha256"],
                "tree_sha256": tree_sha,
                "file_count": manifest["tree"]["file_count"],
            }

        temporary = Path(tempfile.mkdtemp(prefix=".stable-update-", dir=cache))
        archive_name = str(manifest["archive"]["asset_name"])
        archive_asset = assets[archive_name]
        archive_path = temporary / "candidate.archive"
        archive_sha = _download_bounded(
            str(archive_asset["url"]),
            archive_path,
            expected_size=int(manifest["archive"]["size_bytes"]),
        )
        if archive_sha != manifest["archive"]["sha256"]:
            raise StableUpdateError("ARCHIVE_SHA256_MISMATCH")

        extracted = temporary / "extracted"
        _extract_archive(archive_path, archive_name, extracted)
        candidate = _candidate_root(extracted)
        actual_tree = _verify_candidate(candidate, manifest)

        staged_bundle = temporary / "bundle"
        staged_bundle.mkdir()
        staged_candidate = staged_bundle / "candidate"
        os.replace(candidate, staged_candidate)
        receipt_path = staged_bundle / "stable-update.json"
        with receipt_path.open("xb") as handle:
            handle.write(_canonical_json_bytes(_stage_receipt(manifest)) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        if _verify_candidate(staged_candidate, manifest) != actual_tree:
            raise StableUpdateError("STAGE_VERIFICATION_MISMATCH")
        try:
            os.replace(staged_bundle, bundle)
        except FileExistsError:
            existing = _existing_stage(bundle, manifest=manifest)
            if existing is None:
                raise StableUpdateError("CACHE_CONFLICT")
            staged_candidate = existing
        if _verify_candidate(bundle / "candidate", manifest) != actual_tree:
            raise StableUpdateError("STAGE_VERIFICATION_MISMATCH")
        result = {
            "ok": True,
            "status": "staged",
            "reused": False,
            "repository": OFFICIAL_REPOSITORY,
            "tag": release_tag,
            "version": version,
            "candidate_dir": str(bundle / "candidate"),
            "archive_sha256": archive_sha,
            "tree_sha256": actual_tree["tree_sha256"],
            "file_count": actual_tree["file_count"],
        }
    except StableUpdateError as exc:
        result = _failure(exc.code)
    except (OSError, ValueError, TypeError) as exc:
        del exc
        result = _failure("LOCAL_IO_ERROR")
    except Exception as exc:  # pragma: no cover - final content-safety boundary
        del exc
        result = _failure("INTERNAL_ERROR")
    finally:
        if temporary is not None and temporary.exists():
            try:
                cache_for_cleanup = temporary.parent.resolve(strict=True)
            except OSError:
                cleanup_failed = True
            else:
                cleanup_failed = not _safe_cleanup(
                    temporary,
                    cache_dir=cache_for_cleanup,
                )
    if cleanup_failed:
        # A fully verified, atomically published bundle is never deleted after
        # publication: another process may already be using it.  A leftover
        # private temp directory is non-authoritative and can be cleaned by a
        # later invocation without converting a valid stage into data loss.
        if result.get("ok") is not True:
            return _failure("LOCAL_CLEANUP_FAILED")
        result = dict(result)
        result["cleanup_warning"] = "LOCAL_CLEANUP_FAILED"
    return result


__all__ = [
    "MANIFEST_SCHEMA",
    "OFFICIAL_REPOSITORY",
    "STABLE_MANIFEST_ASSET",
    "TREE_ALGORITHM",
    "StableUpdateError",
    "canonical_tree_manifest",
    "stage_latest_stable_update",
]
