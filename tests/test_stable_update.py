"""Focused contracts for the fixed-source stable update staging layer."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from scope_recall import stable_update


VERSION = "2.0.1"
TAG = f"v{VERSION}"
MANIFEST_URL = (
    "https://api.github.com/repos/410979729/"
    "scope-recall-hermes/releases/assets/101"
)
ARCHIVE_URL = (
    "https://api.github.com/repos/410979729/"
    "scope-recall-hermes/releases/assets/102"
)
ARCHIVE_NAME = f"scope-recall-hermes-{VERSION}.tar.gz"


class _Response(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.status = 200
        self.headers = {"Content-Length": str(len(value))}

    def getcode(self) -> int:
        return self.status


def _candidate_files(*, plugin_version: str = VERSION) -> dict[str, bytes]:
    return {
        "plugin.yaml": (
            f"name: scope-recall\nversion: {plugin_version}\n"
        ).encode("utf-8"),
        "pyproject.toml": (
            "[project]\n"
            'name = "hermes-scope-recall"\n'
            f'version = "{VERSION}"\n'
        ).encode("utf-8"),
        "provider.py": b"CANDIDATE = True\n",
    }


def _tree_identity(tmp_path: Path, files: dict[str, bytes]) -> dict[str, object]:
    root = tmp_path / "identity-tree"
    root.mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return stable_update.canonical_tree_manifest(root)


def _tar_bytes(
    files: dict[str, bytes],
    *,
    malicious_kind: str | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative, content in files.items():
            info = tarfile.TarInfo(f"scope-recall-{VERSION}/{relative}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if malicious_kind == "traversal":
            content = b"escape\n"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        elif malicious_kind == "symlink":
            info = tarfile.TarInfo(f"scope-recall-{VERSION}/linked")
            info.type = tarfile.SYMTYPE
            info.linkname = "provider.py"
            archive.addfile(info)
        elif malicious_kind == "hardlink":
            info = tarfile.TarInfo(f"scope-recall-{VERSION}/hard-linked")
            info.type = tarfile.LNKTYPE
            info.linkname = f"scope-recall-{VERSION}/provider.py"
            archive.addfile(info)
    return buffer.getvalue()


def _manifest_bytes(
    archive: bytes,
    tree: dict[str, object],
    *,
    archive_sha256: str | None = None,
    tree_sha256: str | None = None,
    minimum_installed_version: str = "1.10.3",
) -> bytes:
    payload = {
        "schema_version": stable_update.MANIFEST_SCHEMA,
        "repository": stable_update.OFFICIAL_REPOSITORY,
        "tag": TAG,
        "version": VERSION,
        "minimum_installed_version": minimum_installed_version,
        "archive": {
            "asset_name": ARCHIVE_NAME,
            "sha256": archive_sha256 or hashlib.sha256(archive).hexdigest(),
            "size_bytes": len(archive),
        },
        "tree": {
            "algorithm": stable_update.TREE_ALGORITHM,
            "sha256": tree_sha256 or tree["tree_sha256"],
            "file_count": tree["file_count"],
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _release_bytes(
    manifest: bytes,
    archive: bytes,
    *,
    draft: bool = False,
    prerelease: bool = False,
    tag: str = TAG,
    archive_url: str = ARCHIVE_URL,
) -> bytes:
    payload = {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {
                "name": stable_update.STABLE_MANIFEST_ASSET,
                "size": len(manifest),
                "url": MANIFEST_URL,
            },
            {
                "name": ARCHIVE_NAME,
                "size": len(archive),
                "url": archive_url,
            },
        ],
    }
    return json.dumps(payload).encode()


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    release: bytes,
    manifest: bytes,
    archive: bytes,
) -> list[str]:
    responses = {
        stable_update.LATEST_RELEASE_API: release,
        MANIFEST_URL: manifest,
        ARCHIVE_URL: archive,
    }
    calls: list[str] = []

    def open_request(request, *, timeout: int):
        assert timeout == stable_update.NETWORK_TIMEOUT_SECONDS
        calls.append(request.full_url)
        return _Response(responses[request.full_url])

    monkeypatch.setattr(stable_update, "_open_request", open_request)
    return calls


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    files: dict[str, bytes] | None = None,
    malicious_kind: str | None = None,
    archive_sha256: str | None = None,
    tree_sha256: str | None = None,
    draft: bool = False,
    prerelease: bool = False,
    minimum_installed_version: str = "1.10.3",
    archive_url: str = ARCHIVE_URL,
) -> tuple[Path, list[str]]:
    candidate_files = files or _candidate_files()
    tree = _tree_identity(tmp_path, candidate_files)
    archive = _tar_bytes(candidate_files, malicious_kind=malicious_kind)
    manifest = _manifest_bytes(
        archive,
        tree,
        archive_sha256=archive_sha256,
        tree_sha256=tree_sha256,
        minimum_installed_version=minimum_installed_version,
    )
    release = _release_bytes(
        manifest,
        archive,
        draft=draft,
        prerelease=prerelease,
        archive_url=archive_url,
    )
    calls = _install_transport(
        monkeypatch,
        release=release,
        manifest=manifest,
        archive=archive,
    )
    return tmp_path / "cache", calls


def _error_code(result: dict[str, object]) -> str:
    error = result["error"]
    assert isinstance(error, dict)
    return str(error["code"])


def _assert_no_partial_stage(cache: Path) -> None:
    if not cache.exists():
        return
    assert not list(cache.glob(".stable-update-*"))
    assert not list(cache.glob("scope-recall-*"))


def test_happy_path_stages_exact_candidate_atomically_and_reuses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, calls = _fixture(tmp_path, monkeypatch)

    first = stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )

    assert first["ok"] is True
    assert first["status"] == "staged"
    assert first["reused"] is False
    assert first["repository"] == stable_update.OFFICIAL_REPOSITORY
    assert first["version"] == VERSION
    candidate = Path(str(first["candidate_dir"]))
    assert candidate.parent.parent == cache.resolve()
    assert (candidate / "plugin.yaml").read_text(encoding="utf-8").endswith(
        f"version: {VERSION}\n"
    )
    assert not list(cache.glob(".stable-update-*"))
    assert calls == [
        stable_update.LATEST_RELEASE_API,
        MANIFEST_URL,
        ARCHIVE_URL,
    ]

    second = stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )
    assert second["ok"] is True
    assert second["reused"] is True
    assert second["candidate_dir"] == first["candidate_dir"]
    assert calls[-2:] == [stable_update.LATEST_RELEASE_API, MANIFEST_URL]


@pytest.mark.parametrize("field", ["draft", "prerelease"])
def test_draft_or_prerelease_is_refused_before_asset_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    cache, calls = _fixture(
        tmp_path,
        monkeypatch,
        draft=field == "draft",
        prerelease=field == "prerelease",
    )

    result = stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )

    assert result["ok"] is False
    assert _error_code(result) == "RELEASE_NOT_STABLE"
    assert calls == [stable_update.LATEST_RELEASE_API]
    _assert_no_partial_stage(cache)


def test_archive_hash_mismatch_is_cleaned_without_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _calls = _fixture(
        tmp_path,
        monkeypatch,
        archive_sha256="0" * 64,
    )

    result = stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )

    assert _error_code(result) == "ARCHIVE_SHA256_MISMATCH"
    _assert_no_partial_stage(cache)


def test_tree_hash_mismatch_is_cleaned_after_safe_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _calls = _fixture(
        tmp_path,
        monkeypatch,
        tree_sha256="f" * 64,
    )

    result = stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )

    assert _error_code(result) == "TREE_SHA256_MISMATCH"
    _assert_no_partial_stage(cache)


def test_plugin_and_project_version_must_equal_release_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _calls = _fixture(
        tmp_path,
        monkeypatch,
        files=_candidate_files(plugin_version="2.0.2"),
    )

    result = stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )

    assert _error_code(result) == "CANDIDATE_VERSION_MISMATCH"
    _assert_no_partial_stage(cache)


@pytest.mark.parametrize("malicious_kind", ["traversal", "symlink", "hardlink"])
def test_malicious_tar_members_are_rejected_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malicious_kind: str,
) -> None:
    cache, _calls = _fixture(
        tmp_path,
        monkeypatch,
        malicious_kind=malicious_kind,
    )

    result = stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )

    assert _error_code(result) == "UNSAFE_ARCHIVE_MEMBER"
    assert not (tmp_path / "escape.txt").exists()
    _assert_no_partial_stage(cache)


def test_manifest_minimum_version_is_enforced_before_archive_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, calls = _fixture(
        tmp_path,
        monkeypatch,
        minimum_installed_version="2.0.0",
    )

    result = stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )

    assert _error_code(result) == "INSTALLED_VERSION_UNSUPPORTED"
    assert calls == [stable_update.LATEST_RELEASE_API, MANIFEST_URL]
    _assert_no_partial_stage(cache)


def test_non_github_asset_url_is_rejected_without_contacting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, calls = _fixture(
        tmp_path,
        monkeypatch,
        archive_url="https://example.invalid/archive.tar.gz",
    )

    result = stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )

    assert _error_code(result) == "UNTRUSTED_DOWNLOAD_URL"
    assert calls == [stable_update.LATEST_RELEASE_API]
    _assert_no_partial_stage(cache)
