"""Content-free import receipts for interrupted-pass diagnosis and safe retry."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from scope_recall.contracts import ContractError
from scope_recall.core.backup_primitives import atomic_json
from scope_recall.core.file_lock import advisory_file_lock

from .memory_import import apply_import

_STATE = "memory-import.json"
_LOCK = "memory-import.lock"


def read_progress(directory: Path) -> dict:
    """Read counts only; a running record without its lock is an interrupted import."""
    path = directory / _STATE
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return {"status": "unreadable"}
        if state.get("status") == "running":
            try:
                with advisory_file_lock(directory / _LOCK, timeout_seconds=0):
                    state.update(status="interrupted", retryable=True)
            except TimeoutError:
                pass
        return state
    except (OSError, ValueError):
        return {"status": "unreadable"}


def run_import(core, context, plan, *, preview_id, scope_id):
    """Serialize import jobs only, with periodic counts outside the truth writer lease."""
    if not preview_id or preview_id != plan["preview_id"]:
        raise ContractError("VERSION_CONFLICT", "import_preview")
    directory = core.config.binding.data_directory
    try:
        with advisory_file_lock(directory / _LOCK, timeout_seconds=0):
            return _run(core, context, plan, preview_id, scope_id, directory)
    except TimeoutError:
        raise ContractError("STORAGE_UNAVAILABLE", "import_in_progress") from None


def _run(core, context, plan, preview_id, scope_id, directory):
    state: dict[str, Any] = dict(
        status="running",
        preview_id=preview_id,
        source=plan["source"],
        processed=0,
        total=plan["count"],
        counts={},
        retryable=True,
    )
    path = directory / _STATE
    atomic_json(path, state)
    last = time.monotonic()

    def progress(counts):
        nonlocal last
        state.update(counts)
        now = time.monotonic()
        if now - last >= 1:
            atomic_json(path, state)
            last = now

    try:
        result = apply_import(core, context, plan, preview_id=preview_id, scope_id=scope_id, progress=progress)
        state.update(result, status="complete" if result["complete"] else "blocked")
        return result
    finally:
        if state["status"] == "running":
            state.update(status="interrupted", retryable=True)
        atomic_json(path, state)
