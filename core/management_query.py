"""Bounded inventory for the local owner's management interface.

Inventory rows contain identities only. Content leaves through the normal
collection and inspection services, which recheck current visibility.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

from ..contracts import ContractError, utc_timestamp
from . import lineage
from .delete_storage import OBJECT_TABLES, canonical
from .retrieval import CandidateRef, SearchContext, SearchLimits
from .retrieval_storage import scope_digest

# The budget bounds scanning between hydration batches after inventory ordering.
# Every continuation repeats ordering, whose cost grows with the object count.
# Session selection has no deadline: a partial selection would leave sources.
SCAN_SECONDS = 30.0
_SCAN_BATCH = 256
_HYDRATE_BATCH = 16
_VERSION_TABLES = {
    "claim": "claim_versions",
    "episode": "episode_versions",
    "artifact": "artifact_versions",
    "reference": "reference_versions",
}


@dataclass(frozen=True)
class MemoryFilter:
    """Literal search and source filters; times are inclusive UTC instants."""

    kind: str | None = None
    scope: str | None = None
    agent: str | None = None
    session: str | None = None
    since: str | None = None
    until: str | None = None
    query: str = ""

    def __post_init__(self):
        if self.kind is not None and self.kind not in OBJECT_TABLES:
            raise ContractError("INPUT_INVALID", "kind")
        for key in ("scope", "agent", "session", "query"):
            value = getattr(self, key)
            if value is not None and (type(value) is not str or len(value) > 512):
                raise ContractError("INPUT_INVALID", key)
        for key in ("since", "until"):
            value = getattr(self, key)
            if value is not None:
                try:
                    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if moment.utcoffset() is None:
                        raise ValueError
                except (ValueError, AttributeError):
                    raise ContractError("INPUT_INVALID", key) from None
        if self.since and self.until and self._time(self.since) > self._time(self.until):
            raise ContractError("INPUT_INVALID", "time_range")

    @staticmethod
    def _time(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def source_matches(self, source, labels: dict, local_agent: str) -> bool:
        """Match one source, so different witnesses cannot satisfy separate filters."""
        if self.session is not None and source.session_id != self.session:
            return False
        label = labels.get(source.entry_id, {})
        names = {source.entry_id, label.get("display_name"), label.get("host")}
        if source.entry_id == "local":
            names.add(local_agent)
        if self.agent is not None and self.agent not in names:
            return False
        stamp = source.event["occurred_at"] or source.event["recorded_at"]
        return not (
            (self.since and self._time(stamp) < self._time(self.since))
            or (self.until and self._time(stamp) > self._time(self.until))
        )


def partition(tx, context, project, branch, *, writable=False):
    """Use one owner's transaction in a stored partition without widening its scopes.

    The caller has checked installation maintenance authority. These short-lived
    repository views share the outer connection and never commit separately.
    """
    return tx.with_context(replace(context, project_id=project, branch_id=branch), writable=writable)


def _inventory_select(kind, table, key):
    """Current identities and initial ordering timestamps, without content."""
    if kind == "event":
        revision = "max(source_revision)"
        # Source revisions can arrive out of order; source_id records insertion
        # order and keeps a delayed lower revision from moving an existing key.
        # Blocked revisions cannot contribute timestamps to output or cursors.
        stamp = (
            "(SELECT COALESCE(first.occurred_at,first.recorded_at) FROM source_events first "
            "WHERE first.event_id=h.event_id AND first.read_blocked=0 ORDER BY first.source_id LIMIT 1)"
        )
        join, group = "", " GROUP BY event_id"
    else:
        revision = "current_revision"
        stamp = "v.recorded_from" if kind == "claim" else "v.recorded_at"
        versions = _VERSION_TABLES[kind]
        join = (
            f" JOIN {versions} v ON v.{key}=h.{key} "
            f"AND v.revision=(SELECT min(first.revision) FROM {versions} first WHERE first.{key}=h.{key})"
        )
        group = ""
    return (
        f"SELECT '{kind}' AS kind,h.{key} AS ref,{revision} AS revision,"
        f"h.scope_id,h.project_id,h.branch_id,h.suppressed,management_time({stamp}) AS sort_time "
        f"FROM {table} h{join} WHERE h.read_blocked=0{group}"
    )


def _ordering_time(value):
    """Normalize stored times, clamping UTC overflow at the representable endpoint."""
    try:
        return utc_timestamp(value)
    except OverflowError:
        return "0001-01-01T00:00:00.000000+00:00" if value.startswith("0001") else "9999-12-31T23:59:59.999999+00:00"
    except ContractError:
        return "0001-01-01T00:00:00.000000+00:00"


def inventory(tx, filters: MemoryFilter, *, after=None):
    """Stream installation-wide live heads by a stable revision time.

    SQLite sorts once per call. Events use their first stored readable revision's
    occurrence time, or recording time if unknown; other kinds use their first
    revision's recording time. References break ties without a mutable epoch.
    Blocking an event's earliest readable revision can move its ordering key.
    """
    selects = []
    for kind, (table, key) in OBJECT_TABLES.items():
        if filters.kind is not None and filters.kind != kind:
            continue
        selects.append(_inventory_select(kind, table, key))
    scopes = sorted(tx.context.allowed_scope_ids)
    clauses = [f"scope_id IN ({','.join('?' for _ in scopes)})"]
    params = list(scopes)
    if after is not None:
        clauses.append("(sort_time<? OR (sort_time=? AND ref>?))")
        params.extend((after[0], after[0], after[1]))
    if filters.scope is not None:
        clauses.append("scope_id=?")
        params.append(filters.scope)
    connection = tx._check()
    # Normalize both clock and source timestamps without losing microseconds.
    # This function belongs to the connection only; it adds no stored schema.
    connection.create_function("management_time", 1, _ordering_time, deterministic=True)
    return connection.execute(
        "SELECT * FROM ("
        + " UNION ALL ".join(selects)
        + ") WHERE "
        + " AND ".join(clauses)
        + " ORDER BY sort_time DESC,ref",
        params,
    )


def witnesses(tx, row):
    """Load the current object's declared sources through scoped repositories."""
    pairs = (
        [(row["ref"], row["revision"])]
        if row["kind"] == "event"
        else lineage.evidence(tx._check(), row["kind"], row["ref"], row["revision"])
    )
    return tuple(source for ref, revision in pairs if (source := tx.source(ref, revision)) is not None)


def source_metadata(source, labels, local_agent):
    """Describe provenance without copying a source's text into an inventory row."""
    label = labels.get(source.entry_id, {})
    return dict(
        ref=source.ref,
        revision=source.revision,
        entry_id=source.entry_id,
        agent=label.get("display_name", local_agent),
        session=source.session_id,
        occurred_at=source.event["occurred_at"],
        recorded_at=source.event["recorded_at"],
    )


def page_binding(context, filters):
    """Bind cursors to the installation, owner scopes and exact filter values."""
    return hashlib.sha256(canonical([scope_digest(context), asdict(filters)]).encode()).hexdigest()


def decode_cursor(cursor, binding):
    """Validate an ordering key bound to one installation, owner and filter set."""
    if cursor is None:
        return None
    try:
        if type(cursor) is not str or len(cursor) > 2048 or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
        if type(value) is not dict or set(value) != {"after", "binding", "version"}:
            raise ValueError
        after = value["after"]
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or type(value["binding"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["binding"]) is None
            or (after is not None and not _valid_position(after))
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, binascii.Error, RecursionError):
        raise ContractError("INPUT_INVALID", "cursor") from None
    if value["binding"] != binding:
        raise ContractError("VERSION_CONFLICT", "cursor")
    return after


def _valid_position(after):
    """A canonical UTC instant and a bounded stable identity."""
    try:
        return (
            type(after) is list
            and len(after) == 2
            and type(after[0]) is str
            and utc_timestamp(after[0]) == after[0]
            and type(after[1]) is str
            and 1 <= len(after[1]) <= 240
            and re.fullmatch(r"[A-Za-z0-9._/-]+", after[1]) is not None
        )
    except (ContractError, OverflowError):
        return False


def _search_context(context, core):
    """History rendering exposes muted heads without changing recall's policy."""
    return SearchContext(
        query="collection",
        mode="history",
        as_of=None,
        focus_refs=(),
        limits=SearchLimits(),
        deadline=core.clock.monotonic() + SCAN_SECONDS,
        now=core.clock.utc_now(),
        trusted_context=context,
    )


def _hydrate_batch(core, tx, rows):
    """Use the collection reader in bounded partition views of the same snapshot."""
    groups = {}
    for row in rows:
        groups.setdefault((row["project_id"], row["branch_id"]), []).append(row)
    hydrated = {}
    for key, selected in groups.items():
        view = partition(tx, tx.context, *key)
        context = _search_context(view.context, core)
        candidates = tuple(CandidateRef(r["kind"], r["ref"], r["revision"], "exact_ref") for r in selected)
        objects = core.recall_pipeline.storage_reader.collection_objects(view, context, candidates)
        hydrated[key] = view, objects
    return hydrated


def _matching_item(view, row, obj, filters, labels):
    """Match rendered text and one witness satisfying every source constraint."""
    if filters.query.casefold() not in obj.content.casefold():
        return None
    sources = witnesses(view, row)
    agent = view.context.binding.agent_id
    if any((filters.agent, filters.session, filters.since, filters.until)) and not any(
        filters.source_matches(source, labels, agent) for source in sources
    ):
        return None
    return dict(
        **row,
        content=obj.content,
        sources=[source_metadata(s, labels, agent) for s in sources],
        state=obj.temporal_status,
    )


def _scan(core, tx, rows, filters, limit, deadline, after):
    """Scan small hydration batches, completing at least one even after a slow sort.

    Prefetch and rendering share a bounded batch so expiry cannot leave a
    cursor at its starting position. The deadline is checked between those
    batches, including when an inventory page spans several partitions.
    """
    labels = {r["entry_id"]: dict(r) for r in tx._check().execute("SELECT * FROM entries")}
    items, examined = [], 0
    while batch := rows.fetchmany(_SCAN_BATCH):
        for start in range(0, len(batch), _HYDRATE_BATCH):
            if examined and time.monotonic() >= deadline:
                return items, examined, after, "time_budget"
            selected = batch[start : start + _HYDRATE_BATCH]
            hydrated = _hydrate_batch(core, tx, selected)
            for index, row in enumerate(selected, start=start):
                after = [row["sort_time"], row["ref"]]
                examined += 1
                view, objects = hydrated[(row["project_id"], row["branch_id"])]
                if (obj := next(objects)) is not None:
                    if (item := _matching_item(view, dict(row), obj, filters, labels)) is not None:
                        items.append(item)
                if len(items) == limit:
                    more = index + 1 < len(batch) or rows.fetchone() is not None
                    return items, examined, after, "limit" if more else "complete"
    return items, examined, after, "complete"


def session_refs(core, tx, filters):
    """Select at most 2,000 source heads in the caller's preview snapshot.

    Selection must reach exhaustion before previewing; a time-limited partial
    selection would let a session deletion silently leave matching sources.
    """
    filters = replace(filters, kind="event")
    rows = inventory(tx, filters)
    items, _examined, _after, _reason = _scan(core, tx, rows, filters, 2001, float("inf"), None)
    if len(items) > 2000:
        raise ContractError("INPUT_INVALID", "selection_size")
    return [item["ref"] for item in items]


def browse(core, context, filters, *, limit=30, cursor=None) -> dict[str, Any]:
    """Search one snapshot up to the match limit or a fixed wall-clock budget."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ContractError("INPUT_INVALID", "limit")
    binding = page_binding(context, filters)
    after = decode_cursor(cursor, binding)
    deadline = time.monotonic() + SCAN_SECONDS
    with core.storage.read(context) as tx:
        epoch = tx.memory_epoch()
        rows = inventory(tx, filters, after=after)
        items, examined, after, reason = _scan(core, tx, rows, filters, limit, deadline, after)
    return dict(
        items=items,
        memory_epoch=epoch,
        examined=examined,
        scan_complete=reason == "complete",
        stop_reason=reason,
        next_cursor=(
            base64.urlsafe_b64encode(canonical(dict(after=after, binding=binding, version=1)).encode())
            .decode()
            .rstrip("=")
            if reason != "complete"
            else None
        ),
    )
