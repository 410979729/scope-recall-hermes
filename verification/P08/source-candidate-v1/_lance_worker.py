"""Private native vector worker. No Hermes runtime, model, or truth connection."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING


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
                    member = getattr(store, method)
                    result = member if method == "id_lookup_indexed" else member(*request["args"], **request["kwargs"])
                response = {"id": request_id, "ok": True, "result": result}
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
