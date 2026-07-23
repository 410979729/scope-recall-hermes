"""Backup-first repair of classified stale vector companion IDs.

This module never rebuilds embeddings and never changes generation pointers. It
uses SQLite truth only to classify IDs, inspects every local companion it can
find, and deletes terminal-hidden/orphan IDs only after all affected companions
have been backed up. Policy-excluded IDs are report-only unless explicitly
included by an operator.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .capture_filters import sanitize_report_text
from .doctor_vector import lancedb_table_names, lancedb_vector_ids, sqlite_truth_vector_categories
from .maintenance_lease import (
    assert_activation_write_allowed,
    install_activation_lease_authorizer,
)
from .truth_connection import connect_truth_database


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    return str(value)


def _sqlite_truth_hash_connection(conn: sqlite3.Connection) -> str:
    """Return a stable logical hash using the caller's SQLite snapshot."""

    digest = hashlib.sha256()
    rows = conn.execute("SELECT * FROM memories ORDER BY id ASC").fetchall()
    for row in rows:
        payload = {str(key): row[key] for key in row.keys()}
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=_json_safe,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def sqlite_truth_hash(db_path: Path) -> str:
    """Return a stable logical hash of every SQLite truth-row field."""

    db_path = Path(db_path)
    conn = connect_truth_database(db_path, mode="ro")
    try:
        return _sqlite_truth_hash_connection(conn)
    finally:
        conn.close()


def _safe_generation_root(storage_dir: Path, raw_path: Any) -> Path:
    root = Path(storage_dir).resolve()
    relative = Path(str(raw_path or "."))
    if relative.is_absolute():
        raise ValueError("generation storage_path must be relative")
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError("generation storage_path escapes Scope Recall storage root")
    return target


def _generation_manifests(truth_path: Path) -> list[dict[str, Any]]:
    conn = connect_truth_database(truth_path, mode="ro")
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generations'"
        ).fetchone()
        if exists is None:
            return []
        return [dict(row) for row in conn.execute("SELECT * FROM vector_generations ORDER BY generation_id")]
    finally:
        conn.close()


def _current_generation_id(truth_path: Path) -> str:
    """Read the selected generation pointer without creating or migrating schema."""

    conn = connect_truth_database(truth_path, mode="ro")
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generation_state'"
        ).fetchone()
        if exists is None:
            return ""
        row = conn.execute(
            "SELECT value FROM vector_generation_state WHERE key = 'current_generation'"
        ).fetchone()
        return str(row[0] or "") if row else ""
    finally:
        conn.close()


def _descriptor_key(backend: str, path: Path, table_name: str) -> tuple[str, str, str]:
    return backend, str(path.resolve()), str(table_name or "")


def _add_descriptor(
    descriptors: dict[tuple[str, str, str], dict[str, Any]],
    *,
    backend: str,
    path: Path,
    table_name: str = "",
    source: str,
    generation_id: str = "",
    active_generation: bool = False,
) -> None:
    if not path.exists():
        return
    key = _descriptor_key(backend, path, table_name)
    descriptor = descriptors.setdefault(
        key,
        {
            "backend": backend,
            "path": str(path.resolve()),
            "table_name": str(table_name or ""),
            "sources": [],
            "generation_ids": [],
            "active_generation": False,
        },
    )
    if source not in descriptor["sources"]:
        descriptor["sources"].append(source)
    if generation_id and generation_id not in descriptor["generation_ids"]:
        descriptor["generation_ids"].append(generation_id)
    if active_generation:
        descriptor["active_generation"] = True


def discover_local_vector_companions(hermes_home: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Discover root, registered-generation, and unregistered local stores."""

    storage_dir = Path(hermes_home).expanduser().resolve() / "scope-recall"
    truth_path = storage_dir / "memory.sqlite3"
    descriptors: dict[tuple[str, str, str], dict[str, Any]] = {}
    errors: list[str] = []

    _add_descriptor(
        descriptors,
        backend="sqlite-bruteforce",
        path=storage_dir / "vector.sqlite3",
        source="root",
    )
    _add_descriptor(
        descriptors,
        backend="lancedb",
        path=storage_dir / "lancedb",
        table_name="memories",
        source="root",
    )

    try:
        manifests = _generation_manifests(truth_path)
        current_generation_id = _current_generation_id(truth_path)
    except Exception as exc:
        manifests = []
        current_generation_id = ""
        errors.append(sanitize_report_text(f"generation manifest read failed: {exc}"))
    for manifest in manifests:
        generation_id = str(manifest.get("generation_id") or "")
        backend = str(manifest.get("backend") or "").strip().lower()
        if backend == "sqlite":
            backend = "sqlite-bruteforce"
        try:
            generation_root = _safe_generation_root(storage_dir, manifest.get("storage_path"))
        except Exception as exc:
            errors.append(sanitize_report_text(f"generation {generation_id} has invalid storage path: {exc}"))
            continue
        table_name = str(manifest.get("table_name") or "memories")
        if backend == "sqlite-bruteforce":
            _add_descriptor(
                descriptors,
                backend=backend,
                path=generation_root / "vector.sqlite3",
                source="manifest",
                generation_id=generation_id,
                active_generation=generation_id == current_generation_id,
            )
        elif backend == "lancedb":
            _add_descriptor(
                descriptors,
                backend=backend,
                path=generation_root / "lancedb",
                table_name=table_name,
                source="manifest",
                generation_id=generation_id,
                active_generation=generation_id == current_generation_id,
            )
        elif backend == "pgvector":
            errors.append(
                f"generation {generation_id} uses remote pgvector; local backup-first repair cannot operate it"
            )
        else:
            errors.append(f"generation {generation_id} has unsupported backend {backend!r}")

    generation_parent = storage_dir / "vector-generations"
    if generation_parent.is_dir():
        for generation_root in sorted(path for path in generation_parent.iterdir() if path.is_dir()):
            _add_descriptor(
                descriptors,
                backend="sqlite-bruteforce",
                path=generation_root / "vector.sqlite3",
                source="filesystem-scan",
                generation_id=generation_root.name,
                active_generation=generation_root.name == current_generation_id,
            )
            _add_descriptor(
                descriptors,
                backend="lancedb",
                path=generation_root / "lancedb",
                table_name="memories",
                source="filesystem-scan",
                generation_id=generation_root.name,
                active_generation=generation_root.name == current_generation_id,
            )

    return sorted(descriptors.values(), key=lambda item: (str(item["path"]), str(item["backend"]))), errors


def _inspect_sqlite_companion(path: Path) -> tuple[set[str], str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "vector_records" not in tables:
            raise RuntimeError("vector_records table is missing")
        ids = {
            str(row[0])
            for row in conn.execute("SELECT id FROM vector_records").fetchall()
            if str(row[0] or "")
        }
        table_row = conn.execute(
            "SELECT value FROM vector_meta WHERE key = 'table_name'"
        ).fetchone() if "vector_meta" in tables else None
        return ids, str(table_row[0] or "") if table_row else ""
    finally:
        conn.close()


def _inspect_lancedb_companion(path: Path, table_name: str) -> tuple[set[str], str]:
    import lancedb  # type: ignore

    db = lancedb.connect(str(path))
    names = lancedb_table_names(db)
    selected = str(table_name or "")
    if selected not in names:
        if len(names) == 1:
            selected = names[0]
        else:
            raise RuntimeError(f"LanceDB table {selected!r} is missing; available={names}")
    table = db.open_table(selected)
    return set(lancedb_vector_ids(table)), selected


def _inspect_companion(descriptor: dict[str, Any]) -> tuple[set[str], str]:
    path = Path(str(descriptor["path"]))
    backend = str(descriptor["backend"])
    if backend == "sqlite-bruteforce":
        return _inspect_sqlite_companion(path)
    if backend == "lancedb":
        return _inspect_lancedb_companion(path, str(descriptor.get("table_name") or "memories"))
    raise RuntimeError(f"unsupported local backend: {backend}")


def _classify_companion(
    descriptor: dict[str, Any],
    *,
    truth_categories: dict[str, set[str]],
    include_policy_excluded: bool,
) -> dict[str, Any]:
    payload = dict(descriptor)
    try:
        vector_ids, detected_table = _inspect_companion(descriptor)
        if detected_table:
            payload["table_name"] = detected_table
        terminal_hidden = sorted(vector_ids & truth_categories["terminal_hidden"])
        policy_excluded = sorted(vector_ids & truth_categories["policy_excluded"])
        orphan = sorted(vector_ids - truth_categories["all"])
        requested = set(truth_categories["terminal_hidden"]) | set(orphan)
        if include_policy_excluded:
            requested |= set(truth_categories["policy_excluded"])
        planned_delete = sorted(vector_ids & requested)
        missing = sorted(requested - vector_ids)
        payload.update(
            {
                "status": "ready",
                "row_count": len(vector_ids),
                "terminal_hidden_count": len(terminal_hidden),
                "terminal_hidden_samples": terminal_hidden[:20],
                "policy_excluded_count": len(policy_excluded),
                "policy_excluded_samples": policy_excluded[:20],
                "orphan_count": len(orphan),
                "orphan_samples": orphan[:20],
                "requested_count": len(requested),
                "missing_count": len(missing),
                "planned_delete_count": len(planned_delete),
                "planned_delete_ids": planned_delete,
            }
        )
    except Exception as exc:
        payload.update(
            {
                "status": "error",
                "error": sanitize_report_text(str(exc)),
                "row_count": 0,
                "terminal_hidden_count": 0,
                "policy_excluded_count": 0,
                "orphan_count": 0,
                "requested_count": 0,
                "missing_count": 0,
                "planned_delete_count": 0,
                "planned_delete_ids": [],
            }
        )
    return payload


def plan_hidden_vector_companion_repair(
    hermes_home: Path,
    *,
    include_policy_excluded: bool = False,
) -> dict[str, Any]:
    """Build a zero-write classified cleanup plan for every local companion."""

    hermes_home = Path(hermes_home).expanduser().resolve()
    storage_dir = hermes_home / "scope-recall"
    truth_path = storage_dir / "memory.sqlite3"
    if not truth_path.is_file():
        raise FileNotFoundError(f"SQLite truth database not found: {truth_path}")
    truth_sha256 = sqlite_truth_hash(truth_path)
    truth_categories = sqlite_truth_vector_categories(
        hermes_home,
        index_general=bool(include_policy_excluded),
    )
    # Classification of policy-excluded rows must remain visible even when an
    # operator elects to clean them, so derive the normal-policy set separately.
    normal_categories = sqlite_truth_vector_categories(hermes_home, index_general=False)
    truth_categories["policy_excluded"] = normal_categories["policy_excluded"]
    if include_policy_excluded:
        truth_categories["indexable"] -= truth_categories["policy_excluded"]

    descriptors, discovery_errors = discover_local_vector_companions(hermes_home)
    companions = [
        _classify_companion(
            descriptor,
            truth_categories=truth_categories,
            include_policy_excluded=include_policy_excluded,
        )
        for descriptor in descriptors
    ]
    inspection_errors = [
        f"{item['path']}: {item.get('error', 'inspection failed')}"
        for item in companions
        if item.get("status") != "ready"
    ]
    errors = [*discovery_errors, *inspection_errors]
    return {
        "ok": not errors,
        "dry_run": True,
        "include_policy_excluded": bool(include_policy_excluded),
        "truth_path": str(truth_path),
        "truth_sha256_before": truth_sha256,
        "truth_counts": {
            "all": len(truth_categories["all"]),
            "indexable": len(truth_categories["indexable"]),
            "terminal_hidden": len(truth_categories["terminal_hidden"]),
            "policy_excluded": len(truth_categories["policy_excluded"]),
        },
        "companion_count": len(companions),
        "planned_delete": sum(int(item.get("planned_delete_count") or 0) for item in companions),
        "companions": companions,
        "errors": [sanitize_report_text(error) for error in errors],
        "writes": [],
    }


def _backup_name(path: Path, backend: str) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
    suffix = ".sqlite3" if backend == "sqlite-bruteforce" else ""
    return f"{backend}.{path.name}.{digest}{suffix}"


def _backup_sqlite(source_path: Path, destination: Path) -> None:
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()


def _backup_companion(item: dict[str, Any], backup_root: Path) -> dict[str, Any]:
    path = Path(str(item["path"]))
    backend = str(item["backend"])
    destination = backup_root / _backup_name(path, backend)
    if backend == "sqlite-bruteforce":
        _backup_sqlite(path, destination)
    elif backend == "lancedb":
        shutil.copytree(path, destination)
    else:
        raise RuntimeError(f"unsupported backup backend: {backend}")
    return {
        "backend": backend,
        "source": str(path),
        "backup": str(destination),
    }


def _chunks(values: Iterable[str], size: int = 200) -> Iterable[list[str]]:
    batch: list[str] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _delete_sqlite_ids(path: Path, ids: list[str]) -> int:
    conn = sqlite3.connect(path, timeout=30.0)
    install_activation_lease_authorizer(conn, path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing: set[str] = set()
        for batch in _chunks(ids):
            placeholders = ",".join("?" for _ in batch)
            existing.update(
                str(row[0])
                for row in conn.execute(
                    f"SELECT id FROM vector_records WHERE id IN ({placeholders})",
                    batch,
                ).fetchall()
            )
            conn.execute(f"DELETE FROM vector_records WHERE id IN ({placeholders})", batch)
        conn.commit()
        return len(existing)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _lance_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _delete_lancedb_ids(path: Path, table_name: str, ids: list[str]) -> int:
    import lancedb  # type: ignore

    db = lancedb.connect(str(path))
    names = lancedb_table_names(db)
    selected = table_name if table_name in names else names[0] if len(names) == 1 else ""
    if not selected:
        raise RuntimeError(f"LanceDB table {table_name!r} is missing; available={names}")
    table = db.open_table(selected)
    before = set(lancedb_vector_ids(table))
    for batch in _chunks(ids):
        expression = "id IN (" + ",".join(_lance_quote(memory_id) for memory_id in batch) + ")"
        table.delete(expression)
    after = set(lancedb_vector_ids(table))
    remaining = set(ids) & after
    if remaining:
        raise RuntimeError(f"LanceDB delete verification failed for {len(remaining)} id(s)")
    return len((set(ids) & before) - after)


def _apply_companion(item: dict[str, Any]) -> dict[str, Any]:
    ids = [str(memory_id) for memory_id in item.get("planned_delete_ids") or []]
    requested = int(item.get("requested_count") or 0)
    missing = int(item.get("missing_count") or 0)
    result = {
        "backend": str(item["backend"]),
        "path": str(item["path"]),
        "table_name": str(item.get("table_name") or ""),
        "requested": requested,
        "deleted": 0,
        "missing": missing,
        "failed": 0,
        "status": "not_needed" if not ids else "pending",
    }
    if not ids:
        return result
    try:
        if item["backend"] == "sqlite-bruteforce":
            deleted = _delete_sqlite_ids(Path(str(item["path"])), ids)
        elif item["backend"] == "lancedb":
            deleted = _delete_lancedb_ids(
                Path(str(item["path"])),
                str(item.get("table_name") or "memories"),
                ids,
            )
        else:
            raise RuntimeError(f"unsupported apply backend: {item['backend']}")
        result.update({"deleted": deleted, "status": "ok"})
    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "failed": len(ids),
                "error": sanitize_report_text(str(exc)),
            }
        )
    return result


def repair_hidden_vector_companions(
    hermes_home: Path,
    *,
    include_policy_excluded: bool = False,
    apply: bool = False,
    quiescent_confirmed: bool = False,
) -> dict[str, Any]:
    """Plan or apply classified cleanup with mandatory backup and verification."""

    if not apply:
        return plan_hidden_vector_companion_repair(
            hermes_home,
            include_policy_excluded=include_policy_excluded,
        )
    if not quiescent_confirmed:
        raise ValueError("apply requires explicit quiescent confirmation")

    plan = plan_hidden_vector_companion_repair(
        hermes_home,
        include_policy_excluded=include_policy_excluded,
    )
    if not plan["ok"]:
        raise RuntimeError("repair plan contains unreadable or unsupported companions")
    active_affected = [
        item
        for item in plan["companions"]
        if bool(item.get("active_generation")) and int(item.get("planned_delete_count") or 0) > 0
    ]
    if active_affected:
        return {
            **plan,
            "ok": False,
            "status": "blocked_active_generation",
            "dry_run": False,
            "backup_root": "",
            "backups": [],
            "deleted": 0,
            "failed": 0,
            "writes": [],
            "receipt_path": "",
            "errors": [
                *list(plan.get("errors") or []),
                "active generation repair is blocked; build and validate a new shadow generation instead",
            ],
        }
    affected = [
        item for item in plan["companions"] if int(item.get("planned_delete_count") or 0) > 0
    ]
    storage_dir = Path(hermes_home).expanduser().resolve() / "scope-recall"
    backup_root = ""
    backups: list[dict[str, Any]] = []
    if affected:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root = storage_dir / "backups" / f"hidden-vector-repair.{stamp}.{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            backups = [_backup_companion(item, root) for item in affected]
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        backup_root = str(root)

    truth_path = Path(str(plan["truth_path"]))
    assert_activation_write_allowed(truth_path)
    truth_guard = connect_truth_database(truth_path, mode="rw", timeout=30.0)
    install_activation_lease_authorizer(truth_guard, truth_path)
    try:
        # A reserved transaction prevents truth writers from racing between the
        # post-backup drift check and external companion deletion. Apply already
        # requires explicit quiescence confirmation, so bounded write blocking
        # is preferable to deleting a row that became visible again.
        truth_guard.execute("BEGIN IMMEDIATE")
        truth_before_apply = _sqlite_truth_hash_connection(truth_guard)
        if truth_before_apply != plan["truth_sha256_before"]:
            result = {
                **plan,
                "ok": False,
                "status": "blocked_truth_drift",
                "dry_run": False,
                "truth_sha256_after": truth_before_apply,
                "backup_root": backup_root,
                "backups": backups,
                "deleted": 0,
                "failed": 0,
                "companions": [
                    {
                        "backend": str(item["backend"]),
                        "path": str(item["path"]),
                        "table_name": str(item.get("table_name") or ""),
                        "requested": int(item.get("requested_count") or 0),
                        "deleted": 0,
                        "missing": int(item.get("missing_count") or 0),
                        "failed": 0,
                        "status": "blocked_truth_drift",
                    }
                    for item in plan["companions"]
                ],
                "writes": ["backup", "receipt"] if affected else [],
                "errors": [
                    *list(plan.get("errors") or []),
                    "SQLite truth changed after backup; companion deletion was not started",
                ],
            }
            if backup_root:
                receipt_path = Path(backup_root) / "receipt.json"
                receipt_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                result["receipt_path"] = str(receipt_path)
            else:
                result["receipt_path"] = ""
            return result
        applied = [_apply_companion(item) for item in plan["companions"]]
        truth_after = _sqlite_truth_hash_connection(truth_guard)
    finally:
        truth_guard.rollback()
        truth_guard.close()
    failed = sum(int(item.get("failed") or 0) for item in applied)
    result = {
        **plan,
        "ok": failed == 0 and truth_after == plan["truth_sha256_before"],
        "dry_run": False,
        "truth_sha256_after": truth_after,
        "backup_root": backup_root,
        "backups": backups,
        "deleted": sum(int(item.get("deleted") or 0) for item in applied),
        "failed": failed,
        "companions": applied,
        "writes": ["backup", "vector_delete", "receipt"] if affected else [],
    }
    if truth_after != plan["truth_sha256_before"]:
        result["errors"] = [
            *list(result.get("errors") or []),
            "SQLite truth hash changed during companion repair",
        ]
    if backup_root:
        receipt_path = Path(backup_root) / "receipt.json"
        receipt_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result["receipt_path"] = str(receipt_path)
    else:
        result["receipt_path"] = ""
    return result
