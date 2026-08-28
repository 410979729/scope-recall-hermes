#!/usr/bin/env python3
"""Validate and index one raw, source-bound release-candidate evidence package."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Mapping, Sequence


SCHEMA_VERSION = "scope-recall.evidence-index.v1"
TEST_HONESTY_SCHEMA_VERSION = "scope-recall.test-honesty.v1"
REQUIRED_INPUT_FILES = (
    "SOURCE_IDENTITY.json",
    "BUILD_PROVENANCE.json",
    "CANDIDATE_MANIFEST.json",
    "ARTIFACT_MEMBERS_WHEEL.json",
    "ARTIFACT_MEMBERS_SDIST.json",
    "ARTIFACT_SCAN.json",
    "TEST_COMMANDS.json",
    "PYTEST_JUNIT.xml",
    "PYTEST_STDOUT.log",
    "PYTEST_SKIP_REPORT.json",
    "RUFF.log",
    "PYRIGHT.log",
    "DOCTOR.json",
    "MIGRATION_N_MINUS_ONE.json",
    "MIGRATION_N.json",
    "DOWNGRADE_N_MINUS_ONE.json",
    "PURGE_RESTORE_REPLAY.json",
    "READONLY_CANARY.json",
    "WRITER_CANARY.json",
    "ROLLBACK_REHEARSAL.json",
    "ACTIVE_ISOLATION.json",
    "REPOSITORY_CENSUS.json",
    "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
)
RECEIPT_FILES = (
    "DOCTOR.json",
    "MIGRATION_N_MINUS_ONE.json",
    "MIGRATION_N.json",
    "DOWNGRADE_N_MINUS_ONE.json",
    "PURGE_RESTORE_REPLAY.json",
    "READONLY_CANARY.json",
    "WRITER_CANARY.json",
    "ROLLBACK_REHEARSAL.json",
    "ACTIVE_ISOLATION.json",
    "REPOSITORY_CENSUS.json",
    "REPOSITORY_DELETE_RENAME_EVIDENCE.json",
)


class EvidencePackageError(RuntimeError):
    """Raised when raw evidence is incomplete, inconsistent, or unbound."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidencePackageError(
            f"invalid JSON evidence file {path.name}: {type(exc).__name__}"
        ) from exc


def _load_object(path: Path) -> dict[str, object]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise EvidencePackageError(f"JSON evidence root must be an object: {path.name}")
    return payload


def _require_sha(value: object, *, field: str) -> str:
    rendered = str(value or "")
    if len(rendered) != 64 or any(ch not in "0123456789abcdef" for ch in rendered):
        raise EvidencePackageError(f"{field} must be a lowercase SHA-256")
    return rendered


def _require_git_sha(value: object, *, field: str) -> str:
    rendered = str(value or "")
    if len(rendered) != 40 or any(ch not in "0123456789abcdef" for ch in rendered):
        raise EvidencePackageError(f"{field} must be a full lowercase Git SHA")
    return rendered


def validate_test_honesty(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate exact final test accounting without allowing hidden green paths."""

    if payload.get("schema_version") != TEST_HONESTY_SCHEMA_VERSION:
        raise EvidencePackageError("unsupported test honesty schema")
    numeric_fields = (
        "collected",
        "passed",
        "failed",
        "errors",
        "xfail",
        "xpass",
        "rerun_count",
    )
    counts: dict[str, int] = {}
    for field in numeric_fields:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidencePackageError(f"test honesty {field} must be non-negative")
        counts[field] = value
    skipped = payload.get("skipped")
    if not isinstance(skipped, list):
        raise EvidencePackageError("test honesty skipped must be an array")
    node_ids: list[str] = []
    for entry in skipped:
        if not isinstance(entry, dict):
            raise EvidencePackageError("each skipped test must be an object")
        node_id = str(entry.get("node_id") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        if not node_id or not reason:
            raise EvidencePackageError("every skipped test requires node_id and reason")
        node_ids.append(node_id)
    if len(node_ids) != len(set(node_ids)):
        raise EvidencePackageError("test honesty contains duplicate skipped node IDs")
    timeout_overrides = payload.get("timeout_overrides")
    first_failure_fixes = payload.get("first_failure_fixes")
    if not isinstance(timeout_overrides, list):
        raise EvidencePackageError("timeout_overrides must be an array")
    if not isinstance(first_failure_fixes, list):
        raise EvidencePackageError("first_failure_fixes must be an array")
    for entry in first_failure_fixes:
        if not isinstance(entry, dict):
            raise EvidencePackageError("first_failure_fixes entries must be objects")
        _require_git_sha(
            entry.get("first_failure_commit"),
            field="first_failure_commit",
        )
        _require_git_sha(entry.get("fix_commit"), field="fix_commit")
        if not str(entry.get("node_id") or "").strip():
            raise EvidencePackageError("first_failure_fixes requires node_id")
    duration = payload.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        raise EvidencePackageError("duration_seconds must be non-negative")
    accounted = counts["passed"] + len(skipped) + counts["xfail"] + counts["xpass"]
    if accounted != counts["collected"]:
        raise EvidencePackageError(
            "test honesty collected count does not equal passed/skipped/xfail/xpass"
        )
    if counts["failed"] or counts["errors"]:
        raise EvidencePackageError("final test evidence contains failures or errors")
    if counts["rerun_count"]:
        raise EvidencePackageError("final test evidence used retries or reruns")
    return {
        "collected": counts["collected"],
        "passed": counts["passed"],
        "skipped": len(skipped),
        "skipped_node_ids": sorted(node_ids),
        "xfail": counts["xfail"],
        "xpass": counts["xpass"],
        "rerun_count": counts["rerun_count"],
        "timeout_override_count": len(timeout_overrides),
        "duration_seconds": duration,
        "first_failure_fix_count": len(first_failure_fixes),
    }


def _validate_receipt(
    name: str,
    payload: Mapping[str, object],
    *,
    source_commit: str,
    source_tree: str,
    artifact_hashes: set[str],
) -> None:
    if not str(payload.get("schema_version") or ""):
        raise EvidencePackageError(f"{name} schema_version is missing")
    if payload.get("source_commit") != source_commit:
        raise EvidencePackageError(f"{name} source_commit mismatch")
    if payload.get("source_tree") != source_tree:
        raise EvidencePackageError(f"{name} source_tree mismatch")
    if payload.get("artifact_sha256") not in artifact_hashes:
        raise EvidencePackageError(f"{name} artifact_sha256 mismatch")
    for field in ("started_at", "finished_at"):
        value = str(payload.get(field) or "")
        if not value:
            raise EvidencePackageError(f"{name} is missing {field}")
    command = payload.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise EvidencePackageError(f"{name} command must be a non-empty string array")
    if payload.get("exit_code") != 0 or payload.get("result") != "passed":
        raise EvidencePackageError(f"{name} is not a passing receipt")
    boundary = payload.get("environment_boundary")
    if not isinstance(boundary, dict):
        raise EvidencePackageError(f"{name} environment_boundary is missing")
    if boundary.get("hermes_home_kind") != "isolated":
        raise EvidencePackageError(f"{name} did not use an isolated Hermes home")
    if boundary.get("active_instance_touched") is not False:
        raise EvidencePackageError(f"{name} touched or failed to exclude the active instance")
    if not str(boundary.get("database_kind") or ""):
        raise EvidencePackageError(f"{name} database_kind is missing")


def _source_identity(evidence_dir: Path, expected_sha: str) -> tuple[str, str]:
    identity = _load_object(evidence_dir / "SOURCE_IDENTITY.json")
    source_commit = _require_git_sha(identity.get("source_commit"), field="source_commit")
    source_tree = _require_git_sha(identity.get("source_tree"), field="source_tree")
    if source_commit != expected_sha:
        raise EvidencePackageError("evidence source commit differs from expected SHA")
    if identity.get("source_dirty") is not False:
        raise EvidencePackageError("evidence source identity is dirty")
    return source_commit, source_tree


def build_evidence_index(evidence_dir: Path, *, expected_sha: str) -> dict[str, object]:
    requested = Path(evidence_dir)
    if requested.is_symlink():
        raise EvidencePackageError("evidence directory must not be a symlink")
    root = requested.resolve(strict=True)
    if root.name != expected_sha:
        raise EvidencePackageError("evidence directory must be a real full-SHA directory")
    missing = [name for name in REQUIRED_INPUT_FILES if not (root / name).is_file()]
    if missing:
        raise EvidencePackageError(f"evidence package is incomplete: {', '.join(missing)}")
    source_commit, source_tree = _source_identity(root, expected_sha)
    provenance = _load_object(root / "BUILD_PROVENANCE.json")
    candidate = _load_object(root / "CANDIDATE_MANIFEST.json")
    if provenance.get("source_commit") != source_commit:
        raise EvidencePackageError("build provenance source_commit mismatch")
    if provenance.get("source_tree") != source_tree:
        raise EvidencePackageError("build provenance source_tree mismatch")
    candidate_source = candidate.get("source")
    if not isinstance(candidate_source, dict):
        raise EvidencePackageError("candidate manifest source is missing")
    if candidate_source.get("commit") != source_commit:
        raise EvidencePackageError("candidate manifest source_commit mismatch")
    if candidate_source.get("tree") != source_tree:
        raise EvidencePackageError("candidate manifest source_tree mismatch")
    candidate_provenance = candidate.get("provenance")
    if not isinstance(candidate_provenance, dict):
        raise EvidencePackageError("candidate manifest provenance link is missing")
    provenance_hash = _sha256_file(root / "BUILD_PROVENANCE.json")
    if candidate_provenance.get("sha256") != provenance_hash:
        raise EvidencePackageError("candidate manifest provenance hash mismatch")
    artifact_hashes: set[str] = set()
    for kind in ("wheel", "sdist"):
        artifact = provenance.get(kind)
        if isinstance(artifact, dict):
            artifact_hashes.add(
                _require_sha(artifact.get("sha256"), field=f"{kind}.sha256")
            )
    if len(artifact_hashes) != 2:
        raise EvidencePackageError("build provenance must bind distinct wheel and sdist hashes")
    honesty = validate_test_honesty(_load_object(root / "PYTEST_SKIP_REPORT.json"))
    for name in RECEIPT_FILES:
        _validate_receipt(
            name,
            _load_object(root / name),
            source_commit=source_commit,
            source_tree=source_tree,
            artifact_hashes=artifact_hashes,
        )

    files: list[dict[str, object]] = []
    evidence_paths = sorted(
        path
        for path in root.rglob("*")
        if path.name != "EVIDENCE_INDEX.json" and not path.is_dir()
    )
    for path in evidence_paths:
        name = path.relative_to(root).as_posix()
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or name != pure.as_posix():
            raise EvidencePackageError(f"unsafe evidence path: {name}")
        if path.is_symlink():
            raise EvidencePackageError(f"evidence file must not be a symlink: {name}")
        entry: dict[str, object] = {
            "path": name,
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        if path.suffix == ".json":
            payload = _load_json(path)
            if isinstance(payload, dict):
                entry["json_root"] = "object"
                entry["schema_version"] = str(
                    payload.get("schema_version") or "unversioned"
                )
            elif isinstance(payload, list) and all(
                isinstance(item, dict) for item in payload
            ):
                entry["json_root"] = "array"
                entry["item_count"] = len(payload)
                entry["schema_version"] = "unversioned"
            else:
                raise EvidencePackageError(
                    "JSON evidence root must be an object or an array of objects: "
                    f"{path.name}"
                )
        files.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_commit,
        "source_tree": source_tree,
        "build_provenance_sha256": provenance_hash,
        "candidate_manifest_sha256": _sha256_file(root / "CANDIDATE_MANIFEST.json"),
        "artifact_sha256": sorted(artifact_hashes),
        "test_honesty": honesty,
        "file_count": len(files),
        "files": files,
        "environment_boundary": {
            "active_instance_touched": False,
            "evidence_paths": "relative-only",
            "raw_logs_committed": False,
        },
    }


def write_evidence_index(evidence_dir: Path, payload: Mapping[str, object]) -> Path:
    output = evidence_dir.resolve(strict=True) / "EVIDENCE_INDEX.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return output


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    expected_sha = _require_git_sha(args.expected_sha, field="expected_sha")
    payload = build_evidence_index(args.evidence_dir, expected_sha=expected_sha)
    output = write_evidence_index(args.evidence_dir, payload)
    print(
        json.dumps(
            {
                "ok": True,
                "source_commit": payload["source_commit"],
                "file_count": payload["file_count"],
                "index_sha256": _sha256_file(output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvidencePackageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
