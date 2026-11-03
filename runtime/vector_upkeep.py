"""Bounded worker indexing and embedding backfill scheduling."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ..core.deadline import RequestDeadline, remaining_seconds, using_request_deadline
from ..core.failure_policy import CAPABILITY_RECHECK
from ..vector.compaction import (
    EMBED_BACKFILL_STATE_SCHEMA,
    embed_backfill_filename,
    read_state,
    write_state,
)
from ..vector.defaults import INDEX_BATCH_ROWS


def _index_pages(method, batch_rows: int) -> dict[str, Any]:
    """Publish bounded pages until the slice ends, retaining its committed progress.

    A page may reach the deadline while writing an unpublished suffix. Its raw
    rows and earlier pages remain valid; that local yield is not a capability
    failure which should defer the next worker pass.
    """
    receipt: dict[str, Any] = {"outcome": "indexed", "rows": 0}
    for _ in range(64):
        try:
            receipt = method(max_rows=batch_rows)
        except TimeoutError:
            left = remaining_seconds()
            if left is None or left > 0:
                raise
            return {**receipt, "yielded": True, "next_batch_rows": max(1, batch_rows // 2)}
        if receipt.get("outcome") != "indexed" or receipt.get("rows", 0) < batch_rows:
            break
        if (remaining_seconds() or 0) <= 0.5:
            return {**receipt, "next_batch_rows": batch_rows}
        batch_rows = min(INDEX_BATCH_ROWS, batch_rows * 2)
    return receipt


def index_if_due(
    store: Any, vector_config: Any, *, available_seconds: float, now: datetime | None = None
) -> dict[str, Any] | None:
    """Publish bounded cache batches within the worker's existing remaining budget."""
    method = cast(Callable[..., dict[str, Any]], getattr(store, "maintain_index", None))
    if store is None or vector_config is None or not callable(method) or available_seconds < 1:
        return None
    state = read_state(Path(vector_config.storage_dir))
    retry = float(state.get("retry_after", 0))
    if (
        state.get("error") == "TimeoutError"
        and not state.get("index_fault")
        and getattr(store, "backend", None) == "sqlite"
    ):
        # Older worker receipts classified an exhausted cache-build slice as a
        # capability failure. A completed import with uncached rows can resume
        # that bounded local work immediately; actual I/O failures keep backoff.
        try:
            details = store.diagnostics()
            if not any(item["state"] != "complete" for item in details["imports"]):
                if details["cached_rows"] < store.count_rows():
                    retry = 0
        except (sqlite3.Error, OSError, ValueError, KeyError):
            pass
    if retry > (now or datetime.now(UTC)).timestamp():
        return {"outcome": "held", "retry_after": retry}
    try:
        outer = remaining_seconds()
        budget = available_seconds if outer is None else min(available_seconds, max(0.0, outer))
        # Short worker slices start conservatively. A slice which cannot
        # publish its batch leaves a smaller durable hint, down to one row;
        # repeating a too-large rolled-back batch would otherwise never move.
        batch_rows = INDEX_BATCH_ROWS if budget > 8 else 512
        hint = state.get("next_batch_rows")
        if type(hint) is int:
            batch_rows = min(batch_rows, max(1, hint))
        with using_request_deadline(RequestDeadline.from_budget(min(30.0, budget))):
            receipt = _index_pages(method, batch_rows)
            result = {"outcome": receipt["outcome"], "retry_after": 0}
            if "next_batch_rows" in receipt:
                result["next_batch_rows"] = receipt["next_batch_rows"]
            write_state(Path(vector_config.storage_dir), result)
            return receipt
    except Exception as exc:  # noqa: BLE001 - raw vectors stay available when indexing fails.
        return record_vector_failure(vector_config, type(exc).__name__, now=now, index_fault=True)


#: The backfill of an import's embeddings (``core.index_rebuild.queue_import_embeddings``) keeps where it stopped beside
#: the store's other upkeep, a file for each worker partition (``embed_backfill_filename``), where the doctor reads its
#: outcome.  The cursor
#: is its progress: a pass the watchdog ends loses at most one page, and a page looked at twice queues nothing twice
#: (an embedding already queued is not selected).
#: How long a finished backfill stands before a pass looks through the imports again, for a later import's history.
EMBED_BACKFILL_RECHECK = timedelta(days=1)
#: Sources looked at per pass, at most.  No more join the queue than take it to its ceiling.
EMBED_BACKFILL_PAGE = 64


def backfill_if_due(
    storage: Any,
    context: Any,
    vector_config: Any,
    *,
    now: datetime | None = None,
    yield_to: frozenset[str] = frozenset(),
    yield_ceiling: int | None = None,
) -> dict | None:
    """Queue the next page of an import's embeddings, unless the last look found none left within a day.  While
    work of a type in ``yield_to`` is ready, the queue is kept to ``yield_ceiling`` (``queue_import_embeddings``).

    Returns the receipt, also written to the partition's state file.  Never raises: an embedding queued later is
    found later, never wrongly.
    """
    if storage is None or context is None or vector_config is None:
        return None
    storage_dir = Path(vector_config.storage_dir)
    moment = now or datetime.now(UTC)
    filename = embed_backfill_filename(context.allowed_scope_ids, context.project_id, context.branch_id)
    state = read_state(storage_dir, filename=filename, schema=EMBED_BACKFILL_STATE_SCHEMA)
    finished = state.get("outcome") == "finished"
    if finished:
        try:
            checked = datetime.fromisoformat(str(state.get("checked_at")).replace("Z", "+00:00"))
        except ValueError:
            checked = None
        if checked is not None and timedelta(0) <= moment - checked < EMBED_BACKFILL_RECHECK:
            return None
    after_key = None if finished else state.get("after_key")
    # The total runs on across the daily looks: the embeddings this partition queued so far.
    earlier = int(state.get("queued_total") or 0)
    receipt: dict[str, Any] = {"checked_at": moment.isoformat().replace("+00:00", "Z")}
    try:
        from ..core.index_rebuild import queue_import_embeddings

        page = queue_import_embeddings(
            storage,
            context,
            after_key=after_key,
            limit=EMBED_BACKFILL_PAGE,
            yield_to=yield_to,
            now=moment,
            **({} if yield_ceiling is None else {"yield_ceiling": yield_ceiling}),
        )
        receipt.update(
            after_key=list(page["after_key"]),
            queued=page["queued"],
            queued_total=earlier + page["queued"],
            outcome="held" if page["held"] else "finished" if page["finished"] else "progress",
        )
    except Exception as exc:  # noqa: BLE001 - see docstring; upkeep never fails a drain.
        receipt.update(after_key=after_key, queued_total=earlier, outcome="failed", error=type(exc).__name__)
    write_state(storage_dir, receipt, filename=filename, schema=EMBED_BACKFILL_STATE_SCHEMA)
    return receipt


def respace_if_due(
    storage: Any,
    context: Any,
    space_id: str | None,
    *,
    now: datetime | None = None,
    yield_to: frozenset[str] = frozenset(),
    yield_ceiling: int | None = None,
) -> dict | None:
    """Reopen the next page of an operator's re-embed run (``respace-embeddings``) for this worker's space.

    The embed queue is topped up as the import backfill tops it up (``queue_import_embeddings``): to
    ``IMPORT_EMBED_QUEUE_CEILING``, and to ``yield_ceiling`` while work of a type in ``yield_to`` is ready, so a
    message captured now and an evaluation that waits still move.  A run reopens rows of every partition, so the queue
    it counts is the store's (``embed_queue``): counted as this worker's alone, a partition it cannot see would take a
    page every pass however much of it waited.  The run is looked at in a read; the write, and the writer lease it
    takes, come only when a run into this space has room to go on.  Returns the page's receipt, ``None`` when there is
    no run.  Never raises: a page that failed changed nothing and is tried again next drain.
    """
    if storage is None or context is None or not space_id:
        return None
    moment = (now or datetime.now(UTC)).isoformat()
    try:
        from ..core.index_rebuild import IMPORT_EMBED_QUEUE_CEILING

        with storage.read(context) as tx:
            run = tx.work.respace_run()
            if run is None or run["completed"]:
                return None
            if run["embedding_space"] != space_id:
                return {"outcome": "space_mismatch", "embedding_space": run["embedding_space"]}
            ceiling = IMPORT_EMBED_QUEUE_CEILING
            if yield_to and yield_ceiling is not None and tx.work.other_work_ready(now=moment, kinds=yield_to):
                ceiling = yield_ceiling
            room = ceiling - tx.work.embed_queue()["pending"]
        if room <= 0:
            return {"outcome": "held", "reopened": 0, "next_work_id": run["next_work_id"]}
        with storage.write(context) as tx:
            return tx.work.respace_page(space_id, now=moment, room=room)
    except Exception as exc:  # noqa: BLE001 - upkeep never fails a drain; the receipt carries the failure.
        return {"outcome": "failed", "error": type(exc).__name__}


__all__ = [
    "EMBED_BACKFILL_RECHECK",
    "backfill_if_due",
    "index_if_due",
    "respace_if_due",
]


def next_vector_wake(config, now: datetime) -> tuple[datetime, str] | None:
    """Schedule unfinished companion pages independently of model work, without loading extensions."""
    vector = getattr(config, "vector", None)
    if vector is None:
        return None
    root = Path(vector.storage_dir)
    path = root / "vectors.sqlite3"
    retry = float(read_state(root).get("retry_after", 0))
    retry_at = max(now, datetime.fromtimestamp(retry, UTC))
    if not path.is_file():
        return (
            (retry_at, "vector_import") if (root / "vector.sqlite3").is_file() or (root / "lancedb").is_dir() else None
        )
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            imports = conn.execute("SELECT min(retry_after) FROM vector_imports WHERE state!='complete'").fetchone()[0]
            if imports is not None:
                return (max(now, datetime.fromtimestamp(imports, UTC)), "vector_import")
            meta = dict(conn.execute("SELECT key,value FROM vector_meta"))
            latest = conn.execute("SELECT coalesce(max(seq),0) FROM vector_records").fetchone()[0]
            deleting = conn.execute("SELECT 1 FROM index_deletes LIMIT 1").fetchone()
            if latest <= int(meta.get("index_watermark", 0)) and not deleting:
                from ..vector.cache import MappedIndex

                details = MappedIndex(path, int(meta["dimensions"])).diagnostics(conn)
                if details["cache_ready"]:
                    wakes = []
                    if details["collection_pending"]:
                        wakes.append(
                            (max(now, datetime.fromtimestamp(details["collect_after"], UTC)), "vector_collect")
                        )
                    if details["cached_rows"]:
                        wakes.append(
                            (max(now, datetime.fromtimestamp(details["verify_after"], UTC)), "vector_integrity")
                        )
                    return min(wakes) if wakes else None
            retry = max(retry, float(meta.get("index_retry_after", 0)))
            return (max(now, datetime.fromtimestamp(retry, UTC)), "vector_index")
    except (sqlite3.Error, OSError, ValueError, KeyError):
        return (retry_at, "vector_store_unavailable")


def release_requested_reembedding(storage, context, store, space_id: str) -> bool:
    """Release a missing-input hold using durable authorization rather than this pass's progress receipt."""
    method = getattr(store, "allow_requested_reembedding", None)
    if not callable(method):
        return False
    with storage.read(context) as tx:
        authorization = tx.work.respace_authorization(space_id)
    return bool(method(authorization))


def record_vector_failure(
    vector_config, error: str, *, now: datetime | None = None, index_fault: bool = False
) -> dict[str, Any]:
    """Persist a bounded retry outside the raw file so an unreadable file cannot hide its recovery time."""
    moment = now or datetime.now(UTC)
    receipt = {"outcome": "failed", "error": error, "retry_after": moment.timestamp() + CAPABILITY_RECHECK.delay()}
    if index_fault:
        # A confirmed fault keeps its retry even if an older timeout receipt
        # would otherwise permit an unfinished native cache build to resume.
        receipt["index_fault"] = True
    if vector_config is not None:
        write_state(Path(vector_config.storage_dir), receipt)
    return receipt
