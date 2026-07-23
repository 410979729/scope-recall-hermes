"""Explicit, failure-atomic construction of shadow vector generations.

Planning is read-only.  Applying always writes to a new physical generation,
validates it, records a durable receipt, and leaves it READY.  Activation is a
separate CAS pointer change; the previous generation remains available for
rollback.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .capture_filters import sanitize_report_text
from .graph import load_metadata
from .lifecycle_policy import ordinary_recall_lifecycle_visible
from .vector_generation import (
    GenerationCompatibilityError,
    GenerationIdentity,
    activate_generation,
    current_generation_id,
    ensure_vector_generation_schema,
    finish_migration_receipt,
    generation_manifest,
    now_iso,
    register_generation,
    start_migration_receipt,
)
from .vector_reconciliation import mark_generation_snapshot_reconciled
from .vector_store import build_vector_store
from .vector_generation_preflight import (
    PREFLIGHT_RECEIPT_FILENAME,
    physical_records_sha256,
    validate_generation_physical_store,
    write_generation_preflight_receipt,
)


_GENERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_generation_root(storage_dir: Path, generation_id: str) -> tuple[Path, str]:
    generation_id = str(generation_id or "").strip()
    if not _GENERATION_ID_RE.fullmatch(generation_id):
        raise ValueError("generation_id must contain only letters, digits, dot, underscore, or hyphen")
    relative = Path("vector-generations") / generation_id
    root = Path(storage_dir).resolve()
    target = (root / relative).resolve()
    if root not in target.parents:
        raise ValueError("generation path escapes storage root")
    return target, relative.as_posix()


def _current_id_read_only(conn: sqlite3.Connection) -> str:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generation_state'"
    ).fetchone()
    if table is None:
        return ""
    row = conn.execute(
        "SELECT value FROM vector_generation_state WHERE key = 'current_generation'"
    ).fetchone()
    return str(row[0] or "") if row else ""


def _indexable_rows(conn: sqlite3.Connection, *, index_general: bool) -> Iterable[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT id, scope_id, source, target, content, summary, updated_at, metadata
        FROM memories
        ORDER BY id ASC
        """
    )
    for row in rows:
        if not index_general and str(row["target"] or "") == "general":
            continue
        metadata = load_metadata(row["metadata"] or "{}")
        if not ordinary_recall_lifecycle_visible(
            lifecycle=str(metadata.get("lifecycle") or ""),
            target=str(row["target"] or ""),
        ):
            continue
        yield row


def _count_indexable_rows(conn: sqlite3.Connection, *, index_general: bool) -> int:
    return sum(1 for _ in _indexable_rows(conn, index_general=index_general))


def _vector_text(row: Any) -> str:
    summary = str(row["summary"] or "").strip()
    content = str(row["content"] or "").strip()
    return summary if summary and summary != content else content


def _update_source_hash(digest: Any, row: Any) -> None:
    payload = {
        "id": str(row["id"] or ""),
        "scope_id": str(row["scope_id"] or ""),
        "source": str(row["source"] or ""),
        "target": str(row["target"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "content_sha256": hashlib.sha256(str(row["content"] or "").encode("utf-8")).hexdigest(),
        "summary_sha256": hashlib.sha256(str(row["summary"] or "").encode("utf-8")).hexdigest(),
        "metadata_sha256": hashlib.sha256(str(row["metadata"] or "").encode("utf-8")).hexdigest(),
    }
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")


def plan_vector_generation(
    storage_dir: Path,
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    identity: GenerationIdentity,
    index_general: bool,
) -> dict[str, Any]:
    """Return a zero-write migration plan.

    This function deliberately does not call schema initializers, create a
    directory, or register a receipt.
    """

    target, relative = _safe_generation_root(storage_dir, generation_id)
    rows_planned = _count_indexable_rows(conn, index_general=index_general)
    return {
        "ok": True,
        "dry_run": True,
        "generation_id": generation_id,
        "from_generation_id": _current_id_read_only(conn),
        "storage_path": relative,
        "target_exists": target.exists(),
        "rows_planned": rows_planned,
        "identity": identity.canonical(),
        "identity_hash": identity.fingerprint,
        "will_activate": False,
        "writes": [],
    }


def _source_hash(rows: Iterable[sqlite3.Row]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        _update_source_hash(digest, row)
    return digest.hexdigest()


def _manifest_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("metadata")
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _assert_generation_source_is_current(conn: sqlite3.Connection, manifest: dict[str, Any]) -> None:
    metadata = _manifest_metadata(manifest)
    rows = list(_indexable_rows(conn, index_general=bool(metadata.get("index_general", False))))
    expected_count = int(manifest.get("row_count") or 0)
    expected_hash = str(manifest.get("source_hash") or "")
    actual_hash = _source_hash(rows)
    legacy_rollback = (
        str(metadata.get("provenance") or "") == "legacy-config-inference"
        and bool(str(manifest.get("activated_at") or ""))
    )
    if legacy_rollback and not expected_hash:
        return
    if len(rows) != expected_count or not expected_hash or actual_hash != expected_hash:
        raise GenerationCompatibilityError(
            "generation source snapshot is stale: "
            f"expected_count={expected_count}, current_count={len(rows)}, "
            f"expected_hash={expected_hash[:12]!r}, current_hash={actual_hash[:12]!r}"
        )


def _batches(rows: Iterable[sqlite3.Row], size: int) -> Iterable[list[sqlite3.Row]]:
    batch: list[sqlite3.Row] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _validate_shadow_records(
    records: Mapping[str, Mapping[str, Any]],
    source_rows: Iterable[Mapping[str, Any] | sqlite3.Row],
    identity: GenerationIdentity,
) -> tuple[list[float], str]:
    """Validate every persisted row before a shadow generation can become READY."""

    expected = {str(row["id"]): row for row in source_rows}
    actual_ids = {str(memory_id) for memory_id in records}
    expected_ids = set(expected)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)[:5]
        extra = sorted(actual_ids - expected_ids)[:5]
        raise RuntimeError(f"shadow generation id set mismatch: missing={missing}, extra={extra}")

    search_vector: list[float] = []
    search_scope_id = ""
    contract_fields = ("scope_id", "source", "target", "content", "summary", "updated_at")
    for memory_id in sorted(expected_ids):
        source = expected[memory_id]
        record = records[memory_id]
        for field in contract_fields:
            wanted = str(source[field] or "")
            actual = str(record.get(field) or "")
            if actual != wanted:
                raise RuntimeError(
                    f"shadow generation {field} mismatch for {memory_id}: "
                    f"expected_hash={hashlib.sha256(wanted.encode('utf-8')).hexdigest()[:12]}, "
                    f"actual_hash={hashlib.sha256(actual.encode('utf-8')).hexdigest()[:12]}"
                )
        try:
            vector = [float(value) for value in record.get("vector") or []]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"shadow generation vector is not numeric for {memory_id}") from exc
        if len(vector) != identity.dimensions:
            raise RuntimeError(
                f"shadow generation vector dimensions mismatch for {memory_id}: "
                f"expected={identity.dimensions}, actual={len(vector)}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError(f"shadow generation vector contains non-finite values for {memory_id}")
        if not any(value != 0.0 for value in vector):
            raise RuntimeError(f"shadow generation vector is a zero vector for {memory_id}")
        if not search_vector:
            search_vector = vector
            search_scope_id = str(record.get("scope_id") or "")
    return search_vector, search_scope_id


def _seal_store_for_ready(store: Any) -> None:
    """Close a physical store, requiring checkpoint evidence for SQLite."""

    backend = str(getattr(store, "backend", "") or "").strip().lower()
    if backend != "sqlite-bruteforce":
        store.close()
        return

    seal = getattr(store, "seal", None)
    if not callable(seal):
        raise RuntimeError("sqlite-bruteforce store does not support the required READY seal")
    checkpoint = seal()
    if not isinstance(checkpoint, Mapping):
        raise RuntimeError("sqlite-bruteforce READY seal returned invalid checkpoint evidence")
    try:
        busy = int(checkpoint["busy"])
        log = int(checkpoint["log"])
        checkpointed = int(checkpoint["checkpointed"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("sqlite-bruteforce READY seal returned invalid checkpoint evidence") from exc
    if busy != 0 or log != 0 or checkpointed != 0:
        raise RuntimeError(
            "sqlite-bruteforce READY seal checkpoint was incomplete: "
            f"busy={busy}, log={log}, checkpointed={checkpointed}"
        )


def _mark_progress(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    receipt_id: str,
    rows_built: int,
) -> None:
    at = now_iso()
    conn.execute(
        "UPDATE vector_generations SET row_count = ?, unique_id_count = ?, updated_at = ? WHERE generation_id = ? AND status = 'building'",
        (rows_built, rows_built, at, generation_id),
    )
    conn.execute(
        "UPDATE vector_migration_receipts SET rows_built = ?, unique_id_count = ? WHERE receipt_id = ? AND status = 'building'",
        (rows_built, rows_built, receipt_id),
    )
    conn.commit()


def _activate_existing_ready(
    storage_dir: Path,
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    expected_current: str,
) -> dict[str, Any]:
    manifest = generation_manifest(conn, generation_id)
    if manifest is None or str(manifest.get("status") or "") != "ready":
        raise GenerationCompatibilityError(f"generation {generation_id} is not ready for activation")
    activated = activate_generation(
        conn,
        generation_id,
        expected_current=expected_current,
        storage_dir=storage_dir,
    )
    mark_generation_snapshot_reconciled(conn, generation_id=generation_id)
    conn.execute(
        """
        UPDATE vector_migration_receipts
        SET status = 'activated', finished_at = ?
        WHERE receipt_id = (
            SELECT receipt_id FROM vector_migration_receipts
            WHERE generation_id = ? AND status = 'ready'
            ORDER BY started_at DESC LIMIT 1
        )
        """,
        (now_iso(), generation_id),
    )
    conn.commit()
    return {
        "ok": True,
        "status": "activated",
        "generation_id": generation_id,
        "from_generation_id": expected_current,
        "current_generation_id": str(activated["generation_id"]),
        "rebuilt": False,
    }


def build_vector_generation(
    storage_dir: Path,
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    identity: GenerationIdentity,
    embedder: Any,
    index_general: bool,
    batch_size: int = 50,
    activate: bool = False,
    expected_current: str,
    activate_existing_ready: bool = False,
    fail_after_rows: int | None = None,
) -> dict[str, Any]:
    """Build and validate a new physical generation.

    ``fail_after_rows`` is an explicit fault-injection hook used by adversarial
    tests.  Production callers leave it unset.
    """

    ensure_vector_generation_schema(conn)
    actual_current = current_generation_id(conn)
    if actual_current != str(expected_current or ""):
        raise GenerationCompatibilityError(
            f"current generation CAS conflict before build: expected {expected_current!r}, actual {actual_current!r}"
        )
    if activate_existing_ready:
        if not activate:
            raise ValueError("activate_existing_ready requires activate=True")
        return _activate_existing_ready(
            storage_dir,
            conn,
            generation_id=generation_id,
            expected_current=expected_current,
        )

    target_root, relative_path = _safe_generation_root(storage_dir, generation_id)
    preflight_receipt_path = target_root / PREFLIGHT_RECEIPT_FILENAME
    if target_root.exists():
        raise GenerationCompatibilityError(f"target generation path already exists: {target_root}")
    if generation_manifest(conn, generation_id) is not None:
        raise GenerationCompatibilityError(f"generation manifest already exists: {generation_id}")
    source_rows = list(_indexable_rows(conn, index_general=index_general))
    rows_planned = len(source_rows)
    receipt_id = f"vector-migration-{generation_id}-{uuid.uuid4().hex[:12]}"
    register_generation(
        conn,
        generation_id=generation_id,
        identity=identity,
        storage_path=relative_path,
        status="building",
        row_count=0,
        unique_id_count=0,
        config_hash=identity.fingerprint,
        metadata={"batch_size": max(1, int(batch_size)), "index_general": bool(index_general)},
    )
    start_migration_receipt(
        conn,
        receipt_id=receipt_id,
        generation_id=generation_id,
        from_generation_id=actual_current,
        rows_planned=rows_planned,
        details={"storage_path": relative_path, "identity_hash": identity.fingerprint},
    )
    conn.commit()

    store = None
    rows_built = 0
    source_digest = hashlib.sha256()
    try:
        target_root.mkdir(parents=True, exist_ok=False)
        store = build_vector_store(
            identity.backend,
            storage_dir=target_root,
            table_name=identity.table_name,
            dimensions=identity.dimensions,
            metric=identity.metric,
            config={"backend": identity.backend, "table_name": identity.table_name},
        )
        if not store.is_available():
            raise RuntimeError(f"vector backend unavailable for shadow generation: {identity.backend}")
        store.open()
        for batch in _batches(
            source_rows,
            max(1, int(batch_size or 50)),
        ):
            texts = [_vector_text(row) for row in batch]
            vectors = embedder.embed_texts(texts)
            if len(vectors) != len(batch):
                raise RuntimeError(
                    f"embedding response count {len(vectors)} does not match batch size {len(batch)}"
                )
            payload: list[dict[str, Any]] = []
            for row, vector in zip(batch, vectors):
                values = [float(value) for value in vector]
                if len(values) != identity.dimensions:
                    raise RuntimeError(
                        f"embedding dimensions {len(values)} do not match generation dimensions {identity.dimensions}"
                    )
                if not all(math.isfinite(value) for value in values):
                    raise RuntimeError("embedding contains non-finite values")
                if not any(value != 0.0 for value in values):
                    raise RuntimeError("embedding contains a zero vector")
                _update_source_hash(source_digest, row)
                payload.append(
                    {
                        "id": str(row["id"]),
                        "scope_id": str(row["scope_id"] or ""),
                        "source": str(row["source"] or ""),
                        "target": str(row["target"] or ""),
                        "content": str(row["content"] or ""),
                        "summary": str(row["summary"] or ""),
                        "updated_at": str(row["updated_at"] or ""),
                        "vector": values,
                    }
                )
            store.upsert_records(payload)
            rows_built += len(payload)
            _mark_progress(
                conn,
                generation_id=generation_id,
                receipt_id=receipt_id,
                rows_built=rows_built,
            )
            if fail_after_rows is not None and rows_built >= int(fail_after_rows):
                raise RuntimeError(f"injected shadow-build failure after {rows_built} rows")

        audit = store.audit_counts()
        physical_rows = int(audit.get("physical_rows") or 0)
        unique_ids = int(audit.get("unique_ids") or 0)
        duplicate_rows = int(audit.get("duplicate_rows") or 0)
        if physical_rows != rows_planned or unique_ids != rows_planned or duplicate_rows:
            raise RuntimeError(
                "shadow generation audit mismatch: "
                f"planned={rows_planned}, physical={physical_rows}, unique={unique_ids}, duplicates={duplicate_rows}"
            )
        records = store.list_records()
        if len(records) != rows_planned:
            raise RuntimeError(f"shadow generation record map has {len(records)} rows, expected {rows_planned}")
        search_vector, search_scope_id = _validate_shadow_records(records, source_rows, identity)
        audit = {**audit, "physical_records_sha256": physical_records_sha256(records)}
        if search_vector:
            hits = store.search(search_vector, scope_id=search_scope_id, limit=1)
            if not hits:
                raise RuntimeError("shadow generation search smoke returned no result")

        # Seal the physical companion before publishing READY metadata. SQLite
        # must prove a complete TRUNCATE checkpoint and zero WAL/SHM sidecars;
        # every backend is closed before immutable existing-store preflight.
        _seal_store_for_ready(store)
        store = None

        sealed_manifest = register_generation(
            conn,
            generation_id=generation_id,
            identity=identity,
            storage_path=relative_path,
            status="building",
            row_count=physical_rows,
            unique_id_count=unique_ids,
            source_hash=source_digest.hexdigest(),
            config_hash=identity.fingerprint,
            metadata={
                "batch_size": max(1, int(batch_size)),
                "index_general": bool(index_general),
                "duplicate_rows": duplicate_rows,
                "search_smoke": "ok" if records else "skipped_empty",
            },
        )
        # Validate the sealed main database before creating a receipt. Then
        # validate again after the atomic receipt write so a sidecar appearing
        # in either TOCTOU window fails the build before READY is published.
        physical_preflight = validate_generation_physical_store(
            storage_dir,
            sealed_manifest,
            require_receipt=False,
        )
        preflight_receipt = write_generation_preflight_receipt(target_root, sealed_manifest, audit)
        ready_manifest = register_generation(
            conn,
            generation_id=generation_id,
            identity=identity,
            storage_path=relative_path,
            status="ready",
            row_count=physical_rows,
            unique_id_count=unique_ids,
            source_hash=source_digest.hexdigest(),
            config_hash=identity.fingerprint,
            metadata={
                "batch_size": max(1, int(batch_size)),
                "index_general": bool(index_general),
                "duplicate_rows": duplicate_rows,
                "search_smoke": "ok" if records else "skipped_empty",
            },
        )
        # READY is still uncommitted here. Re-open the final manifest through
        # the immutable path so a sidecar introduced while the status/receipt
        # was finalized converts the build to FAILED before publication.
        physical_preflight = validate_generation_physical_store(
            storage_dir,
            ready_manifest,
            require_receipt=True,
        )
        if activate:
            activate_generation(
                conn,
                generation_id,
                expected_current=actual_current,
                storage_dir=storage_dir,
            )
            mark_generation_snapshot_reconciled(conn, generation_id=generation_id)
            receipt_status = "activated"
        else:
            receipt_status = "ready"
        finish_migration_receipt(
            conn,
            receipt_id,
            status=receipt_status,
            rows_built=physical_rows,
            unique_id_count=unique_ids,
            details={
                "source_hash": source_digest.hexdigest(),
                "identity_hash": identity.fingerprint,
                "storage_path": relative_path,
                "search_smoke": "ok" if records else "skipped_empty",
                "preflight_receipt_sha256": str(preflight_receipt["receipt_sha256"]),
                "preflight_physical_rows": int(physical_preflight["physical_rows"]),
                "physical_records_sha256": str(physical_preflight["physical_records_sha256"]),
            },
        )
        conn.commit()
        return {
            "ok": True,
            "status": receipt_status,
            "generation_id": generation_id,
            "from_generation_id": actual_current,
            "current_generation_id": current_generation_id(conn),
            "rows_planned": rows_planned,
            "rows_built": physical_rows,
            "unique_id_count": unique_ids,
            "source_hash": source_digest.hexdigest(),
            "storage_path": relative_path,
            "receipt_id": receipt_id,
            "preflight_receipt_sha256": str(preflight_receipt["receipt_sha256"]),
            "physical_records_sha256": str(physical_preflight["physical_records_sha256"]),
            "rebuilt": True,
        }
    except Exception as exc:
        try:
            preflight_receipt_path.unlink(missing_ok=True)
        except OSError:
            # Preserve the build failure as the primary error. A retained
            # receipt cannot make a failed manifest activatable, and the
            # durable error below still records the failed state.
            pass
        safe_error = sanitize_report_text(str(exc)) or "shadow generation build failed"
        try:
            register_generation(
                conn,
                generation_id=generation_id,
                identity=identity,
                storage_path=relative_path,
                status="failed",
                row_count=rows_built,
                unique_id_count=rows_built,
                source_hash=source_digest.hexdigest(),
                config_hash=identity.fingerprint,
                error=safe_error,
                metadata={"failed_after_rows": rows_built},
            )
            finish_migration_receipt(
                conn,
                receipt_id,
                status="failed",
                rows_built=rows_built,
                unique_id_count=rows_built,
                error=safe_error,
                details={"storage_path": relative_path, "source_hash": source_digest.hexdigest()},
            )
            conn.commit()
        except Exception:
            conn.rollback()
        if isinstance(exc, GenerationCompatibilityError):
            raise GenerationCompatibilityError(safe_error) from exc
        raise RuntimeError(safe_error) from exc
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
