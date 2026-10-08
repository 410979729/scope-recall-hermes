"""Host-owned migration activation boundary.

Loads installer identity, validates injective audience maps and seals completed
TEST archive handoffs. Never stops a service or changes a scheduled principal.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from scope_recall.adapters.clients.config import load_codex_config
from scope_recall.adapters.hermes.installation import (
    AUDIT_RETENTION_SCOPES,
    HermesIdentityError,
    InstallationManifest,
    build_archive_scope_id,
    build_installation_manifest,
    initialize_core,
    load_installation_manifest,
    manifest_payload,
    write_installation_manifest,
)
from scope_recall.contracts import InstanceBinding

from .backup import safe_path
from .legacy_catalog import build_legacy_catalog
from .migration_records import MigrationError

_ARCHIVE_REPORT_NAME = "p15-archive-migration-report.json"
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_STORAGE_DB_NAMES = ("memory.sqlite3",)


def load_installation_handoff(
    manifest_path: str | Path, host: str | None
) -> tuple[Any, Path, Path, dict[str, str], str]:
    """Load the host-owned identity and target directory without inventing one."""
    supplied = safe_path(manifest_path, error_type=MigrationError)
    choice = (host or "").strip().lower()
    if choice not in {"", "hermes", "codex"}:
        raise MigrationError("installation host must be hermes or codex")
    if choice in {"", "hermes"}:
        home = supplied
        if home.is_file() and home.name == "installation.json":
            home = home.parent.parent
        elif home.is_dir() and (home / "installation.json").is_file():
            home = home.parent
        if (home / "scope-recall" / "installation.json").is_file():
            manifest = load_installation_manifest(home)
            return (
                manifest.to_binding(),
                manifest.data_directory,
                home / "scope-recall" / "installation.json",
                dict(manifest.audience_scopes),
                "hermes",
            )
        if choice == "hermes":
            raise MigrationError("Hermes installation manifest is required")
    config_path = supplied
    if config_path.is_dir():
        config_path = config_path / "codex-installation.json"
    config = load_codex_config(config_path)
    return (
        config.to_binding(),
        config.data_directory,
        config.config_path,
        dict(config.audience_scopes),
        "codex",
    )


def resolve_scope_mapping(
    source_scopes: frozenset[str],
    target_scopes: frozenset[str],
    audience_scopes: Mapping[str, str],
    mapping: Mapping[str, str] | None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if mapping is None:
        return {}, [
            {
                "key": scope,
                "reason": "source_scope_mapping_required",
                "auto_promoted": False,
            }
            for scope in sorted(source_scopes)
        ]
    resolved: dict[str, str] = {}
    issues: list[dict[str, Any]] = []
    for source in sorted(source_scopes):
        value = mapping.get(source)
        if type(value) is not str or not value.strip():
            issues.append(
                {
                    "key": source,
                    "reason": "source_scope_not_mapped",
                    "auto_promoted": False,
                }
            )
            continue
        target = audience_scopes.get(value.strip(), value.strip())
        if target not in target_scopes:
            issues.append(
                {
                    "key": source,
                    "target": target,
                    "reason": "target_audience_scope_not_bound",
                    "auto_promoted": False,
                }
            )
            continue
        resolved[source] = target
    reverse: dict[str, list[str]] = defaultdict(list)
    for source, target in resolved.items():
        reverse[target].append(source)
    for target, sources in sorted(reverse.items()):
        if len(sources) > 1:
            issues.append(
                {
                    "key": target,
                    "sources": sorted(sources),
                    "reason": "source_scope_mapping_collision",
                    "auto_promoted": False,
                }
            )
    return resolved, issues


def existing_target_scopes(path: Path) -> frozenset[str] | None:
    db = path / "memory.sqlite3"
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        return frozenset(str(row[0]) for row in conn.execute("SELECT scope_id FROM instance_scopes"))
    finally:
        conn.close()


def require_hex64(value: object, field: str) -> str:
    if type(value) is not str or not _HEX64.fullmatch(value):
        raise MigrationError(f"{field} must be an exact 64-hex digest")
    return value


def require_test_absolute_target(raw: str) -> Path:
    target = Path(raw)
    if not target.is_absolute():
        raise MigrationError("archive-install-test target must be an absolute TEST path")
    resolved = safe_path(target, error_type=MigrationError)
    if not any(part.upper().startswith("TEST") for part in resolved.parts):
        raise MigrationError("archive-install-test target must be beneath a TEST-named path component")
    return resolved


def archive_report_path(target_path: Path, report_arg: str | None) -> Path:
    if report_arg:
        return safe_path(report_arg, error_type=MigrationError)
    return safe_path(target_path / "scope-recall" / _ARCHIVE_REPORT_NAME, error_type=MigrationError)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _storage_file_digests(data_dir: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for name in _STORAGE_DB_NAMES:
        path = data_dir / name
        if path.is_file():
            digests[name] = _file_sha256(path)
    return digests


def _memory_wal_path(db: Path) -> Path:
    return Path(str(db) + "-wal")


def _nonempty_wal_size(db: Path) -> int:
    wal = _memory_wal_path(db)
    if not wal.is_file():
        return 0
    return wal.stat().st_size


def _refuse_nonempty_wal(data_dir: Path) -> None:
    db = data_dir / "memory.sqlite3"
    size = _nonempty_wal_size(db)
    if size > 0:
        raise MigrationError("nonempty WAL prevents identical-run reuse")


def _checkpoint_existing_memory_db(data_dir: Path) -> None:
    db = data_dir / "memory.sqlite3"
    if not db.is_file():
        raise MigrationError("resulting database is missing")
    conn = sqlite3.connect(db.as_posix())
    try:
        mode_row = conn.execute("PRAGMA journal_mode").fetchone()
        if mode_row is None or type(mode_row[0]) is not str:
            raise MigrationError("journal_mode did not return a result")
        journal_mode = mode_row[0]
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or len(row) < 3:
            raise MigrationError("wal checkpoint did not return a result")
        busy, log, checkpointed = (int(row[0]), int(row[1]), int(row[2]))
        completed = (busy, log, checkpointed) == (0, 0, 0)
        non_wal = (busy, log, checkpointed) == (0, -1, -1) and journal_mode.lower() != "wal"
        if not completed and not non_wal:
            raise MigrationError(
                "wal checkpoint was not successful: "
                f"busy={busy}, log={log}, checkpointed={checkpointed}, "
                f"journal_mode={journal_mode}"
            )
    finally:
        conn.close()
    if _nonempty_wal_size(db) > 0:
        raise MigrationError("nonempty WAL remained after checkpoint")


def _archive_receipt_payload(
    *,
    source_sha256: str,
    catalog_sha256: str,
    manifest_sha256: str,
    batch_key: str,
    report_sha256: str,
    database_files: dict[str, str],
    target: Path,
) -> dict[str, Any]:
    return {
        "batch_key": batch_key,
        "catalog_sha256": catalog_sha256,
        "database_files": dict(database_files),
        "manifest_sha256": manifest_sha256,
        "report_sha256": report_sha256,
        "source_sha256": source_sha256,
        "target": str(target),
    }


def _write_exclusive_text(path: Path, text: str) -> None:
    if path.exists() or path.is_symlink():
        raise MigrationError("refusing to overwrite existing evidence")
    path.write_text(text, encoding="utf-8")


def accept_identical_archive_run(
    *,
    target_path: Path,
    report_path: Path,
    receipt_path: Path,
    catalog: dict[str, Any],
    batch_key: str,
) -> None:
    data_dir = target_path / "scope-recall"
    manifest_path = data_dir / "installation.json"
    db_path = data_dir / "memory.sqlite3"
    if not manifest_path.is_file() or not db_path.is_file():
        raise MigrationError("installed manifest or resulting database is missing")
    _refuse_nonempty_wal(data_dir)
    try:
        manifest = load_installation_manifest(target_path)
    except HermesIdentityError as exc:
        raise MigrationError(f"installed manifest is invalid or does not bind this run: {exc}") from exc
    if manifest.test_mode is not True:
        raise MigrationError("installed manifest is not a TEST archive installation")
    if (
        manifest.archive_snapshot_hash != catalog["source_sha256"]
        or manifest.archive_catalog_hash != catalog["catalog_sha256"]
    ):
        raise MigrationError("installed manifest does not bind the current source/catalog")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError("preexisting report or receipt is unreadable") from exc
    if report.get("completion_status") != "complete":
        raise MigrationError("preexisting report is not a complete identical run")
    expected = _archive_receipt_payload(
        source_sha256=catalog["source_sha256"],
        catalog_sha256=catalog["catalog_sha256"],
        manifest_sha256=_file_sha256(manifest_path),
        batch_key=batch_key,
        report_sha256=_file_sha256(report_path),
        database_files=_storage_file_digests(data_dir),
        target=target_path,
    )
    if receipt != expected:
        raise MigrationError("preexisting receipt does not bind this identical run")


def write_complete_archive_receipt(
    *,
    target_path: Path,
    report_path: Path,
    receipt_path: Path,
    catalog: dict[str, Any],
    batch_key: str,
) -> None:
    data_dir = target_path / "scope-recall"
    _checkpoint_existing_memory_db(data_dir)
    payload = _archive_receipt_payload(
        source_sha256=catalog["source_sha256"],
        catalog_sha256=catalog["catalog_sha256"],
        manifest_sha256=_file_sha256(data_dir / "installation.json"),
        batch_key=batch_key,
        report_sha256=_file_sha256(report_path),
        database_files=_storage_file_digests(data_dir),
        target=target_path,
    )
    _write_exclusive_text(
        receipt_path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def _verified_legacy_catalog(source_database: Path | str, source_hash: str, catalog_hash: str) -> dict[str, Any]:
    catalog = build_legacy_catalog(source_database)
    if catalog["source_sha256"] != source_hash:
        raise HermesIdentityError(
            f"source snapshot digest mismatch: expected {source_hash}, got {catalog['source_sha256']}"
        )
    if catalog["catalog_sha256"] != catalog_hash:
        raise HermesIdentityError(f"catalog digest mismatch: expected {catalog_hash}, got {catalog['catalog_sha256']}")
    if not catalog["is_supported"]:
        reasons = [item.get("reason", "unknown") for item in catalog.get("unsupported", [])]
        raise HermesIdentityError(f"legacy catalog reports unsupported semantics: {reasons}")
    return catalog


def install_hermes_archive_migration(
    hermes_home: Path | str,
    *,
    source_database: Path | str,
    agent_id: str = "p15-archive-agent",
    platform: str = "cli",
    user_id: str = "local",
    agent_workspace: str = "default",
    test_mode: bool = True,
    expected_source_hash: str | None = None,
    expected_catalog_hash: str | None = None,
    clock: Any | None = None,
) -> tuple[InstanceBinding, InstallationManifest, dict[str, Any]]:
    """Explicit opt-in trusted install for isolated archive migrations."""
    if test_mode is not True:
        raise HermesIdentityError("archive-only migration requires test_mode=True (literal True)")
    home = Path(hermes_home)
    if not home.is_absolute():
        raise HermesIdentityError("hermes_home must be absolute before resolve")
    home = home.expanduser().resolve()
    if not any(part.upper().startswith("TEST") for part in home.parts):
        raise HermesIdentityError("archive-only installation target must be beneath a TEST-named path component")
    for label, digest in (
        ("expected_source_hash", expected_source_hash),
        ("expected_catalog_hash", expected_catalog_hash),
    ):
        if type(digest) is not str or not _HEX64.fullmatch(digest):
            raise HermesIdentityError(f"{label} must be exact 64-hex string")

    catalog = _verified_legacy_catalog(source_database, expected_source_hash, expected_catalog_hash)
    sources = dict.fromkeys(catalog["content_scopes"] + catalog["shared_only_scopes"] + catalog["audit_only_scopes"])
    manifest = build_installation_manifest(
        home,
        agent_id=agent_id,
        platform=platform,
        user_id=user_id,
        agent_workspace=agent_workspace,
        test_mode=True,
        archive_source_scopes={source: build_archive_scope_id(source) for source in sources},
        archive_retention_scopes=AUDIT_RETENTION_SCOPES,
        archive_snapshot_hash=catalog["source_sha256"],
        archive_catalog_hash=catalog["catalog_sha256"],
    )

    target_data = home / "scope-recall"
    if target_data.exists() and any(target_data.iterdir()):
        try:
            existing = load_installation_manifest(home)
        except HermesIdentityError as exc:
            raise HermesIdentityError(f"archive target exists but manifest is invalid or unreadable: {exc}") from exc
        if manifest_payload(existing) != manifest_payload(manifest):
            raise HermesIdentityError(
                "existing manifest payload does not match intended payload; refusing unrelated target"
            )
        manifest = existing
    else:
        write_installation_manifest(manifest)
    binding, _core = initialize_core(manifest, clock)
    return binding, manifest, catalog
