"""Full backup bundle creation and verification for disaster recovery.

Creates a complete self-contained backup bundle containing a consistent SQLite snapshot,
instance configuration (with credential bodies refused or redacted), retained attachments,
pending captures, remote outbox and retry buffers, budget ledgers, optional vectors,
and a cryptographic manifest covering all files.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .._version import __version__
from ..contracts import InstanceBinding
from ..core.schema import APPLICATION_ID, SCHEMA_VERSION
from ..core.secret_patterns import contains_secret_like_text, is_sensitive_mapping_key
from .backup_archive import ArchiveLayoutError, ArchiveMember, verify_archive_layout, write_verified_members
from .backup_primitives import BackupError, backup_sqlite, exclusive_store, safe_path, sha256

BUNDLE_FORMAT = "scope-recall-backup-bundle/1"
CONFIG_NAMES = (
    "runtime-config.json",
    "installation.json",
    "codex-installation.json",
    "shared-manifest.json",
    "attachment.json",
    "backup-config.json",
)


class BackupBundleError(BackupError):
    """Raised when bundle creation, structure or verification fails."""


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_config_dict(data: Any) -> tuple[Any, int]:
    """Recursively redact secrets from config structures while preserving reference names.

    Returns the sanitized copy and the count of redacted secrets.
    """
    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        redacted_count = 0
        for key, val in data.items():
            if is_sensitive_mapping_key(str(key)):
                if val is not None and val != "":
                    sanitized[key] = "[REDACTED_SECRET]"
                    redacted_count += 1
                else:
                    sanitized[key] = val
            elif isinstance(val, (dict, list)):
                child, count = _sanitize_config_dict(val)
                sanitized[key] = child
                redacted_count += count
            elif isinstance(val, str) and contains_secret_like_text(val):
                sanitized[key] = "[REDACTED_SECRET]"
                redacted_count += 1
            else:
                sanitized[key] = val
        return sanitized, redacted_count
    if isinstance(data, list):
        sanitized_list: list[Any] = []
        redacted_count = 0
        for item in data:
            if isinstance(item, (dict, list)):
                child, count = _sanitize_config_dict(item)
                sanitized_list.append(child)
                redacted_count += count
            elif isinstance(item, str) and contains_secret_like_text(item):
                sanitized_list.append("[REDACTED_SECRET]")
                redacted_count += 1
            else:
                sanitized_list.append(item)
        return sanitized_list, redacted_count
    return copy.deepcopy(data), 0


def _discover_included_entries(data_directory: Path) -> list[dict[str, Any]]:
    """Discover registered shared entries or local hosts for inclusion in manifest."""
    entries: list[dict[str, Any]] = []
    db_path = data_directory / "memory.sqlite3"
    if db_path.is_file():
        try:
            with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT entry_id, display_name, host, first_seen, last_seen FROM entries")
                for row in cursor.fetchall():
                    entries.append(
                        {
                            "entry_id": row[0],
                            "display_name": row[1],
                            "host": row[2],
                            "first_seen": row[3],
                            "last_seen": row[4],
                        }
                    )
        except (sqlite3.Error, OSError):
            pass

    shared_manifest = data_directory / "shared-manifest.json"
    if shared_manifest.is_file() and not entries:
        try:
            data = json.loads(shared_manifest.read_text(encoding="utf-8"))
            for entry_id, meta in data.get("entries", {}).items():
                entries.append(
                    {
                        "entry_id": entry_id,
                        "display_name": meta.get("display_name", entry_id),
                        "host": meta.get("host", "unknown"),
                        "first_seen": meta.get("first_seen", ""),
                        "last_seen": meta.get("last_seen", ""),
                    }
                )
        except (json.JSONDecodeError, OSError):
            pass

    return entries


def create_backup_bundle(
    *,
    binding: InstanceBinding,
    output: Path,
    with_vectors: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a complete backup bundle for an instance.

    Holds exclusive store locks to ensure consistency between SQLite, retained
    attachments, and background queues.
    """
    output_path = safe_path(output)
    if output_path.exists():
        raise BackupBundleError("refusing to overwrite existing bundle")
    data_directory = safe_path(binding.data_directory, must_exist=True)
    if output_path == data_directory or output_path in data_directory.parents or data_directory in output_path.parents:
        raise BackupBundleError("output bundle must be outside data directory")

    timestamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_machine = platform.node() or "unknown-host"

    def _busy(what: str) -> BackupBundleError:
        return BackupBundleError(f"store busy during bundle creation ({what})")

    with exclusive_store(data_directory, busy=_busy):
        entries = _discover_included_entries(data_directory)
        manifest_files: list[dict[str, Any]] = []

        with tempfile.TemporaryDirectory(prefix="sr-bundle-") as tmpdir_raw:
            tmpdir = Path(tmpdir_raw)
            stage_dir = tmpdir / "staged"
            stage_dir.mkdir(parents=True, exist_ok=True)

            db_source = data_directory / "memory.sqlite3"
            if not db_source.is_file():
                raise BackupBundleError("database file memory.sqlite3 not found in instance data directory")
            staged_db = stage_dir / "database.sqlite3"
            backup_sqlite(db_source, staged_db)
            db_sha256 = sha256(staged_db)
            manifest_files.append(
                {
                    "path": "database.sqlite3",
                    "sha256": db_sha256,
                    "size_bytes": staged_db.stat().st_size,
                }
            )

            configs_staged = stage_dir / "configs"
            configs_staged.mkdir(parents=True, exist_ok=True)
            redacted_total = 0
            seen_configs: set[str] = set()
            for name in CONFIG_NAMES:
                for candidate_dir in (data_directory, data_directory.parent):
                    src_conf = candidate_dir / name
                    if src_conf.is_file() and name not in seen_configs:
                        seen_configs.add(name)
                        try:
                            raw = json.loads(src_conf.read_text(encoding="utf-8"))
                            sanitized, count = _sanitize_config_dict(raw)
                            redacted_total += count
                            dst_conf = configs_staged / name
                            dst_conf.write_text(
                                json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                            )
                            manifest_files.append(
                                {
                                    "path": f"configs/{name}",
                                    "sha256": sha256(dst_conf),
                                    "size_bytes": dst_conf.stat().st_size,
                                }
                            )
                        except (json.JSONDecodeError, OSError) as exc:
                            raise BackupBundleError(f"failed reading config {name}: {exc}") from exc
                        break

            def _stage_tree(src_root: Path, target_prefix: str) -> None:
                if not src_root.is_dir():
                    return
                for item in sorted(src_root.rglob("*")):
                    if item.is_file() and not item.is_symlink():
                        rel = item.relative_to(src_root).as_posix()
                        bundle_rel = f"{target_prefix}/{rel}"
                        dst = stage_dir / bundle_rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dst)
                        manifest_files.append(
                            {
                                "path": bundle_rel,
                                "sha256": sha256(dst),
                                "size_bytes": dst.stat().st_size,
                            }
                        )

            _stage_tree(data_directory / "retained", "retained")
            _stage_tree(data_directory / "inbox", "inbox")
            _stage_tree(data_directory / "outbox", "outbox")
            _stage_tree(data_directory / "retry", "retry")
            _stage_tree(data_directory / "budget", "budget")

            if with_vectors:
                vectors_dir = data_directory / "vectors"
                if vectors_dir.is_dir():
                    _stage_tree(vectors_dir, "vectors")

            manifest_content: dict[str, Any] = {
                "bundle_format": BUNDLE_FORMAT,
                "product_version": __version__,
                "schema_version": SCHEMA_VERSION,
                "application_id": APPLICATION_ID,
                "created_at_utc": timestamp,
                "source_machine": source_machine,
                "with_vectors": with_vectors,
                "entries": entries,
                "secrets_redacted_count": redacted_total,
                "files": sorted(manifest_files, key=lambda f: f["path"]),
            }
            canonical_manifest_bytes = json.dumps(
                manifest_content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            manifest_content["manifest_sha256"] = hashlib.sha256(canonical_manifest_bytes).hexdigest()

            manifest_staged = stage_dir / "manifest.json"
            manifest_staged.write_text(
                json.dumps(manifest_content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

            tmp_archive = tmpdir / "bundle.zip"
            with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(manifest_staged, "manifest.json")
                for staged_file in sorted(stage_dir.rglob("*")):
                    if staged_file.is_file() and staged_file != manifest_staged:
                        rel = staged_file.relative_to(stage_dir).as_posix()
                        zf.write(staged_file, rel)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = output_path.with_name(f".{output_path.name}.tmp")
            shutil.move(str(tmp_archive), str(tmp_target))
            os.replace(tmp_target, output_path)

    return {
        "status": "created",
        "bundle_path": str(output_path),
        "created_at_utc": timestamp,
        "files_count": len(manifest_files) + 1,
        "with_vectors": with_vectors,
        "manifest_sha256": manifest_content["manifest_sha256"],
        "secrets_redacted": redacted_total,
    }


@dataclass(frozen=True)
class BackupInspection:
    """Checksummed archive layout. ``members`` is the only set a restore may write."""

    manifest: dict[str, Any]
    members: tuple[ArchiveMember, ...]
    files_verified: int
    quick_check: str
    schema_version: int
    application_id: int


def _read_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    if "manifest.json" not in archive.namelist():
        raise BackupBundleError("bundle is missing manifest.json")
    try:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupBundleError(f"manifest.json is unreadable or malformed: {exc}") from exc
    if type(manifest) is not dict:
        raise BackupBundleError("manifest.json is unreadable or malformed: not an object")
    if manifest.get("bundle_format") != BUNDLE_FORMAT:
        raise BackupBundleError(f"unsupported bundle format: {manifest.get('bundle_format')}")
    return manifest


def _verify_checksums(archive: zipfile.ZipFile, manifest: dict[str, Any]) -> int:
    checked = 0
    for entry in manifest["files"]:
        file_rel = entry["path"]
        try:
            info = archive.getinfo(file_rel)
        except KeyError as exc:
            raise BackupBundleError(f"bundle file missing: {file_rel}") from exc
        if info.file_size != entry["size_bytes"]:
            raise BackupBundleError(
                f"size mismatch for {file_rel}: expected {entry['size_bytes']}, found {info.file_size}"
            )
        digest = hashlib.sha256(archive.read(info)).hexdigest()
        if digest != entry["sha256"]:
            raise BackupBundleError(
                f"checksum mismatch for {file_rel}: expected {entry['sha256']}, found {digest}"
            )
        checked += 1
    return checked


def _quick_check_database(archive: zipfile.ZipFile) -> tuple[str, int, int]:
    try:
        info = archive.getinfo("database.sqlite3")
    except KeyError as exc:
        raise BackupBundleError("bundle is missing database.sqlite3") from exc
    data = archive.read(info)
    with tempfile.TemporaryDirectory(prefix="sr-verify-") as raw:
        target = Path(raw) / "database.sqlite3"
        target.write_bytes(data)
        with closing(sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)) as conn:
            quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            if quick.lower() != "ok":
                raise BackupBundleError(f"database quick_check failed: {quick}")
            user_ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
            app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
    return quick, user_ver, app_id


def inspect_backup_archive(archive: zipfile.ZipFile) -> BackupInspection:
    """Check layout, checksums and the database snapshot without writing a store.

    The returned members are the shared write list. Checksums run only after
    the member set and destination mapping have been accepted.
    """
    manifest = _read_manifest(archive)
    try:
        members = verify_archive_layout(archive, manifest)
    except ArchiveLayoutError as exc:
        raise BackupBundleError(str(exc)) from exc
    if not any(member.archive_name == "database.sqlite3" for member in members):
        raise BackupBundleError("bundle is missing database.sqlite3")
    files_verified = _verify_checksums(archive, manifest)
    quick, user_ver, app_id = _quick_check_database(archive)
    return BackupInspection(manifest, members, files_verified, quick, user_ver, app_id)


def verify_backup_bundle(bundle_path: Path, *, temp_restore_check: bool = False) -> dict[str, Any]:
    """Verify a backup bundle without altering a store.

    Member names, the exact manifest set, link entries and destination
    collisions are rejected before any checksum or optional unpack. The optional
    unpack writes only the verified members into a temporary directory.
    """
    safe_bundle = safe_path(bundle_path, must_exist=True)
    if not safe_bundle.is_file():
        raise BackupBundleError(f"bundle file does not exist: {safe_bundle}")
    if not zipfile.is_zipfile(safe_bundle):
        raise BackupBundleError("bundle is not a valid zip archive")

    with zipfile.ZipFile(safe_bundle, "r") as archive:
        inspection = inspect_backup_archive(archive)
        if temp_restore_check:
            with tempfile.TemporaryDirectory(prefix="sr-verify-") as raw:
                write_verified_members(archive, Path(raw), inspection.members)

    manifest = inspection.manifest
    return {
        "status": "verified",
        "bundle_path": str(safe_bundle),
        "bundle_format": manifest.get("bundle_format"),
        "created_at_utc": manifest.get("created_at_utc"),
        "files_verified": inspection.files_verified,
        "database_quick_check": inspection.quick_check,
        "schema_version": inspection.schema_version,
        "application_id": inspection.application_id,
        "with_vectors": manifest.get("with_vectors", False),
        "entries": manifest.get("entries", []),
    }


__all__ = [
    "BUNDLE_FORMAT",
    "BackupBundleError",
    "BackupInspection",
    "create_backup_bundle",
    "inspect_backup_archive",
    "verify_backup_bundle",
]
