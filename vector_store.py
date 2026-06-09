from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _optional_lancedb():
    try:
        import lancedb  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        return None
    return lancedb


def _optional_pyarrow():
    try:
        import pyarrow as pa  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        return None
    return pa



def _sql_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class LanceVectorStore:
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

    def is_available(self) -> bool:
        return _optional_lancedb() is not None and _optional_pyarrow() is not None

    def open(self) -> None:
        lancedb = _optional_lancedb()
        if lancedb is None or _optional_pyarrow() is None:
            raise RuntimeError("lancedb/pyarrow is not installed")
        self._db_path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self._db_path))
        self._table = self._open_or_create_table()

    def _open_or_create_table(self):
        assert self._db is not None
        try:
            listed = self._db.list_tables()
            tables = set(getattr(listed, "tables", listed))
        except Exception:
            try:
                tables = set(self._db.table_names())
            except Exception:
                tables = set()
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
        if required <= existing:
            return
        if self._db is None:
            raise RuntimeError("vector database is not open")
        self._db.drop_table(self._table_name, ignore_missing=True)
        self._table = self._db.create_table(self._table_name, schema=self._schema())

    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None:
        self._ensure_schema_compatible()
        table = self._require_table()
        payload = list(rows)
        if not payload:
            return
        ids = [str(row.get("id") or "") for row in payload if row.get("id")]
        if ids:
            self.delete_by_ids(ids)
        table.add(payload)

    def delete_by_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        table = self._require_table()
        quoted = ", ".join(_sql_quote(item) for item in ids)
        table.delete(f"id IN ({quoted})")

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
        return ids

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
        """Rewrite the Lance table to exactly one newest row per desired id.

        SQLite remains the source of truth; this method only repairs the vector
        companion by removing stale ids and duplicate physical rows.
        """
        if self._db is None:
            raise RuntimeError("vector database is not open")
        rows = self._table_rows()
        latest_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            memory_id = str(row.get("id") or "")
            if not memory_id or memory_id not in desired_records:
                continue
            current = latest_by_id.get(memory_id)
            if current is None or str(row.get("updated_at") or "") >= str(current.get("updated_at") or ""):
                latest_by_id[memory_id] = row

        repaired: list[dict[str, Any]] = []
        for memory_id, desired in desired_records.items():
            row = latest_by_id.get(memory_id)
            if row is None:
                continue
            if str(row.get("updated_at") or "") != str(desired.get("updated_at") or ""):
                continue
            repaired.append(row)

        self._db.drop_table(self._table_name, ignore_missing=True)
        schema = self._schema()
        if repaired:
            pa = _optional_pyarrow()
            if pa is None:
                raise RuntimeError("pyarrow is not installed")
            self._table = self._db.create_table(self._table_name, data=pa.Table.from_pylist(repaired, schema=schema))
        else:
            self._table = self._db.create_table(self._table_name, schema=schema)
        return len(repaired)

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
