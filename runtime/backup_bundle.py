"""Full backup bundle creation and verification for disaster recovery.

Creates a self-contained backup bundle containing a consistent SQLite snapshot,
instance configuration (with credential bodies refused or redacted), retained
attachments, kept captures, one auxiliary budget snapshot, optional vectors,
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

from jsonschema import Draft202012Validator
from scope_recall.contracts import ENTRY_ID, InstanceBinding

from .._version import __version__
from ..core.deletion_mirror import DELETION_MIRROR_FILENAME, configured_backup_dir, sync_deletion_mirror
from ..core.schema import APPLICATION_ID, SCHEMA_VERSION, UPGRADE_CHAIN
from ..core.secret_patterns import contains_secret_like_text, is_sensitive_mapping_key
from .backup_archive import ArchiveLayoutError, ArchiveMember, verify_archive_layout, write_verified_members
from .backup_external import configured_client_paths, stage_entry_resources, stage_remote_clients
from .backup_primitives import (
    BackupError,
    atomic_write,
    backup_deadline,
    backup_sqlite,
    check_backup_deadline,
    copy_stream,
    exclusive_store,
    safe_path,
    sha256,
    stream_digest,
)
from .backup_resources import real_files, stage_instance_resources

BUNDLE_FORMAT = "scope-recall-backup-bundle/1"
CONFIG_NAMES = (
    "runtime-config.json",
    "installation.json",
    "codex-installation.json",
    "attachment.json",
    "remote-server.json",
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


def _discover_included_entries(data_directory: Path, binding: InstanceBinding) -> list[dict[str, Any]]:
    """Discover registered shared entries or local hosts for inclusion in manifest."""
    entries: list[dict[str, Any]] = []
    if binding.installation_kind == "shared":
        manifest = safe_path(data_directory / "installation.json", must_exist=True)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("installation_id") != binding.installation_id or type(payload.get("entries")) is not list:
            raise BackupBundleError("shared installation identity or entries mismatch")
        for entry in payload["entries"]:
            if type(entry) is not dict or not ENTRY_ID.fullmatch(str(entry.get("entry_id", ""))):
                raise BackupBundleError("shared entry identifier is invalid")
        return payload["entries"]
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

    return entries


def _bundle_busy(what: str) -> BackupBundleError:
    return BackupBundleError(f"store busy during bundle creation ({what})")


def _bundle_locations(output: Path, binding: InstanceBinding) -> tuple[Path, Path]:
    """Reject an output inside the store, and a store path that is a link."""
    output_path = safe_path(output)
    if output_path.exists():
        raise BackupBundleError("refusing to overwrite existing bundle")
    data_directory = safe_path(binding.data_directory, must_exist=True)
    inside = output_path == data_directory or output_path in data_directory.parents
    if inside or data_directory in output_path.parents:
        raise BackupBundleError("output bundle must be outside data directory")
    return output_path, data_directory


def _ensure_backup_mirror(data_directory: Path, output_path: Path) -> Path:
    """Register a backup directory when the store has none, then refresh its mirror."""
    mirror_dir = configured_backup_dir(data_directory)
    if mirror_dir is None:
        mirror_dir = output_path.parent
        atomic_write(data_directory / "backup-config.json", json.dumps({"backup_dir": str(mirror_dir)}) + "\n")
    sync_deletion_mirror(data_directory, mirror_dir)
    return mirror_dir


def _stage_database(data_directory: Path, stage_dir: Path, manifest_files: list[dict[str, Any]]) -> None:
    db_source = data_directory / "memory.sqlite3"
    if not db_source.is_file():
        raise BackupBundleError("database file memory.sqlite3 not found in instance data directory")
    staged_db = stage_dir / "database.sqlite3"
    backup_sqlite(db_source, staged_db)
    manifest_files.append(
        {"path": "database.sqlite3", "sha256": sha256(staged_db), "size_bytes": staged_db.stat().st_size}
    )


def _stage_one_config(
    src_conf: Path,
    stage_dir: Path,
    relative: str,
    manifest_files: list[dict[str, Any]],
) -> int:
    name = src_conf.name
    try:
        raw = json.loads(safe_path(src_conf, must_exist=True).read_text(encoding="utf-8"))
        sanitized, count = _sanitize_config_dict(raw)
    except (json.JSONDecodeError, OSError) as exc:
        raise BackupBundleError(f"failed reading config {name}: {exc}") from exc
    dst_conf = stage_dir / relative
    dst_conf.parent.mkdir(parents=True, exist_ok=True)
    dst_conf.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_files.append({"path": relative, "sha256": sha256(dst_conf), "size_bytes": dst_conf.stat().st_size})
    return count


def _stage_named_configs(
    data_directory: Path,
    stage_dir: Path,
    manifest_files: list[dict[str, Any]],
) -> int:
    """Copy the store's configs and its local client manifest in the host home."""
    redacted = 0
    for name in CONFIG_NAMES:
        candidate_dir = data_directory.parent if name == "codex-installation.json" else data_directory
        src_conf = candidate_dir / name
        if src_conf.is_file():
            redacted += _stage_one_config(src_conf, stage_dir, f"configs/{name}", manifest_files)
    return redacted


def _stage_tree(stage_dir: Path, src_root: Path, target_prefix: str, manifest_files: list[dict[str, Any]]) -> None:
    """Copy regular files without entering linked directories."""
    if not src_root.is_dir():
        return
    for item in real_files(safe_path(src_root, must_exist=True)):
        if item.is_file():
            rel = item.relative_to(src_root).as_posix()
            bundle_rel = f"{target_prefix}/{rel}"
            dst = stage_dir / bundle_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with item.open("rb") as source, dst.open("wb") as target:
                copy_stream(source, target)
            manifest_files.append({"path": bundle_rel, "sha256": sha256(dst), "size_bytes": dst.stat().st_size})


def _manifest_document(
    *,
    timestamp: str,
    binding: InstanceBinding,
    with_vectors: bool,
    entries: list[dict[str, Any]],
    redacted: int,
    manifest_files: list[dict[str, Any]],
    resources: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest_content: dict[str, Any] = {
        "bundle_format": BUNDLE_FORMAT,
        "product_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "application_id": APPLICATION_ID,
        "created_at_utc": timestamp,
        "source_machine": platform.node() or "unknown-host",
        "installation_id": binding.installation_id,
        "installation_kind": binding.installation_kind,
        "deletion_mirror": DELETION_MIRROR_FILENAME,
        "with_vectors": with_vectors,
        "entries": [
            {
                key: entry[key]
                for key in ("entry_id", "display_name", "host", "first_seen", "last_seen", "detached_at")
                if key in entry
            }
            for entry in entries
        ],
        "resources": resources,
        "secrets_redacted_count": redacted,
        "files": sorted(manifest_files, key=lambda item: item["path"]),
    }
    canonical = json.dumps(manifest_content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_content["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    return manifest_content


def _write_archive(stage_dir: Path, output_path: Path, manifest_content: dict[str, Any]) -> None:
    manifest_staged = stage_dir / "manifest.json"
    manifest_staged.write_text(json.dumps(manifest_content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_archive = stage_dir.parent / "bundle.zip"
    with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(manifest_staged, "manifest.json")
        for staged_file in sorted(stage_dir.rglob("*")):
            if staged_file.is_file() and staged_file != manifest_staged:
                with (
                    staged_file.open("rb") as source,
                    archive.open(staged_file.relative_to(stage_dir).as_posix(), "w", force_zip64=True) as target,
                ):
                    copy_stream(source, target)
    check_backup_deadline()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = output_path.with_name(f".{output_path.name}.tmp")
    shutil.move(str(tmp_archive), str(tmp_target))
    os.replace(tmp_target, output_path)


def _stage_resources(data_directory: Path, stage_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Stage kept captures and the budget snapshot. Link paths become bundle errors."""
    try:
        return stage_instance_resources(data_directory, stage_dir)
    except BackupError as exc:
        raise BackupBundleError(str(exc)) from exc


def _assemble_bundle(
    data_directory: Path,
    output_path: Path,
    binding: InstanceBinding,
    with_vectors: bool,
    timestamp: str,
    client_configs: tuple[Path, ...],
) -> dict[str, Any]:
    """Stage the store and write the zip. The caller holds the store lock."""
    entries = _discover_included_entries(data_directory, binding)
    manifest_files: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="sr-bundle-") as tmpdir_raw:
        stage_dir = Path(tmpdir_raw) / "staged"
        stage_dir.mkdir(parents=True, exist_ok=True)
        _stage_database(data_directory, stage_dir, manifest_files)
        redacted = _stage_named_configs(data_directory, stage_dir, manifest_files)
        extra_files, resources = _stage_resources(data_directory, stage_dir)
        manifest_files.extend(extra_files)
        redacted += stage_entry_resources(entries, stage_dir, manifest_files, resources, _stage_one_config)
        redacted += stage_remote_clients(
            client_configs, stage_dir, manifest_files, resources, _stage_one_config, _stage_tree
        )
        _stage_tree(stage_dir, data_directory / "retained", "retained", manifest_files)
        if with_vectors:
            _stage_tree(stage_dir, data_directory / "vectors", "vectors", manifest_files)
        manifest_content = _manifest_document(
            timestamp=timestamp,
            binding=binding,
            with_vectors=with_vectors,
            entries=entries,
            redacted=redacted,
            manifest_files=manifest_files,
            resources=resources,
        )
        _write_archive(stage_dir, output_path, manifest_content)
    return manifest_content


def _created_receipt(
    output_path: Path,
    timestamp: str,
    manifest_content: dict[str, Any],
    with_vectors: bool,
    mirror_dir: Path,
) -> dict[str, Any]:
    return {
        "status": "created",
        "bundle_path": str(output_path),
        "created_at_utc": timestamp,
        "files_count": len(manifest_content["files"]) + 1,
        "with_vectors": with_vectors,
        "manifest_sha256": manifest_content["manifest_sha256"],
        "secrets_redacted": manifest_content["secrets_redacted_count"],
        "deletion_mirror": str(mirror_dir / DELETION_MIRROR_FILENAME),
    }


def create_backup_bundle(
    *,
    binding: InstanceBinding,
    output: Path,
    with_vectors: bool = False,
    now: datetime | None = None,
    client_configs: tuple[Path, ...] = (),
    deadline: float | None = None,
) -> dict[str, Any]:
    """Create a backup bundle for an instance.

    Holds the store lock while the database, kept captures, retained files and
    the auxiliary budget ledger are snapshotted. The budget ledger is read with
    the SQLite backup API.
    """
    try:
        with backup_deadline(deadline):
            output_path, data_directory = _bundle_locations(output, binding)
            timestamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with exclusive_store(data_directory, busy=_bundle_busy):
                mirror_dir = _ensure_backup_mirror(data_directory, output_path)
                client_configs = client_configs or configured_client_paths(data_directory)
                manifest_content = _assemble_bundle(
                    data_directory, output_path, binding, with_vectors, timestamp, client_configs
                )
    except BackupError as exc:
        raise BackupBundleError(str(exc)) from exc
    return _created_receipt(output_path, timestamp, manifest_content, with_vectors, mirror_dir)


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
        with archive.open(info) as stream:
            digest = stream_digest(stream)
        if digest != entry["sha256"]:
            raise BackupBundleError(f"checksum mismatch for {file_rel}: expected {entry['sha256']}, found {digest}")
        checked += 1
    return checked


def _verify_manifest_document(manifest: dict[str, Any]) -> None:
    """The manifest has one published shape and a digest of its canonical document."""
    schema_path = Path(__file__).parent.parent / "contracts" / "backup_manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    error = next(Draft202012Validator(schema).iter_errors(manifest), None)
    if error is not None:
        raise BackupBundleError(f"invalid backup manifest: {error.message}")
    document = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != manifest["manifest_sha256"]:
        raise BackupBundleError("manifest checksum mismatch")


def _check_database_identity(conn: sqlite3.Connection, manifest: dict[str, Any], version: int, app_id: int) -> None:
    """An intact SQLite file must also be the supported installation the manifest names."""
    if app_id != APPLICATION_ID or manifest["application_id"] != app_id:
        raise BackupBundleError("database application identity mismatch")
    if version not in (*UPGRADE_CHAIN, SCHEMA_VERSION) or manifest["schema_version"] != version:
        raise BackupBundleError("database schema is unsupported or differs from manifest")
    row = conn.execute(
        "SELECT installation_id,installation_kind,schema_version FROM instance_meta WHERE singleton=1"
    ).fetchone()
    if row != (manifest["installation_id"], manifest["installation_kind"], version):
        raise BackupBundleError("database installation identity differs from manifest")


def _quick_check_database(archive: zipfile.ZipFile, manifest: dict[str, Any]) -> tuple[str, int, int]:
    try:
        info = archive.getinfo("database.sqlite3")
    except KeyError as exc:
        raise BackupBundleError("bundle is missing database.sqlite3") from exc
    with tempfile.TemporaryDirectory(prefix="sr-verify-") as raw:
        target = Path(raw) / "database.sqlite3"
        with archive.open(info) as source, target.open("wb") as output:
            copy_stream(source, output)
        with closing(sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)) as conn:
            quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            if quick.lower() != "ok":
                raise BackupBundleError(f"database quick_check failed: {quick}")
            user_ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
            app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
            _check_database_identity(conn, manifest, user_ver, app_id)
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
    _verify_manifest_document(manifest)
    files_verified = _verify_checksums(archive, manifest)
    quick, user_ver, app_id = _quick_check_database(archive, manifest)
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
        "installation_id": manifest.get("installation_id"),
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
