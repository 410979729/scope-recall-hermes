"""Physical state of the Lance vector store, and when it needs a compaction.

Every vector publication is its own Lance commit, so the store gains one data
fragment and one manifest per vector and never gives either back.  Measured on
the TianShu store on 2026-09-14, at 2,243 vectors:

    data fragments   2,243        34.4 MB
    manifests        2,245       237.6 MB   <- each one lists every fragment,
    transactions     2,244        0.3 MB       so the history grows as O(n^2)
    search latency     142 ms                 against 29 ms once compacted

Nothing in the code base ever called ``optimize``.  This module holds the two
things that decision needs and that neither the store nor the doctor should own
privately: how to read the footprint off the filesystem, and when a compaction
is due.

Measuring from the filesystem rather than asking LanceDB is deliberate.  The
doctor must be able to report this without opening the table or loading
lancedb, and an operator should be able to confirm the number with a file
listing.

The one residual risk, stated so it is not rediscovered as a surprise: the
worker compacts while the gateway may be mid-search, and the two are separate
processes, so a search could in principle be reading a version the pass drops.
Reads follow the table forward (``vector_store._fresh_table``), which closes
the common case, and ``core/recall.py`` already catches any vector failure and
records ``vector_unavailable`` / ``vector_error`` while the other channels
answer.  So the worst case is one recall degrading visibly for a fraction of a
second per hour -- against a store that otherwise grows a manifest history
without bound.

Not responsible for: performing the compaction (``vector_store.LanceVectorStore
.compact``), or scheduling it (``runtime/vector_upkeep.py``, called once at the
start of each drain).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Fragment count above which a compaction is worth doing.  Compaction is cheap
#: (0.12 s with nothing to do, 3.5 s for the 2,243-fragment backlog), so this is
#: set low enough that the store never accumulates a manifest history worth
#: noticing, rather than tuned to a latency cliff.
FRAGMENT_THRESHOLD = 64

#: Shortest interval between two compactions of the same store.  Guards against
#: repeating the pass every drain while writes keep arriving; it does not bound
#: how much work any single pass may do, because a pass is bounded already.
COOLDOWN = timedelta(minutes=15)

#: Written next to the store it describes, so the state cannot outlive or drift
#: from the thing it reports on.
STATE_FILENAME = "compaction-state.json"
STATE_SCHEMA = "scope-recall.vector-compaction.v1"


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


def physical_vector_footprint(db_path: Path, table_name: str) -> dict[str, int]:
    """Count fragments, manifests and bytes.  Missing store reads as zero."""
    return measure_footprint(db_path, table_name).as_dict()


def measure_footprint(db_path: Path, table_name: str) -> VectorFootprint:
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
                }
            )
    return reports


def read_state(storage_dir: Path) -> dict[str, Any]:
    """Last compaction outcome, or an empty mapping when there has been none."""
    try:
        raw = json.loads((Path(storage_dir) / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA:
        return {}
    return raw


def write_state(storage_dir: Path, payload: dict[str, Any]) -> None:
    """Record an outcome.  Never raises: this is a report, not a commitment."""
    directory = Path(storage_dir)
    record = {"schema": STATE_SCHEMA, **payload}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f"{STATE_FILENAME}.partial"
        partial.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(partial, directory / STATE_FILENAME)
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
    "FRAGMENT_THRESHOLD",
    "STATE_FILENAME",
    "STATE_SCHEMA",
    "VectorFootprint",
    "compaction_due",
    "instance_vector_footprints",
    "measure_footprint",
    "physical_vector_footprint",
    "read_state",
    "table_directory",
    "write_state",
]
