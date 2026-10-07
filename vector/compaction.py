"""Physical state of a Lance vector store, and when it needs compacting.

Every publication is its own Lance commit, so the store gains one data
fragment and one manifest per vector and never gives either back; and each
manifest lists every fragment, so the manifest history grows as O(n^2).
Measured on one production store at 2,243 vectors: 2,243 fragments (34 MB),
2,245 manifests (238 MB), and a 142 ms search that took 29 ms once compacted.

This module owns the two inputs to that decision which neither the store nor
the doctor should own privately: reading the footprint off the filesystem,
and deciding when a pass is due.  Measuring from the filesystem rather than
asking LanceDB is deliberate: the doctor must report this without opening the
table or loading lancedb, and an operator can confirm the number with a file
listing.

Residual risk, stated so it is not rediscovered as a surprise: the worker
compacts while the gateway may be mid-search in another process, so a search
could read a version the pass drops.  Reads follow the table forward
(``store.LanceVectorStore._fresh_table``), which closes the common case, and
recall already records ``vector_unavailable`` / ``vector_error`` and answers
from its other channels, so the worst case is one visibly degraded recall.

Not responsible for performing the compaction (``store.LanceVectorStore
.compact``) or scheduling it (``runtime/vector_upkeep.py``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Fragment count above which a compaction is worth doing.  A pass is cheap
#: (0.12 s with nothing to do, 3.5 s for a 2,243-fragment backlog), so this is
#: low enough that the manifest history never grows worth noticing, rather
#: than tuned to a latency cliff.
FRAGMENT_THRESHOLD = 64

#: Shortest interval between two compactions of the same store, so the pass is
#: not repeated on every drain while writes keep arriving.
COOLDOWN = timedelta(minutes=15)

#: Written next to the store it describes, so the state cannot outlive or drift
#: from the thing it reports on.
STATE_FILENAME = "compaction-state.json"
STATE_SCHEMA = "scope-recall.vector-compaction.v1"
#: The last look at the store's nearest-neighbour index (``runtime/vector_upkeep.index_if_due``), beside it too.
INDEX_STATE_FILENAME = "index-state.json"
INDEX_STATE_SCHEMA = "scope-recall.vector-index.v1"
#: Where the backfill of an import's embeddings stopped (``runtime/vector_upkeep.backfill_if_due``), beside them.
EMBED_BACKFILL_STATE_SCHEMA = "scope-recall.embed-backfill.v1"


def embed_backfill_filename(scope_ids, project_id: str | None, branch_id: str | None) -> str:
    """The backfill state of one worker partition: each worker queues only its own imports (its scopes, project and
    branch), so one file for the store let a worker with none write ``finished`` for another's, which then waited a
    day and went on from the wrong place."""
    key = json.dumps([sorted(scope_ids), project_id, branch_id], separators=(",", ":"))
    return f"embed-backfill-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.json"


@dataclass(frozen=True)
class VectorFootprint:
    fragments: int
    manifests: int
    transactions: int
    bytes: int

    def as_dict(self) -> dict[str, int]:
        return {
            "fragments": self.fragments,
            "manifests": self.manifests,
            "transactions": self.transactions,
            "bytes": self.bytes,
        }


def table_directory(db_path: Path, table_name: str) -> Path:
    return Path(db_path) / f"{table_name}.lance"


def measure_footprint(db_path: Path, table_name: str) -> VectorFootprint:
    """Count fragments, manifests, transactions and bytes.  A missing store reads as zero."""
    table = table_directory(db_path, table_name)
    counts = {"data": 0, "_versions": 0, "_transactions": 0}
    total = 0
    for sub in counts:
        try:
            entries = list((table / sub).iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                total += entry.stat().st_size
            except OSError:
                continue
            counts[sub] += 1
    return VectorFootprint(
        fragments=counts["data"],
        manifests=counts["_versions"],
        transactions=counts["_transactions"],
        bytes=total,
    )


def instance_vector_footprints(data_directory: Path) -> list[dict[str, Any]]:
    """Every vector store under an instance, with its footprint and last pass.

    Walks the directory rather than reading the runtime configuration so the
    doctor can report this for an instance it cannot open, and so a store left
    behind by a retired embedding space is still visible instead of silently
    occupying disk.
    """
    root = Path(data_directory) / "vectors"
    reports: list[dict[str, Any]] = []
    try:
        spaces = sorted(entry for entry in root.iterdir() if entry.is_dir())
    except OSError:
        return reports
    for space in spaces:
        db_path = space / "lancedb"
        try:
            tables = sorted(entry for entry in db_path.iterdir() if entry.suffix == ".lance")
        except OSError:
            continue
        state = read_state(space)
        index = read_state(space, filename=INDEX_STATE_FILENAME, schema=INDEX_STATE_SCHEMA)
        backfill = _backfill_report(space, datetime.now(timezone.utc))
        for table in tables:
            footprint = measure_footprint(db_path, table.stem)
            reports.append(
                {
                    "embedding_space": space.name,
                    "table": table.stem,
                    **footprint.as_dict(),
                    "fragment_threshold": FRAGMENT_THRESHOLD,
                    "last_compaction_at": state.get("finished_at"),
                    "last_compaction_outcome": state.get("outcome"),
                    "compaction_overdue": footprint.fragments > FRAGMENT_THRESHOLD,
                    "last_index_check_at": index.get("checked_at"),
                    "index_outcome": index.get("outcome"),
                    **backfill,
                }
            )
    return reports


#: A backfill partition looked at within this long still has a worker: each looks at least once a day.
EMBED_BACKFILL_CURRENT = timedelta(days=2)
_BACKFILL_NAME = re.compile(r"embed-backfill-[0-9a-f]{16}\.json")


def _backfill_report(space: Path, now: datetime) -> dict[str, Any]:
    """The import backfill's outcome for one vector store.  A backfill that failed was written down, tried again on
    every pass and read by nothing.  Each worker partition keeps its own state (``embed_backfill_filename``): the
    store's outcome is a failed one's, else the latest.  A partition no worker has looked at for
    ``EMBED_BACKFILL_CURRENT`` (a retry lane, a workspace used once) is left out of it, or its last failure would
    stand for good."""
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
        if (checked := _parse_time(state.get("checked_at"))) is not None and now - checked <= EMBED_BACKFILL_CURRENT
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
    """Last compaction outcome (or, with the index names, index outcome); empty when there has been none."""
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


def compaction_due(
    footprint: VectorFootprint,
    state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    """The reason a compaction should run now, or ``None`` to leave it alone.

    Returning the reason rather than a bare boolean means the receipt and the
    doctor report say *why* a pass happened, which is the difference between a
    log line an operator can act on and one they learn to scroll past.
    """
    if footprint.fragments <= FRAGMENT_THRESHOLD:
        return None
    moment = now or datetime.now(timezone.utc)
    last = _parse_time(state.get("finished_at"))
    if last is not None and moment - last < COOLDOWN:
        return None
    return f"fragments_above_threshold:{footprint.fragments}"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


__all__ = [
    "COOLDOWN",
    "EMBED_BACKFILL_STATE_SCHEMA",
    "FRAGMENT_THRESHOLD",
    "INDEX_STATE_FILENAME",
    "INDEX_STATE_SCHEMA",
    "STATE_FILENAME",
    "STATE_SCHEMA",
    "VectorFootprint",
    "compaction_due",
    "embed_backfill_filename",
    "instance_vector_footprints",
    "measure_footprint",
    "read_state",
    "table_directory",
    "write_state",
]
