"""Expire the vectors of tool outputs older than the retention window.

Sits beside ``vector_upkeep.py``: ``runtime/instance.py`` has the single call
site, at the start of a drain, just before the compaction that reclaims what
a pass deleted.  The source text, its lexical index and everything derived
from it (claims, episodes, candidates) stay; only the vector goes.  An expired
tool output is still found by its words and through whatever cites it, never
again by meaning alone.

Why tool outputs, and why a window: on a busy instance four in five captured
sources were tool output, each carrying a 12 KB vector -- the bulk of the
store's daily growth -- while a few hundred of 170,000 sources ever became
claim evidence.  A window keeps recent work fully searchable and lets the old
bulk go, at a pace the operator sets (``vector.tool_output_retention_days``).

Not responsible for: deciding what a tool output is (the source's ``role``),
the native delete (the store's ``delete_by_ids``), or reclaiming the space
(``vector_upkeep.compact_if_due``, which the drain runs next).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Callable

from .validation import utc_now

#: Seconds of the drain budget set aside for one pass; below it the pass waits.
RESERVE_SECONDS = 8.0
#: Sources one pass may expire.  A backlog is cleared over consecutive drains,
#: each deleting one batch, so no single pass holds the store for long.
BATCH_LIMIT = 2000
#: With no backlog a pass runs this often.  The candidate scan reads the source
#: table once (0.2 s for 170,000 sources), so hourly costs nothing noticeable.
PASS_INTERVAL = timedelta(hours=1)
STATE_FILENAME = "retention-state.json"
STATE_SCHEMA = "scope-recall.vector-retention.v1"


def expire_if_due(store: Any, config: Any, storage: Any, context: Any, *,
                  available_seconds: float, now: datetime | None = None) -> dict[str, Any] | None:
    """Expire one batch when a window is set and a pass is due.  Returns the receipt, or ``None``.

    Never raises.  A pass that cannot run leaves the store and the ledger as
    they were, and the next drain tries again; failing the drain would trade a
    tidiness problem for an availability one.
    """
    vector = getattr(config, "vector", None)
    days = getattr(vector, "tool_output_retention_days", 0) if vector is not None else 0
    delete = getattr(store, "delete_by_ids", None)
    if store is None or not days or not callable(delete) or available_seconds < RESERVE_SECONDS:
        return None
    moment = now or datetime.now(timezone.utc)
    storage_dir = Path(vector.storage_dir)
    if not pass_due(read_state(storage_dir), days, now=moment):
        return None
    cutoff = (moment - timedelta(days=days)).isoformat()
    started = time.monotonic()
    receipt: dict[str, Any] = {"started_at": _stamp(moment), "retention_days": days, "cutoff": cutoff}
    try:
        by_reason = expire_tool_vectors(
            storage, context, delete, embedding_space=config.embedding_space_id(),
            cutoff=cutoff, limit=BATCH_LIMIT, remaining_seconds=available_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - see docstring; upkeep never fails a drain.
        receipt.update(outcome="failed", error=type(exc).__name__, expired=0, backlog=False)
    else:
        expired = sum(by_reason.values())
        receipt.update(outcome="expired" if expired else "nothing_due", expired=expired,
                       by_reason=dict(sorted(by_reason.items())), backlog=expired >= BATCH_LIMIT)
    elapsed = time.monotonic() - started
    receipt.update(finished_at=_stamp(moment + timedelta(seconds=elapsed)), seconds=round(elapsed, 3))
    write_state(storage_dir, receipt)
    return receipt


def pass_due(state: dict[str, Any], days: int, *, now: datetime) -> bool:
    """A pass is due at once after a backlog or a changed window, else hourly."""
    if state.get("retention_days") != days or state.get("backlog"):
        return True
    last = _parse_time(state.get("finished_at"))
    return last is None or now - last >= PASS_INTERVAL


#: A tool output the capture filter withheld, in this release's form and the
#: 2.0 release's (see ``core/admission.py``).  Cheap enough to test first.
#: Both conditions are read against ``source_events e``; ``maintenance/shared_import.py``
#: uses them too, so an import never queues an embedding this pass would expire at once.
OMITTED_TOOL_OUTPUT = ("e.content LIKE 'Tool execution summary%' AND "
                       "(e.content LIKE '%output omitted%' OR e.content LIKE '%output_preview=omitted%')")
#: An earlier, still readable tool output in the same scope with the same content.
REPEATED_TOOL_OUTPUT = """EXISTS (SELECT 1 FROM source_events f WHERE f.scope_id=e.scope_id AND f.role='tool'
    AND f.content_sha256=e.content_sha256 AND f.read_blocked=0
    AND COALESCE(json_extract(f.extra_json,'$._scope_recall_admission.disposition'),'')!='source_only'
    AND (f.persisted_at<e.persisted_at OR (f.persisted_at=e.persisted_at AND f.event_id<e.event_id)))"""


def expire_tool_vectors(storage: Any, context: Any, delete: Callable[[list[str]], Any], *,
                        embedding_space: str, cutoff: str, limit: int, remaining_seconds: float) -> Counter:
    """Delete the vectors of the oldest expired tool outputs, then record them by reason.

    A tool output is expired when it entered the store (``persisted_at``)
    before the cutoff, so imported history gets the full window from the day
    of its import.  Two kinds go at once, whatever their age: a repeat of an
    earlier tool output in the same scope (the earlier copy keeps its vector),
    and a summary the capture filter left in place of an output it withheld.
    Both are what the intake gate now keeps as sources only; the pass clears
    what older releases embedded.  Only sources whose embed work finished are
    touched; one still waiting to be embedded is left for a later pass.

    The store goes first.  A pass that dies between the two steps leaves
    vectors gone and rows unwritten, and the next pass repeats an idempotent
    delete.  The other order would leave vectors the ledger says are gone, and
    nothing would ever come back for them.
    """
    scopes = sorted(context.allowed_scope_ids)
    marks = ",".join("?" for _ in scopes)
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        rows = tx._check().execute(
            f"""SELECT e.event_id,e.source_revision,
                   CASE WHEN {OMITTED_TOOL_OUTPUT} THEN 'omitted' WHEN e.persisted_at<? THEN 'window' ELSE 'repeat' END AS reason
            FROM source_events e
            WHERE e.role='tool' AND e.scope_id IN ({marks})
              AND (({OMITTED_TOOL_OUTPUT}) OR e.persisted_at<? OR {REPEATED_TOOL_OUTPUT})
              AND EXISTS (SELECT 1 FROM work_items w WHERE w.work_type='embed' AND w.subject_ref=e.event_id
                          AND w.subject_revision=e.source_revision AND w.state='done')
              AND NOT EXISTS (SELECT 1 FROM expired_vectors x WHERE x.source_ref=e.event_id
                              AND x.source_revision=e.source_revision)
            ORDER BY e.persisted_at,e.event_id LIMIT ?""",
            (cutoff, *scopes, cutoff, limit),
        ).fetchall()
    if not rows:
        return Counter()
    delete([f"p10:{ref}@{revision}:{embedding_space}" for ref, revision, _reason in rows])
    expired_at = utc_now()
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        tx._check(write=True).executemany(
            "INSERT OR IGNORE INTO expired_vectors(source_ref,source_revision,expired_at,reason) VALUES (?,?,?,?)",
            [(ref, revision, expired_at, reason) for ref, revision, reason in rows],
        )
    return Counter(reason for _ref, _revision, reason in rows)


def read_state(storage_dir: Path) -> dict[str, Any]:
    """The last pass's receipt, or an empty mapping when there has been none."""
    try:
        raw = json.loads((Path(storage_dir) / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA:
        return {}
    return raw


def write_state(storage_dir: Path, payload: dict[str, Any]) -> None:
    """Record a receipt.  Never raises: this is a report, not a commitment."""
    directory = Path(storage_dir)
    record = {"schema": STATE_SCHEMA, **payload}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f"{STATE_FILENAME}.partial"
        partial.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(partial, directory / STATE_FILENAME)
    except OSError:
        return


def _stamp(moment: datetime) -> str:
    """The ``Z``-suffixed form every worker receipt carries (``validation.utc_now``)."""
    return moment.isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


__all__ = ["BATCH_LIMIT", "PASS_INTERVAL", "RESERVE_SECONDS", "expire_if_due", "expire_tool_vectors", "pass_due"]
