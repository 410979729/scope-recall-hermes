#!/usr/bin/env python3
"""Generate the deterministic local repository census for repository governance.

The full inventory is intentionally local-only.  It records relative paths,
content hashes, sizes, and coarse lifecycle roles; it never copies file
contents or reads ignored runtime state.  Repository-facing governance files
summarize this output separately.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
from typing import Any, Sequence


SCHEMA_VERSION = "scope-recall.repository-census.v1"
ALGORITHM = "git-ls-files-sha256-v1"
DEFAULT_OUTPUT = Path(".execution/FULL_REPOSITORY_FILE_CENSUS.json")
LARGE_FILE_BYTES = 5 * 1024 * 1024
GOVERNANCE_PATHS = frozenset(
    {
        "REPOSITORY_CENSUS_SUMMARY.md",
        "docs/compatibility-removal-registry.json",
        "docs/repository-census.anomalies.json",
        "docs/repository-census.schema.json",
        "docs/repository-deletion-evidence.json",
        "scripts/report.repository_census.py",
        "tests/test_repository_census.py",
    }
)


class CensusError(RuntimeError):
    """Raised when the repository boundary cannot be proven safely."""


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _run_git(root: Path, args: Sequence[str], *, check: bool = True) -> bytes:
    command = ["git", "-c", "core.quotepath=false", *args]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CensusError(f"Git prerequisite failed: {type(exc).__name__}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CensusError(f"Git command failed ({result.returncode}): {detail[:300]}")
    return result.stdout


def _decode_nul_paths(payload: bytes) -> list[str]:
    paths: list[str] = []
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CensusError("repository contains a non-UTF-8 path") from exc
        normalized = value.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or ".." in pure.parts or normalized != pure.as_posix():
            raise CensusError(f"unsafe repository path: {normalized!r}")
        paths.append(normalized)
    return sorted(set(paths))


def repository_paths(root: Path, *, tracked_only: bool = False) -> tuple[list[str], set[str]]:
    """Return candidate repository paths and the exact tracked subset."""

    tracked = set(_decode_nul_paths(_run_git(root, ["ls-files", "-z"])))
    if tracked_only:
        return sorted(tracked), tracked
    visible = _decode_nul_paths(
        _run_git(
            root,
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        )
    )
    return visible, tracked


def classify_path(path: str) -> tuple[str, str]:
    """Return a stable coarse category and lifecycle for one repository path."""

    if path in GOVERNANCE_PATHS:
        return "governance", "maintainer"
    if path.startswith("tests/"):
        return "test", "verification"
    if path.startswith(".github/"):
        return "workflow", "build-release"
    if path.startswith("constraints/"):
        return "dependency", "build-release"
    if path.startswith("scripts/"):
        return "script", "operator"
    if path.startswith("benchmarks/"):
        return "benchmark", "verification"
    if path.startswith("examples/"):
        return "example", "reference"
    if path.startswith("docs/") or path.endswith(".md"):
        if path.startswith("docs/release-readiness.") and not path.endswith("2.0.0.md"):
            return "documentation", "historical"
        return "documentation", "reference"
    if path.startswith("_internal/") or path.endswith(".py"):
        return "runtime", "production"
    if path in {
        "MANIFEST.in",
        "config.json",
        "plugin.yaml",
        "pyproject.toml",
        "py.typed",
    }:
        return "metadata", "build-release"
    return "repository", "maintainer"


def _file_entry(root: Path, path: str, *, tracked: bool) -> dict[str, Any]:
    absolute = root.joinpath(*PurePosixPath(path).parts)
    if absolute.is_symlink():
        target = os.readlink(absolute)
        content = target.encode("utf-8")
        kind = "symlink"
    elif absolute.is_file():
        content = absolute.read_bytes()
        kind = "file"
    else:
        raise CensusError(f"Git inventory path is not a regular file or symlink: {path}")
    category, lifecycle = classify_path(path)
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "kind": kind,
        "tracked": bool(tracked),
        "category": category,
        "lifecycle": lifecycle,
    }


def _casefold_collisions(paths: Sequence[str]) -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(path.casefold(), []).append(path)
    return [sorted(group) for group in groups.values() if len(group) > 1]


def build_census(root: Path, *, tracked_only: bool = False) -> dict[str, Any]:
    """Build a deterministic census without writing to the repository."""

    resolved_root = root.resolve(strict=True)
    if not (resolved_root / ".git").exists():
        raise CensusError("repository root is not a Git worktree")
    paths, tracked = repository_paths(resolved_root, tracked_only=tracked_only)
    entries = [
        _file_entry(resolved_root, path, tracked=path in tracked)
        for path in paths
    ]
    categories = Counter(str(entry["category"]) for entry in entries)
    lifecycles = Counter(str(entry["lifecycle"]) for entry in entries)
    untracked_paths = [str(entry["path"]) for entry in entries if not entry["tracked"]]
    large_files = [
        {"path": str(entry["path"]), "size_bytes": int(entry["size_bytes"])}
        for entry in entries
        if int(entry["size_bytes"]) > LARGE_FILE_BYTES
    ]
    collisions = _casefold_collisions(paths)
    commit = _run_git(resolved_root, ["rev-parse", "HEAD"]).decode("ascii").strip()
    tree = _run_git(resolved_root, ["rev-parse", "HEAD^{tree}"]).decode("ascii").strip()
    branch = _run_git(resolved_root, ["branch", "--show-current"]).decode(
        "utf-8", errors="strict"
    ).strip()
    dirty = bool(_run_git(resolved_root, ["status", "--porcelain=v1", "-z"]))
    inventory_sha256 = hashlib.sha256(_canonical_json_bytes(entries)).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm": {
            "name": ALGORITHM,
            "path_source": "git ls-files --cached --others --exclude-standard",
            "content_hash": "sha256(raw file bytes); symlinks hash UTF-8 link target",
            "inventory_hash": "sha256(canonical UTF-8 JSON of files array)",
            "ignored_files_included": False,
            "tracked_only": bool(tracked_only),
        },
        "source": {
            "commit": commit,
            "tree": tree,
            "branch": branch,
            "dirty": dirty,
        },
        "file_count": len(entries),
        "tracked_file_count": sum(bool(entry["tracked"]) for entry in entries),
        "untracked_file_count": len(untracked_paths),
        "total_bytes": sum(int(entry["size_bytes"]) for entry in entries),
        "inventory_sha256": inventory_sha256,
        "counts": {
            "category": dict(sorted(categories.items())),
            "lifecycle": dict(sorted(lifecycles.items())),
        },
        "anomalies": {
            "untracked_paths": untracked_paths,
            "large_files": large_files,
            "casefold_collisions": collisions,
        },
        "files": entries,
    }


def _relative_output(root: Path, output: Path) -> str | None:
    try:
        return output.resolve(strict=False).relative_to(root.resolve(strict=True)).as_posix()
    except ValueError:
        return None


def _require_ignored_output(root: Path, output: Path) -> None:
    relative = _relative_output(root, output)
    if relative is None:
        return
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", relative],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    if result.returncode != 0:
        raise CensusError(
            "refusing to write local census to a repository path that is not Git-ignored"
        )


def write_census(root: Path, output: Path, payload: dict[str, Any]) -> None:
    """Atomically write an indented local census after proving its boundary."""

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
    parser.add_argument("--tracked-only", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare the existing output with a freshly computed deterministic census.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve(strict=True)
    output = args.output if args.output.is_absolute() else root / args.output
    payload = build_census(root, tracked_only=bool(args.tracked_only))
    if args.check:
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": type(exc).__name__}))
            return 1
        ok = existing == payload
        print(
            json.dumps(
                {
                    "ok": ok,
                    "file_count": payload["file_count"],
                    "inventory_sha256": payload["inventory_sha256"],
                },
                sort_keys=True,
            )
        )
        return 0 if ok else 1
    write_census(root, output, payload)
    print(
        json.dumps(
            {
                "ok": True,
                "output": _relative_output(root, output) or "[EXTERNAL]",
                "file_count": payload["file_count"],
                "tracked_file_count": payload["tracked_file_count"],
                "untracked_file_count": payload["untracked_file_count"],
                "inventory_sha256": payload["inventory_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CensusError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
