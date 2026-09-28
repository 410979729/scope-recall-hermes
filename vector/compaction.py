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

import json
import os
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
EMBED_BACKFILL_STATE_FILENAME = "embed-backfill-state.json"
EMBED_BACKFILL_STATE_SCHEMA = "scope-recall.embed-backfill.v1"


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
        # A backfill that failed was written down and tried again on every pass, and nothing read it.
        backfill = read_state(space, filename=EMBED_BACKFILL_STATE_FILENAME, schema=EMBED_BACKFILL_STATE_SCHEMA)
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
                    "last_embed_backfill_at": backfill.get("checked_at"),
                    "embed_backfill_outcome": backfill.get("outcome"),
                    "embed_backfill_error": backfill.get("error"),
                    "embed_backfill_queued_total": backfill.get("queued_total"),
                }
            )
    return reports


def read_state(storage_dir: Path, *, filename: str = STATE_FILENAME, schema: str = STATE_SCHEMA) -> dict[str, Any]:
    """Last compaction outcome (or, with the index names, index outcome); empty when there has been none."""
    try:
        raw = json.loads((Path(storage_dir) / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != schema:
        return {}
    return raw


def write_state(storage_dir: Path, payload: dict[str, Any], *, filename: str = STATE_FILENAME,
                schema: str = STATE_SCHEMA) -> None:
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
    "EMBED_BACKFILL_STATE_FILENAME",
    "EMBED_BACKFILL_STATE_SCHEMA",
    "FRAGMENT_THRESHOLD",
    "INDEX_STATE_FILENAME",
    "INDEX_STATE_SCHEMA",
    "STATE_FILENAME",
    "STATE_SCHEMA",
    "VectorFootprint",
    "compaction_due",
    "instance_vector_footprints",
    "measure_footprint",
    "read_state",
    "table_directory",
    "write_state",
]
