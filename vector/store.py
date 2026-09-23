"""The LanceDB companion: table mechanics only.

Record identity, dimensions and repair policy are enforced by the runtime.
SQLite rows remain the source of truth, so a missing vector row is repaired by
rebuild, never treated as a memory deletion.
"""
from __future__ import annotations

import json
import math
import sys
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from ..core.file_lock import advisory_file_lock
from . import VectorRecord, VectorStore, VectorStoreCompatibilityError
from .compaction import measure_footprint
from .lance_native import native_modules

_COLUMNS = ("id", "scope_id", "source", "target", "content", "summary", "updated_at", "vector")
_PURGE_KINDS = frozenset({"event", "claim", "episode", "artifact", "reference"})
_PURGE_METADATA_KEYS = (
    "object_kind", "object_ref", "vector_id", "embedding_space", "agent_id", "installation_id", "logical_scope_id",
)
#: Lance index types under which ``id = '...'`` is an indexed probe, not a scan.
_SCALAR_INDEX_TYPES = frozenset({"bitmap", "btree", "label_list", "scalar"})


def _sql_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _covers_id_only(index: Any) -> bool:
    """True for a listed scalar index whose only column is ``id``."""
    columns = [str(name).strip() for name in (getattr(index, "columns", None) or ())]
    kind = str(getattr(index, "index_type", None) or "").strip().lower().replace("-", "_")
    return columns == ["id"] and (not kind or kind in _SCALAR_INDEX_TYPES)


def purge_request(*, members, agent_id, installation_id, partitions):
    """The validated targets and governed partitions of one purge, for whichever store carries it out."""
    if not isinstance(agent_id, str) or not agent_id or not isinstance(installation_id, str) or not installation_id:
        raise ValueError("trusted purge identity required")
    targets = {(entry["kind"], entry["ref"]) for entry in members}
    if any(kind not in _PURGE_KINDS or not isinstance(ref, str) or not ref for kind, ref in targets):
        raise ValueError("invalid purge members")
    governed = {(entry["scope_id"], entry["embedding_space"]): entry["physical_scope_id"] for entry in partitions}
    return targets, governed


def governed_row_ids(rows: Iterable[dict[str, Any]], *, targets, governed, agent_id, installation_id,
                     project_id, branch_id, check_budget: Callable[[], None] = lambda: None) -> list[str] | None:
    """Ids of the governed rows among ``rows``, or ``None`` when any row cannot be classified.

    One rule for every companion store: a row whose writer metadata cannot be
    read makes the whole inventory unknown, and an unknown inventory is never
    acknowledged as empty.
    """
    scopes = {scope for scope, _ in governed}
    matched: list[str] = []
    seen: set[str] = set()
    for row in rows:
        check_budget()
        row_id = row.get("id")
        if type(row_id) is not str or not row_id or row_id in seen:
            return None
        seen.add(row_id)
        metadata = _purge_metadata(row)
        if metadata is None:
            return None
        if (metadata["agent_id"], metadata["installation_id"]) != (agent_id, installation_id):
            continue
        if (metadata["object_kind"], metadata["object_ref"]) not in targets:
            continue
        if metadata["logical_scope_id"] not in scopes or (metadata["project_id"], metadata["branch_id"]) != (project_id, branch_id):
            continue
        partition = governed.get((metadata["logical_scope_id"], metadata["embedding_space"]))
        if partition is None or row["scope_id"] != partition:
            return None
        matched.append(row_id)
    return matched


def _purge_metadata(row: dict[str, Any]) -> dict[str, Any] | None:
    """The writer metadata of one row, or ``None`` when the row cannot be classified."""
    try:
        metadata = json.loads(row["target"])
        if any(type(metadata.get(key)) is not str or not metadata[key] for key in _PURGE_METADATA_KEYS):
            return None
        if metadata["object_kind"] not in _PURGE_KINDS:
            return None
        if type(metadata.get("object_revision")) is not int or metadata["object_revision"] < 1:
            return None
        for key in ("project_id", "branch_id"):
            if key not in metadata or (metadata[key] is not None and (type(metadata[key]) is not str or not metadata[key])):
                return None
        if row["id"] != metadata["vector_id"] or row["source"] != metadata["object_ref"]:
            return None
        return metadata
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class LanceVectorStore(VectorStore):
    """One Lance table.

    Every write holds the cross-process lock and every read follows the table
    to its newest committed version (see ``_fresh_table``).
    """

    backend = "lancedb"

    def __init__(self, db_path: Path, *, table_name: str, dimensions: int, metric: str = "cosine") -> None:
        super().__init__(db_path, table_name=table_name, dimensions=dimensions, metric=metric)
        self._db = None
        self._table = None

    # -- lifecycle -----------------------------------------------------------

    def is_available(self) -> bool:
        return native_modules() is not None

    def open(self) -> None:
        lancedb, _ = self._require_native()
        self.db_path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self.db_path))
        if self.table_name in self._listed_tables():
            self._table = self._db.open_table(self.table_name)
        else:
            self._table = self._db.create_table(self.table_name, schema=self._schema())
        self._ensure_schema_compatible()

    def open_existing(self) -> None:
        """Open an existing table without creating a directory, database, or table."""
        lancedb, _ = self._require_native()
        if not self.db_path.is_dir():
            raise FileNotFoundError("LanceDB physical storage is missing")
        self._db = lancedb.connect(str(self.db_path))
        if self.table_name not in self._listed_tables():
            self.close()
            raise VectorStoreCompatibilityError(f"LanceDB physical storage is missing table {self.table_name!r}")
        self._table = self._db.open_table(self.table_name)
        self._ensure_schema_compatible()

    def close(self) -> None:
        self._table = None
        self._db = None

    @staticmethod
    def _require_native() -> tuple[Any, Any]:
        modules = native_modules()
        if modules is None:
            raise RuntimeError("lancedb/pyarrow is not installed")
        return modules

    def _listed_tables(self) -> set[str]:
        listed = self._db.list_tables()
        return set(getattr(listed, "tables", listed))

    def _schema(self):
        _, pa = self._require_native()
        return pa.schema([
            *(pa.field(name, pa.string()) for name in _COLUMNS[:-1]),
            pa.field("vector", pa.list_(pa.float32(), self.dimensions)),
        ])

    def _ensure_schema_compatible(self) -> None:
        schema = self._require_table().schema
        existing = set(getattr(schema, "names", []) or [])
        missing = sorted(set(_COLUMNS) - existing)
        actual = 0
        if "vector" in existing:
            try:
                actual = int(getattr(schema.field("vector").type, "list_size", 0) or 0)
            except Exception:
                actual = 0
        if not missing and (not actual or actual == self.dimensions):
            return
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if actual and actual != self.dimensions:
            details.append(f"dimensions {actual} != requested {self.dimensions}")
        if not actual and "vector" in existing:
            details.append("vector dimensions are unreadable")
        raise VectorStoreCompatibilityError(
            f"LanceDB table {self.table_name!r} is incompatible ({'; '.join(details)}); "
            "build and activate a new vector generation explicitly"
        )

    def _require_table(self):
        if self._table is None:
            raise RuntimeError("vector table is not open")
        return self._table

    def _fresh_table(self):
        """The table at its newest committed version, for every read and write.

        An open Lance table pins the version it was opened at.  Without this a
        long-running host that opened the store once never saw a vector the
        worker published afterwards, and only a restart fixed it.  It also lets
        compaction drop superseded versions without stranding a pinned reader.
        Cost: about 1 ms against a 30-140 ms search.
        """
        table = self._require_table()
        table.checkout_latest()
        return table

    # -- writes ----------------------------------------------------------------

    @contextmanager
    def physical_write_lock(self, *, timeout_seconds: float | None = None) -> Iterator[None]:
        """Hold the cross-process Lance mutation lock."""
        with advisory_file_lock(self.db_path.parent / f".{self.db_path.name}.scope-recall-write.lock",
                                timeout_seconds=timeout_seconds):
            yield

    def upsert_records_locked(self, rows: Iterable[dict[str, Any]]) -> None:
        """Commit rows while ``physical_write_lock`` is already held.

        A replay can repeat after the physical commit but before the SQLite
        outbox completion CAS.  ``merge_insert`` makes that retry one
        idempotent transaction instead of a delete/add crash window.
        """
        self._ensure_schema_compatible()
        payload = list(rows)
        if payload:
            (
                self._fresh_table().merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(payload)
            )

    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None:
        with self.physical_write_lock():
            self.upsert_records_locked(rows)

    def fenced_upsert_records(
        self, rows: Iterable[dict[str, Any]], *, guard: Callable[[], bool], remaining_seconds: float,
    ) -> bool:
        """Commit ``rows`` in one Lance transaction, only if ``guard`` still approves under the native lock.

        The worker publishes every embedding through this fenced form
        (``adapters.lance.LanceIndexWriter``).  On Windows the store is the
        helper-process one, and the helper asks the guard with the native lock
        held (``_lance_worker.fenced_upsert``).  Everywhere else
        ``build_vector_store`` selects this in-process store, which did not
        have the method: every publication failed with
        ``fenced_upsert_unsupported`` and the companion stayed empty (#99).
        The order here is the helper's: lock, guard, one merge.
        """
        if not callable(guard):
            raise TypeError("guard must be callable")
        if type(remaining_seconds) not in (int, float) or not math.isfinite(float(remaining_seconds)) or remaining_seconds <= 0:
            raise RuntimeError("native vector fence deadline exhausted")
        payload = list(rows)
        try:
            with self.physical_write_lock(timeout_seconds=float(remaining_seconds)):
                if not guard():
                    return False
                self.upsert_records_locked(payload)
                return True
        except TimeoutError as exc:
            raise RuntimeError("native vector fence deadline exhausted") from exc

    def _delete_ids_locked(self, ids: Iterable[str]) -> None:
        quoted = ", ".join(_sql_quote(item) for item in ids)
        self._fresh_table().delete(f"id IN ({quoted})")

    def delete_by_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        with self.physical_write_lock():
            self._delete_ids_locked(ids)

    def purge_governed_members(self, *, members, agent_id, installation_id, partitions, project_id, branch_id,
                               budget_seconds: float | None = None, remaining_seconds: float | None = None) -> bool:
        """Remove every revision of the governed members and acknowledge only under the publication lock.

        Inputs are opaque identities authorized by the host; no truth database
        or model is touched here.  A row this store cannot classify makes the
        whole inventory unknown, and an unknown inventory is never acknowledged
        as empty.

        The budget has two names because this method has two callers.  The
        Windows helper process passes ``budget_seconds``, what is left of its
        parent's deadline.  ``adapters.lance.LancePurgePort`` passes
        ``remaining_seconds`` to whichever store it holds, and off Windows
        that is this one: the keyword was refused with a ``TypeError`` the port
        turns into "not purged", so a forget never finished there (#99).
        """
        if budget_seconds is None:
            budget_seconds = remaining_seconds
        if type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds) or budget_seconds <= 0:
            raise ValueError("positive finite purge budget required")
        if not members or not partitions:
            return False
        targets, governed = purge_request(members=members, agent_id=agent_id, installation_id=installation_id,
                                          partitions=partitions)
        deadline = time.monotonic() + float(budget_seconds)

        def check_budget() -> None:
            if time.monotonic() >= deadline:
                raise RuntimeError("native vector purge deadline exhausted")

        def inventory() -> list[str] | None:
            check_budget()
            return governed_row_ids(
                self._table_rows(["id", "scope_id", "source", "target"]), targets=targets, governed=governed,
                agent_id=agent_id, installation_id=installation_id, project_id=project_id, branch_id=branch_id,
                check_budget=check_budget)

        check_budget()
        with self.physical_write_lock():
            # Empty inventories also wait behind every already-granted writer.
            ids = inventory()
            if ids is None:
                return False
            if ids:
                check_budget()
                self._delete_ids_locked(sorted(set(ids)))
            remaining = inventory()
            check_budget()
            return remaining == []

    def compact(self) -> dict[str, int]:
        """Merge fragments and drop superseded versions.  Idempotent.

        Every publication is its own commit, so the store accumulates one data
        fragment and one manifest per vector, and each manifest lists every
        fragment: the history grows quadratically until compacted.

        ``cleanup_older_than`` is zero and ``delete_unverified`` is set because
        a longer window does not delay reclamation, it prevents it: the version
        created by the compaction itself is then too young to retire its
        predecessors, so they stay orphaned on disk pass after pass.  Zero is
        safe here because this store is a rebuildable cache, no recovery path
        reads Lance version history, every writer takes the lock held below,
        and every reader follows the table forward.

        Returns the footprint before and after so the caller can report what
        the pass reclaimed rather than merely that it ran.
        """
        before = self._physical_footprint()
        with self.physical_write_lock():
            self._fresh_table().optimize(cleanup_older_than=timedelta(seconds=0), delete_unverified=True)
        after = self._physical_footprint()
        return {f"{key}_before": value for key, value in before.items()} | after

    def _physical_footprint(self) -> dict[str, int]:
        # Read straight off the filesystem: the doctor must report the same
        # numbers without opening the table, and an operator can verify them
        # with a file listing.
        return measure_footprint(self.db_path, self.table_name).as_dict()

    # -- reads -----------------------------------------------------------------

    def contains_id(self, memory_id: str) -> bool:
        """Indexed existence probe; refuses to scan.

        ``where(id).limit(1)`` bounds the result, not the work: without a
        scalar index on ``id`` Lance may still read the corpus, so the probe
        is only issued once such an index is listed.  Ordinary replay keeps
        its counts from the SQLite membership ledger instead.
        """
        resolved = str(memory_id or "")
        if not resolved:
            return False
        table = self._fresh_table()
        if not any(_covers_id_only(index) for index in table.list_indices() or ()):
            raise RuntimeError("LanceDB cannot prove an indexed id lookup")
        rows = table.search().select(["id"]).where(f"id = {_sql_quote(resolved)}").limit(1).to_list()
        return any(str(row.get("id") or "") == resolved for row in rows)

    def _table_rows(self, columns: list[str] | None = None) -> list[dict[str, Any]]:
        arrow = self._fresh_table().to_arrow()
        if columns:
            arrow = arrow.select(columns)
        return arrow.to_pylist()

    def list_ids(self) -> list[str]:
        return sorted(str(row["id"]) for row in self._table_rows(["id"]) if row.get("id"))

    def list_records(self) -> dict[str, dict[str, Any]]:
        """Every row keyed by id; a duplicated id keeps its newest ``updated_at``."""
        output: dict[str, dict[str, Any]] = {}
        for row in self._table_rows():
            memory_id = str(row.get("id") or "")
            if not memory_id:
                continue
            current = output.get(memory_id)
            if current is None or str(row.get("updated_at") or "") >= str(current.get("updated_at") or ""):
                output[memory_id] = row
        return output

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]:
        if not vector:
            return []
        query = self._fresh_table().search(vector).metric(self.metric).where(f"scope_id = {_sql_quote(scope_id)}")
        return query.limit(int(limit)).to_list()

    def search_scopes(self, vector: list[float], *, scope_ids: Iterable[str], limit: int) -> list[dict[str, Any]]:
        """One nearest-neighbour request over a list of partitions, filtered before the search."""
        listed = list(dict.fromkeys(str(scope_id) for scope_id in scope_ids))
        if not vector or not listed:
            return []
        where = f"scope_id IN ({', '.join(_sql_quote(scope_id) for scope_id in listed)})"
        query = self._fresh_table().search(vector).metric(self.metric).where(where, prefilter=True)
        return query.limit(int(limit)).to_list()

    def count_rows(self) -> int:
        return int(self._fresh_table().count_rows())


def build_vector_store(
    backend: str,
    *,
    storage_dir: Path,
    table_name: str,
    dimensions: int,
    metric: str = "cosine",
) -> VectorStore:
    """Select a companion store without opening it."""
    normalized = str(backend or "lancedb").strip().lower()
    if normalized in {"sqlite", "sqlite-bruteforce"}:
        from .sqlite_store import SQLiteBruteForceVectorStore

        return SQLiteBruteForceVectorStore(
            Path(storage_dir) / "vector.sqlite3", table_name=table_name, dimensions=dimensions, metric=metric,
        )
    if normalized == "lancedb":
        vector_dir = Path(storage_dir) / "lancedb"
        if sys.platform == "win32":
            from .process_store import ProcessLanceVectorStore

            return ProcessLanceVectorStore(vector_dir, table_name=table_name, dimensions=dimensions, metric=metric)
        return LanceVectorStore(vector_dir, table_name=table_name, dimensions=dimensions, metric=metric)
    if normalized == "pgvector":
        raise ValueError(
            "pgvector is not supported in this v3 distribution; retain the old "
            "installation and use the migration guide"
        )
    raise ValueError(f"unsupported vector backend: {backend}")


__all__ = ["LanceVectorStore", "VectorRecord", "VectorStore", "VectorStoreCompatibilityError", "build_vector_store"]
