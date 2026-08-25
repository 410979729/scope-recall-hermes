"""LanceDB vector companion implementation.

The store owns vector-table mechanics only; record identity, dimensions, and repair policy are enforced by runtime helpers."""

from __future__ import annotations

import importlib
import inspect
import logging
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, cast, runtime_checkable

from .capture_filters import sanitize_report_text
from .vector_mutation_guard import advisory_file_lock

logger = logging.getLogger(__name__)

_NATIVE_VECTOR_PROBE: dict[str, Any] | None = None
_NATIVE_VECTOR_PROBE_TIMEOUT = 10.0
_lance_write_guard = advisory_file_lock


@dataclass(frozen=True)
class VectorRecord:
    id: str
    scope_id: str
    source: str
    target: str
    content: str
    summary: str
    updated_at: str
    vector: list[float]


@dataclass(frozen=True)
class VectorHit:
    id: str
    scope_id: str
    source: str
    target: str
    content: str
    summary: str
    updated_at: str
    distance: float = 0.0


class VectorStoreCompatibilityError(RuntimeError):
    """An existing vector table cannot be opened with the requested schema.

    Compatibility failures are deliberately non-destructive. Callers that
    need a different schema or embedding space must build a new generation
    explicitly instead of replacing the active table during startup.
    """


@runtime_checkable
class VectorStore(Protocol):
    @property
    def backend(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def is_available(self) -> bool: ...
    def open(self) -> None: ...
    def open_existing(self) -> None: ...
    def open_existing_for_update(self) -> None: ...
    def close(self) -> None: ...
    def upsert(self, record: VectorRecord | dict[str, Any]) -> None: ...
    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None: ...
    def delete(self, ids: list[str]) -> int: ...
    def delete_by_ids(self, ids: list[str]) -> None: ...
    def contains_id(self, memory_id: str) -> bool: ...
    def list_ids(self) -> list[str]: ...
    def list_records(self) -> dict[str, dict[str, Any]]: ...
    def sample_metadata(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]: ...
    def repair_records(self, desired_records: dict[str, dict[str, Any]]) -> int: ...
    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]: ...
    def count_rows(self) -> int: ...
    def audit_counts(self) -> dict[str, int]: ...


VECTOR_METADATA_COLUMNS = (
    "id",
    "scope_id",
    "source",
    "target",
    "content",
    "summary",
    "updated_at",
)
HYGIENE_SAMPLE_HARD_LIMIT = 200
HYGIENE_SAMPLE_PAGE_SIZE = 50


def clamp_vector_sample_limit(
    limit: Any,
    *,
    hard_limit: int = HYGIENE_SAMPLE_HARD_LIMIT,
) -> int:
    """Clamp a hygiene/status sample window so it cannot grow with corpus size."""

    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = hard_limit
    return max(0, min(value, hard_limit))


def metadata_only_record(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy one companion row while dropping any deserialized vector payload."""

    if not isinstance(row, Mapping):
        return {}
    record: dict[str, Any] = {}
    for column in VECTOR_METADATA_COLUMNS:
        if column not in row:
            continue
        value = row[column]
        record[column] = "" if value is None else str(value)
    memory_id = str(record.get("id") or row.get("id") or "")
    if memory_id:
        record["id"] = memory_id
    return record


def store_id_lookup_is_indexed(store: Any) -> bool:
    """Return whether the store has proven an indexed id equality probe.

    A ``limit(1)`` result cap is not proof. Callers must treat a missing or
    false ``id_lookup_indexed`` flag as unindexed and skip ``contains_id``
    when maintaining cached cardinality.
    """

    if store is None:
        return False
    try:
        return getattr(store, "id_lookup_indexed", False) is True
    except Exception:
        return False


def store_contains_id(store: Any, memory_id: str) -> bool | None:
    """Probe one id with a bounded backend lookup.

    ``None`` means existence is unknown. Callers must not invent a count
    adjustment from that result, and this helper never falls back to
    ``list_ids``, ``count_rows``, or ``audit_counts``. Ordinary replay must
    not call this unless :func:`store_id_lookup_is_indexed` is true.
    """

    resolved = str(memory_id or "")
    if not resolved or store is None:
        return None
    probe = getattr(store, "contains_id", None)
    if not callable(probe):
        return None
    try:
        return bool(probe(resolved))
    except Exception:
        return None


def sample_vector_metadata(
    store: Any,
    *,
    limit: int = HYGIENE_SAMPLE_HARD_LIMIT,
    offset: int = 0,
) -> dict[str, dict[str, Any]]:
    """Sample metadata-only companion rows without listing or decoding vectors.

    Callers that only implement ``list_records`` are ignored on purpose: that
    API materializes every physical vector and is reserved for Doctor/repair.
    The hard limit caps both rows consumed from the backend and unique output.
    Duplicate ids cannot extend the loop past that cap.
    """

    if store is None:
        return {}
    sampler = getattr(store, "sample_metadata", None)
    if not callable(sampler):
        return {}
    remaining = clamp_vector_sample_limit(limit)
    cursor = max(0, int(offset or 0))
    output: dict[str, dict[str, Any]] = {}
    consumed_total = 0
    while (
        remaining > 0
        and consumed_total < HYGIENE_SAMPLE_HARD_LIMIT
        and len(output) < HYGIENE_SAMPLE_HARD_LIMIT
    ):
        page = min(
            HYGIENE_SAMPLE_PAGE_SIZE,
            remaining,
            HYGIENE_SAMPLE_HARD_LIMIT - consumed_total,
            HYGIENE_SAMPLE_HARD_LIMIT - len(output),
        )
        if page <= 0:
            break
        try:
            rows = sampler(limit=page, offset=cursor)
        except Exception:
            break
        if not isinstance(rows, list) or not rows:
            break
        page_rows = rows[:page]
        for row in page_rows:
            consumed_total += 1
            remaining -= 1
            record = metadata_only_record(row if isinstance(row, Mapping) else None)
            memory_id = str(record.get("id") or "")
            if memory_id:
                output[memory_id] = record
            if (
                remaining <= 0
                or consumed_total >= HYGIENE_SAMPLE_HARD_LIMIT
                or len(output) >= HYGIENE_SAMPLE_HARD_LIMIT
            ):
                break
        if (
            len(page_rows) < page
            or remaining <= 0
            or consumed_total >= HYGIENE_SAMPLE_HARD_LIMIT
            or len(output) >= HYGIENE_SAMPLE_HARD_LIMIT
        ):
            break
        cursor += len(page_rows)
    return output


def vector_record_to_dict(record: VectorRecord | Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(record, VectorRecord):
        return asdict(record)
    return dict(record)


def _trim_probe_output(value: str, *, limit: int = 500) -> str:
    text = sanitize_report_text(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _probe_native_vector_dependencies() -> dict[str, Any]:
    """Probe LanceDB/PyArrow in a child process before importing in-process.

    Some LanceDB/PyArrow wheels can terminate Python with SIGILL on old CPUs
    without AVX/AVX2. A normal try/except around ``import lancedb`` cannot catch
    that because the current process is already gone. The child-process probe
    turns native crashes into an ordinary non-zero return code so the provider
    can fall back to a safe vector backend.
    """

    global _NATIVE_VECTOR_PROBE
    if _NATIVE_VECTOR_PROBE is not None:
        return dict(_NATIVE_VECTOR_PROBE)
    script = "import lancedb; import pyarrow; print('ok')"
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            text=True,
            capture_output=True,
            timeout=_NATIVE_VECTOR_PROBE_TIMEOUT,
            check=False,
        )
        status = {
            "safe": result.returncode == 0,
            "returncode": int(result.returncode),
            "stdout": _trim_probe_output(result.stdout),
            "stderr": _trim_probe_output(result.stderr),
        }
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        status = {
            "safe": False,
            "returncode": None,
            "stdout": _trim_probe_output(getattr(exc, "stdout", "") or ""),
            "stderr": f"native dependency import probe timed out after {_NATIVE_VECTOR_PROBE_TIMEOUT:.1f}s",
        }
    except Exception as exc:  # pragma: no cover - defensive
        status = {"safe": False, "returncode": None, "stdout": "", "stderr": _trim_probe_output(str(exc))}
    _NATIVE_VECTOR_PROBE = status
    return dict(status)


def native_vector_dependency_status() -> dict[str, Any]:
    return _probe_native_vector_dependencies()


def _optional_lancedb():
    status = _probe_native_vector_dependencies()
    if not status.get("safe"):
        return None
    try:
        return importlib.import_module("lancedb")  # type: ignore[no-any-return]
    except Exception:  # pragma: no cover - optional dependency
        return None


def _optional_pyarrow():
    status = _probe_native_vector_dependencies()
    if not status.get("safe"):
        return None
    try:
        return importlib.import_module("pyarrow")  # type: ignore[no-any-return]
    except Exception:  # pragma: no cover - optional dependency
        return None



def _sql_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _lance_query_builder(table: Any) -> Any | None:
    """Return a query builder that can take select/limit before materialization."""

    search = getattr(table, "search", None)
    if not callable(search):
        return None
    for args in ((), (None,)):
        try:
            builder = search(*args)
        except TypeError:
            continue
        except Exception:
            return None
        if builder is None:
            continue
        owner = type(builder)
        if all(callable(getattr(owner, name, None)) for name in ("select", "limit")):
            return builder
    return None


def _materialize_lance_query(builder: Any) -> list[dict[str, Any]] | None:
    """Materialize only a builder that already received projection/limit."""

    to_list = getattr(builder, "to_list", None)
    if callable(to_list):
        try:
            rows = to_list()
        except Exception:
            return None
        return list(rows) if isinstance(rows, list) else None
    to_arrow = getattr(builder, "to_arrow", None)
    if callable(to_arrow):
        try:
            arrow = to_arrow()
            convert = getattr(arrow, "to_pylist", None)
            if not callable(convert):
                return None
            rows = convert()
        except Exception:
            return None
        return list(rows) if isinstance(rows, list) else None
    return None


def _lance_scanner_rows(
    table: Any,
    *,
    columns: list[str],
    limit: int,
    offset: int,
    where: str | None = None,
) -> list[dict[str, Any]] | None:
    """Use Lance dataset scanner only when limit (and offset if needed) are real parameters."""

    to_lance = getattr(table, "to_lance", None)
    if not callable(to_lance):
        return None
    try:
        dataset = to_lance()
    except Exception:
        return None
    scanner_fn = getattr(dataset, "scanner", None)
    if not callable(scanner_fn):
        return None
    try:
        parameters = inspect.signature(scanner_fn).parameters
    except (TypeError, ValueError):
        return None
    if "limit" not in parameters:
        return None
    if offset and "offset" not in parameters:
        return None
    kwargs: dict[str, Any] = {"columns": list(columns), "limit": int(limit)}
    if "offset" in parameters:
        kwargs["offset"] = int(offset)
    if where and "filter" in parameters:
        kwargs["filter"] = where
    elif where:
        return None
    try:
        scanner = scanner_fn(**kwargs)
        to_table = getattr(scanner, "to_table", None)
        if not callable(to_table):
            return None
        arrow = to_table()
        convert = getattr(arrow, "to_pylist", None)
        if not callable(convert):
            return None
        rows = convert()
    except Exception:
        return None
    return list(rows) if isinstance(rows, list) else None


def sample_lance_table_metadata(
    table: Any,
    *,
    columns: Iterable[str] = VECTOR_METADATA_COLUMNS,
    limit: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Push metadata projection plus offset/limit into Lance before materialization.

    Unbounded ``table.to_list`` / ``to_arrow`` / ``to_pandas`` are never used.
    If the installed API cannot prove that bound, return no sample.
    """

    bounded = clamp_vector_sample_limit(limit)
    start = max(0, int(offset or 0))
    projected = [str(column) for column in columns if str(column) and str(column) != "vector"]
    if bounded <= 0 or not projected:
        return []
    builder = _lance_query_builder(table)
    if builder is not None:
        try:
            builder = builder.select(projected).limit(bounded)
            offset_fn = getattr(builder, "offset", None)
            if start and not callable(offset_fn):
                builder = None
            elif callable(offset_fn):
                builder = offset_fn(start)
        except Exception:
            builder = None
        if builder is not None:
            rows = _materialize_lance_query(builder)
            if rows is not None:
                sampled: list[dict[str, Any]] = []
                for row in rows[:bounded]:
                    record = metadata_only_record(row)
                    if record.get("id"):
                        sampled.append(record)
                return sampled
    rows = _lance_scanner_rows(
        table,
        columns=projected,
        limit=bounded,
        offset=start,
    )
    if rows is None:
        return []
    sampled = []
    for row in rows[:bounded]:
        record = metadata_only_record(row)
        if record.get("id"):
            sampled.append(record)
    return sampled


_LANCE_VECTOR_INDEX_TYPES = {
    "ann",
    "diskann",
    "fts",
    "hnsw",
    "inverted",
    "ivf",
    "ivf_flat",
    "ivf_hnsw_pq",
    "ivf_hnsw_sq",
    "ivf_pq",
    "pq",
    "vector",
}
_LANCE_SCALAR_INDEX_TYPES = {
    "bitmap",
    "btree",
    "label_list",
    "scalar",
}


def _index_column_names(index: Any) -> list[str]:
    raw: Any = None
    if isinstance(index, Mapping):
        raw = index.get("columns") or index.get("column_names") or index.get("fields")
        if raw is None and index.get("column"):
            raw = [index.get("column")]
    else:
        for attr in ("columns", "column_names", "fields"):
            value = getattr(index, attr, None)
            if value is not None:
                raw = value
                break
        if raw is None:
            column = getattr(index, "column", None)
            if column is not None:
                raw = [column]
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    try:
        return [str(item) for item in raw if str(item)]
    except TypeError:
        return []


def _index_type_name(index: Any) -> str:
    if isinstance(index, Mapping):
        value = index.get("index_type") or index.get("type") or index.get("indexType") or ""
    else:
        value = (
            getattr(index, "index_type", None)
            or getattr(index, "type", None)
            or ""
        )
    return str(value or "").strip().lower().replace("-", "_")


def _index_covers_id_scalar(index: Any) -> bool:
    """True only for an id-only scalar/BTree/bitmap index listing."""

    columns = [str(name).strip() for name in _index_column_names(index) if str(name).strip()]
    if columns != ["id"]:
        return False
    index_type = _index_type_name(index)
    if index_type in _LANCE_VECTOR_INDEX_TYPES:
        return False
    return not index_type or index_type in _LANCE_SCALAR_INDEX_TYPES


def _listed_lance_indices(table: Any) -> list[Any] | None:
    """Return index metadata only from a successful synchronous listing API.

    An awaitable, missing, or failing listing is unknown, not "no index".
    An empty list from a working API is proof that no index exists.
    """

    owners: list[Any] = [table]
    to_lance = getattr(table, "to_lance", None)
    if callable(to_lance):
        try:
            dataset = to_lance()
        except Exception:
            dataset = None
        if dataset is not None:
            owners.append(dataset)
    for owner in owners:
        if owner is None:
            continue
        for name in ("list_indices", "list_indexes"):
            listed_fn = getattr(owner, name, None)
            if not callable(listed_fn):
                continue
            try:
                listed = listed_fn()
            except Exception:
                continue
            if inspect.isawaitable(listed):
                closer = getattr(listed, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass
                return None
            if listed is None:
                continue
            if isinstance(listed, list):
                return listed
            try:
                return list(cast(Iterable[Any], listed))
            except TypeError:
                return None
    return None


def lance_table_has_id_scalar_index(table: Any) -> bool:
    """Return True only when Lance lists a scalar index covering ``id``."""

    if table is None:
        return False
    listed = _listed_lance_indices(table)
    if not listed:
        return False
    return any(_index_covers_id_scalar(index) for index in listed)


def lance_table_contains_id(table: Any, memory_id: str) -> bool | None:
    """Indexed id existence probe. ``None`` if no scalar ``id`` index is listed.

    ``search().where(id).limit(1)`` is not a work bound by itself: without a
    listed scalar index Lance may still scan the corpus. This helper therefore
    refuses to issue that filter unless :func:`lance_table_has_id_scalar_index`
    is true.
    """

    resolved = str(memory_id or "")
    if not resolved:
        return False
    if not lance_table_has_id_scalar_index(table):
        return None
    builder = _lance_query_builder(table)
    where_sql = f"id = {_sql_quote(resolved)}"
    if builder is not None and callable(getattr(builder, "where", None)):
        try:
            builder = builder.select(["id"]).where(where_sql).limit(1)
        except Exception:
            builder = None
        if builder is not None:
            rows = _materialize_lance_query(builder)
            if rows is not None:
                return any(str(row.get("id") or "") == resolved for row in rows)
    rows = _lance_scanner_rows(
        table,
        columns=["id"],
        limit=1,
        offset=0,
        where=where_sql,
    )
    if rows is None:
        return None
    return any(str(row.get("id") or "") == resolved for row in rows)


class LanceVectorStore:
    """LanceDB-backed vector companion store.

    This class owns table-level mechanics only. SQLite rows remain the source of truth, so missing/deleted vector rows should be repaired by rebuild rather than treated as memory deletion."""
    def __init__(self, db_path: Path, *, table_name: str, dimensions: int, metric: str = "cosine") -> None:
        self._db_path = db_path
        self._table_name = table_name
        self._dimensions = int(dimensions)
        self._metric = metric or "cosine"
        self._db = None
        self._table = None

    @property
    def backend(self) -> str:
        return "lancedb"

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def table_name(self) -> str:
        return self._table_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def id_lookup_indexed(self) -> bool:
        """True only when Lance lists a scalar index on ``id``.

        Ordinary cardinality maintenance uses the SQLite membership ledger.
        This flag exists so a proven scalar index can still serve as a
        fallback probe; ``limit(1)`` alone never sets it.
        """

        return lance_table_has_id_scalar_index(self._table)

    @property
    def _write_lock_path(self) -> Path:
        return self._db_path.parent / f".{self._db_path.name}.scope-recall-write.lock"

    def is_available(self) -> bool:
        return _optional_lancedb() is not None and _optional_pyarrow() is not None

    def open(self) -> None:
        lancedb = _optional_lancedb()
        if lancedb is None or _optional_pyarrow() is None:
            raise RuntimeError("lancedb/pyarrow is not installed")
        self._db_path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self._db_path))
        self._table = self._open_or_create_table()
        self._ensure_schema_compatible()

    def open_existing(self) -> None:
        """Open an existing table without creating a directory, database, or table."""

        lancedb = _optional_lancedb()
        if lancedb is None or _optional_pyarrow() is None:
            raise RuntimeError("lancedb/pyarrow is not installed")
        if not self._db_path.is_dir():
            raise FileNotFoundError("LanceDB physical storage is missing")
        self._db = lancedb.connect(str(self._db_path))
        tables = self._listed_tables()
        if self._table_name not in tables:
            self.close()
            raise VectorStoreCompatibilityError(
                f"LanceDB physical storage is missing table {self._table_name!r}"
            )
        self._table = self._db.open_table(self._table_name)
        self._ensure_schema_compatible()

    def open_existing_for_update(self) -> None:
        """Open an existing Lance table for runtime mutation without creating it."""

        self.open_existing()

    def _listed_tables(self) -> set[str]:
        assert self._db is not None
        try:
            listed = self._db.list_tables()
            return set(getattr(listed, "tables", listed))
        except Exception:
            try:
                return set(self._db.table_names())
            except Exception:
                return set()

    def _open_or_create_table(self):
        assert self._db is not None
        tables = self._listed_tables()
        if self._table_name in tables:
            return self._db.open_table(self._table_name)
        schema = self._schema()
        return self._db.create_table(self._table_name, schema=schema)

    def _schema(self):
        pa = _optional_pyarrow()
        if pa is None:
            raise RuntimeError("pyarrow is not installed")
        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("scope_id", pa.string()),
                pa.field("source", pa.string()),
                pa.field("target", pa.string()),
                pa.field("content", pa.string()),
                pa.field("summary", pa.string()),
                pa.field("updated_at", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self._dimensions)),
            ]
        )

    def _ensure_schema_compatible(self) -> None:
        table = self._require_table()
        existing = set(getattr(table.schema, "names", []) or [])
        required = {"id", "scope_id", "source", "target", "content", "summary", "updated_at", "vector"}
        missing = sorted(required - existing)
        actual_dimensions = 0
        if "vector" in existing:
            try:
                vector_field = table.schema.field("vector")
                actual_dimensions = int(getattr(vector_field.type, "list_size", 0) or 0)
            except Exception:
                actual_dimensions = 0
        if not missing and (not actual_dimensions or actual_dimensions == self._dimensions):
            return
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if actual_dimensions and actual_dimensions != self._dimensions:
            details.append(f"dimensions {actual_dimensions} != requested {self._dimensions}")
        if not actual_dimensions and "vector" in existing:
            details.append("vector dimensions are unreadable")
        raise VectorStoreCompatibilityError(
            f"LanceDB table {self._table_name!r} is incompatible ({'; '.join(details)}); "
            "build and activate a new vector generation explicitly"
        )

    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None:
        self._ensure_schema_compatible()
        table = self._require_table()
        payload = list(rows)
        if not payload:
            return
        # A replay can repeat after the physical commit but before the SQLite
        # outbox completion CAS.  Lance merge_insert makes that retry one
        # idempotent transaction instead of exposing a delete/add crash window.
        with _lance_write_guard(self._write_lock_path):
            checkout_latest = getattr(table, "checkout_latest", None)
            if callable(checkout_latest):
                checkout_latest()
            (
                table.merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(payload)
            )

    def upsert(self, record: VectorRecord | Mapping[str, Any]) -> None:
        self.upsert_records([vector_record_to_dict(record)])

    def delete_by_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        table = self._require_table()
        quoted = ", ".join(_sql_quote(item) for item in ids)
        with _lance_write_guard(self._write_lock_path):
            checkout_latest = getattr(table, "checkout_latest", None)
            if callable(checkout_latest):
                checkout_latest()
            table.delete(f"id IN ({quoted})")

    def contains_id(self, memory_id: str) -> bool:
        """Return whether one id exists only when a scalar ``id`` index is listed.

        Ordinary replay must not call this method to maintain counts. The
        SQLite membership ledger is the supported incremental path.
        """

        found = lance_table_contains_id(self._require_table(), str(memory_id or ""))
        if found is None:
            raise RuntimeError("LanceDB cannot prove an indexed id lookup")
        return found

    def delete(self, ids: list[str]) -> int:
        existing = [str(item) for item in ids if str(item) and self.contains_id(str(item))]
        if not existing:
            return 0
        self.delete_by_ids(existing)
        return len(existing)

    def _table_rows(self, columns: list[str] | None = None) -> list[dict[str, Any]]:
        table = self._require_table()
        if hasattr(table, "to_list"):
            try:
                if columns:
                    return list(table.to_list(columns=columns))
            except TypeError:
                pass
            return list(table.to_list())
        if hasattr(table, "to_arrow"):
            arrow_table = table.to_arrow()
            if columns:
                keep = [name for name in columns if name in arrow_table.column_names]
                if keep:
                    arrow_table = arrow_table.select(keep)
            return arrow_table.to_pylist()
        if hasattr(table, "to_pandas"):
            frame = table.to_pandas()
            if columns:
                keep = [name for name in columns if name in frame.columns]
                if keep:
                    frame = frame[keep]
            return frame.to_dict(orient="records")
        raise RuntimeError("LanceDB table does not support row iteration")

    def list_ids(self) -> list[str]:
        rows = self._table_rows(columns=["id"])
        ids: list[str] = []
        for row in rows:
            memory_id = str(row.get("id") or "")
            if memory_id:
                ids.append(memory_id)
        return sorted(ids)

    def list_records(self) -> dict[str, dict[str, Any]]:
        rows = self._table_rows()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            memory_id = str(row.get("id") or "")
            if not memory_id:
                continue
            current = output.get(memory_id)
            if current is None or str(row.get("updated_at") or "") >= str(current.get("updated_at") or ""):
                output[memory_id] = row
        return output

    def sample_metadata(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        """Return a bounded metadata page without projecting the vector column.

        Projection and paging are pushed into Lance. If that cannot be proven,
        the method fails closed with an empty sample instead of materializing
        the metadata corpus.
        """

        return sample_lance_table_metadata(
            self._require_table(),
            columns=VECTOR_METADATA_COLUMNS,
            limit=limit,
            offset=offset,
        )

    def audit_counts(self) -> dict[str, int]:
        ids = self.list_ids()
        counts = Counter(ids)
        unique_id_count = len(counts)
        duplicate_rows = sum(count - 1 for count in counts.values() if count > 1)
        duplicate_id_count = sum(1 for count in counts.values() if count > 1)
        return {
            "physical_rows": len(ids),
            "unique_ids": unique_id_count,
            "duplicate_rows": duplicate_rows,
            "duplicate_ids": duplicate_id_count,
        }

    def repair_records(self, desired_records: dict[str, dict[str, Any]]) -> int:
        """Refuse in-place full rewrites of an active LanceDB table.

        A rewrite cannot be made failure-atomic with the ordinary table API:
        deleting the current table before the replacement is validated can
        destroy a healthy generation.  Explicit migration code must build a
        separate generation, validate it, then CAS-switch the durable pointer.
        """

        del desired_records
        raise VectorStoreCompatibilityError(
            "in-place LanceDB repair is disabled; build and activate a shadow vector generation explicitly"
        )

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]:
        if not vector:
            return []
        table = self._require_table()
        query = table.search(vector).metric(self._metric).where(f"scope_id = {_sql_quote(scope_id)}")
        return query.limit(int(limit)).to_list()

    def count_rows(self) -> int:
        table = self._require_table()
        return int(table.count_rows())

    def close(self) -> None:
        self._table = None
        self._db = None

    def _require_table(self):
        if self._table is None:
            raise RuntimeError("vector table is not open")
        return self._table


def normalize_vector_backend(value: Any) -> str:
    backend = str(value or "lancedb").strip().lower()
    if backend == "sqlite":
        return "sqlite-bruteforce"
    return backend


def build_vector_store(
    backend: str,
    *,
    storage_dir: Path,
    table_name: str,
    dimensions: int,
    metric: str = "cosine",
    config: Mapping[str, Any] | None = None,
) -> VectorStore:
    """Build a vector companion store without opening it.

    The factory centralizes backend selection while preserving the existing store
    classes and their rebuildable-cache contract.
    """
    normalized = normalize_vector_backend(backend)
    if normalized == "sqlite-bruteforce":
        from .sqlite_vector_store import SQLiteBruteForceVectorStore

        db_path = Path(storage_dir) / "vector.sqlite3"
        return SQLiteBruteForceVectorStore(db_path, table_name=table_name, dimensions=dimensions, metric=metric)
    if normalized == "lancedb":
        vector_dir = Path(storage_dir) / "lancedb"
        return LanceVectorStore(vector_dir, table_name=table_name, dimensions=dimensions, metric=metric)
    if normalized == "pgvector":
        from .pgvector_store import PGVectorStore

        pg_config = dict((config or {}).get("pgvector") or {}) if isinstance(config, Mapping) else {}
        return PGVectorStore(
            dsn_env=str(pg_config.get("dsn_env") or "SCOPE_RECALL_PGVECTOR_DSN"),
            table_name=str(pg_config.get("table_name") or table_name or "scope_recall_vectors"),
            dimensions=dimensions,
            metric=metric,
            connect_timeout_seconds=int(
                pg_config.get("connect_timeout_seconds") or 10
            ),
            statement_timeout_ms=int(
                pg_config.get("statement_timeout_ms") or 30_000
            ),
            lock_timeout_ms=int(pg_config.get("lock_timeout_ms") or 5_000),
        )
    raise ValueError(f"unsupported vector backend: {backend}")
