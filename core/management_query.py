"""Bounded inventory for the local owner's management interface.

Inventory rows contain identities only. Content leaves through the normal
collection and inspection services, which recheck current visibility.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

from ..contracts import ContractError
from . import lineage
from .delete_storage import OBJECT_TABLES, canonical
from .retrieval import CollectionQuery
from .retrieval_storage import scope_digest


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


def inventory(tx, filters: MemoryFilter, *, after: str = "", limit: int = 200):
    """Enumerate a bounded page of live object heads without reading their bodies."""
    selects = []
    for kind, (table, key) in OBJECT_TABLES.items():
        if filters.kind is not None and filters.kind != kind:
            continue
        revision = "max(source_revision)" if kind == "event" else "current_revision"
        group = " GROUP BY event_id" if kind == "event" else ""
        selects.append(
            f"SELECT '{kind}' AS kind,{key} AS ref,{revision} AS revision,scope_id,project_id,branch_id,suppressed "
            f"FROM {table} WHERE read_blocked=0{group}"
        )
    scopes = sorted(tx.context.allowed_scope_ids)
    clauses = ["ref>?", f"scope_id IN ({','.join('?' for _ in scopes)})"]
    params = [after, *scopes]
    if filters.scope is not None:
        clauses.append("scope_id=?")
        params.append(filters.scope)
    return (
        tx._check()
        .execute(
            "SELECT * FROM ("
            + " UNION ALL ".join(selects)
            + ") WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ref LIMIT ?",
            (*params, limit),
        )
        .fetchall()
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


def decode_cursor(cursor, binding, epoch):
    """Reject stale or foreign cursors rather than silently skip changed rows."""
    if cursor is None:
        return ""
    try:
        value = json.loads(cursor)
        if set(value) != {"after", "binding", "epoch"} or type(value["after"]) is not str:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ContractError("INPUT_INVALID", "cursor") from None
    if value["binding"] != binding or value["epoch"] != epoch:
        raise ContractError("VERSION_CONFLICT", "cursor")
    return value["after"]


def browse(core, context, filters, *, limit=30, cursor=None) -> dict[str, Any]:
    """Read at most 200 identities per page; an empty page may still have a cursor."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ContractError("INPUT_INVALID", "limit")
    binding = page_binding(context, filters)
    with core.storage.read(context) as tx:
        epoch = tx.status().memory_epoch
        after = decode_cursor(cursor, binding, epoch)
        rows = inventory(tx, filters, after=after, limit=201)
        labels = {r["entry_id"]: dict(r) for r in tx._check().execute("SELECT * FROM entries")}
        selected = []
        for row in rows[:200]:
            after = row["ref"]
            view = partition(tx, context, row["project_id"], row["branch_id"])
            sources = witnesses(view, row)
            constrained = any((filters.agent, filters.session, filters.since, filters.until))
            if constrained and not any(filters.source_matches(s, labels, context.binding.agent_id) for s in sources):
                continue
            selected.append((dict(row), [source_metadata(s, labels, context.binding.agent_id) for s in sources]))
    items = []
    visited = ""
    for row, sources in selected:
        visited = row["ref"]
        scoped = replace(context, project_id=row["project_id"], branch_id=row["branch_id"])
        query = CollectionQuery(
            row["kind"],
            where=(("ref", row["ref"]), ("revision", str(row["revision"]))),
            page_size=1,
            memory_epoch=epoch,
            scope_digest=scope_digest(scoped),
            mode="history",
        )
        page = core.collection(scoped, query)
        item = next((x for x in page.items if x.revision == row["revision"]), None)
        if item is None or filters.query.casefold() not in item.content.casefold():
            continue
        items.append(dict(**row, content=item.content, sources=sources, state=item.temporal_status))
        if len(items) == limit:
            break
    with core.storage.read(context) as tx:
        if tx.status().memory_epoch != epoch:
            raise ContractError("VERSION_CONFLICT", "inventory")
    more = len(rows) > 200 or (len(items) == limit and visited != after)
    next_after = visited if len(items) == limit else after
    return dict(
        items=items,
        memory_epoch=epoch,
        next_cursor=canonical(dict(after=next_after, binding=binding, epoch=epoch)) if more else None,
    )
