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
from pathlib import Path
from typing import Any

from ..vector.compaction import compaction_due, measure_footprint, read_state, write_state
from .validation import utc_now

#: Seconds of the drain budget set aside for one pass.  Measured: 0.12 s when
#: there is nothing to do, 3.5 s to clear a 2,243-fragment backlog.  Below this
#: the pass is skipped rather than started and abandoned half-way.
RESERVE_SECONDS = 8.0


def compact_if_due(store: Any, vector_config: Any, *, available_seconds: float) -> dict[str, Any] | None:
    """Compact when the policy says so.  Returns the receipt, or ``None``.

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
        reason = compaction_due(footprint, read_state(storage_dir))
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


__all__ = ["RESERVE_SECONDS", "compact_if_due"]
