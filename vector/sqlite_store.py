"""SQLite brute-force companion for hosts where LanceDB/PyArrow is unsafe.

Vectors are stored as JSON arrays and searched with a bounded brute-force
scan: dependency-free and portable for small or medium local memory sets and
non-AVX CPUs.  It is still only a rebuildable cache, never the truth store.
Recall reads committed embeddings through the WAL while the worker writes.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from ..core.deadline import remaining_seconds
from . import VectorStore, VectorStoreCompatibilityError, require_no_text
from .purge import governed_row_ids, purge_request

_ROW_COLUMNS = "id, scope_id, source, target, content, summary, updated_at, vector_json"

#: How long a statement run outside any request waits for a lock: a worker's pass.
_LOCK_WAIT_SECONDS = 30.0


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

    @staticmethod
    def _harden_regular_file(descriptor: int, what: str) -> None:
        """Owner-only POSIX mode on an already-open regular file.

        Windows uses ACL inheritance rather than POSIX mode bits.  CPython 3.13
        added ``os.fchmod`` there, but it only sets the read-only attribute and
        is refused on a descriptor opened without attribute rights, so it is not
        called on Windows (as ``core.truth_connection`` already does).  The
        containing Hermes profile is the Windows access-control boundary;
        pretending ``os.chmod(path)`` were equivalent would reintroduce a path race.
        """
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise VectorStoreCompatibilityError(f"sqlite-bruteforce mutable {what} is not a regular file")
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None and os.name != "nt":
            fchmod(descriptor, 0o600)

    def _check_mutable_file(self, path: Path, what: str, *, missing_ok: bool = False) -> bool:
        """Refuse a non-regular file at ``path``; on POSIX, narrow a mode that lets group or others in.

        Closing any raw descriptor drops every lock this process holds on the
        file, SQLite's included, so an owner-only file is inspected with lstat
        alone and its connections in this process keep their locks. Narrowing a
        permissive mode raw-opens the file, which would cancel them: it runs
        before this store connects, in a worker pass that opens the store once,
        and once per file, after which the owner-only check opens nothing.
        Windows checks the file type and relies on inherited ACLs. A sidecar
        may disappear when another process's last writer closes; SQLite
        recreates it with the database's mode when needed.
        """
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise VectorStoreCompatibilityError(f"sqlite-bruteforce mutable {what} is not a regular file")
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    self._harden_regular_file(descriptor, what)
                finally:
                    os.close(descriptor)
        except FileNotFoundError as exc:
            if missing_ok:
                return False
            raise VectorStoreCompatibilityError(f"sqlite-bruteforce mutable {what} is unsafe or inaccessible") from exc
        except OSError as exc:
            raise VectorStoreCompatibilityError(f"sqlite-bruteforce mutable {what} is unsafe or inaccessible") from exc
        return True

    def _prepare_mutable_storage(self) -> None:
        """Create storage exclusively, or inspect an existing file without a raw descriptor.

        An existing owner-only database may already have connections in this
        process, whose POSIX locks must survive this open. A raw descriptor is
        opened only to create the file exclusively, before any connection to
        it can exist; two stores of one process opening the same new file at
        once are not supported, and a worker pass opens one.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._check_mutable_file(self.db_path, "storage", missing_ok=True):
            return
        try:
            descriptor = os.open(self.db_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            self._check_mutable_file(self.db_path, "storage")
            return
        except OSError as exc:
            raise VectorStoreCompatibilityError("sqlite-bruteforce mutable storage is unsafe or inaccessible") from exc
        try:
            self._harden_regular_file(descriptor, "storage")
        finally:
            os.close(descriptor)

    def _harden_mutable_sidecars(self) -> None:
        """Inspect existing WAL/SHM files without raw descriptors when owner-only.

        Other connections in this process may hold the WAL index's in-use
        lock. Closing a raw descriptor would cancel it, so only a permissive
        POSIX mode is narrowed, once per file before owner-only checks suffice.
        """
        for path in self._sidecar_paths():
            self._check_mutable_file(path, "sidecar", missing_ok=True)

    # -- lifecycle -----------------------------------------------------------------

    @staticmethod
    def _connect(target: str | Path, *, uri: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(target, uri=uri, check_same_thread=False, timeout=_LOCK_WAIT_SECONDS)
        conn.row_factory = sqlite3.Row
        return conn

    def open(self) -> None:
        with self._store_locked():
            self._prepare_mutable_storage()
            self._harden_mutable_sidecars()
            self._conn = self._connect(self.db_path)
            try:
                self._execute("PRAGMA journal_mode=WAL")
                self._execute("PRAGMA synchronous=NORMAL")
                self._ensure_schema()
                stored_dimensions = self._get_meta_int("dimensions")
                stored_table = self._get_meta_text("table_name")
                if (stored_dimensions and stored_dimensions != self.dimensions) or (
                    stored_table and stored_table != self.table_name
                ):
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
            except Exception:
                self.close()
                raise

    def open_existing(self) -> None:
        """Open an existing generation without creating its companion, tables or metadata.

        The connection never writes data. Each read sees the transactions a
        writer has committed, including those still in the WAL; SQLite's locks
        keep a writer's checkpoint from rewriting pages a read is using. The
        worker writes this file while recall reads it, and a long-lived server
        keeps this connection across requests, so it is not opened with
        ``immutable=1``: that flag makes SQLite ignore the WAL, take no locks
        and never notice a change, and such a connection reads stale rows,
        then pages a checkpoint has rewritten.

        A WAL read needs the ``-wal`` and ``-shm`` files beside the database, or
        permission to create them there, as the worker has; SQLite then creates
        an empty ``-wal`` and its ``-shm`` WAL index. A reader that closes last
        leaves them behind; they are removed when a writer is the last
        connection and closes cleanly.
        """
        with self._store_locked():
            try:
                info = os.lstat(self.db_path)
            except FileNotFoundError as exc:
                raise FileNotFoundError("sqlite-bruteforce physical storage is missing") from exc
            if stat.S_ISLNK(info.st_mode):
                try:
                    info = os.stat(self.db_path)
                except FileNotFoundError as exc:
                    raise VectorStoreCompatibilityError(
                        "sqlite-bruteforce physical storage is not a regular file"
                    ) from exc
            if not stat.S_ISREG(info.st_mode):
                raise VectorStoreCompatibilityError("sqlite-bruteforce physical storage is not a regular file")
            self._conn = self._connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                self._validate_existing_identity()
            except Exception:
                self.close()
                raise

    def _validate_existing_identity(self) -> None:
        """Validate the physical schema and identity without mutating either."""
        tables = {
            str(row[0]) for row in self._execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
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

    @contextmanager
    def _store_locked(self) -> Iterator[None]:
        """Serialize store work within the request budget, allowing nested calls."""
        remaining = remaining_seconds()
        acquired = self._lock.acquire() if remaining is None else self._lock.acquire(timeout=max(0.0, remaining))
        if not acquired:
            raise TimeoutError("sqlite-bruteforce vector store lock timeout")
        try:
            yield
        finally:
            self._lock.release()

    def _execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        """Run a statement with its lock wait capped by the current request.

        A kept connection serves several requests, so the timeout is refreshed
        before every statement, including schema and identity validation. Workers
        outside a request keep ``_LOCK_WAIT_SECONDS``.
        """
        conn = self._require_conn()
        remaining = remaining_seconds()
        seconds = _LOCK_WAIT_SECONDS if remaining is None else min(_LOCK_WAIT_SECONDS, max(0.0, remaining))
        conn.execute(f"PRAGMA busy_timeout={int(seconds * 1000)}")
        return conn.execute(sql, parameters)

    def _ensure_schema(self) -> None:
        self._execute(
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
        self._execute("CREATE INDEX IF NOT EXISTS idx_vector_records_scope ON vector_records(scope_id)")
        self._execute("CREATE INDEX IF NOT EXISTS idx_vector_records_updated ON vector_records(updated_at)")
        self._execute("CREATE TABLE IF NOT EXISTS vector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _get_meta_text(self, key: str) -> str:
        row = self._execute("SELECT value FROM vector_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"] or "") if row else ""

    def _get_meta_int(self, key: str) -> int:
        try:
            return int(self._get_meta_text(key) or 0)
        except (TypeError, ValueError):
            return 0

    def _set_meta(self, key: str, value: str) -> None:
        self._execute(
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
        with self._store_locked():
            return self._execute(sql, params).fetchall()

    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None:
        payload = require_no_text(rows)
        if not payload:
            return
        with self._store_locked():
            conn = self._require_conn()
            for row in payload:
                memory_id = str(row.get("id") or "")
                if not memory_id:
                    continue
                vector = self._coerce_vector(row.get("vector"))
                self._execute(
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
        self,
        rows: Iterable[dict[str, Any]],
        *,
        guard: Callable[[], bool],
        remaining_seconds: float,
    ) -> bool:
        """Write ``rows`` as one group, only if ``guard`` still approves under this store's lock.

        The worker publishes every embedding through this fenced form (see
        ``runtime.lance_port.LanceIndexWriter``), so a companion without it cannot
        be written to at all.  The store's own lock is the fence boundary: the
        guard is evaluated with the write already serialized, a refusing guard
        writes nothing, and ``upsert_records`` commits the group once.
        """
        if not callable(guard):
            raise TypeError("guard must be callable")
        if (
            type(remaining_seconds) not in (int, float)
            or not math.isfinite(float(remaining_seconds))
            or remaining_seconds <= 0
        ):
            raise RuntimeError("vector fence deadline exhausted")
        payload = require_no_text(rows)
        if not payload:
            return True
        with self._store_locked():
            if not guard():
                return False
            self.upsert_records(payload)
        return True

    def purge_governed_members(
        self, *, members, agent_id, installation_id, partitions, project_id, branch_id, remaining_seconds: float
    ) -> bool:
        """Remove every revision of the governed members; acknowledge only an inventory verified empty.

        This companion is purged as it is published to: otherwise a forget
        would leave its vector rows behind and its purge work retrying for good.  The rules
        are the native store's, from the same code: opaque identities only, a
        row that cannot be classified makes the inventory unknown, and an
        unknown inventory is never acknowledged.
        """
        if (
            type(remaining_seconds) not in (int, float)
            or not math.isfinite(float(remaining_seconds))
            or remaining_seconds <= 0
        ):
            raise RuntimeError("vector purge deadline exhausted")
        if not members or not partitions:
            return False
        targets, governed = purge_request(
            members=members, agent_id=agent_id, installation_id=installation_id, partitions=partitions
        )
        select = dict(
            targets=targets,
            governed=governed,
            agent_id=agent_id,
            installation_id=installation_id,
            project_id=project_id,
            branch_id=branch_id,
        )

        def rows() -> list[dict[str, Any]]:
            found = self._execute("SELECT id, scope_id, source, target FROM vector_records").fetchall()
            return [
                dict(id=row["id"], scope_id=row["scope_id"], source=row["source"], target=row["target"])
                for row in found
            ]

        with self._store_locked():
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
        with self._store_locked():
            conn = self._require_conn()
            self._execute(f"DELETE FROM vector_records WHERE id IN ({placeholders})", ids)
            conn.commit()

    def contains_id(self, memory_id: str) -> bool:
        """Primary-key probe; never counts or lists the corpus."""
        resolved = str(memory_id or "")
        if not resolved:
            return False
        with self._store_locked():
            row = self._execute("SELECT 1 FROM vector_records WHERE id = ? LIMIT 1", (resolved,)).fetchone()
        return row is not None

    def list_ids(self) -> list[str]:
        with self._store_locked():
            rows = self._execute("SELECT id FROM vector_records ORDER BY id").fetchall()
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
        with self._store_locked():
            return int(self._execute("SELECT COUNT(*) FROM vector_records").fetchone()[0])

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
