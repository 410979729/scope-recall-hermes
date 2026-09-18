"""Resumable migration job; the existing converter remains the only engine.

This module never stops a host, changes model settings, swaps live directories,
or guesses audience grants. The agent workflow owns those host operations.
All writes here target a new job directory and an installer-bound destination.
"""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from ..contracts import TrustedContext
from ..core.storage import SQLiteStorage
from ..core.file_lock import advisory_file_lock
from .backup import _atomic_json as _write, _safe_path, _sha256, backup_sqlite
from .migrate_v2 import (
    MigrationError,
    build_legacy_catalog,
    migrate_legacy,
    _load_installation_handoff,
)

FORMAT = "scope-recall.upgrade-job/1"
_PLAN_FIELDS = (
    "operation_id",
    "job_root",
    "source_database",
    "source_stamp",
    "snapshot_sha256",
    "catalog_sha256",
    "manifest",
    "manifest_sha256",
    "target_directory",
    "installation_id",
    "host",
    "scope_map",
)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source_stamp(path: Path) -> dict:
    # WAL is part of the source identity. A main-file-only hash misses committed
    # changes, so a changed/vanished WAL invalidates a prepared live snapshot.
    return {
        suffix: _sha256(p) if p.exists() and p.stat().st_size else None
        for suffix in ("", "-wal", "-journal")
        if (p := Path(str(path) + suffix))
    }


def _load(job: str | Path, *, verify_snapshot: bool = False) -> tuple[Path, dict]:
    root = _safe_path(job, must_exist=True)
    path = _safe_path(root / "job.json", must_exist=True)
    if path.stat().st_size > 1024 * 1024:
        raise MigrationError("job metadata too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        type(value) is not dict
        or value.get("format") != FORMAT
        or value.get("job_root") != str(root)
    ):
        raise MigrationError("job binding mismatch")
    if not set(_PLAN_FIELDS).issubset(value) or value.get("plan_sha256") != _digest(
        {k: value[k] for k in _PLAN_FIELDS}
    ):
        raise MigrationError("migration plan changed")
    if verify_snapshot and _sha256(root / "source.sqlite3") != value["snapshot_sha256"]:
        raise MigrationError("source snapshot changed")
    return root, value


def prepare_upgrade(
    source, job, *, installation_manifest, host=None, scope_map=None
) -> dict:
    source = _safe_path(source, must_exist=True)
    root = _safe_path(job)
    binding, target, manifest, audiences, resolved_host = _load_installation_handoff(
        installation_manifest, host
    )
    if (
        root.is_relative_to(source.parent)
        or root.is_relative_to(target)
        or target.is_relative_to(root)
        or source.is_relative_to(target)
    ):
        raise MigrationError("source, job and target must be separate")
    if root.exists():
        raise MigrationError("job already exists; use migrate status or run to resume")
    context = TrustedContext(
        binding, "upgrade-preflight", binding.scope_ids, "host_generated"
    )
    with SQLiteStorage(binding).read(context) as tx:
        if tx.status().sources:
            raise MigrationError("destination must be a new inactive installation")
    root.mkdir(parents=True)
    before = _source_stamp(source)
    backup = backup_sqlite(
        source, root / "source.sqlite3", manifest=root / "backup.json"
    )
    after = _source_stamp(source)
    catalog = build_legacy_catalog(root / "source.sqlite3")
    _write(root / "catalog.json", catalog)
    scopes = sorted(
        set(
            catalog["content_scopes"]
            + catalog["shared_only_scopes"]
            + catalog["audit_only_scopes"]
        )
    )
    mapping = dict(scope_map or {})
    # Exact existing IDs can be reused automatically; never fold an unmapped
    # private/group/user scope into owner_private just to pass migration.
    for scope in scopes:
        if scope in binding.scope_ids:
            mapping.setdefault(scope, scope)
    missing = [s for s in scopes if s not in mapping]
    unknown = [s for s in mapping if s not in scopes]
    resolved = {s: audiences.get(t, t) for s, t in mapping.items()}
    invalid = unknown or [s for s, t in resolved.items() if t not in binding.scope_ids]
    if len(set(resolved.values())) != len(resolved):
        invalid = ["non_injective_scope_map"]
    reasons = []
    if not catalog["is_supported"]:
        reasons.append("legacy_format_unsupported")
    if before != after:
        reasons.append("source_changed_during_backup")
    if missing or invalid:
        reasons.append("audience_binding_required")
    value = dict(
        format=FORMAT,
        operation_id=uuid.uuid4().hex,
        job_root=str(root),
        source_database=str(source),
        source_stamp=after,
        snapshot_sha256=backup["backup_sha256"],
        catalog_sha256=catalog["catalog_sha256"],
        manifest=str(manifest),
        manifest_sha256=_sha256(manifest),
        target_directory=str(target),
        installation_id=binding.installation_id,
        host=resolved_host,
        scope_map=resolved,
        state="blocked" if reasons else "prepared",
        blockers=reasons,
        missing_scope_ids=missing,
        invalid_scope_ids=invalid,
        created_at=datetime.now(timezone.utc).isoformat(),
        history_reextraction=False,
        host_validation="agent_required",
        vector_state="not_started",
        attempts=0,
    )
    value["plan_sha256"] = _digest({k: value[k] for k in _PLAN_FIELDS})
    _write(root / "job.json", value)
    return value


def _binding(root, job):
    manifest = _safe_path(job["manifest"], must_exist=True)
    if _sha256(manifest) != job["manifest_sha256"]:
        raise MigrationError("target audience manifest changed; prepare a new job")
    binding, target, _, _, _ = _load_installation_handoff(manifest, job["host"])
    if (
        str(target) != job["target_directory"]
        or binding.installation_id != job["installation_id"]
    ):
        raise MigrationError("target binding changed")
    return binding


def run_upgrade(job, *, source_quiesced=False, legacy_reader_contract=None) -> dict:
    root, value = _load(job)
    with advisory_file_lock(root / "operation.lock", timeout_seconds=0):
        root, value = _load(root, verify_snapshot=True)
        _binding(root, value)
        if value["state"] in {"converted", "verified"}:
            return _verify_upgrade(root)
        if value["blockers"]:
            return value
        if source_quiesced is not True:
            raise MigrationError("agent must quiesce the source host before migration")
        source = _safe_path(value["source_database"], must_exist=True)
        if _source_stamp(source) != value["source_stamp"]:
            raise MigrationError(
                "source changed since preparation; refresh the snapshot before cutover"
            )
        # Reserve the old SQLite writer while checking freshness and converting.
        # This complements (does not replace) the agent's host shutdown check.
        if value["attempts"] >= 3:
            raise MigrationError(
                "retry limit reached; diagnose the cause before preparing a new job"
            )
        with closing(sqlite3.connect(source, timeout=0)) as guard:
            guard.execute("BEGIN IMMEDIATE")
            if _source_stamp(source) != value["source_stamp"]:
                raise MigrationError(
                    "source changed while acquiring writer reservation"
                )
            value["state"] = "converting"
            value["attempts"] += 1
            report_path = root / f"conversion-{value['attempts']}.json"
            value["conversion_report"] = str(report_path)
            _write(root / "job.json", value)
            try:
                kwargs = {}
                if legacy_reader_contract is not None:
                    kwargs["legacy_memory_reader_contract"] = legacy_reader_contract
                from .legacy_v2_compat import (
                    build_completed_bridge_archive, build_import_ledger_archive,
                )

                with closing(
                    sqlite3.connect(
                        (root / "source.sqlite3").as_uri() + "?mode=ro&immutable=1",
                        uri=True,
                    )
                ) as conn:
                    conn.row_factory = sqlite3.Row
                    tables = {
                        r[0]
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "shared_bridge_outbox" in tables:
                        archive = build_completed_bridge_archive(conn)
                        _write(root / "completed-bridge.json", archive)
                        kwargs["completed_bridge_archive_path"] = (
                            root / "completed-bridge.json"
                        )
                    if "import_ledger" in tables:
                        _write(root / "import-ledger.json", build_import_ledger_archive(conn))
                        kwargs["import_ledger_archive_path"] = root / "import-ledger.json"
                report = migrate_legacy(
                    root / "source.sqlite3",
                    installation_manifest=value["manifest"],
                    host=value["host"],
                    source_scope_map=value["scope_map"],
                    batch_key=value["operation_id"],
                    report_path=report_path,
                    **kwargs,
                )
                value["state"] = (
                    "converted"
                    if report["completion_status"] == "complete"
                    else "blocked"
                )
                value["blockers"] = (
                    [] if value["state"] == "converted" else ["conversion_incomplete"]
                )
                value["report_sha256"] = _sha256(report_path)
                value["counts"] = report["counts"]
                _write(root / "job.json", value)
            except Exception as exc:
                value.update(state="retryable", last_error_type=type(exc).__name__)
                _write(root / "job.json", value)
                raise
            finally:
                guard.rollback()
        if value["state"] == "converted":
            return _verify_upgrade(root)
        return value


def verify_upgrade(job) -> dict:
    root, _ = _load(job)
    with advisory_file_lock(root / "operation.lock", timeout_seconds=0):
        return _verify_upgrade(root)


def _verify_upgrade(job) -> dict:
    root, value = _load(job, verify_snapshot=True)
    binding = _binding(root, value)
    if value["state"] not in {"converted", "verified"}:
        raise MigrationError("conversion is not complete")
    report = Path(value["conversion_report"])
    if _sha256(report) != value["report_sha256"]:
        raise MigrationError("conversion report changed")
    context = TrustedContext(
        binding, "upgrade:" + value["operation_id"], binding.scope_ids, "host_generated"
    )
    with SQLiteStorage(binding).read(context) as tx:
        status = tx.status()
    with closing(
        sqlite3.connect(
            (binding.data_directory / "memory.sqlite3").as_uri() + "?mode=ro", uri=True
        )
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        if (
            conn.execute("PRAGMA quick_check").fetchone()[0] != "ok"
            or conn.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise MigrationError("target integrity check failed")
        counts = {
            t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            for t in ("source_events", "claims", "claim_versions")
        }
    expected = value["counts"]
    if any(counts[key] != expected[key] for key in counts):
        raise MigrationError("target count reconciliation failed")
    value.update(
        state="verified",
        integrity="ok",
        target_counts=counts,
        target_epoch=status.memory_epoch,
        next_agent_action="restart_and_probe_target_host",
    )
    _write(root / "job.json", value)
    return value


def upgrade_status(job) -> dict:
    _, value = _load(job)
    return value


def queue_upgrade_index(job, *, limit=128) -> dict:
    from .migration_index import queue_index_page

    root, value = _load(job)
    with advisory_file_lock(root / "operation.lock", timeout_seconds=0):
        # Indexing reads target truth. Do not rehash a potentially multi-GB
        # legacy snapshot for every small page of an already verified job.
        root, value = _load(job)
        binding = _binding(root, value)
        if value["state"] != "verified":
            raise MigrationError("verify conversion before scheduling its index")
        if value.get("index_queue_complete"):
            return value
        value = queue_index_page(binding, value, limit=limit)
        _write(root / "job.json", value)
        return value
