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

from ._internal.recall.deadline import remaining_seconds
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


def _helper_lock_timeout() -> RuntimeError:
    return RuntimeError(
        "native vector helper lock timeout; SQLite truth is intact and the active helper was not interrupted"
    )


class _OwnedHelperReaper:
    """Handle and outcome for one detached helper teardown.

    Foreground callers may stop waiting at the request deadline, but the store
    keeps this object until the process is gone so close/open cannot abandon it.
    """

    __slots__ = ("thread", "process", "error")

    def __init__(self, process: subprocess.Popen | None) -> None:
        self.thread: threading.Thread | None = None
        self.process = process
        self.error: BaseException | None = None


def _helper_teardown_pending() -> RuntimeError:
    return RuntimeError(
        "native vector helper teardown is still pending; "
        "SQLite truth is intact and the Lance path was not reopened"
    )


def _helper_teardown_failed(cause: BaseException) -> RuntimeError:
    error = RuntimeError(
        "native vector helper teardown failed; "
        "SQLite truth is intact and unacknowledged outbox work remains pending"
    )
    error.__cause__ = cause
    return error


def _reap_detached_helper(
    process: subprocess.Popen | None,
    reader: threading.Thread | None,
    sender: threading.Thread | None,
) -> None:
    if process is not None:
        _stop_worker(process)
    for thread in (sender, reader):
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=3)


def _run_owned_reaper(
    owned: _OwnedHelperReaper,
    reader: threading.Thread | None,
    sender: threading.Thread | None,
) -> None:
    try:
        _reap_detached_helper(owned.process, reader, sender)
    except BaseException as exc:
        owned.error = exc


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
        self._owned_reaper: _OwnedHelperReaper | None = None
        self._sequence = 0
        self._request_timeout = 60.0

    def _acquire_helper_lock(self) -> bool:
        """Wait only for the remaining request budget before owning the helper.

        Failure here does not start, send, close, or mark the helper failed.
        """

        remaining = remaining_seconds()
        if remaining is None:
            self._lock.acquire()
            return True
        if remaining <= 0.0:
            return bool(self._lock.acquire(blocking=False))
        return bool(self._lock.acquire(timeout=remaining))

    def _reap_owned_helper(self, *, failed: bool) -> None:
        if failed:
            self._failed = True
        self._closed = True
        if self._owned_reaper is not None:
            return
        process = self._process
        reader = self._reader
        sender = self._sender
        finalizer = self._finalizer
        self._process = None
        self._reader = None
        self._sender = None
        self._responses = queue.Queue(maxsize=2)
        self._finalizer = None
        if finalizer is not None:
            finalizer.detach()
        owned = _OwnedHelperReaper(process)
        thread = threading.Thread(
            target=_run_owned_reaper,
            args=(owned, reader, sender),
            name="scope-recall-lance-reap",
            daemon=True,
        )
        owned.thread = thread
        self._owned_reaper = owned
        thread.start()

    def _owned_reaper_incomplete(self, owned: _OwnedHelperReaper) -> bool:
        thread = owned.thread
        if thread is not None and thread.is_alive():
            return True
        if owned.process is not None and owned.process.poll() is None:
            return True
        return owned.error is not None

    def _complete_owned_reaper(self, *, timeout: float | None, retry_stop: bool) -> None:
        """Join a detached reaper without holding the helper lock.

        The reaper never acquires ``_lock``. Waiting here while that lock is
        held would block other callers for the whole teardown, and a later
        reaper that needed the lock would deadlock.
        """

        owned = self._owned_reaper
        if owned is None:
            return
        thread = owned.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            if timeout is None:
                thread.join()
            elif timeout > 0.0:
                thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            raise _helper_teardown_pending()
        process = owned.process
        retried = False
        if retry_stop and process is not None and process.poll() is None:
            retried = True
            try:
                _stop_worker(process)
            except Exception as exc:
                if owned.error is None:
                    owned.error = exc
        error = owned.error
        if process is not None and process.poll() is None:
            if error is not None:
                raise _helper_teardown_failed(error)
            raise _helper_teardown_pending()
        if self._owned_reaper is owned:
            self._owned_reaper = None
        if error is not None and not retried:
            raise _helper_teardown_failed(error)

    def _await_owned_reaper_before_start(self) -> None:
        owned = self._owned_reaper
        if owned is None or not self._owned_reaper_incomplete(owned):
            if owned is not None:
                self._owned_reaper = None
            return
        remaining = remaining_seconds()
        if remaining is not None and remaining <= 0.0:
            raise _helper_teardown_pending()
        self._complete_owned_reaper(timeout=remaining, retry_stop=False)

    def _start(self) -> None:
        if self._owned_reaper is not None:
            raise _helper_teardown_pending()
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
        if not self._acquire_helper_lock():
            raise _helper_lock_timeout()
        try:
            return self._invoke_locked(method, *args, **kwargs)
        finally:
            self._lock.release()

    def _invoke_locked(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._failed or self._closed:
            raise RuntimeError("native vector worker is closed; reopen the vector runtime explicitly")
        remaining = remaining_seconds()
        if remaining is not None and remaining <= 0.0:
            raise RuntimeError(
                "native vector helper request deadline exhausted; SQLite truth is intact and the active helper was not interrupted"
            )
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
            wait = self._request_timeout
            if remaining is not None:
                wait = min(wait, max(0.0, remaining_seconds() or 0.0))
            response = self._responses.get(timeout=wait)
            if not isinstance(response, dict) or response.get("id") != self._sequence:
                raise RuntimeError("native vector worker exited or returned an invalid frame")
        except (OSError, ValueError, queue.Empty, RuntimeError) as exc:
            if remaining_seconds() is None:
                self._failed = True
                self.close()
            else:
                self._reap_owned_helper(failed=True)
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
        if self._owned_reaper is not None:
            raise _helper_teardown_pending()
        if self._closed:
            self._process = None
            self._reader = None
            self._sender = None
            self._responses = queue.Queue(maxsize=2)
            self._failed = False
            self._closed = False

    def _open_after_owned_reaper(self, method: str) -> None:
        self._await_owned_reaper_before_start()
        if not self._acquire_helper_lock():
            raise _helper_lock_timeout()
        try:
            if self._owned_reaper is not None:
                raise _helper_teardown_pending()
            self._reopen()
            self._invoke_locked(method)
        finally:
            self._lock.release()

    def open(self) -> None:
        self._open_after_owned_reaper("open")

    def open_existing(self) -> None:
        self._open_after_owned_reaper("open_existing")

    def open_existing_for_update(self) -> None:
        self._open_after_owned_reaper("open_existing_for_update")

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
        if not self._acquire_helper_lock():
            raise _helper_lock_timeout()
        join_reaper = False
        try:
            self._closed = True
            if remaining_seconds() is None:
                if self._owned_reaper is not None:
                    join_reaper = True
                else:
                    if self._finalizer is not None and self._finalizer.alive:
                        self._finalizer()
                    for thread in (self._sender, self._reader):
                        if thread is not None and thread is not threading.current_thread():
                            thread.join(timeout=3)
            elif self._owned_reaper is None:
                self._reap_owned_helper(failed=False)
        finally:
            self._lock.release()
        if join_reaper:
            self._complete_owned_reaper(timeout=None, retry_stop=True)
