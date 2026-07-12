"""Fail-closed physical preflight for durable vector generations.

The generation manifest lives in SQLite, while vectors live in a separately
addressable companion.  A READY manifest is not activation evidence by itself:
this module verifies the immutable build receipt and opens the existing store
without creating any directory, table, schema, or metadata.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .capture_filters import sanitize_report_text
from .vector_generation import (
    GenerationCompatibilityError,
    GenerationIdentity,
    canonical_json_hash,
    now_iso,
    resolve_generation_storage_root,
)
from .vector_store import build_vector_store, normalize_vector_backend


PREFLIGHT_RECEIPT_FILENAME = ".generation-preflight.json"
PREFLIGHT_RECEIPT_SCHEMA = "scope-recall.vector-generation-preflight.v2"


def _manifest_identity(manifest: Mapping[str, Any]) -> GenerationIdentity:
    identity = GenerationIdentity(
        backend=str(manifest.get("backend") or ""),
        provider=str(manifest.get("provider") or ""),
        model=str(manifest.get("model") or ""),
        dimensions=int(manifest.get("dimensions") or 0),
        metric=str(manifest.get("metric") or "cosine"),
        prompt_profile=str(manifest.get("prompt_profile") or "default-v1"),
        document_prefix=str(manifest.get("document_prefix") or ""),
        query_prefix=str(manifest.get("query_prefix") or ""),
        request_dimensions=bool(manifest.get("request_dimensions", False)),
        table_name=str(manifest.get("table_name") or "memories"),
        schema_version=int(manifest.get("schema_version") or 1),
    )
    expected_identity_hash = str(manifest.get("identity_hash") or "")
    if not expected_identity_hash or identity.fingerprint != expected_identity_hash:
        raise GenerationCompatibilityError("vector generation manifest identity hash is missing or inconsistent")
    config_hash = str(manifest.get("config_hash") or "")
    if config_hash and config_hash != identity.fingerprint:
        raise GenerationCompatibilityError("vector generation manifest config hash does not match identity")
    return identity


def physical_records_sha256(records: Mapping[str, Mapping[str, Any]]) -> str:
    """Hash the complete persisted record contract without exposing plaintext."""

    digest = hashlib.sha256()
    for memory_id in sorted(str(item) for item in records):
        record = records[memory_id]
        vector = [float(value) for value in (record.get("vector") or [])]
        vector_json = json.dumps(vector, separators=(",", ":"), allow_nan=False)
        payload = {
            "id": str(record.get("id") or memory_id),
            "scope_id": str(record.get("scope_id") or ""),
            "source": str(record.get("source") or ""),
            "target": str(record.get("target") or ""),
            "updated_at": str(record.get("updated_at") or ""),
            "content_sha256": hashlib.sha256(
                str(record.get("content") or "").encode("utf-8")
            ).hexdigest(),
            "summary_sha256": hashlib.sha256(
                str(record.get("summary") or "").encode("utf-8")
            ).hexdigest(),
            "vector_sha256": hashlib.sha256(vector_json.encode("utf-8")).hexdigest(),
        }
        digest.update(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _receipt_body(manifest: Mapping[str, Any], audit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": PREFLIGHT_RECEIPT_SCHEMA,
        "generation_id": str(manifest.get("generation_id") or ""),
        "storage_path": str(manifest.get("storage_path") or ""),
        "backend": normalize_vector_backend(manifest.get("backend") or ""),
        "table_name": str(manifest.get("table_name") or "memories"),
        "dimensions": int(manifest.get("dimensions") or 0),
        "identity_hash": str(manifest.get("identity_hash") or ""),
        "config_hash": str(manifest.get("config_hash") or ""),
        "source_hash": str(manifest.get("source_hash") or ""),
        "row_count": int(audit.get("physical_rows") or 0),
        "unique_id_count": int(audit.get("unique_ids") or 0),
        "duplicate_rows": int(audit.get("duplicate_rows") or 0),
        "physical_records_sha256": str(audit.get("physical_records_sha256") or ""),
        "validated_at": now_iso(),
    }


def write_generation_preflight_receipt(
    generation_root: Path,
    manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically bind a validated physical build to its SQLite manifest."""

    root = Path(generation_root)
    if not root.is_dir():
        raise GenerationCompatibilityError("vector generation root is missing before receipt write")
    body = _receipt_body(manifest, audit)
    body["receipt_sha256"] = canonical_json_hash(body)
    destination = root / PREFLIGHT_RECEIPT_FILENAME
    temporary = root / f"{PREFLIGHT_RECEIPT_FILENAME}.tmp-{os.getpid()}"
    temporary.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return body


def _load_generation_preflight_receipt(generation_root: Path) -> dict[str, Any]:
    path = Path(generation_root) / PREFLIGHT_RECEIPT_FILENAME
    if not path.is_file():
        raise GenerationCompatibilityError("vector generation physical preflight receipt is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        safe = sanitize_report_text(str(exc))[:300]
        raise GenerationCompatibilityError(
            f"vector generation physical preflight receipt is corrupt: {safe}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != PREFLIGHT_RECEIPT_SCHEMA:
        raise GenerationCompatibilityError("vector generation physical preflight receipt schema is invalid")
    claimed_hash = str(payload.get("receipt_sha256") or "")
    unsigned = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    if not claimed_hash or canonical_json_hash(unsigned) != claimed_hash:
        raise GenerationCompatibilityError("vector generation physical preflight receipt hash is invalid")
    return payload


def _validate_receipt_binding(
    receipt: Mapping[str, Any],
    manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> None:
    expected = _receipt_body(manifest, audit)
    expected.pop("validated_at", None)
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise GenerationCompatibilityError(
                f"vector generation physical preflight receipt mismatch: {key}"
            )
    manifest_rows = int(manifest.get("row_count") or 0)
    manifest_unique = int(manifest.get("unique_id_count") or 0)
    if int(receipt.get("row_count") or 0) != manifest_rows:
        raise GenerationCompatibilityError("vector generation physical row count does not match manifest")
    if int(receipt.get("unique_id_count") or 0) != manifest_unique:
        raise GenerationCompatibilityError("vector generation physical unique-id count does not match manifest")


def validate_generation_physical_store(
    storage_dir: Path,
    manifest: Mapping[str, Any],
    *,
    require_receipt: bool = True,
) -> dict[str, Any]:
    """Validate one existing generation without creating or repairing anything."""

    identity = _manifest_identity(manifest)
    generation_id = str(manifest.get("generation_id") or "")
    if not generation_id:
        raise GenerationCompatibilityError("vector generation id is missing")
    generation_root = resolve_generation_storage_root(Path(storage_dir), manifest.get("storage_path"))
    backend = normalize_vector_backend(identity.backend)
    if not generation_root.is_dir():
        raise GenerationCompatibilityError("vector generation physical storage root is missing")
    store = build_vector_store(
        backend,
        storage_dir=generation_root,
        table_name=identity.table_name,
        dimensions=identity.dimensions,
        metric=identity.metric,
        config={"backend": backend, "table_name": identity.table_name},
    )
    try:
        if not store.is_available():
            raise GenerationCompatibilityError(f"vector generation backend is unavailable: {backend}")
        store.open_existing()
        audit = store.audit_counts()
        physical_rows = int(audit.get("physical_rows") or 0)
        unique_ids = int(audit.get("unique_ids") or 0)
        duplicate_rows = int(audit.get("duplicate_rows") or 0)
        expected_rows = int(manifest.get("row_count") or 0)
        expected_unique = int(manifest.get("unique_id_count") or 0)
        if physical_rows != expected_rows or unique_ids != expected_unique or duplicate_rows:
            raise GenerationCompatibilityError(
                "vector generation physical row mismatch: "
                f"manifest_rows={expected_rows}, physical_rows={physical_rows}, "
                f"manifest_unique={expected_unique}, physical_unique={unique_ids}, duplicates={duplicate_rows}"
            )
        records = store.list_records()
        if len(records) != unique_ids:
            raise GenerationCompatibilityError(
                "vector generation physical record map count does not match unique-id count"
            )
        for record in records.values():
            vector = [float(value) for value in (record.get("vector") or [])]
            if len(vector) != identity.dimensions:
                raise GenerationCompatibilityError("vector generation physical vector dimension mismatch")
            if not all(math.isfinite(value) for value in vector):
                raise GenerationCompatibilityError("vector generation physical vector contains non-finite values")
            if not any(value != 0.0 for value in vector):
                raise GenerationCompatibilityError("vector generation physical vector is zero")
        record_hash = physical_records_sha256(records)
        audit = {**audit, "physical_records_sha256": record_hash}
        receipt: dict[str, Any] | None = None
        if require_receipt:
            receipt = _load_generation_preflight_receipt(generation_root)
            _validate_receipt_binding(receipt, manifest, audit)
        return {
            "ok": True,
            "generation_id": generation_id,
            "storage_path": str(manifest.get("storage_path") or ""),
            "backend": backend,
            "table_name": identity.table_name,
            "dimensions": identity.dimensions,
            "identity_hash": identity.fingerprint,
            "physical_rows": physical_rows,
            "unique_ids": unique_ids,
            "duplicate_rows": duplicate_rows,
            "physical_records_sha256": record_hash,
            "receipt_sha256": str((receipt or {}).get("receipt_sha256") or ""),
        }
    except GenerationCompatibilityError:
        raise
    except Exception as exc:
        safe = sanitize_report_text(str(exc))[:300] or type(exc).__name__
        raise GenerationCompatibilityError(
            f"vector generation physical storage validation failed: {safe}"
        ) from exc
    finally:
        try:
            store.close()
        except Exception:
            pass


def validate_generation_for_activation(
    storage_dir: Path,
    conn: sqlite3.Connection,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind physical evidence and the current truth cohort immediately before CAS."""

    raw_metadata = manifest.get("metadata")
    if isinstance(raw_metadata, Mapping):
        metadata = dict(raw_metadata)
    else:
        try:
            parsed_metadata = json.loads(str(raw_metadata or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_metadata = {}
        metadata = parsed_metadata if isinstance(parsed_metadata, dict) else {}
    legacy_rollback = (
        str(metadata.get("provenance") or "") == "legacy-config-inference"
        and bool(str(manifest.get("activated_at") or ""))
    )
    report = validate_generation_physical_store(
        storage_dir,
        manifest,
        require_receipt=not legacy_rollback,
    )
    # Local import avoids a module cycle while keeping one source-cohort hash
    # implementation for builds, activation, and doctor.
    from .vector_migration import _assert_generation_source_is_current

    _assert_generation_source_is_current(conn, dict(manifest))
    return report
