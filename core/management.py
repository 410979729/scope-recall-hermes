"""The local store owner's API, shared by human-facing management clients.

An explicit installation capability admits this API. Agent tool DTOs cannot
mint it; ordinary agent operations retain their captured-message authority.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, replace
from typing import Any

from ..contracts import ContractError
from .delete_storage import OBJECT_TABLES, canonical
from .management_query import MemoryFilter, browse, partition, session_refs, source_metadata, witnesses
from .management_revision import correct
from .restore import InstallationMaintenance, _maintenance_context
from .suppression import release


class MemoryManagement:
    """Manage a bound installation, preserving Core visibility and version checks."""

    def __init__(self, core, authority: InstallationMaintenance):
        self.core = core
        self.authority = authority
        self._context()

    def _context(self):
        return _maintenance_context(self.core.storage, self.authority)

    def list(self, filters: MemoryFilter | None = None, *, limit=30, cursor=None):
        """Return one inventory page, with an opaque cursor for the next page."""
        return browse(self.core, self._context(), filters or MemoryFilter(), limit=limit, cursor=cursor)

    def _location(self, tx, ref):
        kind = str(ref).split("-", 1)[0]
        if kind not in OBJECT_TABLES:
            raise ContractError("SOURCE_MISSING", "ref")
        table, key = OBJECT_TABLES[kind]
        head = "max(source_revision)" if kind == "event" else "current_revision"
        row = (
            tx._check()
            .execute(f"SELECT {head} AS revision,scope_id,project_id,branch_id FROM {table} WHERE {key}=?", (ref,))
            .fetchone()
        )
        if row is None or row["scope_id"] not in tx.context.allowed_scope_ids:
            raise ContractError("SOURCE_MISSING", "ref")
        return kind, row

    def show(self, ref, *, revision=None):
        """Inspect one current or historical version without permitting raw-source edits."""
        context = self._context()
        with self.core.storage.read(context) as tx:
            _kind, row = self._location(tx, ref)
            scoped = replace(context, project_id=row["project_id"], branch_id=row["branch_id"])
            if revision is None:
                revision = row["revision"]
        return asdict(self.core.inspect_object(scoped, ref, revision))

    def history(self, ref, *, before=None, limit=30):
        """Return a bounded newest-first history page, checked by normal inspection."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError("INPUT_INVALID", "limit")
        context = self._context()
        with self.core.storage.read(context) as tx:
            kind, _row = self._location(tx, ref)
            table, key = OBJECT_TABLES[kind]
            revision_key = "source_revision" if kind == "event" else "revision"
            if kind != "event":
                table = {
                    "claim": "claim_versions",
                    "episode": "episode_versions",
                    "artifact": "artifact_versions",
                    "reference": "reference_versions",
                }[kind]
            revisions = [
                r[0]
                for r in tx._check().execute(
                    f"SELECT {revision_key} FROM {table} WHERE {key}=? AND {revision_key}<? "
                    f"ORDER BY {revision_key} DESC LIMIT ?",
                    (ref, before if before is not None else 2**63 - 1, limit + 1),
                )
            ]
        items = [self.show(ref, revision=r) for r in revisions[:limit]]
        return dict(items=items, next_before=revisions[limit - 1] if len(revisions) > limit else None)

    def export_reader(self, filters: MemoryFilter):
        """Open one owner-authorized, page-at-a-time stream of the visible store."""
        from .management_export import MemoryExport

        return MemoryExport(self.core, self._context(), filters)

    def _plan(self, tx, refs, mode, expected=None):
        groups = {}
        context = self._context()
        for ref in sorted(set(refs)):
            _kind, row = self._location(tx, ref)
            key = (row["project_id"], row["branch_id"])
            view = partition(tx, context, *key)
            target = view.deletions.target(ref)
            if expected is not None and expected.get(ref) != target.revision:
                raise ContractError("VERSION_CONFLICT", "expected_revisions")
            groups.setdefault(key, []).append(target)
        affected, selected, sources = [], [], {}
        labels = {r["entry_id"]: dict(r) for r in tx._check().execute("SELECT * FROM entries")}
        for key, targets in groups.items():
            view = partition(tx, context, *key)
            closure = view.deletions.closure(tuple(targets), delete=mode == "delete")
            affected.extend(asdict(t) for t in closure)
            selected.extend(asdict(t) for t in targets)
            for target in closure:
                row = dict(ref=target.ref, revision=target.revision, kind=target.kind)
                for source in witnesses(view, row):
                    sources[source.ref] = source_metadata(source, labels, context.binding.agent_id)
        if not selected or len(affected) > 2000:
            raise ContractError("INPUT_INVALID", "selection_size")
        result: dict[str, Any] = dict(
            mode=mode,
            memory_epoch=tx.memory_epoch(),
            targets=sorted(selected, key=lambda t: t["ref"]),
            affected=sorted(affected, key=lambda t: t["ref"]),
            sources=sorted(sources.values(), key=lambda s: s["ref"]),
        )
        result["preview_id"] = hashlib.sha256(canonical(result).encode()).hexdigest()
        return result

    def preview(self, refs, *, mode="delete", expected=None):
        """Describe the exact source closure before a delete or suppression is confirmed."""
        if mode not in {"delete", "suppress", "unmute"} or not refs or len(refs) > 2000:
            raise ContractError("INPUT_INVALID", "selection")
        with self.core.storage.read(self._context()) as tx:
            return self._plan(tx, refs, mode, expected)

    def forget_session(self, filters: MemoryFilter):
        """Preview a session or time interval, refusing an unbounded whole-store selection."""
        if not (filters.session or filters.since or filters.until):
            raise ContractError("INPUT_INVALID", "session_or_time_required")
        with self.core.storage.read(self._context()) as tx:
            return self._plan(tx, session_refs(self.core, tx, filters), "delete")

    def apply(self, preview):
        """Commit precisely the reviewed closure, or refuse if the inventory changed."""
        context = self._context()
        try:
            refs = [t["ref"] for t in preview["targets"]]
            expected = {t["ref"]: t["revision"] for t in preview["targets"]}
            mode, identity = preview["mode"], preview["preview_id"]
        except (TypeError, KeyError):
            raise ContractError("INPUT_INVALID", "preview") from None
        if mode not in {"delete", "suppress", "unmute"}:
            raise ContractError("INPUT_INVALID", "mode")
        with self.core.storage.write(context, remaining_seconds=10) as tx:
            current = self._plan(tx, refs, mode, expected)
            if current["preview_id"] != identity:
                raise ContractError("VERSION_CONFLICT", "preview")
            receipts, groups = [], {}
            for target in current["targets"]:
                groups.setdefault((target["project_id"], target["branch_id"]), []).append(target["ref"])
            for key, selected in groups.items():
                view = partition(tx, context, *key, writable=True)
                request = dict(
                    mode=mode, target_refs=selected, expected_revisions={ref: expected[ref] for ref in selected}
                )
                targets = tuple(view.deletions.target(ref) for ref in selected)
                affected = view.deletions.closure(targets, delete=mode == "delete")
                if mode == "unmute":
                    release(view, affected, now=self.core.clock.utc_now())
                    receipts.append(dict(mode="unmute", affected_objects=len(affected), suppressed=False))
                    continue
                op = view.deletions.block(request, affected, now=self.core.clock.utc_now())
                receipts.append(view.deletions.receipt(op))
            self._audit(tx, identity, mode, current["affected"])
            return dict(
                preview_id=identity,
                receipts=receipts,
                memory_epoch=tx.status().memory_epoch,
                actor="local_user_management",
            )

    def _audit(self, tx, identity, action, targets):
        tx._check(write=True).execute(
            "INSERT INTO management_actions VALUES (?,'local_user_management',?,?,?,?) "
            "ON CONFLICT(action_id) DO NOTHING",
            (identity, action, canonical(targets), self.core.clock.utc_now(), tx.status().memory_epoch),
        )

    def revise(self, ref, *, expected_revision, value):
        """Correct a derived claim; a null value withdraws it while keeping history."""
        context = self._context()
        with self.core.storage.write(context, remaining_seconds=10) as tx:
            kind, row = self._location(tx, ref)
            if kind != "claim":
                raise ContractError("ACCESS_DENIED", "source_text_immutable")
            view = partition(tx, context, row["project_id"], row["branch_id"], writable=True)
            action, item, source = correct(view, self.core.clock, ref, expected_revision, value)
            self._audit(tx, action, "revise", [dict(ref=ref, revision=item.revision, source_ref=source)])
            return dict(
                item=asdict(item),
                actor="local_user_management",
                action_id=action,
                source_ref=source,
                memory_epoch=tx.status().memory_epoch,
            )
