"""Owner-authorized quarantine review and atomic return to normal admission."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace

from ..contracts import ContractError
from .injection_guard import group_rows, notice, scan
from .restore import maintenance_context
from .source_admission import admit_source, observe_admitted
from .source_records import SourceWrite, stored_source


def review_page(storage, authority, *, limit=30, before=None):
    """Return held source versions only to the bound local owner; deletion still wins."""
    context = maintenance_context(storage, authority)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ContractError("INPUT_INVALID", "limit")
    if before is not None and (type(before) is not int or before < 1):
        raise ContractError("INPUT_INVALID", "before")
    with storage.read(context) as tx:
        rows = (
            tx._check()
            .execute(
                """SELECT e.source_id,e.event_id,e.source_revision,e.scope_id,e.project_id,e.branch_id,e.content,
                s.state,s.rules_json FROM source_safety s JOIN source_events e
                ON e.event_id=s.source_ref AND e.source_revision=s.source_revision
                WHERE s.state!='released' AND e.source_id<? ORDER BY e.source_id DESC LIMIT ?""",
                (before if before is not None else 2**63 - 1, limit + 1),
            )
            .fetchall()
        )
        items = []
        for row in rows[:limit]:
            view = tx.with_context(replace(context, project_id=row["project_id"], branch_id=row["branch_id"]))
            held = notice(view, row["event_id"], row["source_revision"])
            if held is not None:
                items.append(
                    dict(
                        ref=row["event_id"],
                        revision=row["source_revision"],
                        scope=row["scope_id"],
                        content=row["content"],
                        **{k: held[k] for k in ("state", "rules")},
                    )
                )
        return dict(items=items, next_before=rows[limit - 1]["source_id"] if len(rows) > limit else None)


def _read_held(tx, ref, revision, *, writable=True):
    if type(ref) is not str or not 1 <= len(ref) <= 240 or type(revision) is not int or revision < 1:
        raise ContractError("INPUT_INVALID", "source_ref")
    row = (
        tx._check()
        .execute(
            "SELECT * FROM source_events WHERE event_id=? AND source_revision=?",
            (ref, revision),
        )
        .fetchone()
    )
    if row is None or row["source_revision"] != revision:
        raise ContractError("VERSION_CONFLICT", "source_revision")
    view = tx.with_context(
        replace(tx.context, project_id=row["project_id"], branch_id=row["branch_id"]), writable=writable
    )
    held = notice(view, ref, revision)
    own = (
        tx._check()
        .execute(
            "SELECT state,rules_json FROM source_safety WHERE source_ref=? AND source_revision=? AND state!='released'",
            (ref, revision),
        )
        .fetchone()
    )
    if held is None or own is None:
        raise ContractError("SOURCE_MISSING", "quarantine")
    held = dict(state=own["state"], rules=json.loads(own["rules_json"]), revision=revision)
    return view, row, held


def preview_release(core, authority, ref, revision):
    """Bind owner review to the exact current bytes of this held version."""
    context = maintenance_context(core.storage, authority)
    with core.storage.read(context) as tx:
        _view, row, held = _read_held(tx, ref, revision, writable=False)
        return dict(
            ref=ref,
            content=row["content"],
            content_sha256=hashlib.sha256(row["content"].encode("utf-8")).hexdigest(),
            **held,
        )


def _resume(tx, core, ref, revision):
    """Restore approved versions, scheduling only the latest group."""
    rows = group_rows(tx, ref)
    latest = max(row["source_revision"] for row in rows)
    sources = []
    for row in rows:
        decision = admit_source(
            tx,
            core.clock,
            row["event_id"],
            row["source_revision"],
            core.config.admission_policy,
            schedule=row["source_revision"] == latest,
        )
        if decision is not None:
            sources.append((SourceWrite("inserted", row["event_id"], latest), decision))
    observe_admitted(tx, core.clock, sources)


def release_source(core, authority, ref, revision, *, content_sha256):
    """Release exactly the reviewed source version and record the owner's action atomically."""
    context = maintenance_context(core.storage, authority)
    with core.storage.write(context, remaining_seconds=10) as tx:
        view, row, held = _read_held(tx, ref, revision)
        if hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != content_sha256:
            raise ContractError("VERSION_CONFLICT", "source_content")
        conn = view._check(write=True)
        conn.execute(
            "UPDATE source_safety SET state='released',reviewed_at=? WHERE source_ref=? AND source_revision=?",
            (core.clock.utc_now(), ref, revision),
        )
        _resume(view, core, ref, revision)
        conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        action = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO management_actions VALUES (?,'local_user_management','release_quarantine',?,?,?)",
            (
                action,
                json.dumps(
                    [
                        dict(
                            ref=ref,
                            revision=revision,
                            rules=held["rules"],
                            state=held["state"],
                            content_sha256=content_sha256,
                        )
                    ]
                ),
                core.clock.utc_now(),
                tx.memory_epoch(),
            ),
        )
        return dict(
            actor="local_user_management", action_id=action, ref=ref, revision=revision, memory_epoch=tx.memory_epoch()
        )


def finish_review(core, authority, ref, revision, *, content_sha256, approved):
    """Complete an exact pending verdict without invoking a model.

    The caller supplies a trusted completion, not a SourceEvent flag. Changed
    versions, stale hashes and newly matching deterministic rules cannot open
    content. With no completion the pending row remains held indefinitely.
    """
    context = maintenance_context(core.storage, authority)
    if type(approved) is not bool:
        raise ContractError("INPUT_INVALID", "approved")
    with core.storage.write(context, remaining_seconds=10) as tx:
        view, row, held = _read_held(tx, ref, revision)
        current = (
            view._check()
            .execute("SELECT max(source_revision) FROM source_events WHERE event_id=?", (ref,))
            .fetchone()[0]
        )
        if (
            held["state"] != "pending_review"
            or hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != content_sha256
            or current != revision
        ):
            raise ContractError("VERSION_CONFLICT", "pending_review")
        verdict = scan(view, stored_source(row, None).event, remaining_seconds=5)
        reasons = list(dict.fromkeys((*held["rules"], *verdict.rules)))
        if not approved:
            reasons.append("review_rejected")
        state = (
            "released"
            if approved
            and verdict.state == "clear"
            and row["segment_total"] in (None, 1)
            and not {"invalid_scan_rule", "scan_input_budget", "incomplete_scan_group"}.intersection(
                (*verdict.rules, *held["rules"])
            )
            else "quarantined"
        )
        view._check(write=True).execute(
            "UPDATE source_safety SET state=?,rules_json=?,reviewed_at=? WHERE source_ref=? AND source_revision=?",
            (
                state,
                json.dumps(reasons),
                core.clock.utc_now(),
                ref,
                revision,
            ),
        )
        if state == "released":
            _resume(view, core, ref, revision)
        view._check(write=True).execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        action = uuid.uuid4().hex
        view._check(write=True).execute(
            "INSERT INTO management_actions VALUES (?,'local_user_management','complete_injection_review',?,?,?)",
            (
                action,
                json.dumps([dict(ref=ref, revision=revision, state=state)]),
                core.clock.utc_now(),
                tx.memory_epoch(),
            ),
        )
        return dict(state=state, action_id=action)
