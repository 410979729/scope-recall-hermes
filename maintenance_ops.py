from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def effective_apply(*, apply: bool = False, dry_run: bool = False) -> bool:
    """Return whether a maintenance command should mutate state.

    ``--dry-run`` is an explicit safety override and wins over accidental
    ``--apply`` when both flags are present.
    """

    return bool(apply and not dry_run)


def memory_db_path(hermes_home: Path, *, db_path: Path | str | None = None) -> Path:
    if db_path:
        return Path(db_path).expanduser()
    return hermes_home.expanduser() / "scope-recall" / "memory.sqlite3"


def connect_memory_db(path: Path, *, apply: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    mode = "rw" if apply else "ro"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def json_dumps_stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_batch_id(prefix: str) -> str:
    safe_prefix = str(prefix or "batch").strip().strip("-") or "batch"
    return f"{safe_prefix}-{uuid.uuid4().hex}"
