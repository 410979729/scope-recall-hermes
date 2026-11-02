"""Owner review orchestration over native source adapters and the shared audit."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from ..contracts import ContractError
from .review_policy import digest, health_decided, health_key
from .review_sources import DECISIONS, QUERIES, list_native, restore_native, snapshot

SOURCES = (*QUERIES, "health")


def version_token(tx, key, state, effects=None):
    """Fence native metadata, current heads, appended history and previous decisions."""
    _, native, fence, _ = state
    row = (
        tx._check()
        .execute(
            "SELECT max(rowid) FROM management_actions WHERE action IN ('review_decide','review_undo') "
            "AND json_extract(targets_json,'$.key_hash')=?",
            (digest(key),),
        )
        .fetchone()
    )
    related = []
    for ref in (effects or {}).get("heads", {}):
        head = (
            tx._check()
            .execute(
                "SELECT current_revision,(SELECT max(revision) FROM claim_versions WHERE claim_id=?) "
                "FROM claims WHERE claim_id=? AND read_blocked=0 AND suppressed=0",
                (ref, ref),
            )
            .fetchone()
        )
        related.append((ref, list(head) if head else None))
    return digest([native, fence, row[0], related])


def _item(tx, key, state):
    return dict(key=key, source=key[0], version=version_token(tx, key, state), **state[3])


def _cursor(cursor, source):
    if source is not None and source not in SOURCES:
        raise ContractError("INPUT_INVALID", "review_source")
    if cursor is None:
        return source or SOURCES[0], 0
    try:
        phase, after = json.loads(cursor)
        if phase not in SOURCES or source is not None and phase != source:
            raise ValueError
        if phase != "health" and (type(after) is not int or after < 0):
            raise ValueError
        return phase, after
    except (ValueError, TypeError) as exc:
        raise ContractError("INPUT_INVALID", "review_cursor") from exc


def reviews(api, *, source=None, limit=30, cursor=None):
    """Return a bounded phase/page of the native queues, with no persisted mirror."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ContractError("INPUT_INVALID", "limit")
    phase, after = _cursor(cursor, source)
    context = api._context()
    items = []
    for current in SOURCES[SOURCES.index(phase) :]:
        if current == "health":
            page = _health_reviews(api, context, after, limit - len(items))
            return dict(items=items + page["items"], next_cursor=page["next_cursor"])
        with api.core.storage.read(context) as tx:
            rows, last, complete = list_native(tx, api.core.clock, current, after, limit - len(items))
            items.extend(_item(tx, key, (view, native, fence, item)) for key, view, native, fence, item in rows)
        if not complete:
            return dict(items=items, next_cursor=json.dumps([current, last]))
        if source is not None:
            break
        after = 0
    return dict(items=items, next_cursor=None)


def _health_reviews(api, context, after, limit) -> dict[str, Any]:
    page = api.health(limit=limit, cursor=after or None)
    items = []
    with api.core.storage.read(context) as tx:
        for category in ("entities", "duplicates", "contradictions", "unused"):
            for issue in page[category]:
                key = health_key(category, issue)
                if health_decided(tx, key):
                    continue
                try:
                    state = snapshot(tx, api.core.clock, key)
                    items.append(_item(tx, key, (*state[:3], dict(state[3], category=category))))
                except ContractError:
                    continue
    cursor = page["next_cursor"]
    return dict(items=items, next_cursor=json.dumps(["health", cursor]) if cursor else None)


def decide(api, key, decision, *, expected_version, selection=None):
    """Apply one reviewed source transition and its audit in the same writer lease."""
    if (
        type(decision) is not str
        or decision not in {"approve", "reject"}
        or type(expected_version) is not str
        or selection is not None
        and not isinstance(selection, dict)
    ):
        raise ContractError("INPUT_INVALID", "review_decision")
    context = api._context()
    with api.core.storage.write(context, remaining_seconds=10) as tx:
        state = snapshot(tx, api.core.clock, key, writable=True)
        view, native, _, _ = state
        if version_token(tx, key, state) != expected_version:
            raise ContractError("VERSION_CONFLICT", "expected_version")
        if key[0] == "health" and health_decided(tx, key):
            raise ContractError("VERSION_CONFLICT", "review_state")
        effects = DECISIONS[key[0]](view, api.core.clock, key, decision, selection)
        action = "review-" + uuid4().hex
        if effects.get("health_targets"):
            api._audit(
                tx,
                action + "-health",
                effects.get("health_action")
                or ("health_fact_merge" if key[1] == "duplicates" else "health_fact_choose"),
                effects["health_targets"],
            )
        after_state = snapshot(tx, api.core.clock, key)
        _, after_native, after_fence, _ = after_state
        payload = dict(
            key=key,
            key_hash=digest(key),
            decision=decision,
            before=native,
            effects=effects,
            after=digest([after_native, after_fence]),
        )
        api._audit(tx, action, "review_decide", payload)
        return dict(action_id=action, version=version_token(tx, key, after_state, effects), decision=decision)


def undo(api, action_id, *, expected_version):
    """Reverse one unchanged decision; a repeated or concurrent undo conflicts."""
    with api.core.storage.write(api._context(), remaining_seconds=10) as tx:
        row = (
            tx._check()
            .execute(
                "SELECT targets_json FROM management_actions WHERE action_id=? AND action='review_decide'", (action_id,)
            )
            .fetchone()
        )
        if row is None:
            raise ContractError("SOURCE_MISSING", "review_action")
        payload = json.loads(row[0])
        key, effects = payload["key"], payload["effects"]
        last = (
            tx._check()
            .execute(
                "SELECT action_id,action FROM management_actions WHERE action IN ('review_decide','review_undo') "
                "AND json_extract(targets_json,'$.key_hash')=? ORDER BY rowid DESC LIMIT 1",
                (digest(key),),
            )
            .fetchone()
        )
        state = snapshot(tx, api.core.clock, key, writable=True)
        if (
            last[0] != action_id
            or last[1] != "review_decide"
            or version_token(tx, key, state, effects) != expected_version
        ):
            raise ContractError("VERSION_CONFLICT", "expected_version")
        view, native, fence, _ = state
        if digest([native, fence]) != payload["after"] or not effects_current(tx, effects):
            raise ContractError("VERSION_CONFLICT", "review_effects")
        restored = restore_native(view, api.core.clock, key, payload["before"], effects)
        undo_id = "review-" + uuid4().hex
        api._audit(
            tx, undo_id, "review_undo", dict(key=key, key_hash=digest(key), undo_of=action_id, restored=restored)
        )
        return dict(action_id=undo_id, versions=restored)


def actions(api, *, limit=30, before=None):
    """Return undoable decisions, rechecking visibility before exposing any metadata."""
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or before is not None
        and (type(before) is not int or before < 1)
    ):
        raise ContractError("INPUT_INVALID", "limit_or_before")
    items = []
    with api.core.storage.read(api._context()) as tx:
        rows = (
            tx._check()
            .execute(
                "SELECT rowid,* FROM management_actions WHERE action='review_decide' AND "
                "rowid<? ORDER BY rowid DESC LIMIT ?",
                (before or 2**63 - 1, limit),
            )
            .fetchall()
        )
        for row in rows:
            payload = json.loads(row["targets_json"])
            key = payload["key"]
            last = (
                tx._check()
                .execute(
                    "SELECT action_id FROM management_actions WHERE action IN "
                    "('review_decide','review_undo') AND json_extract(targets_json,'$.key_hash')=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (digest(key),),
                )
                .fetchone()
            )
            if last[0] != row["action_id"]:
                continue
            try:
                state = snapshot(tx, api.core.clock, key)
                token = version_token(tx, key, state, payload["effects"])
                _, native, fence, _ = state
                if digest([native, fence]) != payload["after"] or not effects_current(tx, payload["effects"]):
                    continue
            except ContractError:
                continue
            items.append(
                dict(
                    action_id=row["action_id"],
                    source=key[0],
                    decision=payload["decision"],
                    time=row["recorded_at"],
                    version=token,
                )
            )
    return dict(items=items, next_before=rows[-1]["rowid"] if len(rows) == limit else None)


def effects_current(tx, effects):
    """Never let a newly refreshed receipt overwrite an intervening fact change."""
    return all(
        (row := tx._check().execute("SELECT current_revision FROM claims WHERE claim_id=?", (ref,)).fetchone())
        and row[0] == revision
        for ref, revision in effects.get("heads", {}).items()
    )
