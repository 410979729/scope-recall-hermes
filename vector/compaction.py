"""Physical companion diagnostics and embedding backfill receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STATE_FILENAME = "vector-state.json"
STATE_SCHEMA = "scope-recall.vector-state.v1"
INDEX_STATE_FILENAME = "index-state.json"
INDEX_STATE_SCHEMA = "scope-recall.vector-index.v1"
EMBED_BACKFILL_STATE_SCHEMA = "scope-recall.embed-backfill.v1"
EMBED_BACKFILL_CURRENT = timedelta(days=2)
_BACKFILL_NAME = re.compile(r"embed-backfill-[0-9a-f]{16}\.json")


def instance_vector_footprints(data_directory: Path) -> list[dict[str, Any]]:
    """Report raw files and index watermarks without loading native extensions."""
    root = Path(data_directory) / "vectors"
    paths = [root / "vectors.sqlite3", *root.glob("*/vectors.sqlite3")]
    reports = []
    for path in paths:
        if not path.is_file():
            continue
        from .cache import cache_footprint

        record: dict[str, Any] = {
            "embedding_space": path.parent.name,
            "bytes": path.stat().st_size,
            **cache_footprint(path),
        }
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                meta = dict(conn.execute("SELECT key,value FROM vector_meta"))
                record.update(
                    table=meta.get("table_name"),
                    dimensions=int(meta["dimensions"]),
                    index_watermark=int(meta.get("index_watermark", 0)),
                    import_states=list(conn.execute("SELECT state,count(*) FROM vector_imports GROUP BY state")),
                )
        except (sqlite3.Error, OSError, KeyError, ValueError):
            record["unreadable"] = True
        reports.append({**record, **_backfill_report(path.parent, datetime.now(UTC))})
    return reports


def embed_backfill_filename(scope_ids, project_id: str | None, branch_id: str | None) -> str:
    """The backfill state of one worker partition: each worker queues only its own imports (its scopes, project and
    branch), so one file for the store would let a worker with none write ``finished`` for another's, which would
    then wait a day and go on from the wrong place."""
    key = json.dumps([sorted(scope_ids), project_id, branch_id], separators=(",", ":"))
    return f"embed-backfill-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.json"


def _backfill_report(space: Path, now: datetime) -> dict[str, Any]:
    """The import backfill's outcome for one vector store.  Without it a backfill that failed would be written down,
    tried again on every pass and read by nothing.  Each worker partition keeps its own state
    (``embed_backfill_filename``): the store's outcome is a failed one's, else the latest.  A partition no worker has
    looked at for ``EMBED_BACKFILL_CURRENT`` (a retry lane, a workspace used once) is left out of it, or its last
    failure would stand for good."""
    states = [
        state
        for state in (
            read_state(space, filename=path.name, schema=EMBED_BACKFILL_STATE_SCHEMA)
            for path in sorted(space.glob("embed-backfill-*.json"))
            if _BACKFILL_NAME.fullmatch(path.name)
        )
        if state
    ]
    current = [
        state
        for state in states
        if (checked := parse_time(state.get("checked_at"))) is not None and now - checked <= EMBED_BACKFILL_CURRENT
    ]
    failed = [state for state in current if state.get("outcome") == "failed"]
    latest = max(current or states, key=lambda state: str(state.get("checked_at") or ""), default={})
    return {
        "last_embed_backfill_at": latest.get("checked_at"),
        "embed_backfill_outcome": "failed" if failed else latest.get("outcome"),
        "embed_backfill_error": (failed[0] if failed else latest).get("error")
        if (failed or latest.get("outcome") == "failed")
        else None,
        "embed_backfill_queued_total": sum(int(state.get("queued_total") or 0) for state in states) if states else None,
    }


def read_state(storage_dir: Path, *, filename: str = STATE_FILENAME, schema: str = STATE_SCHEMA) -> dict[str, Any]:
    """Read a bounded worker receipt; return an empty mapping when no valid receipt exists."""
    try:
        raw = json.loads((Path(storage_dir) / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != schema:
        return {}
    return raw


def write_state(
    storage_dir: Path, payload: dict[str, Any], *, filename: str = STATE_FILENAME, schema: str = STATE_SCHEMA
) -> None:
    """Record an outcome.  Never raises: this is a report, not a commitment."""
    directory = Path(storage_dir)
    record = {"schema": schema, **payload}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f"{filename}.partial"
        partial.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(partial, directory / filename)
    except OSError:
        return


def parse_time(value: Any) -> datetime | None:
    """A state file's time; one without a zone is UTC, and anything that is not a time is None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
