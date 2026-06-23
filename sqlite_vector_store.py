from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


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

    def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema()
            stored_dimensions = self._get_meta_int("dimensions")
            stored_table = self._get_meta_text("table_name")
            if (stored_dimensions and stored_dimensions != self._dimensions) or (stored_table and stored_table != self._table_name):
                self._conn.execute("DELETE FROM vector_records")
            self._set_meta("dimensions", str(self._dimensions))
            self._set_meta("table_name", self._table_name)
            self._conn.commit()

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

    def delete_by_ids(self, ids: list[str]) -> None:
        ids = [str(item) for item in ids if str(item)]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            conn = self._require_conn()
            conn.execute(f"DELETE FROM vector_records WHERE id IN ({placeholders})", ids)
            conn.commit()

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
