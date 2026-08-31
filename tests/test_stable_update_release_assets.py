"""Release-producer contracts for official stable-update source assets."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

from scope_recall import stable_update


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build.stable_update_assets.py"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
VERSION = "2.0.1"
TAG = f"v{VERSION}"
MANIFEST_URL = (
    "https://api.github.com/repos/410979729/"
    "scope-recall-hermes/releases/assets/201"
)
ARCHIVE_URL = (
    "https://api.github.com/repos/410979729/"
    "scope-recall-hermes/releases/assets/202"
)


class _Response(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.status = 200
        self.headers = {"Content-Length": str(len(value))}

    def getcode(self) -> int:
        return self.status


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_stable_update_asset_builder",
        BUILDER_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            f"safe.directory={root.as_posix()}",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return result.stdout


def _write_repo(
    tmp_path: Path,
    *,
    plugin_version: str = VERSION,
    project_version: str = VERSION,
    extra_relative: str | None = None,
) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "stable_update.py", source / "stable_update.py")
    (source / "plugin.yaml").write_text(
        f"name: scope-recall\nversion: {plugin_version}\n",
        encoding="utf-8",
        newline="\n",
    )
    (source / "pyproject.toml").write_text(
        "[project]\n"
        'name = "hermes-scope-recall"\n'
        f'version = "{project_version}"\n',
        encoding="utf-8",
        newline="\n",
    )
    (source / "provider.py").write_text(
        "CANDIDATE = 'stable'\n",
        encoding="utf-8",
        newline="\n",
    )
    (source / ".env.example").write_text(
        "EXAMPLE_ONLY=\n",
        encoding="utf-8",
        newline="\n",
    )
    excluded = {
        ".env": "REAL_TOKEN=do-not-package\n",
        ".env.production": "PRODUCTION_TOKEN=do-not-package\n",
        "secrets.json": '{"token":"do-not-package"}\n',
        "local.sqlite3": "local truth\n",
        ".venv/pyvenv.cfg": "local environment\n",
        ".pytest_cache/state": "cache\n",
        "build/output.bin": "build\n",
        "dist/old.whl": "distribution\n",
        "private/overlay.py": "PRIVATE = True\n",
        "logs/runtime.log": "runtime\n",
    }
    for relative, content in excluded.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    if extra_relative is not None:
        extra = source / extra_relative
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("USTAR boundary\n", encoding="utf-8", newline="\n")

    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "stable-update-test")
    _git(source, "config", "user.email", "stable-update@example.invalid")
    _git(source, "add", "-f", ".")
    _git(source, "commit", "-m", "stable candidate")
    _git(source, "tag", "-a", TAG, "-m", TAG)
    return source


def _archive_members(archive_path: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for info in archive.getmembers():
            if not info.isfile():
                continue
            handle = archive.extractfile(info)
            assert handle is not None
            members[info.name] = handle.read()
    return members


def _stage_with_consumer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    archive: bytes,
    manifest: bytes,
    cache: Path,
) -> dict[str, object]:
    manifest_payload = json.loads(manifest)
    archive_name = manifest_payload["archive"]["asset_name"]
    release = json.dumps(
        {
            "tag_name": TAG,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": stable_update.STABLE_MANIFEST_ASSET,
                    "size": len(manifest),
                    "url": MANIFEST_URL,
                },
                {
                    "name": archive_name,
                    "size": len(archive),
                    "url": ARCHIVE_URL,
                },
            ],
        }
    ).encode("utf-8")
    responses = {
        stable_update.LATEST_RELEASE_API: release,
        MANIFEST_URL: manifest,
        ARCHIVE_URL: archive,
    }

    def open_request(request, *, timeout: int):
        assert timeout == stable_update.NETWORK_TIMEOUT_SECONDS
        return _Response(responses[request.full_url])

    monkeypatch.setattr(stable_update, "_open_request", open_request)
    return stable_update.stage_latest_stable_update(
        cache_dir=cache,
        installed_version="1.10.3",
    )


def test_producer_bytes_are_accepted_by_stable_update_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    source = _write_repo(tmp_path)
    output = tmp_path / "assets"

    result = builder.build_stable_update_assets(
        source_root=source,
        output_dir=output,
        release_tag=TAG,
    )

    assert result["ok"] is True
    archive_path = output / f"scope-recall-hermes-{VERSION}.tar.gz"
    manifest_path = output / stable_update.STABLE_MANIFEST_ASSET
    consumer = _stage_with_consumer(
        monkeypatch,
        archive=archive_path.read_bytes(),
        manifest=manifest_path.read_bytes(),
        cache=tmp_path / "consumer-cache",
    )
    assert consumer["ok"] is True
    assert consumer["version"] == VERSION
    assert consumer["tree_sha256"] == json.loads(
        manifest_path.read_text(encoding="utf-8")
    )["tree"]["sha256"]


def test_stable_source_archive_and_manifest_are_deterministic(tmp_path: Path) -> None:
    builder = _load_builder()
    source = _write_repo(tmp_path)
    first = tmp_path / "assets-first"
    second = tmp_path / "assets-second"

    builder.build_stable_update_assets(
        source_root=source,
        output_dir=first,
        release_tag=TAG,
    )
    builder.build_stable_update_assets(
        source_root=source,
        output_dir=second,
        release_tag=TAG,
    )

    assert sorted(path.name for path in first.iterdir()) == sorted(
        path.name for path in second.iterdir()
    )
    for first_path in first.iterdir():
        assert first_path.read_bytes() == (second / first_path.name).read_bytes()


def test_private_secret_cache_build_and_local_files_are_not_archived(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    source = _write_repo(tmp_path)
    output = tmp_path / "assets"
    builder.build_stable_update_assets(
        source_root=source,
        output_dir=output,
        release_tag=TAG,
    )

    archive_path = output / f"scope-recall-hermes-{VERSION}.tar.gz"
    members = _archive_members(archive_path)
    relative_names = {
        name.split("/", 1)[1]
        for name in members
    }
    assert ".env.example" in relative_names
    assert {
        ".env",
        ".env.production",
        "secrets.json",
        "local.sqlite3",
        ".venv/pyvenv.cfg",
        ".pytest_cache/state",
        "build/output.bin",
        "dist/old.whl",
        "private/overlay.py",
        "logs/runtime.log",
    }.isdisjoint(relative_names)
    serialized = b"\n".join(members.values())
    assert b"do-not-package" not in serialized
    assert b"local truth" not in serialized


def test_source_version_mismatch_fails_without_partial_assets(tmp_path: Path) -> None:
    builder = _load_builder()
    source = _write_repo(tmp_path, plugin_version="2.0.2")
    output = tmp_path / "assets"

    with pytest.raises(
        builder.StableUpdateAssetBuildError,
        match="source_version_mismatch",
    ):
        builder.build_stable_update_assets(
            source_root=source,
            output_dir=output,
            release_tag=TAG,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".stable-update-assets-*"))


def test_path_outside_ustar_name_boundary_fails_closed(tmp_path: Path) -> None:
    builder = _load_builder()
    source = _write_repo(tmp_path, extra_relative="x" * 101)
    output = tmp_path / "assets"

    with pytest.raises(
        builder.StableUpdateAssetBuildError,
        match="ustar_path_unrepresentable",
    ):
        builder.build_stable_update_assets(
            source_root=source,
            output_dir=output,
            release_tag=TAG,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".stable-update-assets-*"))


def test_release_workflow_builds_sums_and_uploads_both_stable_assets() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    gate_at = workflow.index("Run tagged release gate")
    build_at = workflow.index("scripts/build.stable_update_assets.py")
    create_at = workflow.index("gh release create")

    assert gate_at < build_at < create_at
    assert "--output-dir stable-update-assets" in workflow
    assert "Path(\"stable-update-assets\")" in workflow
    assert "stable-update-assets/*" in workflow
    assert "release-assets/stable-update-assets/*" in workflow
