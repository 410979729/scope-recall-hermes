"""Native-state adapters for the owner's unified review view.

Review items resolve source text and claims from their existing repositories.
Receipts retain reversible controls; source deletion purges adoption preimages.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from ..contracts import ContractError
from . import private
from .claims import Qualification, evidence_refs
from .management_query import partition
from .mutate import health_apply_facts, health_fact_pair, health_restore_fact, management_claims_private
from .reflection_validity import reviewable_observation
from .review_policy import PENDING, digest, owner_publication
from .secret_patterns import structure_contains_secret_like_text

QUERIES = {
    "import": "SELECT e.source_id AS position,e.event_id AS ref,e.source_revision AS revision FROM source_events e "
    "WHERE json_extract(extra_json,'$._scope_recall_admission.reason')='import_requires_confirmation' "
    "AND NOT EXISTS(SELECT 1 FROM source_events n WHERE n.event_id=e.event_id AND n.source_revision>e.source_revision)",
    "adoption": "SELECT e.source_id AS position,e.event_id AS ref,e.source_revision AS "
    "revision FROM adoption_relations a "
    "JOIN source_events e ON e.event_id=a.confirmation_ref AND e.source_revision=a.confirmation_revision "
    "WHERE a.state='pending_review' AND coalesce(a.reason,'')!='owner_review_rejected'",
    "observation": "SELECT o.rowid AS position,o.claim_ref AS ref,o.claim_revision AS revision FROM observations o "
    "JOIN claims c ON c.claim_id=o.claim_ref AND c.current_revision=o.claim_revision WHERE o.confirmed=0",
    "fact": "SELECT v.rowid AS position,v.claim_id AS ref,v.revision FROM claim_versions v "
    "WHERE v.qualification_reason='owner_review_required'",
}


def key_for(source, ref, revision):
    """Build a content-free identity using the source's native reference."""
    return [source, ref, revision]


def _claim(tx, ref, revision, *, writable=False):
    row = tx._check().execute("SELECT project_id,branch_id FROM claims WHERE claim_id=?", (ref,)).fetchone()
    if row is None:
        raise ContractError("SOURCE_MISSING", "review_claim")
    view = partition(tx, tx.context, row[0], row[1], writable=writable)
    version = view.claims.version(ref, revision)
    if version is None or version.suppressed:
        raise ContractError("SOURCE_MISSING", "review_claim")
    return view, version


def _source(tx, ref, revision, clock, *, writable=False):
    row = (
        tx._check()
        .execute("SELECT * FROM source_events WHERE event_id=? AND source_revision=?", (ref, revision))
        .fetchone()
    )
    if row is None:
        raise ContractError("SOURCE_MISSING", "review_source")
    view = partition(tx, tx.context, row["project_id"], row["branch_id"], writable=writable)
    source = view.source(ref, revision)
    intervals = private.active_intervals(view, clock.utc_now())
    if writable:
        intervals = [dict(rule, mode="full") for rule in intervals]
    if source is None or source.suppressed or private.source_blocked(intervals, source):
        raise ContractError("SOURCE_MISSING", "review_source")
    view.claims.require_live_source(ref, revision)
    return view, source, row


def claim_snapshot(tx, clock, key, *, writable=False):
    """Resolve a held fact or observation with live evidence and scope fences."""
    kind, ref, revision = key
    view, version = _claim(tx, ref, revision, writable=writable)
    view.claims.live_sources(evidence_refs(version.payload), version.scope_id)
    if management_claims_private(view, clock, (version,)):
        raise ContractError("ACCESS_DENIED", "private_mode")
    native = dict(state=version.state, basis=version.basis, reason=version.reason)
    if version.payload["kind"] == "alias":
        from .entity_names import review_control_state

        native["entity_controls"] = review_control_state(view, version.ref)
    if kind == "observation":
        row = (
            view._check()
            .execute("SELECT * FROM observations WHERE claim_ref=? AND claim_revision=?", (ref, revision))
            .fetchone()
        )
        if row is None or (version.current_revision == revision and not reviewable_observation(view, version)):
            raise ContractError("SOURCE_MISSING", "observation_dependencies")
        native["observation"] = dict(row)
    heads = {ref: version.current_revision}
    latest = view._check().execute("SELECT max(revision) FROM claim_versions WHERE claim_id=?", (ref,)).fetchone()[0]
    item = dict(
        content=version.payload["value_text"],
        evidence=version.payload["evidence_spans"],
        time=version.recorded_from,
        scope=version.scope_id,
    )
    fence: dict[str, Any] = dict(heads=heads, latest=latest)
    if version.payload["kind"] == "decision":
        fence["adoption_bindings"] = [
            dict(row)
            for row in view._check().execute(
                "SELECT confirmation_ref,confirmation_revision,claim_ref,claim_revision,state,selection_json,reason "
                "FROM adoption_relations WHERE claim_ref=?",
                (ref,),
            )
        ]
    return view, native, fence, item


def source_snapshot(tx, clock, key, *, writable=False):
    """Resolve an import/adoption without changing its recorded authorship."""
    kind, ref, revision = key
    view, source, row = _source(tx, ref, revision, clock, writable=writable)
    extra = json.loads(row["extra_json"])
    native = dict(admission=extra.get("_scope_recall_admission"))
    refs = [dict(source_ref=ref, source_revision=revision)]
    item = dict(content=source.event["content"], evidence=refs, time=source.event["recorded_at"], scope=source.scope_id)
    if kind == "adoption":
        relation = (
            view._check()
            .execute(
                "SELECT * FROM adoption_relations WHERE confirmation_ref=? AND confirmation_revision=?", (ref, revision)
            )
            .fetchone()
        )
        if relation is None:
            raise ContractError("SOURCE_MISSING", "review_adoption")
        native = dict(relation)
        _, assistant, _ = _source(view, relation["assistant_ref"], relation["assistant_revision"], clock)
        from .adoption import fragments

        item["fragments"] = list(fragments(assistant.event["content"]))
        refs.append(dict(source_ref=assistant.ref, source_revision=assistant.revision))
    return view, native, dict(queue=digest(queue_state(view, ref, revision))), item


def health_snapshot(tx, clock, key, *, writable=False):
    """Resolve a computed finding using its exact fact versions."""
    _, category, pairs, name = key
    facts, heads, latest = [], {}, {}
    for ref, revision in pairs:
        view, version = _claim(tx, ref, revision, writable=writable)
        view.claims.live_sources(evidence_refs(version.payload), version.scope_id)
        if management_claims_private(view, clock, (version,)):
            raise ContractError("ACCESS_DENIED", "private_mode")
        facts.append(version)
        heads[ref] = version.current_revision
        latest[ref] = (
            view._check().execute("SELECT max(revision) FROM claim_versions WHERE claim_id=?", (ref,)).fetchone()[0]
        )
    if not facts or category not in {"duplicates", "contradictions", "entities", "unused"}:
        raise ContractError("INPUT_INVALID", "health_review")
    item = dict(
        content=[v.payload["value_text"] for v in facts],
        evidence=[span for v in facts for span in v.payload["evidence_spans"]],
        time=max(v.recorded_from for v in facts),
        scope=facts[0].scope_id,
        name=name,
    )
    return view, dict(category=category, pairs=pairs, name=name), dict(heads=heads, latest=latest), item


SNAPSHOTS = {
    "import": source_snapshot,
    "adoption": source_snapshot,
    "observation": claim_snapshot,
    "fact": claim_snapshot,
    "health": health_snapshot,
}


def valid_pair(pair):
    """Validate a bounded fact-version pair before adapter unpacking."""
    return (
        isinstance(pair, list)
        and len(pair) == 2
        and type(pair[0]) is str
        and 1 <= len(pair[0]) <= 240
        and type(pair[1]) is int
        and pair[1] > 0
    )


def snapshot(tx, clock, key, *, writable=False):
    """Dispatch only known, bounded source identities to their native adapter."""
    if not isinstance(key, list) or not key or type(key[0]) is not str or key[0] not in SNAPSHOTS:
        raise ContractError("INPUT_INVALID", "review_key")
    if key[0] == "health":
        if (
            len(key) != 4
            or not isinstance(key[2], list)
            or not 1 <= len(key[2]) <= 2
            or not all(valid_pair(pair) for pair in key[2])
        ):
            raise ContractError("INPUT_INVALID", "review_key")
    elif len(key) != 3 or type(key[1]) is not str or type(key[2]) is not int or key[2] < 1:
        raise ContractError("INPUT_INVALID", "review_key")
    return SNAPSHOTS[key[0]](tx, clock, key, writable=writable)


def list_native(tx, clock, source, after, limit):
    """Scan bounded native rows; invisible rows still advance the continuation."""
    rows = (
        tx._check()
        .execute("SELECT * FROM (" + QUERIES[source] + ") WHERE position>? ORDER BY position LIMIT ?", (after, limit))
        .fetchall()
    )
    items = []
    for row in rows:
        key = key_for(source, row["ref"], row["revision"])
        try:
            view, native, fence, item = snapshot(tx, clock, key)
        except ContractError:
            continue
        if source == "observation" and native["state"] not in {"active", "proposed"}:
            continue
        items.append((key, view, native, fence, item))
    return items, rows[-1]["position"] if rows else after, len(rows) < limit


def decide_fact(tx, clock, key, decision, selection):
    """Publish an owner's exact claim choice and retain the proposed revision."""
    from .claims import select_effective
    from .mutate import denial_verdict

    _, ref, revision = key
    head = tx.claims.version(ref, revision)
    if head.reason != PENDING:
        raise ContractError("VERSION_CONFLICT", "review_state")
    if structure_contains_secret_like_text(head.payload):
        raise ContractError("INPUT_INVALID", "plaintext_secret_rejected")
    result: dict[str, Any] = {}
    if decision == "approve":
        history = tx.claims.versions(ref)
        held = select_effective(history, clock.utc_now())
        if denial_verdict(head.payload, held, tx.claims.roots(evidence_refs(head.payload)), history)[0]:
            raise ContractError("DERIVATION_INVALID", "denial_not_a_value")
        current = tx.claims.version(ref, head.current_revision)
        saved = publish_reviewed_claim(tx, clock, head, current)
        result = dict(heads={ref: saved.revision}, before_heads={ref: current.revision})
        from .adoption_dedup import approve_deferred_adoption

        deferred = approve_deferred_adoption(tx, head, saved, now=clock.utc_now())
        result["heads"].update(deferred.get("heads", {}))
        result["before_heads"].update(deferred.get("before_heads", {}))
        if deferred.get("adoption_bindings"):
            result["adoption_bindings"] = deferred["adoption_bindings"]
    tx._check(write=True).execute(
        "UPDATE claim_versions SET qualification_reason=? WHERE claim_id=? AND revision=?",
        ("owner_review_approved" if decision == "approve" else "owner_review_rejected", ref, revision),
    )
    return result


def publish_reviewed_claim(tx, clock, head, current):
    """Preserve entity authority when approving an alias; other facts use owner review."""
    if head.payload["kind"] == "alias" and head.payload["predicate"].startswith("entity_identity:"):
        from .entity_resolution import approve_entity_alias

        return approve_entity_alias(tx, clock, head, current)
    with owner_publication():
        return tx.claims.append(
            head.scope_id,
            head.payload,
            Qualification("active", "direct_report", "local_user_management"),
            previous=current,
            expected_revision=current.revision,
            recorded_at=clock.utc_now(),
        )


def decide_observation(tx, clock, key, decision, selection):
    """Append review while preserving inductive provenance and dependency signature."""
    _, ref, revision = key
    head = tx.claims.version(ref, revision)
    row = (
        tx._check()
        .execute("SELECT * FROM observations WHERE claim_ref=? AND claim_revision=?", (ref, revision))
        .fetchone()
    )
    if head.current_revision != revision or row["confirmed"] or not reviewable_observation(tx, head):
        raise ContractError("VERSION_CONFLICT", "observation_review")
    saved = tx.claims.append(
        head.scope_id,
        head.payload,
        Qualification("active" if decision == "approve" else "retracted", "derived_summary", "confirmed_observation"),
        previous=head,
        expected_revision=revision,
        recorded_at=clock.utc_now(),
    )
    tx._check(write=True).execute(
        "INSERT INTO observations VALUES(?,?,?,?,1)", (ref, saved.revision, row["manifest_json"], row["signature"])
    )
    return dict(heads={ref: saved.revision}, before_heads={ref: revision})


def decide_import(tx, clock, key, decision, selection):
    """Record review in the existing admission marker; original origin is immutable."""
    _, ref, revision = key
    row = (
        tx._check()
        .execute("SELECT extra_json FROM source_events WHERE event_id=? AND source_revision=?", (ref, revision))
        .fetchone()
    )
    extra = json.loads(row[0])
    before_queue = queue_state(tx, ref, revision)
    marker = extra.get("_scope_recall_admission", {})
    if marker.get("reason") != "import_requires_confirmation":
        raise ContractError("VERSION_CONFLICT", "import_review")
    marker.update(
        disposition="schedule" if decision == "approve" else "source_only",
        reason="owner_review_approved" if decision == "approve" else "owner_review_rejected",
    )
    extra["_scope_recall_admission"] = marker
    tx._check(write=True).execute(
        "UPDATE source_events SET extra_json=? WHERE event_id=? AND source_revision=?",
        (json.dumps(extra), ref, revision),
    )
    if decision == "approve":
        resume_queue(tx, clock, ref, revision)
    return dict(queue_before=before_queue)


def decide_adoption(tx, clock, key, decision, selection):
    """Bind the owner's chosen fragment IDs, then resume the existing adoption stages."""
    from .adoption_storage import adoption_context, source_reason
    from .entry_control import entry_fence

    _, ref, revision = key
    row = (
        tx._check()
        .execute(
            "SELECT * FROM adoption_relations WHERE confirmation_ref=? AND confirmation_revision=?", (ref, revision)
        )
        .fetchone()
    )
    if row["state"] != "pending_review" or row["reason"] == "owner_review_rejected":
        raise ContractError("VERSION_CONFLICT", "adoption_review")
    before_queue = queue_state(tx, ref, revision)
    chosen = json.loads(row["selection_json"])
    if decision == "approve":
        owner = tx.source(ref, revision)
        context = adoption_context(tx, owner)
        if context is None or source_reason(tx, context) or not isinstance(selection, dict):
            raise ContractError("INPUT_INVALID", "adoption_selection")
        ids = selection.get("fragment_ids")
        valid = {p["id"] for p in context.fragments}
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) > 32
            or any(type(i) is not int or i not in valid for i in ids)
            or len(ids) != len(set(ids))
        ):
            raise ContractError("INPUT_INVALID", "fragment_ids")
        chosen.update(fragment_ids=ids, owner_reviewed=True)
        state = dict(
            stage="describe", selection=chosen, entry_fence=entry_fence(tx, (context.confirmation, context.assistant))
        )
        resume_queue(tx, clock, ref, revision)
        work = (
            tx._check()
            .execute(
                "SELECT work_id FROM work_items WHERE subject_ref=? AND subject_revision=? AND work_type='consolidate'",
                (ref, revision),
            )
            .fetchone()
        )
        tx._check(write=True).execute(
            "UPDATE work_items SET state='pending',attempt=0,last_error_code=NULL WHERE work_id=?", (work[0],)
        )
        tx._check(write=True).execute(
            "INSERT INTO adoption_processing VALUES(?,?,?,?,?,?) ON CONFLICT(work_id) DO "
            "UPDATE SET payload_json=excluded.payload_json",
            (work[0], ref, revision, context.assistant_ref, context.assistant_revision, json.dumps(state)),
        )
    tx._check(write=True).execute(
        "UPDATE adoption_relations SET state=?,reason=?,selection_json=? WHERE "
        "confirmation_ref=? AND confirmation_revision=?",
        (
            "bound" if decision == "approve" else "pending_review",
            "owner_review_approved" if decision == "approve" else "owner_review_rejected",
            json.dumps(chosen),
            ref,
            revision,
        ),
    )
    return dict(queue_before=before_queue)


def decide_health(tx, clock, key, decision, selection):
    """Apply existing health mutations; rejecting a finding only records its disposition."""
    _, category, pairs, name = key
    versions = [tx.claims.version(ref, revision) for ref, revision in pairs]
    if any(
        v is None or v.current_revision != revision or v.state != "active"
        for v, (_, revision) in zip(versions, pairs, strict=True)
    ):
        raise ContractError("VERSION_CONFLICT", "health_review")
    if decision == "reject" or category == "unused":
        return {}
    if category == "entities":
        from .mutate import merge_entity_from_management

        alias = merge_entity_from_management(tx, clock, *pairs[0], *pairs[1], name=name)
        return dict(
            heads={alias.ref: alias.revision},
            before_heads={alias.ref: None},
            health_action="health_entity_merge",
            health_targets=[dict(ref=alias.ref, revision=alias.revision)],
        )
    first, second = health_fact_pair(tx, clock, *pairs[0], *pairs[1])
    if selection and selection.get("keep") == second.ref:
        first, second = second, first
    kept, replaced = health_apply_facts(tx, clock, first, second, merge=category == "duplicates")
    return dict(
        heads={kept.ref: kept.revision, replaced.ref: replaced.revision},
        before_heads={first.ref: first.revision, second.ref: second.revision},
        health_targets=[
            dict(ref=kept.ref, before=first.revision, revision=kept.revision),
            dict(
                ref=replaced.ref,
                before=second.revision,
                revision=replaced.revision,
                replacement_ref=kept.ref,
                replacement_revision=kept.revision,
            ),
        ],
    )


DECISIONS = {
    "fact": decide_fact,
    "import": decide_import,
    "adoption": decide_adoption,
    "observation": decide_observation,
    "health": decide_health,
}


def restore_native(tx, clock, key, before, effects):
    """Restore metadata and append restored heads; never rewrite published claim history."""
    kind = key[0]
    if "queue_before" in effects:
        restore_queue(tx, key[1], key[2], effects["queue_before"])
    restored = {}
    for ref, revision in effects.get("before_heads", {}).items():
        view, head = _claim(tx, ref, effects["heads"][ref], writable=True)
        if management_claims_private(view, clock, (head,)):
            raise ContractError("ACCESS_DENIED", "private_mode")
        original = view.claims.version(ref, revision) if revision else replace(head, state="retracted")
        if kind == "fact" and original.state == "proposed":
            # A proposed restored head would let temporal selection fall back to
            # the approved historical version. A withdrawal ends that authority.
            original = replace(original, state="retracted", reason="owner_review_undo")
        saved = health_restore_fact(view, clock, original, head)
        restored[ref] = saved.revision
    if before.get("entity_controls"):
        from .entity_names import restore_review_controls

        restore_review_controls(tx, before["entity_controls"])
    if kind == "fact":
        tx._check(write=True).execute(
            "UPDATE claim_versions SET qualification_reason=? WHERE claim_id=? AND revision=?",
            (before["reason"], key[1], key[2]),
        )
    if kind == "import":
        tx._check(write=True).execute(
            "UPDATE source_events SET "
            "extra_json=json_set(extra_json,'$._scope_recall_admission',json(?)) "
            "WHERE event_id=? AND source_revision=?",
            (json.dumps(before["admission"]), key[1], key[2]),
        )
    if kind == "adoption":
        fields = [name for name in before if name not in {"confirmation_ref", "confirmation_revision"}]
        tx._check(write=True).execute(
            "UPDATE adoption_relations SET "
            + ",".join(name + "=?" for name in fields)
            + " WHERE confirmation_ref=? AND confirmation_revision=?",
            (*[before[name] for name in fields], key[1], key[2]),
        )
    from .adoption_dedup import restore_reviewed_adoptions

    restore_reviewed_adoptions(tx, effects.get("adoption_bindings", ()), now=clock.utc_now())
    return restored


def queue_state(tx, ref, revision):
    """Snapshot reversible queue controls; source content remains in its repository."""
    conn = tx._check()
    work = conn.execute(
        "SELECT * FROM work_items WHERE subject_ref=? AND subject_revision=? AND work_type='consolidate'",
        (ref, revision),
    ).fetchone()
    checkpoint = conn.execute(
        "SELECT * FROM adoption_processing WHERE confirmation_ref=? AND confirmation_revision=?", (ref, revision)
    ).fetchone()
    return dict(work=dict(work) if work else None, checkpoint=dict(checkpoint) if checkpoint else None)


def resume_queue(tx, clock, ref, revision):
    """Resume an unchanged consolidation job without taking an active worker's lease."""
    state = queue_state(tx, ref, revision)["work"]
    if state and state["state"] == "leased":
        raise ContractError("VERSION_CONFLICT", "review_work")
    tx.sources.enqueue_source(ref, revision, work_type="consolidate", available_at=clock.utc_now())
    tx._check(write=True).execute(
        "UPDATE work_items SET "
        "state='pending',attempt=0,available_at=?,last_error_code=NULL,lease_token=lease_token+1 "
        "WHERE subject_ref=? AND subject_revision=? AND work_type='consolidate'",
        (clock.utc_now(), ref, revision),
    )


def restore_queue(tx, ref, revision, before):
    """Fence late publication and restore only controls changed by this owner action."""
    conn = tx._check(write=True)
    current = queue_state(tx, ref, revision)["work"]
    if current is None:
        return
    original = before["work"]
    if original is None:
        conn.execute(
            "UPDATE work_items SET "
            "state='obsolete',lease_token=lease_token+1,last_error_code='owner_review_undo' WHERE work_id=?",
            (current["work_id"],),
        )
    else:
        fields = ("state", "attempt", "available_at", "lease_owner", "lease_until", "last_error_code")
        conn.execute(
            "UPDATE work_items SET "
            + ",".join(name + "=?" for name in fields)
            + ",lease_token=lease_token+1 WHERE work_id=?",
            (*[original[name] for name in fields], current["work_id"]),
        )
    checkpoint = before["checkpoint"]
    if checkpoint is None:
        conn.execute("DELETE FROM adoption_processing WHERE work_id=?", (current["work_id"],))
    else:
        conn.execute("INSERT OR REPLACE INTO adoption_processing VALUES(?,?,?,?,?,?)", tuple(checkpoint.values()))
