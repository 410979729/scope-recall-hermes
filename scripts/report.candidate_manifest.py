#!/usr/bin/env python3
"""Generate the exact, content-free Scope Recall release-candidate manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import types
from typing import Sequence


SCHEMA_VERSION = "scope-recall.candidate-manifest.v1"
ALGORITHM = "git-ls-files-content-sha256-v1"
DEFAULT_OUTPUT = Path(".execution/CANDIDATE_MANIFEST.json")
CANONICAL_PROFILES = (
    "core",
    "compatibility",
    "maintenance",
    "developer",
    "extension",
)


class CandidateManifestError(RuntimeError):
    """Raised when an exact candidate boundary cannot be proven."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _run_git(root: Path, args: Sequence[str]) -> bytes:
    resolved = root.resolve(strict=True)
    command = [
        "git",
        "-c",
        "core.quotepath=false",
        "-c",
        f"safe.directory={resolved.as_posix()}",
        "-C",
        str(resolved),
        *args,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateManifestError(
            f"Git prerequisite failed: {type(exc).__name__}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CandidateManifestError(
            f"Git command failed ({result.returncode}): {detail[:300]}"
        )
    return result.stdout


def _tracked_paths(root: Path) -> list[str]:
    paths: list[str] = []
    for raw in _run_git(root, ["ls-files", "-z"]).split(b"\0"):
        if not raw:
            continue
        try:
            value = raw.decode("utf-8").replace("\\", "/")
        except UnicodeDecodeError as exc:
            raise CandidateManifestError("repository contains a non-UTF-8 path") from exc
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts or value != pure.as_posix():
            raise CandidateManifestError(f"unsafe tracked path: {value!r}")
        paths.append(value)
    if len(paths) != len(set(paths)):
        raise CandidateManifestError("duplicate tracked path")
    return sorted(paths)


def source_manifest(root: Path) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for relative in _tracked_paths(root):
        absolute = root.joinpath(*PurePosixPath(relative).parts)
        if not absolute.is_file():
            raise CandidateManifestError(f"tracked path is not a regular file: {relative}")
        content = absolute.read_bytes()
        entries.append(
            {
                "path": relative,
                "sha256": _sha256_bytes(content),
                "size_bytes": len(content),
            }
        )
    return {
        "algorithm": ALGORITHM,
        "file_count": len(entries),
        "manifest_sha256": _sha256_bytes(_canonical_bytes(entries)),
        "files": entries,
    }


def _project_version(root: Path) -> str:
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def _plugin_version(root: Path) -> str:
    for line in (root / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    raise CandidateManifestError("plugin.yaml version is missing")


def _schema_fingerprints(root: Path) -> dict[str, object]:
    package = sys.modules.get("scope_recall")
    if package is None:
        package = types.ModuleType("scope_recall")
        package.__path__ = [str(root)]  # type: ignore[attr-defined]
        sys.modules["scope_recall"] = package
    else:
        package_paths = {
            Path(value).resolve(strict=False)
            for value in getattr(package, "__path__", ())
        }
        if root.resolve(strict=True) not in package_paths:
            raise CandidateManifestError(
                "loaded scope_recall package does not match candidate root"
            )
    from scope_recall.provider_schemas import (  # pyright: ignore[reportMissingImports]
        build_config_schema,
        build_tool_schemas,
    )
    from scope_recall.sql_store import (  # pyright: ignore[reportMissingImports]
        ensure_schema,
        schema_migration_status,
    )

    config_schema = build_config_schema()
    enabled = {
        "maintenance_tools_enabled": True,
        "secret_index_tools_enabled": True,
        "experience": {"enabled": True},
        "temporal_queries": {"enabled": True},
        "reflection": {"enabled": True},
    }
    tool_profiles = {
        profile: build_tool_schemas(
            {**enabled, "tool_schema_profile": profile}
        )
        for profile in CANONICAL_PROFILES
    }
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        ensure_schema(connection)
        schema_rows = [
            {
                "type": str(row["type"]),
                "name": str(row["name"]),
                "table": str(row["tbl_name"]),
                "sql": str(row["sql"] or ""),
            }
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        ]
        migration_status = schema_migration_status(connection)
        migration_rows = [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "plugin_version",
                    "description",
                    "checksum",
                    "status",
                    "error",
                )
            }
            for row in (migration_status.get("applied_migrations") or [])
            if isinstance(row, dict)
        ]
    finally:
        connection.close()
    return {
        "config_schema_sha256": _sha256_bytes(_canonical_bytes(config_schema)),
        "tool_schema_sha256": _sha256_bytes(_canonical_bytes(tool_profiles)),
        "sqlite_schema_sha256": _sha256_bytes(_canonical_bytes(schema_rows)),
        "migration_registry_sha256": _sha256_bytes(
            _canonical_bytes(migration_rows)
        ),
        "sqlite_schema_version": int(migration_status.get("schema_version") or 0),
    }


def _artifact_entries(paths: Sequence[Path]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve(strict=True)
        name = resolved.name
        if name in seen:
            raise CandidateManifestError(f"duplicate artifact basename: {name}")
        seen.add(name)
        entries.append(
            {
                "name": name,
                "size_bytes": resolved.stat().st_size,
                "sha256": _sha256_file(resolved),
            }
        )
    return sorted(entries, key=lambda item: str(item["name"]))


def _repository_identity(root: Path, *, require_clean: bool) -> dict[str, object]:
    status = _run_git(root, ["status", "--porcelain=v1", "-z"])
    clean = not bool(status)
    if require_clean and not clean:
        raise CandidateManifestError("candidate worktree is not clean")
    return {
        "commit": _run_git(root, ["rev-parse", "HEAD"]).decode("ascii").strip(),
        "tree": _run_git(root, ["rev-parse", "HEAD^{tree}"]).decode("ascii").strip(),
        "clean": clean,
    }


def _hermes_identity(hermes_root: Path | None) -> dict[str, object]:
    if hermes_root is None:
        return {"commit": "unbound", "tree": "unbound", "version": "unbound"}
    identity = _repository_identity(hermes_root, require_clean=True)
    identity["version"] = _project_version(hermes_root)
    return identity


def build_candidate_manifest(
    root: Path,
    *,
    hermes_root: Path | None = None,
    artifacts: Sequence[Path] = (),
    ci_run_ids: Sequence[str] = (),
    expected_version: str = "2.0.0",
    require_clean: bool = True,
) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    source_identity = _repository_identity(resolved, require_clean=require_clean)
    package_version = _project_version(resolved)
    plugin_version = _plugin_version(resolved)
    if package_version != expected_version or plugin_version != expected_version:
        raise CandidateManifestError(
            "package/plugin version does not match the expected candidate version"
        )
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "candidate_version": expected_version,
        "source": {
            **source_identity,
            "manifest": source_manifest(resolved),
            "pyproject_sha256": _sha256_file(resolved / "pyproject.toml"),
            "plugin_yaml_sha256": _sha256_file(resolved / "plugin.yaml"),
        },
        "schemas": _schema_fingerprints(resolved),
        "hermes": _hermes_identity(hermes_root),
        "ci_run_ids": sorted({str(item).strip() for item in ci_run_ids if str(item).strip()}),
        "artifacts": _artifact_entries(artifacts),
        "private_artifacts_included": False,
        "authorization": {
            "merge": False,
            "tag": False,
            "release": False,
            "deploy": False,
        },
    }
    payload["manifest_sha256"] = _sha256_bytes(_canonical_bytes(payload))
    return payload


def _require_ignored_output(root: Path, output: Path) -> None:
    try:
        relative = output.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError:
        return
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--quiet", "--", relative.as_posix()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    if result.returncode != 0:
        raise CandidateManifestError(
            "refusing to write candidate manifest to an unignored repository path"
        )


def write_manifest(root: Path, output: Path, payload: dict[str, object]) -> None:
    resolved_output = output if output.is_absolute() else root / output
    _require_ignored_output(root, resolved_output)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{resolved_output.name}.",
        suffix=".tmp",
        dir=resolved_output.parent,
        delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, resolved_output)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--hermes-root", type=Path)
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--ci-run-id", action="append", default=[])
    parser.add_argument("--expected-version", default="2.0.0")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve(strict=True)
    payload = build_candidate_manifest(
        root,
        hermes_root=args.hermes_root,
        artifacts=tuple(args.artifact),
        ci_run_ids=tuple(args.ci_run_id),
        expected_version=str(args.expected_version),
    )
    write_manifest(root, args.output, payload)
    print(
        json.dumps(
            {
                "ok": True,
                "candidate_version": payload["candidate_version"],
                "source_commit": payload["source"]["commit"],  # type: ignore[index]
                "source_file_count": payload["source"]["manifest"]["file_count"],  # type: ignore[index]
                "manifest_sha256": payload["manifest_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CandidateManifestError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
