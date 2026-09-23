"""SQLite brute-force companion for hosts where LanceDB/PyArrow is unsafe.

Vectors are stored as JSON arrays and searched with a bounded brute-force
scan: dependency-free and portable for small or medium local memory sets and
non-AVX CPUs.  It is still only a rebuildable cache, never the truth store.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from . import VectorStore, VectorStoreCompatibilityError

_ROW_COLUMNS = "id, scope_id, source, target, content, summary, updated_at, vector_json"


class SQLiteBruteForceVectorStore(VectorStore):
    backend = "sqlite-bruteforce"

    def __init__(self, db_path: Path, *, table_name: str, dimensions: int, metric: str = "cosine") -> None:
        super().__init__(db_path, table_name=table_name, dimensions=dimensions, metric=metric)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def is_available(self) -> bool:
        return True

    # -- physical files ----------------------------------------------------------

    def _sidecar_paths(self) -> tuple[Path, Path]:
        return (
            self.db_path.with_name(f"{self.db_path.name}-wal"),
            self.db_path.with_name(f"{self.db_path.name}-shm"),
        )

    def _existing_sidecars(self) -> list[str]:
        wal, shm = self._sidecar_paths()
        return [suffix for suffix, path in (("-wal", wal), ("-shm", shm)) if path.exists() or path.is_symlink()]

    @staticmethod
    def _harden_regular_file(descriptor: int, what: str) -> None:
        """Owner-only POSIX mode on an already-open regular file.

        Windows uses ACL inheritance rather than POSIX mode bits and CPython
        does not expose ``os.fchmod`` there.  The containing Hermes profile is
        the Windows access-control boundary; pretending ``os.chmod(path)`` were
        equivalent would reintroduce a path race.
        """
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise VectorStoreCompatibilityError(f"sqlite-bruteforce mutable {what} is not a regular file")
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(descriptor, 0o600)

    def _prepare_mutable_storage(self) -> None:
        """Create or harden the database file without following symlinks."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.db_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            raise VectorStoreCompatibilityError("sqlite-bruteforce mutable storage is unsafe or inaccessible") from exc
        try:
            self._harden_regular_file(descriptor, "storage")
        finally:
            os.close(descriptor)

    def _harden_mutable_sidecars(self) -> None:
        """Owner-only mode on the WAL/SHM files created during open."""
        for path in self._sidecar_paths():
            if not path.exists() and not path.is_symlink():
                continue
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError as exc:
                raise VectorStoreCompatibilityError("sqlite-bruteforce mutable sidecar is unsafe or inaccessible") from exc
            try:
                self._harden_regular_file(descriptor, "sidecar")
            finally:
                os.close(descriptor)

    def _reject_existing_sidecars(self) -> None:
        sidecars = self._existing_sidecars()
        if sidecars:
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce immutable storage has mutable sidecars: " + ", ".join(sorted(sidecars))
            )

    # -- lifecycle -----------------------------------------------------------------

    @staticmethod
    def _connect(target: str | Path, *, uri: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(target, uri=uri, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def open(self) -> None:
        self._prepare_mutable_storage()
        with self._lock:
            self._conn = self._connect(self.db_path)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._ensure_schema()
                stored_dimensions = self._get_meta_int("dimensions")
                stored_table = self._get_meta_text("table_name")
                if (stored_dimensions and stored_dimensions != self.dimensions) or (stored_table and stored_table != self.table_name):
                    self._conn.rollback()
                    raise VectorStoreCompatibilityError(
                        "existing sqlite-bruteforce generation is incompatible: "
                        f"dimensions={stored_dimensions}, table_name={stored_table!r}; "
                        f"requested dimensions={self.dimensions}, table_name={self.table_name!r}; "
                        "build and activate a shadow generation explicitly"
                    )
                self._set_meta("dimensions", str(self.dimensions))
                self._set_meta("table_name", self.table_name)
                self._conn.commit()
                self._harden_mutable_sidecars()
            except Exception:
                self.close()
                raise

    def open_existing(self) -> None:
        """Open a READY generation read-only; never create files, schema, or metadata.

        ``mode=ro`` alone can still create WAL shared-memory sidecars when the
        journal mode is WAL; ``immutable=1`` prevents those writes but also
        ignores WAL contents.  Sidecars are rejected before and after opening
        so preflight cannot validate only the main database while private or
        receipt-unbound state remains in ``-wal``/``-shm``.
        """
        with self._lock:
            self._reject_existing_sidecars()
            if not self.db_path.is_file():
                raise FileNotFoundError("sqlite-bruteforce physical storage is missing")
            self._conn = self._connect(f"file:{self.db_path.resolve()}?mode=ro&immutable=1", uri=True)
            try:
                self._reject_existing_sidecars()
                self._validate_existing_identity()
                self._reject_existing_sidecars()
            except Exception:
                self.close()
                raise

    def _validate_existing_identity(self) -> None:
        """Validate the physical schema and identity without mutating either."""
        tables = {
            str(row[0])
            for row in self._require_conn().execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if not {"vector_records", "vector_meta"} <= tables:
            raise VectorStoreCompatibilityError(
                "sqlite-bruteforce physical storage is corrupt or incomplete: missing required tables"
            )
        stored_dimensions = self._get_meta_int("dimensions")
        stored_table = self._get_meta_text("table_name")
        if stored_dimensions != self.dimensions or stored_table != self.table_name:
            raise VectorStoreCompatibilityError(
                f"sqlite-bruteforce physical identity mismatch: dimensions={stored_dimensions}, table_name={stored_table!r}"
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("sqlite-bruteforce vector store is not open")
        return self._conn

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
        conn.execute("CREATE TABLE IF NOT EXISTS vector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

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

    # -- records -------------------------------------------------------------------

    def _coerce_vector(self, value: Any) -> list[float]:
        raw = json.loads(value) if isinstance(value, str) else value
        vector = [float(item) for item in (raw or [])]
        if len(vector) != self.dimensions:
            raise ValueError(f"vector dimension mismatch: expected {self.dimensions}, got {len(vector)}")
        return vector

    def _row_to_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "scope_id": str(row["scope_id"]),
            "source": str(row["source"]),
            "target": str(row["target"]),
            "content": str(row["content"]),
            "summary": str(row["summary"]),
            "updated_at": str(row["updated_at"]),
            "vector": self._coerce_vector(row["vector_json"]),
        }

    def _rows(self, where: str = "", params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        sql = f"SELECT {_ROW_COLUMNS} FROM vector_records"
        if where:
            sql += f" WHERE {where}"
        with self._lock:
            return self._require_conn().execute(sql, params).fetchall()

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

    def fenced_upsert_records(
        self, rows: Iterable[dict[str, Any]], *, guard: Callable[[], bool], remaining_seconds: float,
    ) -> bool:
        """Write ``rows`` as one group, only if ``guard`` still approves under this store's lock.

        The worker publishes every embedding through this fenced form (see
        ``adapters.lance.LanceIndexWriter``), so a companion without it cannot
        be written to at all.  The store's own lock is the fence boundary: the
        guard is evaluated with the write already serialized, a refusing guard
        writes nothing, and ``upsert_records`` commits the group once.
        """
        if not callable(guard):
            raise TypeError("guard must be callable")
        if type(remaining_seconds) not in (int, float) or not math.isfinite(float(remaining_seconds)) or remaining_seconds <= 0:
            raise RuntimeError("vector fence deadline exhausted")
        payload = list(rows)
        if not payload:
            return True
        with self._lock:
            if not guard():
                return False
            self.upsert_records(payload)
        return True

    def purge_governed_members(self, *, members, agent_id, installation_id,
                               partitions, project_id, branch_id, remaining_seconds: float) -> bool:
        """Remove every revision of the governed members; acknowledge only an inventory verified empty.

        This companion could not be purged at all.  That did not matter while
        it could not be published to either (#85); once it could, a forget left
        its vector rows behind and its purge work retrying for good.  The rules
        are the native store's, from the same code: opaque identities only, a
        row that cannot be classified makes the inventory unknown, and an
        unknown inventory is never acknowledged.
        """
        from .store import governed_row_ids, purge_request

        if type(remaining_seconds) not in (int, float) or not math.isfinite(float(remaining_seconds)) or remaining_seconds <= 0:
            raise RuntimeError("vector purge deadline exhausted")
        if not members or not partitions:
            return False
        targets, governed = purge_request(members=members, agent_id=agent_id, installation_id=installation_id,
                                          partitions=partitions)
        select = dict(targets=targets, governed=governed, agent_id=agent_id, installation_id=installation_id,
                      project_id=project_id, branch_id=branch_id)

        def rows() -> list[dict[str, Any]]:
            found = self._require_conn().execute("SELECT id, scope_id, source, target FROM vector_records").fetchall()
            return [dict(id=row["id"], scope_id=row["scope_id"], source=row["source"], target=row["target"]) for row in found]

        with self._lock:
            ids = governed_row_ids(rows(), **select)
            if ids is None:
                return False
            if ids:
                self.delete_by_ids(sorted(set(ids)))
            return governed_row_ids(rows(), **select) == []

    def delete_by_ids(self, ids: list[str]) -> None:
        ids = [str(item) for item in ids if str(item)]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            conn = self._require_conn()
            conn.execute(f"DELETE FROM vector_records WHERE id IN ({placeholders})", ids)
            conn.commit()

    def contains_id(self, memory_id: str) -> bool:
        """Primary-key probe; never counts or lists the corpus."""
        resolved = str(memory_id or "")
        if not resolved:
            return False
        with self._lock:
            row = self._require_conn().execute(
                "SELECT 1 FROM vector_records WHERE id = ? LIMIT 1", (resolved,)
            ).fetchone()
        return row is not None

    def list_ids(self) -> list[str]:
        with self._lock:
            rows = self._require_conn().execute("SELECT id FROM vector_records ORDER BY id").fetchall()
        return [str(row["id"]) for row in rows]

    def list_records(self) -> dict[str, dict[str, Any]]:
        return {str(record["id"]): record for record in map(self._row_to_record, self._rows())}

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]:
        return self.search_scopes(vector, scope_ids=(scope_id,), limit=limit)

    def search_scopes(self, vector: list[float], *, scope_ids, limit: int) -> list[dict[str, Any]]:
        listed = list(dict.fromkeys(str(scope_id) for scope_id in scope_ids))
        if not vector or not listed:
            return []
        query_vector = self._coerce_vector(vector)
        candidates: list[dict[str, Any]] = []
        for row in self._rows(f"scope_id IN ({','.join('?' for _ in listed)})", tuple(listed)):
            try:
                record = self._row_to_record(row)
                distance = self._distance(query_vector, record.pop("vector"))
            except Exception:
                continue
            record["_distance"] = distance
            candidates.append(record)
        # Nearest first, then newest truth revision, then id for deterministic
        # ties.  Three stable sorts: a single tuple key would order the ISO
        # timestamp ascending and prefer stale records.
        candidates.sort(key=lambda item: str(item.get("id") or ""))
        candidates.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        candidates.sort(key=lambda item: float(item.get("_distance") or 0.0))
        return candidates[: max(0, int(limit))]

    def count_rows(self) -> int:
        with self._lock:
            return int(self._require_conn().execute("SELECT COUNT(*) FROM vector_records").fetchone()[0])

    def _distance(self, query: list[float], candidate: list[float]) -> float:
        if self.metric in {"l2", "euclidean"}:
            return math.sqrt(sum((left - right) ** 2 for left, right in zip(query, candidate)))
        if self.metric in {"dot", "inner_product"}:
            return 1.0 - sum(left * right for left, right in zip(query, candidate))
        # Cosine distance, matching LanceDB's semantic-search shape.
        q_norm = math.sqrt(sum(value * value for value in query))
        c_norm = math.sqrt(sum(value * value for value in candidate))
        if q_norm <= 0.0 or c_norm <= 0.0:
            return 1.0
        similarity = sum(left * right for left, right in zip(query, candidate)) / (q_norm * c_norm)
        return max(0.0, min(2.0, 1.0 - similarity))


__all__ = ["SQLiteBruteForceVectorStore"]
