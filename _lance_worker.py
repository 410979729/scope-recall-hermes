"""Private native vector worker. No Hermes runtime, model, or truth connection."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
import types
from pathlib import Path
from typing import TYPE_CHECKING


class FenceHandshakeError(RuntimeError):
    """The guarded publication handshake cannot safely continue."""


def _read_guard_reply(stdin, *, request_id: int, nonce: str, timeout: float | None, max_bytes: int) -> dict:
    result: queue.Queue[object] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            line = stdin.buffer.readline(max_bytes + 1)
            result.put(line)
        except BaseException as exc:
            result.put(exc)

    reader = threading.Thread(target=read, daemon=True, name="scope-recall-lance-fence-reply")
    reader.start()
    reader.join(None if timeout is None else max(0.0, timeout))
    if reader.is_alive():
        raise FenceHandshakeError("guard reply deadline exhausted")
    line = result.get_nowait()
    if isinstance(line, BaseException):
        raise FenceHandshakeError("guard reply read failed") from line
    if not line or len(line) > max_bytes:
        raise FenceHandshakeError("guard reply EOF or oversized")
    try:
        reply = json.loads(line)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FenceHandshakeError("guard reply malformed") from exc
    if not isinstance(reply, dict) or reply.get("id") != request_id or reply.get("kind") != "guard_result" or reply.get("nonce") != nonce:
        raise FenceHandshakeError("guard reply mismatch")
    if type(reply.get("approved")) is not bool:
        raise FenceHandshakeError("guard approval must be boolean")
    return reply


def main() -> None:
    # Directory plugins need a package alias; importing their __init__ would
    # load the host integration and potentially torch into this native worker.
    package = types.ModuleType("scope_recall")
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules["scope_recall"] = package
    if TYPE_CHECKING:
        from . import vector_store
        from .capture_filters import sanitize_report_text
        from .lance_process_store import LANCE_WORKER_METHODS, MAX_LANCE_FRAME_BYTES
        from .vector_store import LanceVectorStore
    else:
        from scope_recall import vector_store
        from scope_recall.capture_filters import sanitize_report_text
        from scope_recall.lance_process_store import LANCE_WORKER_METHODS, MAX_LANCE_FRAME_BYTES
        from scope_recall.vector_store import LanceVectorStore

    # This entire interpreter is already disposable. A second import-probe
    # subprocess adds no isolation and complicates deadline/process ownership.
    vector_store._NATIVE_VECTOR_PROBE = {"safe": True, "returncode": 0, "stdout": "", "stderr": ""}

    store = None
    # Keep protocol output on a private duplicate. Redirect the actual stdout
    # descriptor as well as Python stdout: Arrow/Rust diagnostics may bypass
    # contextlib and write directly to descriptor 1.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        def fenced_upsert(rows, nonce, *, request_id, guard_timeout_seconds):
            if not isinstance(nonce, str) or not nonce:
                raise FenceHandshakeError("guard nonce missing")
            with store.physical_write_lock():
                guard_frame = {"id": request_id, "kind": "guard_request", "nonce": nonce}
                output.write((json.dumps(guard_frame, ensure_ascii=False) + "\n").encode("utf-8"))
                output.flush()
                reply = _read_guard_reply(
                    sys.stdin,
                    request_id=request_id,
                    nonce=nonce,
                    timeout=guard_timeout_seconds,
                    max_bytes=MAX_LANCE_FRAME_BYTES,
                )
                if not reply["approved"]:
                    return False
                # The native advisory lock remains held across checkout_latest
                # and the single Lance merge transaction.
                store.upsert_records_locked(rows)
                return True

        while True:
            line = sys.stdin.buffer.readline(MAX_LANCE_FRAME_BYTES + 1)
            if not line or len(line) > MAX_LANCE_FRAME_BYTES:
                return
            request = json.loads(line)
            request_id = request.get("id")
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    if store is None:
                        spec = request["store"]
                        store = LanceVectorStore(Path(spec["db_path"]), table_name=spec["table_name"],
                                                 dimensions=int(spec["dimensions"]), metric=spec["metric"])
                    method = request["method"]
                    if method not in LANCE_WORKER_METHODS:
                        raise ValueError("unsupported native vector operation")
                    if method == "fenced_upsert_records":
                        args = request.get("args") or []
                        if len(args) != 2:
                            raise FenceHandshakeError("guard request shape invalid")
                        result = fenced_upsert(
                            args[0],
                            args[1],
                            request_id=request_id,
                            guard_timeout_seconds=request.get("kwargs", {}).get("guard_timeout_seconds"),
                        )
                    else:
                        member = getattr(store, method)
                        result = member if method == "id_lookup_indexed" else member(*request["args"], **request["kwargs"])
                response = {"id": request_id, "ok": True, "result": result}
            except FenceHandshakeError:
                # No terminal response is safe after a malformed/late guard
                # reply. EOF makes the host fail closed and reap this helper.
                return
            except Exception as exc:
                response = {"id": request_id, "ok": False, "error_type": type(exc).__name__,
                            "error": sanitize_report_text(str(exc))[:500]}
            encoded = (json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > MAX_LANCE_FRAME_BYTES:
                encoded = (json.dumps({"id": request_id, "ok": False,
                                       "error": "native vector response exceeds the 64 MiB frame limit"}) + "\n").encode()
            output.write(encoded)
            output.flush()
    finally:
        if store is not None:
            store.close()
        output.close()


if __name__ == "__main__":
    main()
