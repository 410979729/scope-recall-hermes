"""Durable vector generation manifests, activation state, receipts, and outbox.

SQLite remains the truth boundary for vector companion state.  A generation is
an immutable embedding space plus a separately addressable physical store.
Changing model, dimensions, metric, prompt profile, or schema therefore
requires a new generation; ordinary runtime setup never replaces the active
one.

The helpers intentionally do not commit.  Callers may compose manifest,
lifecycle, audit, and outbox writes in one SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from .capture_filters import sanitize_report_text, sanitize_structured_value


VECTOR_GENERATION_SCHEMA_VERSION = 1
CURRENT_GENERATION_KEY = "current_generation"
_VALID_GENERATION_STATUSES = {"building", "ready", "active", "failed", "retired"}
_VALID_OUTBOX_OPERATIONS = {"upsert", "delete"}


class GenerationCompatibilityError(RuntimeError):
    """A generation identity, state transition, or CAS precondition is unsafe."""


@dataclass(frozen=True)
class GenerationIdentity:
    """Fields that define one non-interchangeable vector space."""

    backend: str
    provider: str
    model: str
    dimensions: int
    metric: str = "cosine"
    prompt_profile: str = "default-v1"
    document_prefix: str = ""
    query_prefix: str = ""
    request_dimensions: bool = False
    table_name: str = "memories"
    schema_version: int = VECTOR_GENERATION_SCHEMA_VERSION

    def canonical(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["backend"] = str(payload["backend"] or "").strip().lower()
        payload["provider"] = str(payload["provider"] or "").strip().lower()
        payload["model"] = str(payload["model"] or "").strip()
        payload["metric"] = str(payload["metric"] or "cosine").strip().lower()
        payload["prompt_profile"] = str(payload["prompt_profile"] or "default-v1").strip()
        payload["document_prefix"] = str(payload["document_prefix"] or "")
        payload["query_prefix"] = str(payload["query_prefix"] or "")
        payload["request_dimensions"] = bool(payload["request_dimensions"])
        payload["table_name"] = str(payload["table_name"] or "memories").strip()
        payload["dimensions"] = int(payload["dimensions"] or 0)
        payload["schema_version"] = int(payload["schema_version"] or VECTOR_GENERATION_SCHEMA_VERSION)
        if not payload["backend"] or not payload["provider"] or not payload["model"]:
            raise ValueError("backend, provider, and model are required for a vector generation")
        if payload["dimensions"] <= 0:
            raise ValueError("vector generation dimensions must be positive")
        return payload

    @property
    def fingerprint(self) -> str:
        return canonical_json_hash(self.canonical())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_generation_storage_path(storage_path: Any) -> str:
    """Validate one durable generation path without touching the filesystem.

    Generation manifests are portable across Linux and Windows installations,
    so reject both POSIX and Windows absolute/drive paths plus traversal using
    either separator before the value can reach SQLite.
    """

    raw = str(storage_path or "").strip()
    if not raw:
        raise GenerationCompatibilityError("vector generation storage_path is required")
    if "\x00" in raw:
        raise GenerationCompatibilityError("vector generation storage_path contains a null byte")
    posix_path = PurePosixPath(raw.replace("\\", "/"))
    windows_path = PureWindowsPath(raw)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise GenerationCompatibilityError("vector generation storage_path must be relative")
    if ".." in posix_path.parts:
        raise GenerationCompatibilityError("vector generation storage_path contains parent traversal")
    return raw


def resolve_generation_storage_root(storage_dir: Path, storage_path: Any) -> Path:
    """Resolve a manifest storage path inside the Scope Recall storage root.

    Manifests are durable input. Every runtime, doctor, and repair caller must
    apply the same traversal guard before touching the physical companion.
    """

    root = Path(storage_dir).expanduser().resolve()
    relative = Path(validate_generation_storage_path(storage_path or "."))
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise GenerationCompatibilityError("vector generation storage_path escapes the Scope Recall storage root")
    return target


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sanitize_mapping_payload(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Recursively sanitize arbitrary mapping keys and values before persistence."""

    sanitized, _changed = sanitize_structured_value(dict(value or {}))
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_manifest_for_report(manifest: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a report-safe manifest, including rows written before hardening."""

    if manifest is None:
        return None
    result = dict(manifest)
    raw_storage_path = str(result.get("storage_path") or "")
    try:
        validate_generation_storage_path(raw_storage_path)
    except GenerationCompatibilityError:
        result["storage_path"] = "[INVALID_STORAGE_PATH]"
    else:
        result["storage_path"] = sanitize_report_text(raw_storage_path)
    raw_metadata = result.get("metadata")
    try:
        parsed_metadata = json.loads(str(raw_metadata or "{}"))
    except (TypeError, ValueError):
        parsed_metadata = {"value": str(raw_metadata or "")}
    sanitized_metadata, _changed = sanitize_structured_value(parsed_metadata)
    result["metadata"] = _json_dumps(sanitized_metadata)
    result["error"] = sanitize_report_text(result.get("error") or "")[:4000]
    return result


def _sanitize_outbox_payload(payload: Mapping[str, Any] | None) -> dict[str, str]:
    """Persist only the replay contract, never arbitrary memory or secret data."""

    raw = dict(payload or {})
    result: dict[str, str] = {}
    if "updated_at" in raw:
        result["updated_at"] = sanitize_report_text(str(raw.get("updated_at") or ""))[:200]
    if "reason" in raw:
        result["reason"] = sanitize_report_text(str(raw.get("reason") or ""))[:500]
    return result


def _row_dict(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()} if isinstance(row, sqlite3.Row) else dict(row)


def ensure_vector_generation_schema(conn: sqlite3.Connection) -> None:
    """Create generation companion tables inside the caller's transaction."""

    statements = (
        """
        CREATE TABLE IF NOT EXISTS vector_generations (
            generation_id TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            table_name TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            metric TEXT NOT NULL,
            prompt_profile TEXT NOT NULL,
            document_prefix TEXT NOT NULL DEFAULT '',
            query_prefix TEXT NOT NULL DEFAULT '',
            request_dimensions INTEGER NOT NULL DEFAULT 0,
            schema_version INTEGER NOT NULL,
            identity_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            activated_at TEXT NOT NULL DEFAULT '',
            row_count INTEGER NOT NULL DEFAULT 0,
            unique_id_count INTEGER NOT NULL DEFAULT 0,
            source_hash TEXT NOT NULL DEFAULT '',
            config_hash TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS vector_generation_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS vector_migration_receipts (
            receipt_id TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL,
            from_generation_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            dry_run INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            rows_planned INTEGER NOT NULL DEFAULT 0,
            rows_built INTEGER NOT NULL DEFAULT 0,
            unique_id_count INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS vector_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            generation_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            worker_id TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_vector_generation_status ON vector_generations(status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_vector_outbox_claim ON vector_outbox(generation_id, status, available_at, id)",
        "CREATE INDEX IF NOT EXISTS idx_vector_outbox_memory ON vector_outbox(memory_id, generation_id, id)",
    )
    for statement in statements:
        conn.execute(statement)
    outbox_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(vector_outbox)").fetchall()}
    if "completed_at" not in outbox_columns:
        conn.execute("ALTER TABLE vector_outbox ADD COLUMN completed_at TEXT NOT NULL DEFAULT ''")
    generation_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(vector_generations)").fetchall()}
    generation_migrations = {
        "document_prefix": "ALTER TABLE vector_generations ADD COLUMN document_prefix TEXT NOT NULL DEFAULT ''",
        "query_prefix": "ALTER TABLE vector_generations ADD COLUMN query_prefix TEXT NOT NULL DEFAULT ''",
        "request_dimensions": "ALTER TABLE vector_generations ADD COLUMN request_dimensions INTEGER NOT NULL DEFAULT 0",
    }
    for column, statement in generation_migrations.items():
        if column not in generation_columns:
            conn.execute(statement)


def generation_manifest(conn: sqlite3.Connection, generation_id: str) -> dict[str, Any] | None:
    ensure_vector_generation_schema(conn)
    row = conn.execute("SELECT * FROM vector_generations WHERE generation_id = ?", (generation_id,)).fetchone()
    return _row_dict(row)


def current_generation_id(conn: sqlite3.Connection) -> str:
    ensure_vector_generation_schema(conn)
    row = conn.execute("SELECT value FROM vector_generation_state WHERE key = ?", (CURRENT_GENERATION_KEY,)).fetchone()
    return str(row[0] or "") if row else ""


def current_generation(conn: sqlite3.Connection) -> dict[str, Any] | None:
    generation_id = current_generation_id(conn)
    return generation_manifest(conn, generation_id) if generation_id else None


def register_generation(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    identity: GenerationIdentity,
    storage_path: str,
    status: str = "building",
    row_count: int = 0,
    unique_id_count: int = 0,
    source_hash: str = "",
    config_hash: str = "",
    error: str = "",
    metadata: Mapping[str, Any] | None = None,
    timestamp: str = "",
) -> dict[str, Any]:
    """Register or idempotently refresh one generation manifest."""

    ensure_vector_generation_schema(conn)
    generation_id = str(generation_id or "").strip()
    storage_path = str(storage_path or "").strip()
    status = str(status or "building").strip().lower()
    if not generation_id or not storage_path:
        raise ValueError("generation_id and storage_path are required")
    storage_path = validate_generation_storage_path(storage_path)
    if status not in _VALID_GENERATION_STATUSES:
        raise ValueError(f"unsupported generation status: {status}")
    fields = identity.canonical()
    at = timestamp or now_iso()
    safe_error = sanitize_report_text(error)[:4000]
    existing = generation_manifest(conn, generation_id)
    if existing is not None:
        validate_generation_compatibility(existing, identity)
        if str(existing.get("storage_path") or "") != storage_path:
            raise GenerationCompatibilityError("vector generation storage_path changed; repair required")
        conn.execute(
            """
            UPDATE vector_generations
            SET status = ?, updated_at = ?, row_count = ?, unique_id_count = ?,
                source_hash = ?, config_hash = ?, error = ?, metadata = ?
            WHERE generation_id = ?
            """,
            (
                status,
                at,
                max(0, int(row_count)),
                max(0, int(unique_id_count)),
                str(source_hash or ""),
                str(config_hash or ""),
                safe_error,
                _json_dumps(_sanitize_mapping_payload(metadata)),
                generation_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO vector_generations(
                generation_id, backend, storage_path, table_name, provider, model,
                dimensions, metric, prompt_profile, document_prefix, query_prefix,
                request_dimensions, schema_version, identity_hash,
                status, created_at, updated_at, row_count, unique_id_count,
                source_hash, config_hash, error, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                fields["backend"],
                storage_path,
                fields["table_name"],
                fields["provider"],
                fields["model"],
                fields["dimensions"],
                fields["metric"],
                fields["prompt_profile"],
                fields["document_prefix"],
                fields["query_prefix"],
                1 if fields["request_dimensions"] else 0,
                fields["schema_version"],
                identity.fingerprint,
                status,
                at,
                at,
                max(0, int(row_count)),
                max(0, int(unique_id_count)),
                str(source_hash or ""),
                str(config_hash or ""),
                safe_error,
                _json_dumps(_sanitize_mapping_payload(metadata)),
            ),
        )
    manifest = generation_manifest(conn, generation_id)
    assert manifest is not None
    return manifest


def bootstrap_legacy_generation(
    conn: sqlite3.Connection,
    *,
    identity: GenerationIdentity,
    row_count: int = 0,
    unique_id_count: int | None = None,
    storage_path: str = ".",
    source_hash: str = "",
    config_hash: str = "",
    timestamp: str = "",
) -> dict[str, Any]:
    """Register an existing pre-generation store without moving or rewriting it."""

    at = timestamp or now_iso()
    generation_id = f"legacy-{identity.fingerprint[:16]}"
    manifest = register_generation(
        conn,
        generation_id=generation_id,
        identity=identity,
        storage_path=storage_path,
        status="active",
        row_count=row_count,
        unique_id_count=row_count if unique_id_count is None else unique_id_count,
        source_hash=source_hash,
        config_hash=config_hash,
        metadata={"provenance": "legacy-config-inference"},
        timestamp=at,
    )
    conn.execute(
        "INSERT OR IGNORE INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
        (CURRENT_GENERATION_KEY, generation_id, at),
    )
    return manifest


def validate_generation_compatibility(manifest: Mapping[str, Any], identity: GenerationIdentity) -> None:
    """Require an exact embedding-space identity match."""

    expected = identity.canonical()
    mismatches: list[str] = []
    for key in (
        "backend",
        "provider",
        "model",
        "dimensions",
        "metric",
        "prompt_profile",
        "document_prefix",
        "query_prefix",
        "request_dimensions",
        "table_name",
        "schema_version",
    ):
        actual: Any = manifest.get(key)
        if key in {"dimensions", "schema_version"}:
            actual = int(actual or 0)
        elif key == "request_dimensions":
            actual = bool(actual)
        elif key in {"document_prefix", "query_prefix"}:
            actual = str(actual or "")
        else:
            actual = str(actual or "").strip()
            if key in {"backend", "provider", "metric"}:
                actual = actual.lower()
        if actual != expected[key]:
            mismatches.append(f"{key}: current={actual!r}, requested={expected[key]!r}")
    if mismatches:
        raise GenerationCompatibilityError("vector generation identity mismatch: " + "; ".join(mismatches))


def activate_generation(
    conn: sqlite3.Connection,
    generation_id: str,
    *,
    expected_current: str,
    storage_dir: Path,
    timestamp: str = "",
) -> dict[str, Any]:
    """Validate physical/source evidence, then CAS-switch the current pointer."""

    ensure_vector_generation_schema(conn)
    target = generation_manifest(conn, generation_id)
    if target is None:
        raise GenerationCompatibilityError(f"generation not found: {generation_id}")
    if str(target.get("status") or "") not in {"ready", "active"}:
        raise GenerationCompatibilityError(
            f"generation {generation_id} is {target.get('status')!r}, expected ready or active"
        )
    actual = current_generation_id(conn)
    if actual != str(expected_current or ""):
        raise GenerationCompatibilityError(
            f"current generation CAS conflict: expected {expected_current!r}, actual {actual!r}"
        )
    if actual == generation_id:
        return target

    # Import lazily to keep durable SQLite state independent from optional
    # vector backends at module import time. The validator opens existing
    # storage read-only and binds the current source cohort before any write.
    from .vector_generation_preflight import validate_generation_for_activation

    validate_generation_for_activation(Path(storage_dir), conn, target)
    at = timestamp or now_iso()
    savepoint = "vector_generation_activation"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        if actual:
            cursor = conn.execute(
                "UPDATE vector_generation_state SET value = ?, updated_at = ? WHERE key = ? AND value = ?",
                (generation_id, at, CURRENT_GENERATION_KEY, actual),
            )
        else:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO vector_generation_state(key, value, updated_at) VALUES (?, ?, ?)",
                (CURRENT_GENERATION_KEY, generation_id, at),
            )
        if cursor.rowcount != 1:
            raise GenerationCompatibilityError("current generation changed during activation")
        if actual:
            old_cursor = conn.execute(
                """
                UPDATE vector_generations
                SET status = 'ready', updated_at = ?,
                    activated_at = CASE WHEN activated_at = '' THEN ? ELSE activated_at END
                WHERE generation_id = ? AND status = 'active'
                """,
                (at, at, actual),
            )
            if old_cursor.rowcount != 1:
                raise GenerationCompatibilityError("current generation manifest is not active")
        target_cursor = conn.execute(
            "UPDATE vector_generations SET status = 'active', activated_at = ?, updated_at = ?, error = '' "
            "WHERE generation_id = ? AND status = 'ready'",
            (at, at, generation_id),
        )
        if target_cursor.rowcount != 1:
            raise GenerationCompatibilityError("target generation manifest is not ready")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    activated = generation_manifest(conn, generation_id)
    assert activated is not None
    return activated


def enqueue_vector_event(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    generation_id: str,
    memory_id: str,
    operation: str,
    payload: Mapping[str, Any] | None = None,
    available_at: str = "",
    timestamp: str = "",
) -> dict[str, Any]:
    """Insert one idempotent replay event without committing the caller transaction."""

    ensure_vector_generation_schema(conn)
    operation = str(operation or "").strip().lower()
    if operation not in _VALID_OUTBOX_OPERATIONS:
        raise ValueError(f"unsupported vector outbox operation: {operation}")
    if not event_key or not generation_id or not memory_id:
        raise ValueError("event_key, generation_id, and memory_id are required")
    at = timestamp or now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO vector_outbox(
            event_key, generation_id, memory_id, operation, payload,
            status, attempts, available_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
        """,
        (
            str(event_key),
            str(generation_id),
            str(memory_id),
            operation,
            _json_dumps(_sanitize_outbox_payload(payload)),
            available_at or at,
            at,
            at,
        ),
    )
    row = conn.execute("SELECT * FROM vector_outbox WHERE event_key = ?", (event_key,)).fetchone()
    result = _row_dict(row)
    assert result is not None
    return result


def _lease_cutoff(timestamp: str, lease_seconds: int) -> str:
    normalized = str(timestamp or "").strip().replace("Z", "+00:00")
    try:
        current = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid vector outbox timestamp: {timestamp!r}") from exc
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current - timedelta(seconds=max(1, int(lease_seconds)))).isoformat()


def claim_vector_events(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    worker_id: str,
    limit: int = 100,
    lease_seconds: int = 300,
    timestamp: str = "",
) -> list[dict[str, Any]]:
    """Claim pending/retry events and reclaim expired processing leases."""

    ensure_vector_generation_schema(conn)
    at = timestamp or now_iso()
    lease_cutoff = _lease_cutoff(at, lease_seconds)
    rows = conn.execute(
        """
        SELECT id FROM vector_outbox
        WHERE generation_id = ? AND (
            (status IN ('pending', 'retry') AND julianday(available_at) <= julianday(?))
            OR (status = 'processing' AND julianday(updated_at) <= julianday(?))
        )
        ORDER BY id ASC LIMIT ?
        """,
        (generation_id, at, lease_cutoff, max(1, int(limit or 100))),
    ).fetchall()
    claimed: list[dict[str, Any]] = []
    for row in rows:
        event_id = int(row[0])
        cursor = conn.execute(
            """
            UPDATE vector_outbox
            SET status = 'processing', worker_id = ?, attempts = attempts + 1,
                updated_at = ?, last_error = ''
            WHERE id = ? AND (
                (status IN ('pending', 'retry') AND julianday(available_at) <= julianday(?))
                OR (status = 'processing' AND julianday(updated_at) <= julianday(?))
            )
            """,
            (worker_id, at, event_id, at, lease_cutoff),
        )
        if cursor.rowcount != 1:
            continue
        claimed_row = conn.execute("SELECT * FROM vector_outbox WHERE id = ?", (event_id,)).fetchone()
        result = _row_dict(claimed_row)
        if result is not None:
            claimed.append(result)
    return claimed


def complete_vector_event(conn: sqlite3.Connection, event_id: int, *, worker_id: str, timestamp: str = "") -> None:
    at = timestamp or now_iso()
    cursor = conn.execute(
        """
        UPDATE vector_outbox
        SET status = 'completed', updated_at = ?, completed_at = ?, worker_id = ?, last_error = ''
        WHERE id = ? AND status = 'processing' AND worker_id = ?
        """,
        (at, at, worker_id, int(event_id), worker_id),
    )
    if cursor.rowcount != 1:
        raise GenerationCompatibilityError(f"vector outbox completion CAS conflict for event {event_id}")


def fail_vector_event(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    worker_id: str,
    error: str,
    available_at: str = "",
    max_attempts: int = 8,
    timestamp: str = "",
) -> None:
    at = timestamp or now_iso()
    attempt_limit = max(1, int(max_attempts))
    safe_error = sanitize_report_text(error)[:2000]
    cursor = conn.execute(
        """
        UPDATE vector_outbox
        SET status = CASE WHEN attempts >= ? THEN 'dead_letter' ELSE 'retry' END,
            updated_at = ?, worker_id = '', last_error = ?, available_at = ?,
            completed_at = CASE WHEN attempts >= ? THEN ? ELSE '' END
        WHERE id = ? AND status = 'processing' AND worker_id = ?
        """,
        (
            attempt_limit,
            at,
            safe_error,
            available_at or at,
            attempt_limit,
            at,
            int(event_id),
            worker_id,
        ),
    )
    if cursor.rowcount != 1:
        raise GenerationCompatibilityError(f"vector outbox failure CAS conflict for event {event_id}")


def start_migration_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    generation_id: str,
    from_generation_id: str = "",
    rows_planned: int = 0,
    dry_run: bool = False,
    details: Mapping[str, Any] | None = None,
    timestamp: str = "",
) -> dict[str, Any]:
    ensure_vector_generation_schema(conn)
    at = timestamp or now_iso()
    conn.execute(
        """
        INSERT INTO vector_migration_receipts(
            receipt_id, generation_id, from_generation_id, status, dry_run,
            started_at, rows_planned, details
        ) VALUES (?, ?, ?, 'building', ?, ?, ?, ?)
        """,
        (
            receipt_id,
            generation_id,
            from_generation_id,
            int(bool(dry_run)),
            at,
            max(0, int(rows_planned)),
            _json_dumps(_sanitize_mapping_payload(details)),
        ),
    )
    row = conn.execute("SELECT * FROM vector_migration_receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()
    result = _row_dict(row)
    assert result is not None
    return result


def finish_migration_receipt(
    conn: sqlite3.Connection,
    receipt_id: str,
    *,
    status: str,
    rows_built: int = 0,
    unique_id_count: int = 0,
    error: str = "",
    details: Mapping[str, Any] | None = None,
    timestamp: str = "",
) -> dict[str, Any]:
    if status not in {"ready", "failed", "activated"}:
        raise ValueError(f"unsupported migration receipt status: {status}")
    at = timestamp or now_iso()
    safe_error = sanitize_report_text(error)[:4000]
    cursor = conn.execute(
        """
        UPDATE vector_migration_receipts
        SET status = ?, finished_at = ?, rows_built = ?, unique_id_count = ?, error = ?, details = ?
        WHERE receipt_id = ? AND status = 'building'
        """,
        (
            status,
            at,
            max(0, int(rows_built)),
            max(0, int(unique_id_count)),
            safe_error,
            _json_dumps(_sanitize_mapping_payload(details)),
            receipt_id,
        ),
    )
    if cursor.rowcount != 1:
        raise GenerationCompatibilityError(f"migration receipt CAS conflict: {receipt_id}")
    row = conn.execute("SELECT * FROM vector_migration_receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()
    result = _row_dict(row)
    assert result is not None
    return result


def generation_health_report(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_vector_generation_schema(conn)
    current = _sanitize_manifest_for_report(current_generation(conn))
    statuses = {
        str(row["status"]): int(row["count"])
        for row in conn.execute("SELECT status, COUNT(*) AS count FROM vector_generations GROUP BY status").fetchall()
    }
    outbox = {
        str(row["status"]): int(row["count"])
        for row in conn.execute("SELECT status, COUNT(*) AS count FROM vector_outbox GROUP BY status").fetchall()
    }
    failed_receipts = int(
        conn.execute("SELECT COUNT(*) FROM vector_migration_receipts WHERE status = 'failed'").fetchone()[0]
    )
    return {
        "current": current,
        "generation_status_counts": statuses,
        "outbox_status_counts": outbox,
        "failed_migration_receipts": failed_receipts,
    }
