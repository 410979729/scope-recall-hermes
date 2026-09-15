#!/usr/bin/env python3
"""Build deterministic official stable-update source assets from a tagged tree."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from types import ModuleType
from typing import Sequence


OFFICIAL_REPOSITORY = "410979729/scope-recall-hermes"
MANIFEST_SCHEMA = "scope-recall.stable-update.v1"
TREE_ALGORITHM = "scope-recall-canonical-tree-sha256-v1"
MANIFEST_NAME = "scope-recall-stable-update.json"
MINIMUM_INSTALLED_VERSION = "1.10.3"
_SEMVER_RE = re.compile(
    r"(?P<major>0|[1-9][0-9]{0,5})\."
    r"(?P<minor>0|[1-9][0-9]{0,5})\."
    r"(?P<patch>0|[1-9][0-9]{0,5})"
)
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".agents",
        ".cache",
        ".codex",
        ".execution",
        ".git",
        ".hermes-agent-src",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".secrets",
        ".tox",
        ".venv",
        ".vscode",
        "__pycache__",
        "activity-state",
        "activity_state",
        "backups",
        "build",
        "cache",
        "candidates",
        "dist",
        "htmlcov",
        "lancedb",
        "lancepro",
        "local",
        "logs",
        "private",
        "quarantine",
        "secrets",
        "stable-update-assets",
        "tests",
        "venv",
        "workspace",
    }
)
_EXCLUDED_FILE_NAMES = frozenset(
    {
        ".coverage",
        ".env",
        ".npmrc",
        ".pypirc",
        ".ds_store",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "private_key",
        "scope-recall-stable-update.json",
        "secrets.json",
        "token.json",
        "tokens.json",
    }
)
_EXCLUDED_SUFFIXES = (
    ".db",
    ".key",
    ".log",
    ".pem",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".sqlite-shm",
    ".sqlite-wal",
)
_ALLOWED_GIT_MODES = frozenset({"100644", "100755"})


class StableUpdateAssetBuildError(RuntimeError):
    """Fail-closed build error with a stable, content-free reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(
    root: Path,
    args: Sequence[str],
) -> bytes:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-c",
                f"safe.directory={root.as_posix()}",
                "-C",
                str(root),
                *args,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StableUpdateAssetBuildError("git_prerequisite_failed") from exc
    if result.returncode != 0:
        raise StableUpdateAssetBuildError("git_prerequisite_failed")
    return result.stdout


def _parse_semver(value: str, *, reason_code: str) -> tuple[int, int, int]:
    if len(value) > 32:
        raise StableUpdateAssetBuildError(reason_code)
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        raise StableUpdateAssetBuildError(reason_code)
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _safe_relative_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise StableUpdateAssetBuildError("unsafe_tracked_path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value != pure.as_posix() or ".." in pure.parts:
        raise StableUpdateAssetBuildError("unsafe_tracked_path")
    if any(not part or part in {".", ".."} or ":" in part for part in pure.parts):
        raise StableUpdateAssetBuildError("unsafe_tracked_path")
    return value


def _is_excluded(relative: str) -> bool:
    pure = PurePosixPath(relative)
    folded_parts = tuple(part.casefold() for part in pure.parts)
    if any(part in _EXCLUDED_DIRECTORY_NAMES for part in folded_parts[:-1]):
        return True
    name = folded_parts[-1]
    if name in _EXCLUDED_FILE_NAMES:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if name.startswith("review-report.") or name.startswith(".stable-update-"):
        return True
    if name.startswith("scope-recall-hermes-") and name.endswith((".tar.gz", ".zip")):
        return True
    return name.endswith(_EXCLUDED_SUFFIXES)


def _tracked_regular_files(root: Path) -> list[str]:
    raw = _run_git(root, ["ls-files", "--stage", "-z"])
    selected: list[str] = []
    seen: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode = metadata.split(b" ", 1)[0].decode("ascii")
            relative = encoded_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise StableUpdateAssetBuildError("invalid_git_index") from exc
        relative = _safe_relative_path(relative)
        folded = relative.casefold()
        if folded in seen:
            raise StableUpdateAssetBuildError("duplicate_tracked_path")
        seen.add(folded)
        if _is_excluded(relative):
            continue
        if mode not in _ALLOWED_GIT_MODES:
            raise StableUpdateAssetBuildError("non_regular_tracked_entry")
        selected.append(relative)
    selected.sort()
    required = {"plugin.yaml", "pyproject.toml", "stable_update.py"}
    if not required.issubset(selected):
        raise StableUpdateAssetBuildError("required_source_missing")
    return selected


def _verify_checkout(root: Path, release_tag: str) -> str:
    if not release_tag.startswith("v"):
        raise StableUpdateAssetBuildError("invalid_release_tag")
    version = release_tag[1:]
    _parse_semver(version, reason_code="invalid_release_tag")
    if _run_git(root, ["status", "--porcelain=v1", "--untracked-files=no"]):
        raise StableUpdateAssetBuildError("tracked_checkout_dirty")
    tags = {
        line.strip()
        for line in _run_git(root, ["tag", "--points-at", "HEAD"]).decode(
            "utf-8", errors="strict"
        ).splitlines()
        if line.strip()
    }
    if release_tag not in tags:
        raise StableUpdateAssetBuildError("release_tag_not_at_head")
    return version


def _copy_source_stage(root: Path, destination: Path, paths: Sequence[str]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for relative in paths:
        source = root.joinpath(*PurePosixPath(relative).parts)
        target = destination.joinpath(*PurePosixPath(relative).parts)
        try:
            source_resolved = source.resolve(strict=True)
            source_resolved.relative_to(root)
            source_stat = source.lstat()
        except (OSError, ValueError) as exc:
            raise StableUpdateAssetBuildError("tracked_source_unreadable") from exc
        if (
            source.is_symlink()
            or not stat.S_ISREG(source_stat.st_mode)
            or source_resolved != source
        ):
            raise StableUpdateAssetBuildError("non_regular_tracked_entry")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
        except OSError as exc:
            raise StableUpdateAssetBuildError("source_stage_failed") from exc


def _load_stable_update(source_root: Path) -> ModuleType:
    module_path = source_root / "stable_update.py"
    spec = importlib.util.spec_from_file_location(
        "_scope_recall_stable_update_asset_consumer",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise StableUpdateAssetBuildError("stable_update_consumer_unavailable")
    module = importlib.util.module_from_spec(spec)
    previous_dont_write_bytecode = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    except Exception as exc:
        raise StableUpdateAssetBuildError("stable_update_consumer_unavailable") from exc
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    if (
        getattr(module, "MANIFEST_SCHEMA", None) != MANIFEST_SCHEMA
        or getattr(module, "TREE_ALGORITHM", None) != TREE_ALGORITHM
        or not callable(getattr(module, "canonical_tree_manifest", None))
        or not callable(getattr(module, "_extract_archive", None))
        or not callable(getattr(module, "_candidate_root", None))
        or not callable(getattr(module, "_validated_manifest", None))
    ):
        raise StableUpdateAssetBuildError("stable_update_contract_mismatch")
    return module


def _source_versions(source_root: Path) -> tuple[str, str]:
    try:
        project = tomllib.loads(
            (source_root / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        project_name = project["name"]
        project_version = project["version"]
        plugin_lines = (source_root / "plugin.yaml").read_text(
            encoding="utf-8"
        ).splitlines()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise StableUpdateAssetBuildError("invalid_source_metadata") from exc
    names = [
        line.split(":", 1)[1].strip().strip("\"'")
        for line in plugin_lines
        if line.strip().startswith("name:")
    ]
    versions = [
        line.split(":", 1)[1].strip().strip("\"'")
        for line in plugin_lines
        if line.strip().startswith("version:")
    ]
    if (
        project_name != "hermes-scope-recall"
        or len(names) != 1
        or names[0] != "scope-recall"
        or len(versions) != 1
        or not isinstance(project_version, str)
    ):
        raise StableUpdateAssetBuildError("invalid_source_metadata")
    return str(project_version), versions[0]


def _tar_info(name: str, *, is_dir: bool, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if is_dir and not name.endswith("/") else ""))
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    info.mode = 0o755 if is_dir else 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.size = 0 if is_dir else size
    return info


def _build_deterministic_tar(source_root: Path, destination: Path, version: str) -> None:
    prefix = f"scope-recall-hermes-{version}"
    files: list[tuple[str, Path]] = []
    directories: set[str] = {prefix}
    try:
        for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(source_root).as_posix()
            if path.is_symlink() or not path.is_file():
                if path.is_dir() and not path.is_symlink():
                    directories.add(f"{prefix}/{relative}")
                    continue
                raise StableUpdateAssetBuildError("non_regular_staged_entry")
            files.append((f"{prefix}/{relative}", path))
            parent = PurePosixPath(f"{prefix}/{relative}").parent
            while parent.as_posix() not in {".", ""}:
                directories.add(parent.as_posix())
                parent = parent.parent
        with destination.open("xb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.USTAR_FORMAT,
                ) as archive:
                    for directory in sorted(directories):
                        archive.addfile(_tar_info(directory, is_dir=True))
                    for name, path in files:
                        size = path.stat().st_size
                        with path.open("rb") as handle:
                            archive.addfile(
                                _tar_info(name, is_dir=False, size=size),
                                handle,
                            )
            raw.flush()
            os.fsync(raw.fileno())
    except StableUpdateAssetBuildError:
        raise
    except (ValueError, tarfile.TarError) as exc:
        raise StableUpdateAssetBuildError("ustar_path_unrepresentable") from exc
    except OSError as exc:
        raise StableUpdateAssetBuildError("archive_build_failed") from exc


def _verify_final_archive(
    stable_update: ModuleType,
    archive_path: Path,
    archive_name: str,
    verify_root: Path,
) -> dict[str, object]:
    verify_root.mkdir(parents=True, exist_ok=False)
    try:
        extracted = verify_root / "extracted"
        stable_update._extract_archive(archive_path, archive_name, extracted)
        candidate = stable_update._candidate_root(extracted)
        identity = stable_update.canonical_tree_manifest(candidate)
    except Exception as exc:
        raise StableUpdateAssetBuildError("final_archive_consumer_rejected") from exc
    if (
        identity.get("algorithm") != TREE_ALGORITHM
        or not isinstance(identity.get("tree_sha256"), str)
        or not isinstance(identity.get("file_count"), int)
    ):
        raise StableUpdateAssetBuildError("final_archive_identity_invalid")
    return dict(identity)


def _cleanup(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)


def build_stable_update_assets(
    *,
    source_root: Path,
    output_dir: Path,
    release_tag: str,
) -> dict[str, object]:
    """Build and atomically publish deterministic stable-update release assets."""

    try:
        source = Path(source_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise StableUpdateAssetBuildError("source_root_unavailable") from exc
    if not source.is_dir() or source.is_symlink():
        raise StableUpdateAssetBuildError("source_root_unavailable")
    version = _verify_checkout(source, release_tag)
    paths = _tracked_regular_files(source)
    output = Path(output_dir).expanduser().resolve(strict=False)
    if output.exists():
        raise StableUpdateAssetBuildError("output_already_exists")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StableUpdateAssetBuildError("output_unavailable") from exc

    publish = Path(
        tempfile.mkdtemp(prefix=".stable-update-assets-", dir=output.parent)
    )
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix="scope-recall-stable-source-") as raw_temp:
            temp_root = Path(raw_temp)
            source_stage = temp_root / "source"
            _copy_source_stage(source, source_stage, paths)
            project_version, plugin_version = _source_versions(source_stage)
            if project_version != version or plugin_version != version:
                raise StableUpdateAssetBuildError("source_version_mismatch")
            _parse_semver(project_version, reason_code="source_version_mismatch")

            stable_update = _load_stable_update(source_stage)
            try:
                staged_identity = dict(
                    stable_update.canonical_tree_manifest(source_stage)
                )
            except Exception as exc:
                raise StableUpdateAssetBuildError(
                    "source_stage_consumer_rejected"
                ) from exc
            archive_name = f"scope-recall-hermes-{version}.tar.gz"
            archive_path = publish / archive_name
            _build_deterministic_tar(source_stage, archive_path, version)
            final_identity = _verify_final_archive(
                stable_update,
                archive_path,
                archive_name,
                temp_root / "verify",
            )
            if final_identity != staged_identity:
                raise StableUpdateAssetBuildError("producer_consumer_tree_mismatch")

            archive_size = archive_path.stat().st_size
            archive_sha256 = _sha256_file(archive_path)
            manifest = {
                "schema_version": MANIFEST_SCHEMA,
                "repository": OFFICIAL_REPOSITORY,
                "tag": release_tag,
                "version": version,
                "minimum_installed_version": MINIMUM_INSTALLED_VERSION,
                "archive": {
                    "asset_name": archive_name,
                    "sha256": archive_sha256,
                    "size_bytes": archive_size,
                },
                "tree": {
                    "algorithm": TREE_ALGORITHM,
                    "sha256": final_identity["tree_sha256"],
                    "file_count": final_identity["file_count"],
                },
            }
            manifest_bytes = _canonical_json_bytes(manifest) + b"\n"
            try:
                stable_update._validated_manifest(
                    manifest_bytes,
                    release_tag=release_tag,
                    installed_version=MINIMUM_INSTALLED_VERSION,
                    assets={archive_name: {"size": archive_size}},
                )
            except Exception as exc:
                raise StableUpdateAssetBuildError(
                    "final_manifest_consumer_rejected"
                ) from exc
            manifest_path = publish / MANIFEST_NAME
            with manifest_path.open("xb") as handle:
                handle.write(manifest_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(publish, output)
            published = True
    finally:
        if not published:
            _cleanup(publish)

    return {
        "ok": True,
        "repository": OFFICIAL_REPOSITORY,
        "tag": release_tag,
        "version": version,
        "minimum_installed_version": MINIMUM_INSTALLED_VERSION,
        "archive": str(output / f"scope-recall-hermes-{version}.tar.gz"),
        "manifest": str(output / MANIFEST_NAME),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--release-tag", required=True)
    args = parser.parse_args(argv)
    try:
        result = build_stable_update_assets(
            source_root=args.source,
            output_dir=args.output_dir,
            release_tag=str(args.release_tag),
        )
    except StableUpdateAssetBuildError as exc:
        print(
            json.dumps(
                {"ok": False, "reason_code": exc.reason_code},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
