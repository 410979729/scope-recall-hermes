"""Independent SQLite raw embeddings with worker-maintained mapped arrays.

Raw values and cache watermarks share a SQLite snapshot. Derived files can be
rebuilt while readers continue to rank committed original float32 values.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..core.deadline import RequestDeadline, current_request_deadline, remaining_seconds, using_request_deadline
from . import VectorStore, VectorStoreCompatibilityError, require_no_text
from .cache import MappedIndex, space_capabilities
from .purge import governed_row_ids, purge_request
from .search import as_vector, search

_ROW_COLUMNS = "id, scope_id, source, target, updated_at, vector"

#: How long a statement run outside any request waits for a lock: a worker's pass.
_LOCK_WAIT_SECONDS = 30.0


class SQLiteVectorStore(VectorStore):
    """One raw vector generation; maintain_index publishes its derived file prefixes."""

    backend = "sqlite"

    def __init__(
        self,
        db_path: Path,
        *,
        table_name: str,
        dimensions: int,
        metric: str = "cosine",
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Configure a generation without opening files; an injected clock governs its bounded waits."""
        super().__init__(db_path, table_name=table_name, dimensions=dimensions, metric=metric)
        self._clock = clock
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._mapped = MappedIndex(self.db_path, self.dimensions)
        self._readonly = False
        self.created_fresh = False

    @property
    def requires_reopen(self) -> bool:
        """Allow the runtime to recover after an interrupted connection transition."""
        return self._conn is None

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
            raise VectorStoreCompatibilityError(f"SQLite vector mutable {what} is not a regular file")
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
                raise VectorStoreCompatibilityError(f"SQLite vector mutable {what} is not a regular file")
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    self._harden_regular_file(descriptor, what)
                finally:
                    os.close(descriptor)
        except FileNotFoundError as exc:
            if missing_ok:
                return False
            raise VectorStoreCompatibilityError(f"SQLite vector mutable {what} is unsafe or inaccessible") from exc
        except OSError as exc:
            raise VectorStoreCompatibilityError(f"SQLite vector mutable {what} is unsafe or inaccessible") from exc
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
            raise VectorStoreCompatibilityError("SQLite vector mutable storage is unsafe or inaccessible") from exc
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
            self.close()
            self.created_fresh = not self.db_path.exists()
            self._prepare_mutable_storage()
            self._harden_mutable_sidecars()
            self._conn = self._connect(self.db_path)
            self._readonly = False
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
                        "existing SQLite vector generation is incompatible: "
                        f"dimensions={stored_dimensions}, table_name={stored_table!r}; "
                        f"requested dimensions={self.dimensions}, table_name={self.table_name!r}; "
                        "build and activate a shadow generation explicitly"
                    )
                self._set_meta("dimensions", str(self.dimensions))
                self._set_meta("table_name", self.table_name)
                self._set_meta("format", "float32-le")
                from .imports import seed

                self._ensure_import_columns()
                seed(self)
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
            self.close()
            try:
                info = os.lstat(self.db_path)
            except FileNotFoundError as exc:
                raise FileNotFoundError("SQLite vector physical storage is missing") from exc
            if stat.S_ISLNK(info.st_mode):
                try:
                    info = os.stat(self.db_path)
                except FileNotFoundError as exc:
                    raise VectorStoreCompatibilityError("SQLite vector physical storage is not a regular file") from exc
            if not stat.S_ISREG(info.st_mode):
                raise VectorStoreCompatibilityError("SQLite vector physical storage is not a regular file")
            self._conn = self._connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
            self._readonly = True
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
                "SQLite vector physical storage is corrupt or incomplete: missing required tables"
            )
        stored_dimensions = self._get_meta_int("dimensions")
        stored_table = self._get_meta_text("table_name")
        if stored_dimensions != self.dimensions or stored_table != self.table_name:
            raise VectorStoreCompatibilityError(
                f"SQLite vector physical identity mismatch: dimensions={stored_dimensions}, table_name={stored_table!r}"
            )

    def close(self) -> None:
        with self._lock:
            self._mapped.close()
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SQLite vector vector store is not open")
        return self._conn

    @contextmanager
    def _store_locked(self) -> Iterator[None]:
        """Serialize store work within the request budget, allowing nested calls."""
        remaining = remaining_seconds()
        acquired = self._lock.acquire() if remaining is None else self._lock.acquire(timeout=max(0.0, remaining))
        if not acquired:
            raise TimeoutError("SQLite vector vector store lock timeout")
        try:
            yield
        finally:
            self._lock.release()

    @contextmanager
    def _operation(self, budget: float) -> Iterator[None]:
        """Keep both the process lock and SQLite lock wait inside the publication or purge budget."""
        outer = remaining_seconds()
        seconds = budget if outer is None else min(budget, max(0.0, outer))
        current = current_request_deadline()
        clock = self._clock if self._clock is not None else current.clock if current is not None else None
        with using_request_deadline(RequestDeadline.from_budget(seconds, clock=clock)), self._store_locked():
            yield

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
        self._execute("""CREATE TABLE IF NOT EXISTS vector_records (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
            scope_id TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
            updated_at TEXT NOT NULL, vector BLOB NOT NULL, identity TEXT UNIQUE)""")
        columns = {row[1] for row in self._execute("PRAGMA table_info(vector_records)")}
        if "vector" not in columns:
            raise VectorStoreCompatibilityError("raw float32 storage schema mismatch")
        if "identity" not in columns:
            self._execute("ALTER TABLE vector_records ADD COLUMN identity TEXT")
        self._execute("CREATE UNIQUE INDEX IF NOT EXISTS vector_identity ON vector_records(identity)")
        self._execute("CREATE INDEX IF NOT EXISTS idx_vector_records_scope ON vector_records(scope_id,seq)")
        self._execute("CREATE TABLE IF NOT EXISTS vector_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self._execute(
            "CREATE TABLE IF NOT EXISTS embedding_spaces(id TEXT PRIMARY KEY,descriptor TEXT NOT NULL,"
            "capabilities TEXT NOT NULL DEFAULT '{}')"
        )
        space_columns = {row[1] for row in self._execute("PRAGMA table_info(embedding_spaces)")}
        if "capabilities" not in space_columns:
            self._execute("ALTER TABLE embedding_spaces ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}'")
        self._execute("CREATE TABLE IF NOT EXISTS index_deletes(seq INTEGER PRIMARY KEY)")
        self._execute("CREATE TABLE IF NOT EXISTS vector_tombstones(id TEXT PRIMARY KEY)")
        self._execute(
            "CREATE TABLE IF NOT EXISTS purge_fences(scope_id TEXT,kind TEXT,ref TEXT,PRIMARY KEY(scope_id,kind,ref))"
        )
        self._execute(
            "CREATE TRIGGER IF NOT EXISTS vector_removed AFTER DELETE ON vector_records "
            "BEGIN INSERT OR IGNORE INTO index_deletes(seq) VALUES(old.seq); END"
        )
        self._execute(
            "CREATE TABLE IF NOT EXISTS vector_imports(source TEXT PRIMARY KEY,watermark INTEGER NOT NULL,"
            "state TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '')"
        )

    def _ensure_import_columns(self) -> None:
        columns = {row[1] for row in self._execute("PRAGMA table_info(vector_imports)")}
        for column, declaration in (
            ("retry_after", "REAL NOT NULL DEFAULT 0"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                self._execute(f"ALTER TABLE vector_imports ADD COLUMN {column} {declaration}")

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
        return as_vector(value, self.dimensions).tolist()

    def _row_to_record(self, row: sqlite3.Row) -> dict[str, Any]:
        import numpy as np

        return dict(
            id=row["id"],
            scope_id=row["scope_id"],
            source=row["source"],
            target=row["target"],
            content="",
            summary="",
            updated_at=row["updated_at"],
            vector=np.frombuffer(row["vector"], dtype="<f4").tolist(),
        )

    def _rows(self, where: str = "", params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        sql = f"SELECT {_ROW_COLUMNS} FROM vector_records"
        with self._store_locked():
            return self._execute(sql + (f" WHERE {where}" if where else ""), params).fetchall()

    def _write_rows(self, rows, *, importing: bool = False) -> None:
        for row in rows:
            memory_id = str(row.get("id") or "")
            if not memory_id:
                raise ValueError("vector id is required")
            vector = as_vector(row["vector"], self.dimensions).tobytes()
            if self._execute("SELECT 1 FROM vector_tombstones WHERE id=?", (memory_id,)).fetchone():
                continue
            try:
                metadata = json.loads(row.get("target") or "{}")
            except ValueError:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            fence = (row["scope_id"], metadata.get("object_kind", ""), metadata.get("object_ref", ""))
            if self._execute("SELECT 1 FROM purge_fences WHERE scope_id=? AND kind=? AND ref=?", fence).fetchone():
                continue
            identity = None
            if type(metadata.get("object_revision")) is int and all(
                type(metadata.get(key)) is str and metadata[key]
                for key in ("object_kind", "object_ref", "embedding_space")
            ):
                identity = json.dumps(
                    [
                        row["scope_id"],
                        metadata["object_kind"],
                        metadata["object_ref"],
                        metadata["object_revision"],
                        metadata["embedding_space"],
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                duplicate = self._execute("SELECT id FROM vector_records WHERE identity=?", (identity,)).fetchone()
                if duplicate is not None and duplicate[0] != memory_id:
                    raise VectorStoreCompatibilityError("conflicting vector ids for one object revision and space")
            values = (
                memory_id,
                row["scope_id"],
                row.get("source", ""),
                row.get("target", "{}"),
                row.get("updated_at", ""),
                vector,
                identity,
            )
            if importing:
                self._execute(
                    "INSERT OR IGNORE INTO vector_records(id,scope_id,source,target,updated_at,vector,identity) "
                    "VALUES(?,?,?,?,?,?,?)",
                    values,
                )
            else:
                # Replacing allocates a new sequence; readers include it even while its old code still exists.
                self._execute("DELETE FROM vector_records WHERE id=?", (memory_id,))
                self._execute(
                    "INSERT INTO vector_records(id,scope_id,source,target,updated_at,vector,identity) "
                    "VALUES(?,?,?,?,?,?,?)",
                    values,
                )

    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None:
        """Commit a group atomically; the worker publishes its derived arrays."""
        payload = require_no_text(rows)
        with self._store_locked():
            conn = self._require_conn()
            try:
                self._execute("BEGIN IMMEDIATE")
                self._write_rows(payload)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def register_space(self, space_id: str, descriptor: dict) -> None:
        """Persist the model, dimensions, normalization and query/document encoding contracts."""
        if self._readonly:
            return
        encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        with self._store_locked():
            prior = self._execute("SELECT descriptor FROM embedding_spaces WHERE id=?", (space_id,)).fetchone()
            if prior and prior[0] != encoded:
                raise VectorStoreCompatibilityError("embedding space descriptor mismatch")
            capabilities = json.dumps(space_capabilities(descriptor), sort_keys=True, separators=(",", ":"))
            self._execute(
                "INSERT INTO embedding_spaces(id,descriptor,capabilities) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET capabilities=excluded.capabilities",
                (space_id, encoded, capabilities),
            )
            self._require_conn().commit()

    def maintain_index(self, *, max_rows: int = 512) -> dict[str, Any]:
        """Publish one bounded cache page from committed raw vectors."""
        with self._store_locked():
            return self._mapped.maintain(self._require_conn(), max_rows)

    def note_missing_legacy(self, authorization: str | None = None) -> None:
        """Keep an explicit gap when completed truth projections have no surviving vector input."""
        with self._store_locked():
            self._execute(
                "INSERT OR IGNORE INTO vector_imports(source,watermark,state,detail) "
                "VALUES('missing:legacy-vectors',0,'unreadable','FileNotFoundError')"
            )
            self._set_meta("missing_input_authorization", authorization or "")
            self._require_conn().commit()

    def allow_requested_reembedding(self, authorization: str | None) -> bool:
        """Clear the missing-input hold only for a new explicit re-embedding authorization."""
        with self._store_locked():
            if not authorization or authorization == self._get_meta_text("missing_input_authorization"):
                return False
            cursor = self._execute("DELETE FROM vector_imports WHERE source='missing:legacy-vectors'")
            self._require_conn().commit()
            pending = self._execute("SELECT 1 FROM vector_imports WHERE state!='complete' LIMIT 1").fetchone()
            return bool(cursor.rowcount) and pending is None

    def import_legacy_page(self, *, visible, max_rows: int = 512) -> dict[str, Any]:
        """Import one recoverable page without any model or embedding capability."""
        from .imports import import_page

        return import_page(self, visible=visible, max_rows=max_rows)

    def diagnostics(self) -> dict[str, Any]:
        """Describe the selected search engine, watermark and legacy import without changing them."""
        with self._store_locked():
            imports = [dict(row) for row in self._execute("SELECT * FROM vector_imports")]
            return {**self._mapped.diagnostics(self._require_conn()), "imports": imports}

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
        with self._operation(remaining_seconds):
            conn = self._require_conn()
            try:
                self._execute("BEGIN IMMEDIATE")
                if not guard():
                    conn.rollback()
                    return False
                self._write_rows(payload)
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

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

        with self._operation(remaining_seconds):
            conn = self._require_conn()
            self._execute("BEGIN IMMEDIATE")
            try:
                ids = governed_row_ids(rows(), **select)
                if ids is None:
                    conn.rollback()
                    return False
                for partition in partitions:
                    for member in members:
                        self._execute(
                            "INSERT OR IGNORE INTO purge_fences VALUES(?,?,?)",
                            (partition["physical_scope_id"], member["kind"], member["ref"]),
                        )
                self._delete_ids(ids)
                done = governed_row_ids(rows(), **select) == []
                conn.commit()
                return done
            except BaseException:
                conn.rollback()
                raise

    def _delete_ids(self, ids: Iterable[str]) -> None:
        for key in ids:
            self._execute("INSERT OR IGNORE INTO vector_tombstones VALUES(?)", (key,))
            self._execute("DELETE FROM vector_records WHERE id=?", (key,))

    def delete_by_ids(self, ids: list[str]) -> None:
        """Remove raw rows and persist fences before another import can republish them."""
        with self._store_locked():
            conn = self._require_conn()
            try:
                self._execute("BEGIN IMMEDIATE")
                self._delete_ids(ids)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

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
        """Filter trusted physical partitions before either index or raw-vector ranking."""
        listed = list(dict.fromkeys(str(scope) for scope in scope_ids))
        if not vector or not listed or limit <= 0:
            return []
        with self._store_locked():
            conn = self._require_conn()
            self._execute("BEGIN")
            try:
                pending = self._execute("SELECT 1 FROM vector_imports WHERE state!='complete' LIMIT 1").fetchone()
                if pending:
                    raise RuntimeError("vector_import_pending")
                return search(
                    conn,
                    vector,
                    listed,
                    dimensions=self.dimensions,
                    metric=self.metric,
                    limit=limit,
                    index=self._mapped,
                )
            finally:
                conn.rollback()

    def count_rows(self) -> int:
        with self._store_locked():
            return int(self._execute("SELECT COUNT(*) FROM vector_records").fetchone()[0])


# The compatibility import names use the same raw store, never a second storage implementation.
SQLiteBruteForceVectorStore = SQLiteVectorStore

__all__ = ["SQLiteVectorStore", "SQLiteBruteForceVectorStore"]
