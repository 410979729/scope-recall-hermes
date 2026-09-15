"""Contain Windows Arrow/Lance native failures in a private local worker.

SQLite truth and generation identities remain in the host. The worker owns only
the selected Lance table and speaks a bounded, sequential JSON protocol over
anonymous pipes. A crash/timeout is never retried here: writes with an uncertain
physical outcome remain owned by the existing idempotent vector outbox.
"""

from __future__ import annotations

import json
import math
import os
import queue
import secrets
import subprocess
import sys
import threading
import weakref
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ._internal.recall.deadline import RequestDeadline, current_request_deadline, remaining_seconds, using_request_deadline
from .vector_store import VectorRecord, VectorStoreCompatibilityError, _python_subprocess_options, vector_record_to_dict

MAX_LANCE_FRAME_BYTES = 64 * 1024 * 1024
# Lance's Rust object writer creates additional temporary/data path suffixes.
# Keep the projected Lance data object within legacy Win32 MAX_PATH instead of
# relying on the extended path prefix, which the bundled Lance writer does not
# preserve.  The projection already includes Lance's fixed data filename.
_WINDOWS_NATIVE_PATH_SAFE_LIMIT = 260
_WINDOWS_NATIVE_OBJECT_NAME = "0" * 64 + ".lance"


class NativeVectorPathError(VectorStoreCompatibilityError):
    code = "native_vector_path_too_long"


def _windows_utf16_units(value: str) -> int:
    """Count the path units used by the Win32 native boundary."""

    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2
LANCE_WORKER_METHODS = frozenset({
    "purge_governed_members",
    "is_available", "open", "open_existing", "open_existing_for_update",
    "close", "upsert_records", "fenced_upsert_records", "delete", "delete_by_ids", "contains_id",
    "list_ids", "list_records", "sample_metadata", "repair_records", "search",
    "count_rows", "audit_counts", "id_lookup_indexed", "compact",
})


def _worker_command() -> list[str]:
    # The worker installs a small ``scope_recall`` package alias itself, so it
    # does not need project ``PYTHONPATH`` entries.  Isolated mode prevents a
    # source-tree directory named ``packaging`` (or another dependency name)
    # from shadowing the wheel installed in the selected interpreter.
    return [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).with_name("_lance_worker.py")),
    ]


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

    def native_path_error(self) -> str | None:
        """Return a stable gap before Lance creates any native file.

        LanceDB's Windows object writer appends table/data/temp components to
        the configured root and still uses ordinary Win32 paths.  A long path
        prefix is therefore insufficient; preflight the conservative object
        path and fail before starting the helper or mutating the index.
        """

        if os.name != "nt":
            return None
        root = Path(os.path.abspath(os.fspath(self.db_path)))
        object_path = root / f"{self.table_name}.lance" / "data" / _WINDOWS_NATIVE_OBJECT_NAME
        # Win32 path limits are expressed in UTF-16 code units rather than
        # Python Unicode scalar values.  Include the deterministic object
        # suffix Lance uses for its first data file so this check runs before
        # the native helper can create a writer or a temporary file.
        length = _windows_utf16_units(str(object_path))
        if length >= _WINDOWS_NATIVE_PATH_SAFE_LIMIT:
            return (
                "native_vector_path_too_long:"
                f"path_length={length};safe_limit={_WINDOWS_NATIVE_PATH_SAFE_LIMIT}"
            )
        return None

    def _raise_if_native_path_unsafe(self) -> None:
        error = self.native_path_error()
        if error is not None:
            raise NativeVectorPathError(error)

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
        if method not in {"is_available", "close"}:
            self._raise_if_native_path_unsafe()
        if not self._acquire_helper_lock():
            raise _helper_lock_timeout()
        try:
            return self._invoke_locked(method, *args, **kwargs)
        finally:
            self._lock.release()

    def _invoke_locked(self, method: str, *args: Any, _during_wait=None, **kwargs: Any) -> Any:
        if method not in {"is_available", "close"}:
            self._raise_if_native_path_unsafe()
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
        work_failed = False
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
            if _during_wait is not None:
                try:
                    _during_wait()
                except BaseException:
                    work_failed = True
                    self._reap_owned_helper(failed=True)
                    raise
                if remaining_seconds() is not None and remaining_seconds() <= 0:
                    raise queue.Empty
            wait = self._request_timeout
            if remaining is not None:
                wait = min(wait, max(0.0, remaining_seconds() or 0.0))
            response = self._responses.get(timeout=wait)
            if not isinstance(response, dict) or response.get("id") != self._sequence:
                raise RuntimeError("native vector worker exited or returned an invalid frame")
        except (OSError, ValueError, queue.Empty, RuntimeError) as exc:
            if work_failed:
                raise
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

    def _send_fence_frame(self, encoded: bytes, timeout: float) -> None:
        """Write one handshake frame with the same cumulative deadline."""

        process = self._process
        stdin = process.stdin if process is not None else None
        if process is None or stdin is None:
            raise RuntimeError("native vector worker is not running")
        outcome: queue.Queue[bool] = queue.Queue(maxsize=1)

        def send() -> None:
            try:
                stdin.write(encoded)
                stdin.flush()
                outcome.put(True)
            except (OSError, ValueError):
                try:
                    outcome.put(False)
                except queue.Full:
                    pass

        sender = threading.Thread(target=send, daemon=True, name="scope-recall-lance-fence-send")
        self._sender = sender
        sender.start()
        sender.join(max(0.0, timeout))
        if sender.is_alive() or not outcome.get(False):
            raise RuntimeError("native vector fence handshake send failed")

    def _invoke_fenced_locked(self, rows: list[dict[str, Any]], guard: Callable[[], bool]) -> bool:
        if self._failed or self._closed:
            raise RuntimeError("native vector worker is closed; reopen the vector runtime explicitly")
        remaining = remaining_seconds()
        if remaining is not None and remaining <= 0.0:
            raise RuntimeError("native vector helper request deadline exhausted")
        self._sequence += 1
        request_id = self._sequence
        nonce = secrets.token_hex(16)
        request = {
            "id": request_id,
            "method": "fenced_upsert_records",
            "args": [rows, nonce],
            "kwargs": {"guard_timeout_seconds": remaining},
            "store": {
                "db_path": str(self.db_path),
                "table_name": self.table_name,
                "dimensions": self.dimensions,
                "metric": self._metric,
            },
        }
        encoded = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_LANCE_FRAME_BYTES:
            raise ValueError("native vector request exceeds the 64 MiB frame limit")
        try:
            if self._process is None:
                self._start()
            wait = self._request_timeout
            if remaining is not None:
                wait = min(wait, max(0.0, remaining_seconds() or 0.0))
            self._send_fence_frame(encoded, wait)
            # Sending can consume a material part of the shared budget.  Do
            # not reuse the pre-send timeout for the guard request.
            remaining_after_send = remaining_seconds()
            if remaining_after_send is not None and remaining_after_send <= 0.0:
                raise RuntimeError("native vector fence deadline exhausted before guard")
            wait = self._request_timeout if remaining_after_send is None else min(
                self._request_timeout, max(0.0, remaining_after_send)
            )
            frame = self._responses.get(timeout=wait)
            if not isinstance(frame, dict) or frame.get("id") != request_id or frame.get("kind") != "guard_request" or frame.get("nonce") != nonce:
                raise RuntimeError("native vector fence guard request mismatch")
            approved = False
            try:
                approved = bool(guard())
            except Exception:
                approved = False
            remaining = remaining_seconds()
            if remaining is not None and remaining <= 0.0:
                approved = False
            reply = json.dumps(
                {"id": request_id, "kind": "guard_result", "nonce": nonce, "approved": approved},
                ensure_ascii=False,
            ).encode("utf-8") + b"\n"
            wait = self._request_timeout if remaining is None else min(self._request_timeout, max(0.0, remaining))
            self._send_fence_frame(reply, wait)
            remaining_after_reply = remaining_seconds()
            if remaining_after_reply is not None and remaining_after_reply <= 0.0:
                raise RuntimeError("native vector fence deadline exhausted before final")
            wait = self._request_timeout if remaining_after_reply is None else min(
                self._request_timeout, max(0.0, remaining_after_reply)
            )
            final = self._responses.get(timeout=wait)
            if not isinstance(final, dict) or final.get("id") != request_id:
                raise RuntimeError("native vector fence final response mismatch")
        except (OSError, ValueError, queue.Empty, RuntimeError) as exc:
            self._reap_owned_helper(failed=True)
            raise RuntimeError("native vector fence failed; physical outcome is uncertain") from exc
        if not final.get("ok"):
            error_type = final.get("error_type")
            message = str(final.get("error") or "native vector fence failed")
            if error_type == "VectorStoreCompatibilityError":
                raise VectorStoreCompatibilityError(message)
            raise RuntimeError(message)
        return bool(final.get("result"))

    def fenced_upsert_records(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        guard: Callable[[], bool],
        remaining_seconds: float,
    ) -> bool:
        """Guard an upsert after native lock acquisition and before commit."""

        if not callable(guard):
            raise TypeError("guard must be callable")
        if type(remaining_seconds) not in (int, float) or not math.isfinite(float(remaining_seconds)) or remaining_seconds <= 0:
            raise RuntimeError("native vector fence deadline exhausted")
        self._raise_if_native_path_unsafe()
        deadline = RequestDeadline.from_budget(float(remaining_seconds))
        outer = current_request_deadline()
        if outer is not None and outer.deadline_monotonic < deadline.deadline_monotonic:
            deadline = outer
        with using_request_deadline(deadline):
            if not self._acquire_helper_lock():
                raise _helper_lock_timeout()
            try:
                return self._invoke_fenced_locked(list(rows), guard)
            finally:
                self._lock.release()

    def purge_governed_members(self, *, members, agent_id, installation_id,
                               partitions, project_id, branch_id,
                               remaining_seconds: float) -> bool:
        """One cumulative budget covers locking, inventory, deletion and ACK."""
        if type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
            raise RuntimeError("native vector purge deadline exhausted")
        deadline = RequestDeadline.from_budget(float(remaining_seconds))
        outer = current_request_deadline()
        if outer is not None and outer.deadline_monotonic < deadline.deadline_monotonic:
            deadline = outer
        with using_request_deadline(deadline):
            if not self._acquire_helper_lock():
                raise _helper_lock_timeout()
            try:
                budget = deadline.remaining()
                if budget <= 0:
                    raise RuntimeError("native vector purge deadline exhausted")
                return self._invoke_locked(
                    "purge_governed_members", members=members, agent_id=agent_id,
                    installation_id=installation_id, partitions=partitions,
                    project_id=project_id, branch_id=branch_id, budget_seconds=budget,
                ) is True
            finally:
                self._lock.release()

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

    def _open_after_owned_reaper(self, method: str, *, during_wait=None) -> None:
        self._await_owned_reaper_before_start()
        if not self._acquire_helper_lock():
            raise _helper_lock_timeout()
        try:
            if self._owned_reaper is not None:
                raise _helper_teardown_pending()
            self._reopen()
            self._invoke_locked(method, _during_wait=during_wait)
        finally:
            self._lock.release()

    def open(self) -> None:
        self._open_after_owned_reaper("open")

    def open_existing(self) -> None:
        self._open_after_owned_reaper("open_existing")

    def open_existing_with_work(self, work) -> None:
        """Overlap read-only native opening with caller-owned bounded work."""
        if not callable(work):
            raise TypeError('open work must be callable')
        if not (self.db_path / f'{self.table_name}.lance').is_dir():
            raise FileNotFoundError('LanceDB physical storage is missing')
        if remaining_seconds() is None:
            raise ValueError('overlapped open requires an absolute request deadline')
        self._open_after_owned_reaper('open_existing', during_wait=work)

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

    def compact(self) -> dict[str, int]:
        """Forward a bounded compaction to the helper that owns the table."""
        result = self._call("compact")
        return dict(result) if isinstance(result, dict) else {}

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
