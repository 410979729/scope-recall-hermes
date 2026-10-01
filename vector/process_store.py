"""Contain Windows Arrow/Lance native failures in a private local worker.

SQLite truth and generation identities remain in the host.  The worker owns
only the selected Lance table and speaks a bounded, sequential JSON protocol
over anonymous pipes.  A crash or timeout is never retried here: a write with
an uncertain physical outcome stays owned by the idempotent vector outbox.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from ..core.deadline import RequestDeadline, current_request_deadline, remaining_seconds, using_request_deadline
from . import VectorStore, VectorStoreCompatibilityError
from .lance_native import python_subprocess_options

MAX_LANCE_FRAME_BYTES = 64 * 1024 * 1024
LANCE_WORKER_METHODS = frozenset({
    "is_available", "open", "open_existing", "upsert_records", "fenced_upsert_records",
    "delete_by_ids", "contains_id", "list_ids", "list_records", "search", "search_scopes", "count_rows",
    "compact", "ensure_vector_index", "purge_governed_members",
})
# Lance's Rust object writer appends table/data/temp components to the root and
# uses ordinary Win32 paths, so the extended-length prefix does not help.  The
# projected first data object must stay within legacy MAX_PATH, counted in
# UTF-16 units as Win32 does.
_WINDOWS_NATIVE_PATH_SAFE_LIMIT = 260
_WINDOWS_NATIVE_OBJECT_NAME = "0" * 64 + ".lance"


class NativeVectorPathError(VectorStoreCompatibilityError):
    code = "native_vector_path_too_long"


class _RequestBudgetExpired(TimeoutError):
    """The caller stopped waiting, but the sequential helper remains healthy."""


def _helper_lock_timeout() -> RuntimeError:
    return RuntimeError(
        "native vector helper lock timeout; SQLite truth is intact and the active helper was not interrupted"
    )


def _request_budget_expired() -> _RequestBudgetExpired:
    return _RequestBudgetExpired(
        "native vector helper request deadline exhausted; "
        "SQLite truth is intact and the active helper was not interrupted"
    )


def _helper_teardown_pending() -> RuntimeError:
    return RuntimeError(
        "native vector helper teardown is still pending; SQLite truth is intact and the Lance path was not reopened"
    )


def _helper_teardown_failed(cause: BaseException) -> RuntimeError:
    error = RuntimeError(
        "native vector helper teardown failed; SQLite truth is intact and unacknowledged outbox work remains pending"
    )
    error.__cause__ = cause
    return error


def _worker_failed() -> RuntimeError:
    return RuntimeError(
        "native vector worker failed; SQLite truth is intact and unacknowledged outbox work remains pending"
    )


def _fence_failed() -> RuntimeError:
    return RuntimeError("native vector fence failed; physical outcome is uncertain")


def _remote_failure(error_type: Any, message: str) -> RuntimeError:
    """A helper-side failure keeps the helper's own name for it.

    ``_lance_worker`` reports the exception class as ``error_type``; carrying
    it on the RuntimeError is what lets recall gaps and worker outcomes say
    which fault it was instead of the bare class.
    """
    error = RuntimeError(message)
    if type(error_type) is str and 0 < len(error_type) <= 64 and error_type.isascii() and error_type.replace("_", "").isalnum():
        error.error_type = error_type
    return error


def _worker_command() -> list[str]:
    # The worker installs its own ``scope_recall`` alias, so it needs no
    # PYTHONPATH; isolated mode keeps a source-tree directory such as
    # ``packaging`` from shadowing the wheel installed in the interpreter.
    return [sys.executable, "-I", "-B", str(Path(__file__).resolve().parents[1] / "_lance_worker.py")]


def _spawn_helper() -> subprocess.Popen:
    return subprocess.Popen(
        _worker_command(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        **python_subprocess_options(),
    )


#: A helper started before any store asked for one (``prestart``), for the next store that starts a helper.
_spare: subprocess.Popen | None = None
_spare_lock = threading.Lock()


def prestart() -> None:
    """Start a helper now, for the next store that starts one to take.

    A helper spends about 2 s importing LanceDB before it answers.  A Claude Code or Codex hook is a new process
    for every prompt, and a helper started when its recall reached the vector search was not ready before that
    search's budget ran out: those hosts' automatic recall answered from words alone.  Started when the hook
    starts, the import runs while the message is stored and the words are searched.  A server that runs on starts
    one for the store all its runtimes share (``share``); it kept a spare as well, replaced each time one was taken,
    about 0.55 GB of committed memory idle once the shared store holds its helper (3.4.9).
    """
    global _spare
    with _spare_lock:
        if _spare is None or _spare.poll() is not None:
            _spare = _spawn_helper()


def discard_spare() -> None:
    """Let a spare that was never taken go: without its input it exits once its import is done."""
    global _spare
    with _spare_lock:
        spare, _spare = _spare, None
    if spare is not None:
        for stream in (spare.stdin, spare.stdout):
            if stream is not None:
                stream.close()


atexit.register(discard_spare)


def _take_spare() -> subprocess.Popen | None:
    global _spare
    with _spare_lock:
        spare, _spare = _spare, None
    if spare is not None and spare.poll() is not None:
        # It is gone already (its import failed): the store starts its own and meets the same failure there.
        for stream in (spare.stdin, spare.stdout):
            if stream is not None:
                stream.close()
        return None
    return spare


def _budget_exhausted() -> bool:
    remaining = remaining_seconds()
    return remaining is not None and remaining <= 0.0


@contextmanager
def _request_budget(seconds: float) -> Iterator[RequestDeadline]:
    """Bound a call by ``seconds`` or the caller's own deadline, whichever ends first."""
    deadline = RequestDeadline.from_budget(float(seconds))
    outer = current_request_deadline()
    if outer is not None and outer.deadline_monotonic < deadline.deadline_monotonic:
        deadline = outer
    with using_request_deadline(deadline):
        yield deadline


def _put_eof(output: queue.Queue) -> None:
    """``None`` on the response queue tells the waiting caller the pipe is gone."""
    try:
        output.put(None, timeout=1)
    except queue.Full:
        pass


def _write_worker_frame(stream: Any, encoded: bytes, output: queue.Queue) -> None:
    try:
        stream.write(encoded)
        stream.flush()
    except (OSError, ValueError):
        _put_eof(output)


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
        _put_eof(output)


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


def _join_helper_threads(*threads: threading.Thread | None) -> None:
    for thread in threads:
        if thread is not None:
            thread.join(timeout=3)


class _HelperTeardown:
    """Stop one detached helper on a background thread.

    A foreground caller may stop waiting at its request deadline, but the
    store keeps this object until the process is gone so close/open cannot
    abandon a live helper.  The thread never takes the store lock; waiting on
    it while holding that lock would block every other caller for the whole
    teardown.
    """

    def __init__(self, process: subprocess.Popen | None, threads: tuple[threading.Thread | None, ...]) -> None:
        self.process = process
        self.error: BaseException | None = None
        self.done = False
        self.thread = threading.Thread(target=self._run, args=(threads,), name="scope-recall-lance-reap", daemon=True)
        self.thread.start()

    def _run(self, threads: tuple[threading.Thread | None, ...]) -> None:
        try:
            if self.process is not None:
                _stop_worker(self.process)
            _join_helper_threads(*threads)
        except BaseException as exc:
            self.error = exc

    def _process_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def settled(self) -> bool:
        """Finished by itself: nothing left to wait for and nothing to report."""
        return not self.thread.is_alive() and not self._process_alive() and self.error is None

    def finish(self, *, timeout: float | None, retry_stop: bool) -> None:
        """Wait up to ``timeout`` (``None``: indefinitely) for the helper to be gone.

        Raises teardown-pending while the thread or process is still alive and
        teardown-failed for an error that a retried stop did not clear.
        """
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise _helper_teardown_pending()
        retried = retry_stop and self._process_alive()
        if retried:
            try:
                _stop_worker(self.process)
            except Exception as exc:
                if self.error is None:
                    self.error = exc
        if self._process_alive():
            raise _helper_teardown_failed(self.error) if self.error is not None else _helper_teardown_pending()
        self.done = True
        if self.error is not None and not retried:
            raise _helper_teardown_failed(self.error)


class ProcessLanceVectorStore(VectorStore):
    """The Lance store, driven through ``_lance_worker`` in a child process.

    The host never imports Lance or PyArrow.  One request is in flight at a
    time.  A caller that runs out of budget mid-request leaves the frame it is
    owed parked for the next caller to drain: the helper itself is healthy and
    merely slower than that one budget.
    """

    backend = "lancedb"

    def __init__(self, db_path: Path, *, table_name: str, dimensions: int, metric: str = "cosine") -> None:
        super().__init__(db_path, table_name=table_name, dimensions=dimensions, metric=metric)
        self._lock = threading.RLock()
        self._request_timeout = 60.0
        # A parked frame older than this means the helper is wedged, not slow.
        # It is then reaped so an explicit reopen recovers the runtime instead
        # of every later request spending its own budget on the same frame.
        self._pending_response_timeout = self._request_timeout
        self._failed = False
        self._closed = False
        self._teardown: _HelperTeardown | None = None
        self._sequence = 0
        self._reset_transport()

    def _reset_transport(self) -> None:
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._sender: threading.Thread | None = None
        self._responses: queue.Queue = queue.Queue(maxsize=2)
        self._finalizer: weakref.finalize | None = None
        # Whether this helper was asked to open the table, answered or not yet: a failed open detaches the helper.
        self._table_asked = False
        self._clear_pending_response()

    @property
    def requires_reopen(self) -> bool:
        """True after a transport failure, until an explicit open resets it."""
        return self._failed

    # -- native path preflight ---------------------------------------------------

    def native_path_error(self) -> str | None:
        """A stable gap for a root Lance could not write to, found before any native file exists."""
        if os.name != "nt":
            return None
        root = Path(os.path.abspath(os.fspath(self.db_path)))
        object_path = root / f"{self.table_name}.lance" / "data" / _WINDOWS_NATIVE_OBJECT_NAME
        length = len(str(object_path).encode("utf-16-le", errors="surrogatepass")) // 2
        if length >= _WINDOWS_NATIVE_PATH_SAFE_LIMIT:
            return f"native_vector_path_too_long:path_length={length};safe_limit={_WINDOWS_NATIVE_PATH_SAFE_LIMIT}"
        return None

    def _raise_if_native_path_unsafe(self) -> None:
        error = self.native_path_error()
        if error is not None:
            raise NativeVectorPathError(error)

    # -- helper process lifecycle ------------------------------------------------

    @contextmanager
    def _helper_locked(self) -> Iterator[None]:
        """Own the sequential helper, waiting only for the remaining request budget.

        A timeout here neither starts, sends to, closes, nor fails the helper.
        """
        remaining = remaining_seconds()
        if remaining is None:
            acquired = self._lock.acquire()
        elif remaining <= 0.0:
            acquired = self._lock.acquire(blocking=False)
        else:
            acquired = self._lock.acquire(timeout=remaining)
        if not acquired:
            raise _helper_lock_timeout()
        try:
            yield
        finally:
            self._lock.release()

    def _start(self) -> None:
        if self._teardown is not None:
            raise _helper_teardown_pending()
        self._process = _take_spare() or _spawn_helper()
        self._finalizer = weakref.finalize(self, _stop_worker, self._process)
        self._reader = threading.Thread(
            target=_read_worker_frames, args=(self._process.stdout, self._responses),
            name="scope-recall-lance-pipe", daemon=True,
        )
        self._reader.start()

    def _detach_helper(self, *, failed: bool) -> None:
        """Hand the current helper to a background teardown; the store stays closed until reopened."""
        if failed:
            self._failed = True
        self._closed = True
        if self._teardown is not None:
            return
        if self._finalizer is not None:
            self._finalizer.detach()
        self._teardown = _HelperTeardown(self._process, (self._sender, self._reader))
        self._reset_transport()

    def _finish_teardown(self, *, timeout: float | None, retry_stop: bool) -> None:
        """Join the detached teardown; never called while holding ``_lock``."""
        teardown = self._teardown
        if teardown is None:
            return
        try:
            teardown.finish(timeout=timeout, retry_stop=retry_stop)
        finally:
            if teardown.done and self._teardown is teardown:
                self._teardown = None

    def _await_teardown(self) -> None:
        """Before starting a helper, wait for the previous one within the request budget."""
        teardown = self._teardown
        if teardown is None:
            return
        if teardown.settled:
            self._teardown = None
            return
        remaining = remaining_seconds()
        if remaining is not None and remaining <= 0.0:
            raise _helper_teardown_pending()
        self._finish_teardown(timeout=remaining, retry_stop=False)

    def _reopen_locked(self) -> None:
        if self._teardown is not None:
            raise _helper_teardown_pending()
        if self._closed:
            self._reset_transport()
            self._failed = False
            self._closed = False

    def _open(self, method: str, *, during_wait: Callable[[], Any] | None = None) -> None:
        self._await_teardown()
        with self._helper_locked():
            self._reopen_locked()
            try:
                self._invoke_locked(method, during_wait=during_wait)
            except BaseException:
                if self._process is None:
                    # The open never reached a helper (its time ran out first): the store has no table and stays
                    # closed for the next request to open.  Left looking open, it sent the next search to a helper
                    # started for that search, which held no table, and nothing opened it again (review of 3.4.9).
                    self._failed = self._closed = True
                raise

    def open(self) -> None:
        self._open("open")

    def open_existing(self) -> None:
        self._open("open_existing")

    def open_existing_with_work(self, work: Callable[[], Any]) -> None:
        """Overlap the read-only native open with caller-owned bounded work; work that returns False failed, and the
        open is then taken only if it has come (parked for the next request if not)."""
        if not callable(work):
            raise TypeError("open work must be callable")
        if not (self.db_path / f"{self.table_name}.lance").is_dir():
            raise FileNotFoundError("LanceDB physical storage is missing")
        if remaining_seconds() is None:
            raise ValueError("overlapped open requires an absolute request deadline")
        self._open("open_existing", during_wait=work)

    def close(self) -> None:
        """Stop the helper: synchronously without a request deadline, in the background with one."""
        join_teardown = False
        with self._helper_locked():
            self._closed = True
            if remaining_seconds() is not None:
                if self._teardown is None:
                    self._detach_helper(failed=False)
            elif self._teardown is not None:
                join_teardown = True
            else:
                if self._finalizer is not None:
                    self._finalizer()
                _join_helper_threads(self._sender, self._reader)
        if join_teardown:
            self._finish_teardown(timeout=None, retry_stop=True)

    # -- one request ---------------------------------------------------------------

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method not in LANCE_WORKER_METHODS:
            raise ValueError("unsupported native vector operation")
        with self._helper_locked():
            return self._invoke_locked(method, *args, **kwargs)

    def _invoke_locked(
        self,
        method: str,
        *args: Any,
        during_wait: Callable[[], Any] | None = None,
        guard: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> Any:
        """One sequential request to the helper; the caller holds ``_lock``.

        ``during_wait`` runs caller-owned work while the helper handles the
        request, so a cold open overlaps with query embedding.  ``guard``
        makes the request the fenced handshake: the helper takes the native
        lock, asks the host whether to proceed, and commits only on approval.
        """
        if method != "is_available":
            self._raise_if_native_path_unsafe()
        if self._failed or self._closed:
            raise RuntimeError("native vector worker is closed; reopen the vector runtime explicitly")
        if self._process is None and method not in ("open", "open_existing", "is_available"):
            # A helper is started to open the table (or to say whether LanceDB is installed): one started for a
            # search held no table and answered every search so, and nothing opened the table (review of 3.4.9).
            self._failed = self._closed = True
            raise RuntimeError("native vector worker is closed; reopen the vector runtime explicitly")
        # The frame a previous caller gave up on is discarded here rather than
        # raised into this unrelated request.
        self._drain_pending_response_locked()
        if _budget_exhausted():
            raise _request_budget_expired()
        self._sequence += 1
        request_id = self._sequence
        nonce = secrets.token_hex(16) if guard is not None else None
        if guard is not None:
            args = (*args, nonce)
            kwargs = {**kwargs, "guard_timeout_seconds": remaining_seconds()}
        request = {
            "id": request_id, "method": method, "args": args, "kwargs": kwargs,
            "store": {"db_path": str(self.db_path), "table_name": self.table_name,
                      "dimensions": self.dimensions, "metric": self.metric},
        }
        encoded = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_LANCE_FRAME_BYTES:
            raise ValueError("native vector request exceeds the 64 MiB frame limit")
        overlap_failed = False
        try:
            if self._process is None:
                self._start()
            if guard is not None:
                response = self._fenced_exchange(request_id, encoded, nonce, guard)
            else:
                self._send_request_frame(encoded)
                if method in ("open", "open_existing"):
                    self._table_asked = True
                work_failed = False
                if during_wait is not None:
                    try:
                        # Work that failed in its own way says so with False: its caller has nothing to search with,
                        # so the open is taken only if it has come, and parked for the next request otherwise.
                        work_failed = during_wait() is False
                    except BaseException:
                        overlap_failed = True
                        self._detach_helper(failed=True)
                        raise
                response = self._receive_response_locked(
                    request_id, only_if_ready=during_wait is not None and (work_failed or _budget_exhausted()),
                )
        except _RequestBudgetExpired:
            self._park_pending_response(request_id, method)
            if during_wait is not None:
                return None
            raise
        except (OSError, ValueError, queue.Empty, RuntimeError) as exc:
            if overlap_failed:
                raise
            if remaining_seconds() is None:
                self._failed = True
                self.close()
            else:
                self._detach_helper(failed=True)
            raise (_fence_failed() if guard is not None else _worker_failed()) from exc
        if not response.get("ok") and method in ("open", "open_existing"):
            # A helper that could not open the table holds none, and every search it answered said so until the
            # process ended: the store is closed for the next request to reopen, as for an open answered late
            # (``_drain_pending_response_locked``).  A store a runtime owned alone was closed by it and made anew;
            # one its process shares is not (review of 3.4.9).
            self._detach_helper(failed=True)
        return self._response_result(response)

    def _send_request_frame(self, encoded: bytes) -> None:
        # A wedged worker may stop reading stdin; put pipe backpressure inside
        # the receive deadline rather than blocking on the write.
        self._sender = threading.Thread(
            target=_write_worker_frame, args=(self._process.stdin, encoded, self._responses),
            name="scope-recall-lance-send", daemon=True,
        )
        self._sender.start()

    def _send_fence_frame(self, encoded: bytes, timeout: float) -> None:
        """Write one handshake frame within the shared budget, or fail the fence."""
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("native vector worker is not running")
        outcome: queue.Queue[bool] = queue.Queue(maxsize=1)

        def send() -> None:
            try:
                process.stdin.write(encoded)
                process.stdin.flush()
                outcome.put(True)
            except (OSError, ValueError):
                outcome.put(False)

        self._sender = threading.Thread(target=send, daemon=True, name="scope-recall-lance-fence-send")
        self._sender.start()
        self._sender.join(max(0.0, timeout))
        if self._sender.is_alive() or not outcome.get(False):
            raise RuntimeError("native vector fence handshake send failed")

    def _fenced_exchange(self, request_id: int, encoded: bytes, nonce: str, guard: Callable[[], bool]) -> dict[str, Any]:
        """Send, answer the helper's guard request, then take the terminal frame.

        Each step waits only for what is left of the shared budget, re-read
        after every send because a send can consume a material part of it.
        Running out mid-handshake must fail rather than park: the helper is
        holding the native lock for this request.
        """
        self._send_fence_frame(encoded, self._bounded_wait())
        if _budget_exhausted():
            raise RuntimeError("native vector fence deadline exhausted before guard")
        frame = self._responses.get(timeout=self._bounded_wait())
        if (
            not isinstance(frame, dict) or frame.get("id") != request_id
            or frame.get("kind") != "guard_request" or frame.get("nonce") != nonce
        ):
            raise RuntimeError("native vector fence guard request mismatch")
        try:
            approved = bool(guard())
        except Exception:
            approved = False
        if _budget_exhausted():
            approved = False
        reply = json.dumps(
            {"id": request_id, "kind": "guard_result", "nonce": nonce, "approved": approved}, ensure_ascii=False,
        ).encode("utf-8") + b"\n"
        self._send_fence_frame(reply, self._bounded_wait())
        if _budget_exhausted():
            raise RuntimeError("native vector fence deadline exhausted before final")
        final = self._responses.get(timeout=self._bounded_wait())
        if not isinstance(final, dict) or final.get("id") != request_id:
            raise RuntimeError("native vector fence final response mismatch")
        return final

    def _bounded_wait(self) -> float:
        """The helper timeout, cut to what is left of the request budget."""
        remaining = remaining_seconds()
        if remaining is None:
            return self._request_timeout
        return min(self._request_timeout, max(0.0, remaining))

    def _receive_response_locked(self, request_id: int, *, only_if_ready: bool = False) -> dict[str, Any]:
        """The frame for ``request_id``; ``_RequestBudgetExpired`` if the budget, not the helper, ran out."""
        remaining = remaining_seconds()
        if only_if_ready:
            # The budget went on the caller's own overlapped work: take the
            # frame only if the helper has already answered.
            try:
                response = self._responses.get_nowait()
            except queue.Empty as exc:
                raise _request_budget_expired() from exc
        else:
            if remaining is not None and remaining <= 0.0:
                raise _request_budget_expired()
            try:
                response = self._responses.get(timeout=self._bounded_wait())
            except queue.Empty as exc:
                if remaining is not None and remaining <= self._request_timeout:
                    raise _request_budget_expired() from exc
                raise
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise RuntimeError("native vector worker exited or returned an invalid frame")
        return response

    def _park_pending_response(self, request_id: int, method: str) -> None:
        """Remember the frame a caller stopped waiting for; the helper stays up."""
        self._pending_response_id = request_id
        self._pending_response_method = method
        self._pending_response_since = time.monotonic()

    def _clear_pending_response(self) -> None:
        self._pending_response_id: int | None = None
        self._pending_response_method: str | None = None
        self._pending_response_since: float | None = None

    def _drain_pending_response_locked(self) -> None:
        """Collect the frame a previous caller stopped waiting for, or reap a wedged helper."""
        request_id = self._pending_response_id
        if request_id is None:
            return
        # A frame nobody asked for since it was parked may have come in long ago.  A kept recall handler asked again
        # minutes after its first, cold search took that answer for a wedged helper, closed the helper, and recalled
        # the next prompt cold as well (worker_unresponsive).  Only a frame that has still not come is a wedge.
        stale = time.monotonic() - self._pending_response_since > self._pending_response_timeout
        try:
            response = self._receive_response_locked(request_id, only_if_ready=stale)
        except _RequestBudgetExpired:
            if not stale:
                raise
            # Every caller since the frame was parked has spent its budget on
            # it: that is a wedged helper, not a slow one.
            self._detach_helper(failed=True)
            raise RuntimeError(
                "native vector worker unresponsive; SQLite truth is intact and unacknowledged outbox work remains pending"
            ) from None
        except (OSError, ValueError, queue.Empty, RuntimeError) as exc:
            self._detach_helper(failed=True)
            raise _worker_failed() from exc
        method = self._pending_response_method
        self._clear_pending_response()
        if not response.get("ok") and method in ("open", "open_existing"):
            # A table open that ran out of its caller's time and then failed in the helper leaves no open table, and
            # every later search said so until the host restarted: the store is closed for the next request to
            # reopen.  Any other late failure (a purge out of its own time, say) was that request's; the helper holds
            # its table and goes on.
            self._detach_helper(failed=True)
            self._response_result(response)

    @staticmethod
    def _response_result(response: dict[str, Any]) -> Any:
        if response.get("ok"):
            return response.get("result")
        error_type = response.get("error_type")
        message = str(response.get("error") or "native vector operation failed")
        if error_type == "VectorStoreCompatibilityError":
            raise VectorStoreCompatibilityError(message)
        if error_type == "FileNotFoundError":
            raise FileNotFoundError(message)
        raise _remote_failure(error_type, message)

    # -- store interface -------------------------------------------------------------

    def fenced_upsert_records(
        self, rows: Iterable[dict[str, Any]], *, guard: Callable[[], bool], remaining_seconds: float,
    ) -> bool:
        """Upsert only if ``guard`` still approves once the helper holds the native lock."""
        if not callable(guard):
            raise TypeError("guard must be callable")
        if type(remaining_seconds) not in (int, float) or not math.isfinite(float(remaining_seconds)) or remaining_seconds <= 0:
            raise RuntimeError("native vector fence deadline exhausted")
        with _request_budget(remaining_seconds), self._helper_locked():
            return bool(self._invoke_locked("fenced_upsert_records", list(rows), guard=guard))

    def purge_governed_members(self, *, members, agent_id, installation_id,
                               partitions, project_id, branch_id, remaining_seconds: float) -> bool:
        """One cumulative budget covers locking, inventory, deletion and acknowledgement."""
        if type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
            raise RuntimeError("native vector purge deadline exhausted")
        with _request_budget(remaining_seconds) as deadline, self._helper_locked():
            budget = deadline.remaining()
            if budget <= 0:
                raise RuntimeError("native vector purge deadline exhausted")
            return self._invoke_locked(
                "purge_governed_members", members=members, agent_id=agent_id, installation_id=installation_id,
                partitions=partitions, project_id=project_id, branch_id=branch_id, budget_seconds=budget,
            ) is True

    def is_available(self) -> bool:
        return bool(self._call("is_available"))

    def upsert_records(self, rows: Iterable[dict[str, Any]]) -> None:
        self._call("upsert_records", list(rows))

    def delete_by_ids(self, ids: list[str]) -> None:
        self._call("delete_by_ids", ids)

    def contains_id(self, memory_id: str) -> bool:
        return bool(self._call("contains_id", memory_id))

    def list_ids(self) -> list[str]:
        return self._call("list_ids")

    def list_records(self) -> dict[str, dict[str, Any]]:
        return self._call("list_records")

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict[str, Any]]:
        return self._call("search", vector, scope_id=scope_id, limit=limit)

    def search_scopes(self, vector: list[float], *, scope_ids, limit: int) -> list[dict[str, Any]]:
        return self._call("search_scopes", vector, scope_ids=list(scope_ids), limit=limit)

    def count_rows(self) -> int:
        return int(self._call("count_rows"))

    def compact(self) -> dict[str, int]:
        """Forward a bounded compaction to the helper that owns the table."""
        return dict(self._call("compact"))

    def ensure_vector_index(self, *, min_rows: int, timeout_seconds: float, build: bool = True) -> dict[str, Any]:
        """Forward the index build, waiting for it up to ``timeout_seconds``: it can take longer than a request."""
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        with self._helper_locked():
            usual = self._request_timeout
            self._request_timeout = max(usual, float(timeout_seconds))
            try:
                return dict(self._invoke_locked("ensure_vector_index", min_rows=min_rows, build=bool(build)))
            finally:
                self._request_timeout = usual

    def _serving(self) -> bool:
        """Whether a helper asked to open the table is up for this store and nothing has closed it (a failure detaches
        the helper).  One started only to say whether LanceDB is installed holds no table (review of 3.4.9)."""
        return self._process is not None and self._table_asked and not self._closed and self._teardown is None


class _Shared:
    """A process's one store of a table, and the lock its first open (or its reopen) is taken under."""

    def __init__(self, store: ProcessLanceVectorStore) -> None:
        self.store = store
        self.opening = threading.Lock()


#: The one store of each table in a process that shares them (``share``), by path, table, dimensions and metric.
_shared: dict[tuple[str, str, int, str], _Shared] = {}
_shared_lock = threading.Lock()
_sharing = False


def share() -> None:
    """Give every store of one table this process builds one helper (``store_for``): for a server that runs on.

    The server answers a prompt with the handler it keeps, and each prompt that comes meanwhile with a handler made
    for it, whose store started a helper of its own: about 2 s importing LanceDB, then the table's open, while that
    prompt's words were searched.  Parallel sub-agents opening sessions two and three a second on the work computer
    lost their vector search that way, and some their whole recall.  On a copy of the shared store, bursts of three
    prompts with long briefs, each stored and then recalled within the hook's 6 s, kept their vector search in 29 of
    48 recalls (16 lost it to a helper's start); with one store for the process, 45 of 48 (none) (2026-10-01).  The
    prompts take turns on the helper, one search each, 10-40 ms when warm.  A store of a table the process no longer
    uses (its embedding space changed under it) keeps its helper until the process ends.
    """
    global _sharing
    _sharing = True


def store_for(db_path: Path, *, table_name: str, dimensions: int,
              metric: str = "cosine") -> ProcessLanceVectorStore | SharedStore:
    """The process's one store of this table, while it shares them (``share``); a store of its own otherwise."""
    if not _sharing:
        return ProcessLanceVectorStore(db_path, table_name=table_name, dimensions=dimensions, metric=metric)
    key = (os.path.normcase(os.path.abspath(os.fspath(db_path))), table_name, int(dimensions), metric)
    with _shared_lock:
        shared = _shared.get(key)
        if shared is None:
            shared = _shared[key] = _Shared(ProcessLanceVectorStore(db_path, table_name=table_name,
                                                                    dimensions=dimensions, metric=metric))
    return SharedStore(shared)


class SharedStore:
    """A runtime's view of its process's one store of a table (``share``).

    It opens the store only when no helper serves it, one runtime at a time, and never closes it: the process's other
    runtimes search it too, and a kept handler that is made anew finds its helper warm.  Everything else is the
    store's own.
    """

    def __init__(self, shared: _Shared) -> None:
        self._shared = shared
        self._store = shared.store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def _open(self, method: str) -> None:
        shared = self._shared
        if shared.store._serving():
            return
        remaining = remaining_seconds()
        if remaining is None:
            acquired = shared.opening.acquire()
        else:
            acquired = shared.opening.acquire(timeout=max(0.0, remaining))
        if not acquired:
            raise _helper_lock_timeout()
        try:
            # Another runtime may have opened it meanwhile: two that started together opened it twice (review).
            if not shared.store._serving():
                getattr(shared.store, method)()
        except _RequestBudgetExpired:
            if method == "open":
                raise  # the drain's, which reports it as for a store of its own
            # A recall's (or a server's warm-up): parked, its answer waiting for the next request; or never sent, the
            # store then closed for the next request to open (``ProcessLanceVectorStore._open``).  Either way its time
            # is up, and a recall says so (runtime/instance.py ``_LazyVectorPort.search``).
            return
        finally:
            shared.opening.release()

    def open(self) -> None:
        self._open("open")

    def open_existing(self) -> None:
        self._open("open_existing")

    def open_existing_with_work(self, work: Callable[[], Any]) -> None:
        # A table not made yet is said at once, as by a store of its own: each prompt started a helper to learn it,
        # about 2 s each with no spare (review of 3.4.9).
        if not (self._store.db_path / f"{self._store.table_name}.lance").is_dir():
            raise FileNotFoundError("LanceDB physical storage is missing")
        # The work (the query's embedding, already asked for when the recall started: runtime/instance.py
        # ``_QueryEmbedding``) runs outside the store's lock: overlapped with the open, it held every other runtime's
        # search for the length of one prompt's embedding (review of 3.4.9).
        self._open("open_existing")
        work()

    def close(self) -> None:
        """Left to the process: its other runtimes search the store (``_close_shared`` stops it at exit)."""


def _close_shared() -> None:
    with _shared_lock:
        entries = list(_shared.values())
        _shared.clear()
    for shared in entries:
        try:
            shared.store.close()
        except Exception:  # noqa: BLE001 - the process is ending; a helper that will not stop is reaped with it
            pass


atexit.register(_close_shared)


def _reset_sharing_for_tests() -> None:
    """Stop the shared stores and share no more: a server a test started would leave the whole test process sharing."""
    global _sharing
    _close_shared()
    _sharing = False


__all__ =["LANCE_WORKER_METHODS", "MAX_LANCE_FRAME_BYTES", "NativeVectorPathError", "ProcessLanceVectorStore",
           "SharedStore", "discard_spare", "prestart", "share", "store_for"]
