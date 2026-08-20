"""SQLite-backed brute-force vector companion used for lightweight or dependency-free deployments.

It trades speed for portability and must follow the same rebuildable-companion contract as LanceDB."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from .vector_store import VectorStoreCompatibilityError


class SQLiteBruteForceVectorStore:
    """Pure-SQLite vector companion for hosts where LanceDB/pyarrow is unsafe.

    SQLite remains a rebuildable vector cache, not the Scope Recall truth store.
    Vectors are stored as JSON arrays and searched with a bounded brute-force
    scan. This is intentionally simple and dependency-free for small/medium
    local memory sets and non-AVX CPUs.
    """

    def __init__(self, db_path: Path, *, table_name: str = "memories", dimensions: int, metric: str = "cosine") -> None:
        self._db_path = db_path
        self._table_name = table_name or "memories"
        self._dimensions = int(dimensions)
        self._metric = (metric or "cosine").strip().lower()
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def backend(self) -> str:
        return "sqlite-bruteforce"

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def is_available(self) -> bool:
        return True

    def _sidecar_paths(self) -> tuple[Path, Path]:
        return (
            self._db_path.with_name(f"{self._db_path.name}-wal"),
            self._db_path.with_name(f"{self._db_path.name}-shm"),
        )

    def _existing_sidecars(self) -> list[str]:
        wal_path, shm_path = self._sidecar_paths()
        return [
            suffix
            for suffix, path in (("-wal", wal_path), ("-shm", shm_path))
            if path.exists() or path.is_symlink()
        ]

    @staticmethod
    def _harden_descriptor_mode(descriptor: int) -> None:
        """Apply an owner-only POSIX mode when descriptor chmod is available.

        Windows uses ACL inheritance rather than POSIX owner/group/other mode
        bits, and CPython does not expose ``os.fchmod`` there. The containing
        Hermes profile remains the Windows access-control boundary; pretending
        that ``os.chmod(path, 0o600)`` is equivalent would be misleading and
        would reintroduce a path race.
        """

        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(descriptor, 0o600)

    def _prepare_mutable_storage(self, *, create: bool) -> None:
        """Create or harden mutable SQLite files without following symlinks."""

        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if create:
            flags |= os.O_CREAT
        try:
            descriptor = os.open(self._db_path, flags, 0o600)
        except FileNotFoundError:
            raise FileNotFoundError(
                "sqlite-bruteforce physical storage is missing"
            ) from None
        except OSError as exc:
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce mutable storage is unsafe or inaccessible"
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise VectorStoreCompatibilityError(
                    "sqlite-bruteforce mutable storage is not a regular file"
                )
            self._harden_descriptor_mode(descriptor)
        finally:
            os.close(descriptor)

    def _harden_mutable_sidecars(self) -> None:
        """Apply owner-only mode to SQLite WAL/SHM files created during open."""

        for path in self._sidecar_paths():
            if not path.exists() and not path.is_symlink():
                continue
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
            except OSError as exc:
                raise VectorStoreCompatibilityError(
                    "sqlite-bruteforce mutable sidecar is unsafe or inaccessible"
                ) from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise VectorStoreCompatibilityError(
                        "sqlite-bruteforce mutable sidecar is not a regular file"
                    )
                self._harden_descriptor_mode(descriptor)
            finally:
                os.close(descriptor)

    def _reject_existing_sidecars(self) -> None:
        sidecars = self._existing_sidecars()
        if sidecars:
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce immutable storage has mutable sidecars: "
                + ", ".join(sorted(sidecars))
            )

    def open(self) -> None:
        self._prepare_mutable_storage(create=True)
        with self._lock:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
            try:
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._ensure_schema()
                stored_dimensions = self._get_meta_int("dimensions")
                stored_table = self._get_meta_text("table_name")
                if (stored_dimensions and stored_dimensions != self._dimensions) or (stored_table and stored_table != self._table_name):
                    requested = f"dimensions={self._dimensions}, table_name={self._table_name!r}"
                    existing = f"dimensions={stored_dimensions}, table_name={stored_table!r}"
                    self._conn.rollback()
                    raise VectorStoreCompatibilityError(
                        "existing sqlite-bruteforce generation is incompatible: "
                        f"{existing}; requested {requested}; build and activate a shadow generation explicitly"
                    )
                self._set_meta("dimensions", str(self._dimensions))
                self._set_meta("table_name", self._table_name)
                self._conn.commit()
                self._harden_mutable_sidecars()
            except Exception:
                self._conn.close()
                self._conn = None
                raise

    def open_existing(self) -> None:
        """Open an existing companion read-only; never create files, schema, or metadata."""

        # READY generations are immutable snapshots. ``mode=ro`` alone can
        # still create WAL shared-memory sidecars when the database journal
        # mode is WAL; ``immutable=1`` prevents those writes but also ignores
        # WAL contents. Reject sidecars before and after opening so preflight
        # cannot silently validate only the main database while private or
        # receipt-unbound state remains in ``-wal``/``-shm``.
        uri = f"file:{self._db_path.resolve()}?mode=ro&immutable=1"
        with self._lock:
            self._reject_existing_sidecars()
            if not self._db_path.is_file():
                raise FileNotFoundError("sqlite-bruteforce physical storage is missing")
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            try:
                self._reject_existing_sidecars()
                self._validate_existing_identity()
                self._reject_existing_sidecars()
            except Exception:
                self._conn.close()
                self._conn = None
                raise

    def open_existing_for_update(self) -> None:
        """Open existing mutable storage without creating files, tables, or metadata."""

        if not self._db_path.is_file():
            raise FileNotFoundError("sqlite-bruteforce physical storage is missing")
        self._prepare_mutable_storage(create=False)
        uri = f"file:{self._db_path.resolve()}?mode=rw"
        with self._lock:
            self._conn = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
                timeout=30.0,
            )
            self._conn.row_factory = sqlite3.Row
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._validate_existing_identity()
                self._harden_mutable_sidecars()
            except Exception:
                self._conn.close()
                self._conn = None
                raise

    def _validate_existing_identity(self) -> None:
        """Validate the physical schema and identity without mutating either."""

        tables = {
            str(row[0])
            for row in self._require_conn()
            .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            .fetchall()
        }
        required = {"vector_records", "vector_meta"}
        if not required <= tables:
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce physical storage is corrupt or incomplete: missing required tables"
            )
        stored_dimensions = self._get_meta_int("dimensions")
        stored_table = self._get_meta_text("table_name")
        if stored_dimensions != self._dimensions or stored_table != self._table_name:
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce physical identity mismatch: "
                f"dimensions={stored_dimensions}, table_name={stored_table!r}"
            )

    def _ensure_schema(self) -> None:
        conn = self._require_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_records (
                id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                vector_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vector_records_scope ON vector_records(scope_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vector_records_updated ON vector_records(updated_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

    def _get_meta_text(self, key: str) -> str:
        row = self._require_conn().execute("SELECT value FROM vector_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"] or "") if row else ""

    def _get_meta_int(self, key: str) -> int:
        try:
            return int(self._get_meta_text(key) or 0)
        except (TypeError, ValueError):
            return 0

    def _set_meta(self, key: str, value: str) -> None:
        self._require_conn().execute(
            "INSERT INTO vector_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _coerce_vector(self, value: Any) -> list[float]:
        if isinstance(value, str):
            raw = json.loads(value)
        else:
            raw = value
        vector = [float(item) for item in (raw or [])]
        if len(vector) != self._dimensions:
            raise ValueError(f"vector dimension mismatch: expected {self._dimensions}, got {len(vector)}")
        return vector

    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None:
        payload = list(rows)
        if not payload:
            return
        with self._lock:
            conn = self._require_conn()
            for row in payload:
                memory_id = str(row.get("id") or "")
                if not memory_id:
                    continue
                vector = self._coerce_vector(row.get("vector"))
                conn.execute(
                    """
                    INSERT INTO vector_records(id, scope_id, source, target, content, summary, updated_at, vector_json)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        scope_id = excluded.scope_id,
                        source = excluded.source,
                        target = excluded.target,
                        content = excluded.content,
                        summary = excluded.summary,
                        updated_at = excluded.updated_at,
                        vector_json = excluded.vector_json
                    """,
                    (
                        memory_id,
                        str(row.get("scope_id") or ""),
                        str(row.get("source") or ""),
                        str(row.get("target") or ""),
                        str(row.get("content") or ""),
                        str(row.get("summary") or ""),
                        str(row.get("updated_at") or ""),
                        json.dumps(vector, separators=(",", ":")),
                    ),
                )
            conn.commit()

    def upsert(self, record: Mapping[str, Any] | Any) -> None:
        try:
            payload = dict(record)
        except TypeError:
            payload = dict(vars(record))
        self.upsert_records([payload])

    def delete_by_ids(self, ids: list[str]) -> None:
        ids = [str(item) for item in ids if str(item)]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            conn = self._require_conn()
            conn.execute(f"DELETE FROM vector_records WHERE id IN ({placeholders})", ids)
            conn.commit()

    def delete(self, ids: list[str]) -> int:
        before = set(self.list_ids())
        self.delete_by_ids(ids)
        after = set(self.list_ids())
        return len(before - after)

    def list_ids(self) -> list[str]:
        with self._lock:
            rows = self._require_conn().execute("SELECT id FROM vector_records ORDER BY id").fetchall()
        return [str(row["id"]) for row in rows]

    def _rows(self, where: str = "", params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        sql = "SELECT id, scope_id, source, target, content, summary, updated_at, vector_json FROM vector_records"
        if where:
            sql += f" WHERE {where}"
        with self._lock:
            return self._require_conn().execute(sql, params).fetchall()

    def list_records(self) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for row in self._rows():
            record = self._row_to_record(row, include_vector=True)
            output[str(record["id"])] = record
        return output

    def audit_counts(self) -> dict[str, int]:
        ids = self.list_ids()
        counts = Counter(ids)
        return {
            "physical_rows": len(ids),
            "unique_ids": len(counts),
            "duplicate_rows": sum(count - 1 for count in counts.values() if count > 1),
            "duplicate_ids": sum(1 for count in counts.values() if count > 1),
        }

    def repair_records(self, desired_records: dict[str, dict[str, Any]]) -> int:
        desired_ids = set(str(memory_id) for memory_id in desired_records)
        with self._lock:
            keep: list[dict[str, Any]] = []
            for row in self._rows():
                record = self._row_to_record(row, include_vector=True)
                memory_id = str(record.get("id") or "")
                desired = desired_records.get(memory_id)
                if not memory_id or memory_id not in desired_ids or desired is None:
                    continue
                if str(record.get("updated_at") or "") != str(desired.get("updated_at") or ""):
                    continue
                keep.append(record)
            conn = self._require_conn()
            conn.execute("DELETE FROM vector_records")
            conn.commit()
            self.upsert_records(keep)
        return len(keep)

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]:
        if not vector:
            return []
        query_vector = self._coerce_vector(vector)
        candidates: list[dict[str, Any]] = []
        for row in self._rows("scope_id = ?", (str(scope_id),)):
            try:
                record = self._row_to_record(row, include_vector=True)
                distance = self._distance(query_vector, record["vector"])
            except Exception:
                continue
            record.pop("vector", None)
            record["_distance"] = distance
            candidates.append(record)
        candidates.sort(key=lambda item: (float(item.get("_distance") or 0.0), str(item.get("updated_at") or "")))
        return candidates[: max(0, int(limit))]

    def count_rows(self) -> int:
        with self._lock:
            return int(self._require_conn().execute("SELECT COUNT(*) FROM vector_records").fetchone()[0])

    def seal(self) -> dict[str, int]:
        """Checkpoint and close a writable generation before READY publication.

        ``Connection.close()`` alone may leave a fully checkpointed WAL pinned
        by another reader. A successful seal therefore requires a successful
        TRUNCATE checkpoint *and* physical absence of both SQLite sidecars.
        """

        checkpoint: dict[str, int] | None = None
        checkpoint_error: Exception | None = None
        close_error: Exception | None = None
        with self._lock:
            conn = self._require_conn()
            try:
                if conn.in_transaction:
                    raise VectorStoreCompatibilityError(
                        "sqlite-bruteforce READY seal refused an open transaction"
                    )
                # Tolerate sub-5s transient reader pins, then still fail
                # closed: READY publication remains retryable after a longer
                # pin releases. A zero timeout made every writer-tick overlap
                # a hard seal failure under multi-writer load (issue #47's
                # collision family); a bounded wait removes the hair trigger
                # without weakening the fail-closed contract below.
                conn.execute("PRAGMA busy_timeout=5000")
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row is None or len(row) != 3:
                    raise VectorStoreCompatibilityError(
                        "sqlite-bruteforce READY seal returned an invalid checkpoint result"
                    )
                busy, log, checkpointed = (int(value) for value in row)
                checkpoint = {
                    "busy": busy,
                    "log": log,
                    "checkpointed": checkpointed,
                }
            except Exception as exc:
                checkpoint_error = exc
            finally:
                try:
                    conn.close()
                except Exception as exc:  # pragma: no cover - defensive close failure
                    close_error = exc
                self._conn = None

            sidecars = self._existing_sidecars()

        if checkpoint_error is not None:
            if isinstance(checkpoint_error, VectorStoreCompatibilityError):
                raise checkpoint_error
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce READY seal checkpoint execution failed"
            ) from checkpoint_error
        if close_error is not None:
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce READY seal could not close the checkpointed database"
            ) from close_error
        if checkpoint is None:  # pragma: no cover - guarded above
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce READY seal did not return checkpoint evidence"
            )

        busy = checkpoint["busy"]
        log = checkpoint["log"]
        checkpointed = checkpoint["checkpointed"]
        checkpoint_summary = f"busy={busy}, log={log}, checkpointed={checkpointed}"
        if busy != 0 or log != 0 or checkpointed != 0:
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce READY seal checkpoint was incomplete: " + checkpoint_summary
            )
        if sidecars:
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce READY seal left mutable sidecars after checkpoint "
                f"({checkpoint_summary}): {', '.join(sorted(sidecars))}"
            )
        return checkpoint

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _row_to_record(self, row: sqlite3.Row, *, include_vector: bool) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": str(row["id"]),
            "scope_id": str(row["scope_id"]),
            "source": str(row["source"]),
            "target": str(row["target"]),
            "content": str(row["content"]),
            "summary": str(row["summary"]),
            "updated_at": str(row["updated_at"]),
        }
        if include_vector:
            record["vector"] = self._coerce_vector(row["vector_json"])
        return record

    def _distance(self, query: list[float], candidate: list[float]) -> float:
        if self._metric in {"l2", "euclidean"}:
            return math.sqrt(sum((left - right) ** 2 for left, right in zip(query, candidate)))
        if self._metric in {"dot", "inner_product"}:
            return 1.0 - sum(left * right for left, right in zip(query, candidate))
        # Default to cosine distance to match LanceDB's semantic-search shape.
        q_norm = math.sqrt(sum(value * value for value in query))
        c_norm = math.sqrt(sum(value * value for value in candidate))
        if q_norm <= 0.0 or c_norm <= 0.0:
            return 1.0
        similarity = sum(left * right for left, right in zip(query, candidate)) / (q_norm * c_norm)
        return max(0.0, min(2.0, 1.0 - similarity))

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("sqlite-bruteforce vector store is not open")
        return self._conn
