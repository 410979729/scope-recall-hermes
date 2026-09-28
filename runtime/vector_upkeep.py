"""Run a vector compaction when one is due, and record what it did.

Sits between the policy (``vector/compaction.py``, which knows the footprint
and the threshold) and the store (``vector.store.LanceVectorStore.compact``,
which knows LanceDB).  ``runtime/instance.py`` has the single call site, at the
start of a drain.

Why the start and not the end: on a busy instance the drain budget is usually
spent by the time the work queue is empty, so upkeep placed at the end is
upkeep that never runs.  Reserving a few seconds up front is the difference
between a guard that holds and one that only holds while the instance is idle
-- which is exactly when it is not needed.

Not responsible for: deciding the threshold, or performing the native work.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..vector.compaction import (INDEX_STATE_FILENAME, INDEX_STATE_SCHEMA, compaction_due, measure_footprint,
                                 read_state, write_state)
from ..vector.store import VECTOR_INDEX_MIN_ROWS
from .validation import utc_now

#: Seconds of the drain budget set aside for one pass.  Measured: 0.12 s when
#: there is nothing to do, 3.5 s to clear a 2,243-fragment backlog.  Below this
#: the pass is skipped rather than started and abandoned half-way.
RESERVE_SECONDS = 8.0


def compact_if_due(store: Any, vector_config: Any, *, available_seconds: float,
                   reason: str | None = None) -> dict[str, Any] | None:
    """Compact when the policy says so.  Returns the receipt, or ``None``.

    ``reason`` names a cause the caller already knows, such as a retention
    pass that just deleted rows; it skips the threshold and the cooldown, which
    guard against compacting for nothing.

    Never raises.  A compaction that cannot run leaves the store exactly as it
    was, and the next drain will try again; letting it fail a drain would trade
    a tidiness problem for an availability one.
    """
    if store is None or vector_config is None or available_seconds < RESERVE_SECONDS:
        return None
    compact = getattr(store, "compact", None)
    if not callable(compact):
        return None

    storage_dir = Path(vector_config.storage_dir)
    db_path = storage_dir / "lancedb"
    try:
        footprint = measure_footprint(db_path, vector_config.table_name)
        reason = reason or compaction_due(footprint, read_state(storage_dir))
    except OSError:
        return None
    if reason is None:
        return None

    started = time.monotonic()
    receipt: dict[str, Any] = {
        "reason": reason,
        "started_at": utc_now(),
        "fragments_before": footprint.fragments,
        "manifests_before": footprint.manifests,
        "bytes_before": footprint.bytes,
    }
    try:
        compact()
    except Exception as exc:  # noqa: BLE001 - see docstring; upkeep never fails a drain.
        receipt["outcome"] = "failed"
        receipt["error"] = type(exc).__name__
    else:
        receipt["outcome"] = "compacted"
    after = measure_footprint(db_path, vector_config.table_name)
    receipt.update(
        finished_at=utc_now(),
        seconds=round(time.monotonic() - started, 3),
        fragments=after.fragments,
        manifests=after.manifests,
        bytes=after.bytes,
    )
    write_state(storage_dir, receipt)
    return receipt


#: Seconds an index build takes per row and dimension: on a copy of the pilot's shared store the build took 7.7 s
#: for 78,374 rows of 3,072 dimensions when the machine was quiet and 25.6 s when it was not, and this is three times
#: the slow rate.  A pass builds only when the estimate fits in what is left of it: the watchdog ends a pass that
#: outlives its budget.
INDEX_SECONDS_PER_ROW_DIMENSION = 3.2e-7
#: Seconds of the pass kept free beside the estimate, for the drain that follows.
INDEX_MARGIN_SECONDS = 20.0
#: How long each outcome stands before a pass looks again.  A failure is not retried sooner than this; a store
#: below the threshold or too large for the time left is looked at again as it grows or as a pass has more time.
INDEX_RECHECK = {
    "built": timedelta(days=1),
    "rebuilt": timedelta(days=1),
    "present": timedelta(days=1),
    "below_threshold": timedelta(hours=1),
    "deferred": timedelta(minutes=15),
    "failed": timedelta(hours=6),
    # Written before a build starts.  A pass the watchdog ended mid-build leaves it, and the next looks again only
    # after this long, as after a failure, instead of starting the same build on every pass.
    "started": timedelta(hours=6),
}


def index_if_due(store: Any, vector_config: Any, *, available_seconds: float,
                 now: datetime | None = None) -> dict[str, Any] | None:
    """Build the nearest-neighbour index when the table needs one and this pass has the time.  Returns the receipt.

    The receipt goes to ``index-state.json`` beside the store, where the doctor reads it.  Never raises: without
    the index a search is slower, never wrong, so a failed build is recorded and tried again after
    ``INDEX_RECHECK["failed"]``, not on every pass.
    """
    build = getattr(store, "ensure_vector_index", None)
    count = getattr(store, "count_rows", None)
    if store is None or vector_config is None or not callable(build) or not callable(count):
        return None
    storage_dir = Path(vector_config.storage_dir)
    moment = now or datetime.now(timezone.utc)
    state = read_state(storage_dir, filename=INDEX_STATE_FILENAME, schema=INDEX_STATE_SCHEMA)
    wait = INDEX_RECHECK.get(state.get("outcome"))
    try:
        checked = datetime.fromisoformat(str(state.get("checked_at")).replace("Z", "+00:00"))
    except ValueError:
        checked = None
    if wait is not None and checked is not None and timedelta(0) <= moment - checked < wait:
        return None
    receipt: dict[str, Any] = {"checked_at": moment.isoformat().replace("+00:00", "Z")}
    try:
        rows = int(count())
        estimate = rows * int(vector_config.dimensions) * INDEX_SECONDS_PER_ROW_DIMENSION
        receipt.update(rows=rows, estimate_seconds=round(estimate, 1))
        if estimate + INDEX_MARGIN_SECONDS > available_seconds:
            receipt["outcome"] = "deferred"
            receipt["available_seconds"] = round(available_seconds, 1)
        else:
            # The store decides: below the threshold it builds nothing, but an index of another kind is replaced.
            write_state(storage_dir, {**receipt, "outcome": "started"}, filename=INDEX_STATE_FILENAME,
                        schema=INDEX_STATE_SCHEMA)
            receipt.update(build(min_rows=VECTOR_INDEX_MIN_ROWS,
                                 timeout_seconds=max(1.0, available_seconds - INDEX_MARGIN_SECONDS / 2)))
    except Exception as exc:  # noqa: BLE001 - see docstring; upkeep never fails a drain.
        receipt["outcome"] = "failed"
        receipt["error"] = type(exc).__name__
    write_state(storage_dir, receipt, filename=INDEX_STATE_FILENAME, schema=INDEX_STATE_SCHEMA)
    return receipt


__all__ = ["INDEX_RECHECK", "RESERVE_SECONDS", "compact_if_due", "index_if_due"]
