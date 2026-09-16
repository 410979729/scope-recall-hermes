"""One lazy, private HTTP helper per query adapter; no listening socket.

Pipe I/O runs off the caller thread so the deadline also bounds blocked writes.
Timeout/cancellation/protocol failure discards the helper, never replays a POST.
Only the next caller may start a replacement. Stderr is never retained or logged.
"""
from __future__ import annotations

import atexit
import queue
import subprocess
import threading
import time
import weakref

_SESSIONS = weakref.WeakSet()


def _close_sessions():
    for session in list(_SESSIONS):
        session.close()


atexit.register(_close_sessions)


class HttpWorkerSession:
    """Own a serialized newline-framed worker and its bounded cleanup."""

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._io_thread = None
        self._closed = False
        _SESSIONS.add(self)

    def discard(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        thread = self._io_thread
        if thread is not None:
            thread.join(timeout=0.2)
        if thread is None or not thread.is_alive():
            process.stdin.close()
            process.stdout.close()

    def close(self):
        """Cancel an active exchange and permanently release this session."""
        self._closed = True
        self.discard()

    def exchange(self, command, request, *, deadline, max_stdout, startupinfo, creationflags):
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError
        process = None
        io_thread = None
        try:
            if self._closed:
                raise OSError("http_session_closed")
            if self._process is None or self._process.poll() is not None:
                self.discard()
                self._process = subprocess.Popen(
                    [*command, "--persistent"], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    startupinfo=startupinfo, creationflags=creationflags,
                )
            process = self._process
            if self._closed:  # close may race with process creation
                raise OSError("http_session_closed")
            replies = queue.Queue(maxsize=1)

            def exchange_line():
                try:
                    process.stdin.write(request + b"\n")
                    process.stdin.flush()
                    result = process.stdout.readline(max_stdout + 1)
                    replies.put(result)
                except (OSError, ValueError):
                    replies.put(None)

            io_thread = threading.Thread(target=exchange_line, daemon=True)
            self._io_thread = io_thread
            io_thread.start()
            try:
                result = replies.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError from None
            if result is None or not result.endswith(b"\n") or len(result) > max_stdout:
                raise OSError("http_worker_protocol")
            return result, b""
        except BaseException:
            self.discard()
            raise
        finally:
            if io_thread is not None and self._process is not process:
                io_thread.join(timeout=0.2)
            if process is not None and process.poll() is not None:
                # Killing the child releases blocked pipe I/O before closing
                # handles; never close a live thread's locked buffered stream.
                if io_thread is None or not io_thread.is_alive():
                    process.stdin.close()
                    process.stdout.close()
            self._lock.release()

    def __del__(self):
        self.close()
