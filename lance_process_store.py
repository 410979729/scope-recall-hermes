"""Contain Windows Arrow/Lance native failures in a private local worker.

SQLite truth and generation identities remain in the host. The worker owns only
the selected Lance table and speaks a bounded, sequential JSON protocol over
anonymous pipes. A crash/timeout is never retried here: writes with an uncertain
physical outcome remain owned by the existing idempotent vector outbox.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import weakref
from pathlib import Path
from typing import Any, Iterable, Mapping

from .vector_store import VectorRecord, VectorStoreCompatibilityError, _python_subprocess_options, vector_record_to_dict

MAX_LANCE_FRAME_BYTES = 64 * 1024 * 1024
LANCE_WORKER_METHODS = frozenset({
    "is_available", "open", "open_existing", "open_existing_for_update",
    "close", "upsert_records", "delete", "delete_by_ids", "contains_id",
    "list_ids", "list_records", "sample_metadata", "repair_records", "search",
    "count_rows", "audit_counts", "id_lookup_indexed",
})


def _worker_command() -> list[str]:
    return [sys.executable, str(Path(__file__).with_name("_lance_worker.py"))]


def _write_worker_frame(stream: Any, encoded: bytes, output: queue.Queue) -> None:
    try:
        stream.write(encoded)
        stream.flush()
    except (OSError, ValueError):
        try:
            output.put(None, timeout=1)
        except queue.Full:
            pass


def _stop_worker(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            # The worker can exit between poll() and TerminateProcess().
            if process.poll() is None:
                raise
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                if process.poll() is None:
                    raise
            process.wait(timeout=3)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


def _read_worker_frames(stream: Any, output: queue.Queue) -> None:
    try:
        while True:
            line = stream.readline(MAX_LANCE_FRAME_BYTES + 1)
            if not line or len(line) > MAX_LANCE_FRAME_BYTES:
                break
            output.put(json.loads(line), timeout=1)
    except (ValueError, OSError, queue.Full):
        pass
    finally:
        try:
            output.put(None, timeout=1)
        except queue.Full:
            pass


class ProcessLanceVectorStore:
    """VectorStore implementation with no Lance/PyArrow imports in the host."""

    def __init__(self, db_path: Path, *, table_name: str, dimensions: int, metric: str = "cosine") -> None:
        self.db_path = Path(db_path)
        self.table_name = table_name
        self.dimensions = int(dimensions)
        self.backend = "lancedb"
        self._metric = metric
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._sender: threading.Thread | None = None
        self._responses: queue.Queue = queue.Queue(maxsize=2)
        self._finalizer: Any = None
        self._failed = False
        self._closed = False
        self._sequence = 0
        self._request_timeout = 60.0

    def _start(self) -> None:
        self._process = subprocess.Popen(
            _worker_command(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            **_python_subprocess_options(),
        )
        self._finalizer = weakref.finalize(self, _stop_worker, self._process)
        self._reader = threading.Thread(
            target=_read_worker_frames, args=(self._process.stdout, self._responses),
            name="scope-recall-lance-pipe", daemon=True,
        )
        self._reader.start()

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method not in LANCE_WORKER_METHODS:
            raise ValueError("unsupported native vector operation")
        with self._lock:
            if self._failed or self._closed:
                raise RuntimeError("native vector worker is closed; reopen the vector runtime explicitly")
            self._sequence += 1
            request = {
                "id": self._sequence, "method": method, "args": args, "kwargs": kwargs,
                "store": {"db_path": str(self.db_path), "table_name": self.table_name,
                          "dimensions": self.dimensions, "metric": self._metric},
            }
            encoded = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > MAX_LANCE_FRAME_BYTES:
                raise ValueError("native vector request exceeds the 64 MiB frame limit")
            try:
                if self._process is None:
                    self._start()
                assert self._process is not None and self._process.stdin is not None
                # A wedged native worker may stop reading stdin. Include pipe
                # backpressure in the deadline rather than blocking on write.
                self._sender = threading.Thread(
                    target=_write_worker_frame,
                    args=(self._process.stdin, encoded, self._responses), daemon=True,
                    name="scope-recall-lance-send",
                )
                self._sender.start()
                response = self._responses.get(timeout=self._request_timeout)
                if not isinstance(response, dict) or response.get("id") != self._sequence:
                    raise RuntimeError("native vector worker exited or returned an invalid frame")
            except (OSError, ValueError, queue.Empty, RuntimeError) as exc:
                self._failed = True
                self.close()
                raise RuntimeError(
                    "native vector worker failed; SQLite truth is intact and unacknowledged outbox work remains pending"
                ) from exc
            if not response.get("ok"):
                error_type = response.get("error_type")
                message = str(response.get("error") or "native vector operation failed")
                if error_type == "VectorStoreCompatibilityError":
                    raise VectorStoreCompatibilityError(message)
                if error_type == "FileNotFoundError":
                    raise FileNotFoundError(message)
                raise RuntimeError(message)
            return response.get("result")

    @property
    def requires_reopen(self) -> bool:
        """True after a transport failure, until an explicit open resets it."""
        return self._failed

    @property
    def id_lookup_indexed(self) -> bool:
        return bool(self._call("id_lookup_indexed"))

    def is_available(self) -> bool:
        return bool(self._call("is_available"))

    def _reopen(self) -> None:
        if self._closed:
            self._process = None
            self._reader = None
            self._sender = None
            self._responses = queue.Queue(maxsize=2)
            self._failed = False
            self._closed = False

    def open(self) -> None:
        with self._lock:
            self._reopen()
            self._call("open")

    def open_existing(self) -> None:
        with self._lock:
            self._reopen()
            self._call("open_existing")

    def open_existing_for_update(self) -> None:
        with self._lock:
            self._reopen()
            self._call("open_existing_for_update")

    def upsert(self, record: VectorRecord | Mapping[str, Any]) -> None:
        self.upsert_records([vector_record_to_dict(record)])

    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None:
        self._call("upsert_records", list(rows))

    def delete(self, ids: list[str]) -> int:
        return int(self._call("delete", ids))

    def delete_by_ids(self, ids: list[str]) -> None:
        self._call("delete_by_ids", ids)

    def contains_id(self, memory_id: str) -> bool:
        return bool(self._call("contains_id", memory_id))

    def list_ids(self) -> list[str]:
        return self._call("list_ids")

    def list_records(self) -> dict[str, dict[str, Any]]:
        return self._call("list_records")

    def sample_metadata(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        return self._call("sample_metadata", limit=limit, offset=offset)

    def repair_records(self, desired_records: dict[str, dict[str, Any]]) -> int:
        return int(self._call("repair_records", desired_records))

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]:
        return self._call("search", vector, scope_id=scope_id, limit=limit)

    def count_rows(self) -> int:
        return int(self._call("count_rows"))

    def audit_counts(self) -> dict[str, int]:
        return self._call("audit_counts")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._finalizer is not None and self._finalizer.alive:
                self._finalizer()
            for thread in (self._sender, self._reader):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=3)
