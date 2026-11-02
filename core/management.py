"""The local store owner's API, shared by human-facing management clients.

An explicit installation capability admits this API. Agent tool DTOs cannot
mint it; ordinary agent operations retain their captured-message authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from typing import Any
from uuid import uuid4

from ..contracts import ContractError
from .delete_storage import OBJECT_TABLES, canonical
from .importance import set_importance
from .management_query import MemoryFilter, browse, partition, session_refs, source_metadata, witnesses
from .management_revision import correct
from .quarantine_management import preview_release, release_source, review_page
from .restore import InstallationMaintenance, maintenance_context
from .secret_patterns import contains_secret_like_text
from .suppression import release


class MemoryManagement:
    """Manage a bound installation, preserving Core visibility and version checks."""

    def __init__(self, core, authority: InstallationMaintenance, *, entry_grants=None):
        self.core = core
        self.authority = authority
        self.entry_grants = entry_grants
        self._context()

    def _context(self):
        return maintenance_context(self.core.storage, self.authority)

    def memory_instructions(self, scope_id):
        """Read the owner's instruction history for one authorized scope, newest first."""
        context = self._context()
        if scope_id not in context.allowed_scope_ids:
            raise ContractError("ACCESS_DENIED", "scope_id")
        with self.core.storage.read(context) as tx:
            return [
                dict(row)
                for row in tx._check().execute(
                    "SELECT scope_id,version,text,changed_at,changed_by FROM scope_memory_instructions "
                    "WHERE scope_id=? ORDER BY version DESC",
                    (scope_id,),
                )
            ]

    def set_memory_instruction(self, scope_id, text):
        """Append one owner instruction version; source and fact rows are unchanged."""
        context = self._context()
        if scope_id not in context.allowed_scope_ids:
            raise ContractError("ACCESS_DENIED", "scope_id")
        if type(text) is not str or len(text) > 2000:
            raise ContractError("INPUT_INVALID", "instruction_max_2000_chars")
        if contains_secret_like_text(text):
            raise ContractError("SECRET_DETECTED", "memory_instruction")
        with self.core.storage.write(context, remaining_seconds=10) as tx:
            conn = tx._check()
            version = conn.execute(
                "SELECT coalesce(max(version),0)+1 FROM scope_memory_instructions WHERE scope_id=?", (scope_id,)
            ).fetchone()[0]
            changed_at = self.core.clock.utc_now()
            conn.execute(
                "INSERT INTO scope_memory_instructions VALUES(?,?,?,?,?)",
                (scope_id, version, text, changed_at, "local_user_management"),
            )
            return dict(
                scope_id=scope_id, version=version, text=text, changed_at=changed_at, changed_by="local_user_management"
            )

    def list(self, filters: MemoryFilter | None = None, *, limit=30, cursor=None):
        """Return one inventory page, with an opaque cursor for the next page."""
        return browse(self.core, self._context(), filters or MemoryFilter(), limit=limit, cursor=cursor)

    def quarantine(self, *, limit=30, before=None):
        """Read a bounded held-content page with the installation owner capability."""
        return review_page(self.core.storage, self.authority, limit=limit, before=before)

    def recalls(self, *, session=None, request_id=None, entry_id=None, before=None, limit=30):
        """Read recall attempts by session or request, retaining current visibility fences."""
        from .recall_log_view import history

        return history(
            self.core,
            self._context(),
            session=session,
            request_id=request_id,
            entry_id=entry_id,
            before=before,
            limit=limit,
        )

    def entries(self):
        """List each registered entry and its current control state."""
        from .entry_control import state_for

        context = self._context()
        with self.core.storage.read(context) as tx:
            registered = (
                tx.registry.entries()
                if context.binding.installation_kind == "shared"
                else {"local": {"name": context.binding.agent_id, "host": None}}
            )
            controls = {key: state_for(tx, context, entry_id=key) for key in registered}
        grants = self.entry_grants() if self.entry_grants is not None else {}
        return [
            dict(id=key, state=controls[key][0], version=controls[key][1], **(details | grants.get(key, {})))
            for key, details in registered.items()
        ]

    def set_entry_state(self, entry_id, state, *, actor):
        """Change an entry only through the installation owner's capability."""
        from .entry_control import change

        context = self._context()
        with self.core.storage.write(context) as tx:
            return change(tx, context, entry_id, state, actor, self.core.clock.utc_now())

    def entry(self, entry_id):
        """Read the current entry state and bounded change history."""
        from .entry_control import state_for

        context = self._context()
        with self.core.storage.read(context) as tx:
            if context.binding.installation_kind == "shared":
                if entry_id not in tx.registry.entries():
                    raise ContractError("SOURCE_MISSING", "entry")
            elif entry_id != "local":
                raise ContractError("SOURCE_MISSING", "entry")
            state, version = state_for(tx, context, entry_id=entry_id)
            rows = tx._check().execute(
                "SELECT version,from_state,to_state,actor,account_id,changed_at FROM entry_control_history "
                "WHERE entry_id=? ORDER BY version DESC LIMIT 100",
                (entry_id,),
            )
            history_rows = [dict(row) for row in rows]
            latest_capture = (
                tx._check()
                .execute(
                    "SELECT persisted_at,extra_json FROM source_events WHERE entry_id=? "
                    "ORDER BY persisted_at DESC,source_id DESC LIMIT 1",
                    (entry_id,),
                )
                .fetchone()
            )
            latest = latest_capture[0] if latest_capture is not None else None
            capture_version = json.loads(latest_capture[1]).get("protocol_version") if latest_capture else None
        log_entry = context.binding.agent_id if context.binding.installation_kind == "local" else entry_id
        records = self.recalls(entry_id=log_entry, limit=10)["items"]
        recorded = records[0] if records else None
        activity = max((stamp for stamp in (latest, recorded and recorded.get("timestamp")) if stamp), default=None)
        recent = sorted(
            (
                item
                for kind in ("event", "claim")
                for item in self.list(MemoryFilter(kind=kind, agent=entry_id), limit=10)["items"]
            ),
            key=lambda item: item.get("sort_time", ""),
            reverse=True,
        )
        return {
            "id": entry_id,
            "state": state,
            "version": version,
            "history": history_rows,
            "recent_writes": recent,
            "recent_reads": records,
            "last_activity": activity,
            "version_recorded": (
                recorded.get("protocol_version")
                if recorded and activity == recorded.get("timestamp")
                else capture_version
            ),
        }

    def health(self, *, limit=30, cursor=None, deadline_seconds=2.0):
        """Compute one page of visible health candidates without persisting it."""
        from .health import health_page

        return health_page(self.core, self._context(), limit=limit, cursor=cursor, deadline_seconds=deadline_seconds)

    def health_actions(self, *, limit=30, before=None):
        """List recent reversible decisions whose targets remain visible and unchanged."""
        from .mutate import management_claims_private

        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or (before is not None and (type(before) is not int or before < 1))
        ):
            raise ContractError("INPUT_INVALID", "limit_or_before")
        context = self._context()
        items = []
        with self.core.storage.read(context) as tx:
            rows = (
                tx._check()
                .execute(
                    "SELECT rowid,action_id,action,targets_json,recorded_at FROM management_actions "
                    "WHERE action IN ('health_entity_merge','health_fact_merge','health_fact_choose') AND rowid<? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (before or 2**63 - 1, limit + 1),
                )
                .fetchall()
            )
            for row in rows[:limit]:
                targets = json.loads(row["targets_json"])
                try:
                    location = self._location(tx, targets[0]["ref"])[1]
                    view = partition(tx, context, location["project_id"], location["branch_id"])
                    heads = [view.claims.version(item["ref"], item["revision"]) for item in targets]
                    if any(
                        head is None or head.current_revision != item["revision"] or head.suppressed
                        for head, item in zip(heads, targets, strict=True)
                    ):
                        continue
                    if management_claims_private(view, self.core.clock, heads):
                        continue
                except ContractError:
                    continue
                items.append(
                    dict(
                        action_id=row["action_id"],
                        action=row["action"],
                        recorded_at=row["recorded_at"],
                        versions={item["ref"]: item["revision"] for item in targets},
                    )
                )
        return dict(items=items, next_before=rows[limit - 1]["rowid"] if len(rows) > limit else None)

    def preview_quarantine_release(self, ref, *, expected_revision):
        """Return the owner-only content and hash required to authorize release."""
        return preview_release(self.core, self.authority, ref, expected_revision)

    def release_quarantine(self, ref, *, expected_revision, content_sha256):
        """Return the reviewed source to normal admission and retain its action receipt."""
        return release_source(self.core, self.authority, ref, expected_revision, content_sha256=content_sha256)

    def secrets(self):
        """Find stored secret-bearing versions for the local owner, without content or writes."""
        from .management_secrets import find_secrets

        return find_secrets(self.core.storage, self._context())

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
        return asdict(self.core.inspect_object(scoped, ref, revision, owner_authority=self.authority))

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

    # -- private intervals -------------------------------------------------
    #
    # These are local owner operations, like the inventory above.  Agent tools
    # never expose them: a model writing the same words is not the operator's
    # command.

    def private_on(
        self,
        *,
        target: str,
        session: str | None = None,
        project: str | None = None,
        mode: str = "full",
        duration_seconds: int | None = None,
    ) -> dict:
        """Open a private interval for one session, project path or the whole store."""
        from . import private

        return private.open_interval(
            self.core.storage,
            self.core.clock,
            self._context(),
            target=target,
            session=session,
            project=project,
            mode=mode,
            duration_seconds=duration_seconds,
        )

    def private_off_preview(self, *, target: str, session: str | None = None, project: str | None = None) -> dict:
        """Read the exact target rules required by a later reviewed confirmation."""
        from . import private

        return private.preview_close_interval(
            self.core.storage, self.core.clock, self._context(), target=target, session=session, project=project
        )

    def private_off(
        self, *, target: str, session: str | None = None, project: str | None = None, expected_rules=None
    ) -> dict:
        """Close only this target, atomically checking an optional reviewed rule snapshot."""
        from . import private

        return private.close_interval(
            self.core.storage,
            self.core.clock,
            self._context(),
            target=target,
            session=session,
            project=project,
            expected_rules=expected_rules,
        )

    def private_status(self, *, session: str | None = None, project: str | None = None) -> dict:
        """The current effective mode and active rules for a target or the store."""
        from . import private

        return private.status(self.core.storage, self.core.clock, self._context(), session=session, project=project)

    def private_list(self, *, active_only: bool = True) -> dict:
        """Every stored private interval, metadata only."""
        from . import private

        return private.list_intervals(self.core.storage, self.core.clock, self._context(), active_only=active_only)

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

    def health_merge_entity(self, source_ref, source_revision, target_ref, target_revision, *, name=None):
        """Apply an owner's entity decision with both reviewed fact versions."""
        from .mutate import merge_entity_from_management

        context = self._context()
        with self.core.storage.write(context, remaining_seconds=10) as tx:
            source_kind, source_row = self._location(tx, source_ref)
            target_kind, target_row = self._location(tx, target_ref)
            if source_kind != "claim" or target_kind != "claim":
                raise ContractError("INPUT_INVALID", "claim_required")
            audience = (source_row["scope_id"], source_row["project_id"], source_row["branch_id"])
            if audience != (target_row["scope_id"], target_row["project_id"], target_row["branch_id"]):
                raise ContractError("ACCESS_DENIED", "entity_audience")
            view = partition(tx, context, audience[1], audience[2], writable=True)
            alias = merge_entity_from_management(
                view, self.core.clock, source_ref, source_revision, target_ref, target_revision, name=name
            )
            action = "management-" + uuid4().hex
            targets = [dict(ref=alias.ref, revision=alias.revision)]
            self._audit(tx, action, "health_entity_merge", targets)
            return dict(action_id=action, ref=alias.ref, versions={alias.ref: alias.revision})

    def _health_facts(self, first_ref, first_revision, second_ref, second_revision, *, merge):
        from .mutate import health_apply_facts, health_fact_pair

        context = self._context()
        with self.core.storage.write(context, remaining_seconds=10) as tx:
            first_kind, first_row = self._location(tx, first_ref)
            second_kind, second_row = self._location(tx, second_ref)
            audience = (first_row["scope_id"], first_row["project_id"], first_row["branch_id"])
            if first_kind != "claim" or second_kind != "claim":
                raise ContractError("INPUT_INVALID", "claim_required")
            if audience != (second_row["scope_id"], second_row["project_id"], second_row["branch_id"]):
                raise ContractError("ACCESS_DENIED", "fact_audience")
            view = partition(tx, context, audience[1], audience[2], writable=True)
            first, second = health_fact_pair(
                view, self.core.clock, first_ref, first_revision, second_ref, second_revision
            )
            kept, replaced = health_apply_facts(view, self.core.clock, first, second, merge=merge)
            targets = [
                dict(ref=first.ref, before=first.revision, revision=kept.revision),
                dict(
                    ref=second.ref,
                    before=second.revision,
                    revision=replaced.revision,
                    replacement_ref=kept.ref,
                    replacement_revision=kept.revision,
                ),
            ]
            action = "management-" + uuid4().hex
            self._audit(tx, action, "health_fact_merge" if merge else "health_fact_choose", targets)
            return dict(action_id=action, versions={item["ref"]: item["revision"] for item in targets})

    def health_merge_facts(self, first_ref, first_revision, second_ref, second_revision):
        """Merge a reviewed duplicate pair while retaining their separate histories."""
        return self._health_facts(first_ref, first_revision, second_ref, second_revision, merge=True)

    def health_choose_fact(self, first_ref, first_revision, second_ref, second_revision):
        """Select the first of two reviewed, overlapping conflicting facts."""
        return self._health_facts(first_ref, first_revision, second_ref, second_revision, merge=False)

    def health_undo(self, action_id, versions):
        """Withdraw an entity decision only while its reviewed alias is unchanged."""
        from .mutate import management_claims_private

        context = self._context()
        with self.core.storage.write(context, remaining_seconds=10) as tx:
            row = (
                tx._check()
                .execute("SELECT action,targets_json FROM management_actions WHERE action_id=?", (action_id,))
                .fetchone()
            )
            if row is None or row["action"] not in {"health_entity_merge", "health_fact_merge", "health_fact_choose"}:
                raise ContractError("SOURCE_MISSING", "health_action")
            targets = json.loads(row["targets_json"])
            expected = {item["ref"]: item["revision"] for item in targets}
            if versions != expected:
                raise ContractError("VERSION_CONFLICT", "expected_revision")
            if row["action"] != "health_entity_merge":
                return self._health_undo_facts(tx, context, targets)
            ref, revision = next(iter(expected.items()))
            _kind, location = self._location(tx, ref)
            view = partition(tx, context, location["project_id"], location["branch_id"], writable=True)
            head = view.claims.version(ref, revision)
            if head is None or head.current_revision != revision:
                raise ContractError("VERSION_CONFLICT", "expected_revision")
            if management_claims_private(view, self.core.clock, (head,)):
                raise ContractError("ACCESS_DENIED", "private_mode")
            action, alias, source = correct(view, self.core.clock, ref, revision, None)
            self._audit(tx, action, "health_entity_undo", [dict(ref=ref, revision=alias.revision, source_ref=source)])
            return dict(action_id=action, ref=ref, versions={ref: alias.revision})

    def _health_undo_facts(self, tx, context, targets):
        from .mutate import health_restore_fact, management_claims_private

        locations = [self._location(tx, item["ref"])[1] for item in targets]
        first = locations[0]
        view = partition(tx, context, first["project_id"], first["branch_id"], writable=True)
        heads = [view.claims.version(item["ref"], item["revision"]) for item in targets]
        if any(
            head is None or head.current_revision != item["revision"] for head, item in zip(heads, targets, strict=True)
        ):
            raise ContractError("VERSION_CONFLICT", "expected_revision")
        if management_claims_private(view, self.core.clock, heads):
            raise ContractError("ACCESS_DENIED", "private_mode")
        restored = {}
        for head, item in zip(heads, targets, strict=True):
            original = view.claims.version(item["ref"], item["before"])
            if original is None:
                raise ContractError("SOURCE_MISSING", "fact_history")
            saved = (
                health_restore_fact(view, self.core.clock, original, head)
                if item["before"] != item["revision"]
                else head
            )
            restored[head.ref] = saved.revision
        action = "management-" + uuid4().hex
        self._audit(tx, action, "health_fact_undo", [dict(ref=ref, revision=rev) for ref, rev in restored.items()])
        return dict(action_id=action, versions=restored)

    def importance(self, ref, *, expected_revision, importance=None, pinned=None):
        """Change fact salience through the local owner's management capability."""
        context = self._context()
        with self.core.storage.write(context, remaining_seconds=10) as tx:
            kind, row = self._location(tx, ref)
            if kind != "claim":
                raise ContractError("INPUT_INVALID", "claim_required")
            view = partition(tx, context, row["project_id"], row["branch_id"], writable=True)
            head = view.claims.version(ref, expected_revision)
            if head is None:
                raise ContractError("SOURCE_MISSING", "claim")
            set_importance(view, head, importance=importance, pinned=pinned)
            action = "importance-" + hashlib.sha256(canonical([ref, tx.memory_epoch()]).encode()).hexdigest()
            self._audit(
                tx, action, "importance", [dict(ref=ref, revision=head.revision, importance=importance, pinned=pinned)]
            )
        return self.show(ref)

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
