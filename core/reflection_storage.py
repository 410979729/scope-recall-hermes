"""Bounded idle discovery and ordinary claim publication for inductive observations."""

from __future__ import annotations

import hashlib
import json

from ..contracts import ContractError, validate_payload
from . import private
from .claims import Qualification, claim_slot
from .episodes import source_origin
from .evidence_quote import align_claim_evidence, resolve_evidence_quotes
from .evidence_support import support_for_contents, validate_positions
from .reflection_policy import discovery_page, discovery_ready, reflection_usage, scope_available
from .reflection_validity import dependency_signature, live_members, owner_conflict, reflection_live
from .secret_patterns import structure_contains_secret_like_text
from .summary_validity import obsolete_ranges
from .suppression import renew_observation_head

MIN_NEW_SOURCES = 8
MAX_SOURCES = 16
MAX_OBSERVATIONS = 3


AVAILABLE_GROUP = (
    "(job_ref IS NULL OR EXISTS(SELECT 1 FROM work_items w WHERE w.subject_ref=job_ref "
    "AND w.subject_revision=job_revision AND w.work_type='rebuild_projection' AND w.s"
    "tate IN ('failed','obsolete')))"
)


def reflection_audience(tx):
    """Bind discovery progress to the exact authorized scope, project and branch."""
    ctx = tx.context
    return hashlib.sha256(
        json.dumps([sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id]).encode()
    ).hexdigest()


def _member(source, position):
    return dict(
        ref=source.ref,
        revision=source.revision,
        position=position,
        digest=hashlib.sha256(source.event["content"].encode()).hexdigest(),
        total=len(source.event["content"]),
        session_id=source.session_id,
        entry_id=source.entry_id,
        occurred_at=source.event["occurred_at"],
    )


def _discover(tx, *, limit, now=None):
    """Advance a durable audience cursor; retain only a bounded unsummarized source window."""
    ctx, conn = tx.context, tx._check(write=True)
    audience = reflection_audience(tx)
    if now is not None and not discovery_ready(tx, audience, now=now):
        return
    conn.execute("INSERT OR IGNORE INTO reflection_cursors(audience) VALUES(?)", (audience,))
    position = conn.execute("SELECT position FROM reflection_cursors WHERE audience=?", (audience,)).fetchone()[0]
    rows = conn.execute(
        ("SELECT rowid AS position,event_id,source_revision FROM source_events WHERE rowid>? AND scope_id IN (")
        + ",".join("?" for _ in ctx.allowed_scope_ids)
        + ") AND project_id IS ? AND branch_id IS ? AND role='user' ORDER BY rowid LIMIT ?",
        (position, *sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id, limit),
    ).fetchall()
    for row in rows:
        source = tx.source(row["event_id"], row["source_revision"])
        if source is None or source.suppressed or source.capture_gaps or source_origin(source) != "human_direct":
            continue
        principal = source.event.get("source_principal", {})
        subject = source.project_id or principal.get("principal_ref")
        if not subject:
            continue
        key = hashlib.sha256(
            json.dumps([source.scope_id, source.project_id, source.branch_id, subject]).encode()
        ).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO reflection_groups(group_key,scope_id,project_id,branch_id,subject) "
            "VALUES(?,?,?,?,?)",
            (key, source.scope_id, source.project_id, source.branch_id, subject),
        )
        old = conn.execute(
            "SELECT buffered_json,manifest_json FROM reflection_groups WHERE group_key=?", (key,)
        ).fetchone()
        buffered, prior = json.loads(old[0]), json.loads(old[1])
        # Audience cursors may overlap across hosts. The group's evidence high
        # water mark prevents the same old event from becoming new work twice.
        if row["position"] <= max((m["position"] for m in [*buffered, *prior]), default=0):
            continue
        members = [m for m in buffered if m["ref"] != source.ref]
        members.append(_member(source, row["position"]))
        conn.execute(
            "UPDATE reflection_groups SET buffered_json=?,new_sources=new_sources+1 WHERE group_key=?",
            (json.dumps(members[-MAX_SOURCES:]), key),
        )
    if rows:
        conn.execute("UPDATE reflection_cursors SET position=? WHERE audience=?", (rows[-1]["position"], audience))
        if now is not None:
            discovery_page(tx, audience, rows[-1]["position"], now=now)


def candidate_groups(tx):
    """Cycle through bounded group pages so inactive early subjects cannot starve later ones."""
    ctx, conn = tx.context, tx._check(write=True)
    audience = "groups:" + reflection_audience(tx)
    conn.execute("INSERT OR IGNORE INTO reflection_cursors(audience) VALUES(?)", (audience,))
    position = conn.execute("SELECT position FROM reflection_cursors WHERE audience=?", (audience,)).fetchone()[0]
    query = (
        "SELECT rowid AS position,* FROM reflection_groups WHERE rowid>? AND scope_id IN ("
        + ",".join("?" for _ in ctx.allowed_scope_ids)
        + ") AND project_id IS ? AND branch_id IS ? AND "
        + AVAILABLE_GROUP
        + " ORDER BY rowid LIMIT 64"
    )
    params = (*sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id)
    rows = conn.execute(query, (position, *params)).fetchall()
    if not rows and position:
        rows = conn.execute(query, (0, *params)).fetchall()
    conn.execute(
        "UPDATE reflection_cursors SET position=? WHERE audience=?", (rows[-1]["position"] if rows else 0, audience)
    )
    return rows


def queue_reflections(tx, *, now, enabled=False, limit=128):
    """Queue at most one group after foreground and other durable work are idle.

    Eight new first-hand events amortize model cost; two independent sessions
    are required even when one conversation alone fills the whole window.
    """
    if not enabled or private.source_blocked(private.active_intervals(tx, now), None):
        return 0
    if not any(row["calls"] < row["limit"] for row in reflection_usage(tx, now=now)):
        return 0
    _discover(tx, limit=min(limit, MIN_NEW_SOURCES), now=now)
    rows = candidate_groups(tx)
    for row in rows:
        members = json.loads(row["buffered_json"])
        if row["new_sources"] < row["required_sources"] or not scope_available(tx, row["scope_id"], now=now):
            continue
        if len(members) < MIN_NEW_SOURCES or len({m["session_id"] for m in members}) < 2:
            continue
        try:
            live_members(tx, members, now=now)
        except ContractError:
            tx._check().execute(
                "UPDATE reflection_groups SET buffered_json='[]',new_sources=0 WHERE group_key=?", (row["group_key"],)
            )
            continue
        obsolete = obsolete_ranges(tx, members)
        if obsolete:
            members = [m for m in members if (m["ref"], m["revision"]) not in obsolete]
            tx._check().execute(
                ("UPDATE reflection_groups SET buffered_json=?,new_sources=min(new_sources,?) WHERE group_key=?"),
                (json.dumps(members), len(members), row["group_key"]),
            )
        if len(members) < MIN_NEW_SOURCES or len({m["session_id"] for m in members}) < 2:
            continue
        signature = dependency_signature(tx, members)
        proposal = dict(
            kind="observation",
            subject=row["subject"],
            predicate="reflection:" + signature[:32],
            value_text="Pending cross-session observation",
            conditions=[],
            statement_kind="assertion",
            valid_from=None,
            valid_to=None,
            evidence_spans=[
                dict(
                    source_ref=members[0]["ref"],
                    source_revision=members[0]["revision"],
                    quote=tx.source(members[0]["ref"], members[0]["revision"]).event["content"][:1],
                )
            ],
        )
        saved = tx.claims.append(
            row["scope_id"],
            proposal,
            Qualification("proposed", "derived_summary", "reflection_pending"),
            recorded_at=now,
        )
        tx._check().execute(
            "UPDATE reflection_groups SET job_ref=?,job_revision=?,manifest_json=?,signature="
            "?,buffered_json='[]',new_sources=0,"
            "proposals_json=NULL,next_proposal=0 WHERE group_key=?",
            (saved.ref, saved.revision, json.dumps(members), signature, row["group_key"]),
        )
        tx.work.enqueue("rebuild_projection", saved.ref, saved.revision, available_at=now)
        return 1
    return 0


def reflection_job(tx, item):
    """Recognize only leased claim-backed reflection stages."""
    return (
        tx._check()
        .execute(
            "SELECT * FROM reflection_groups WHERE job_ref=? AND job_revision=?",
            (item.subject_ref, item.subject_revision),
        )
        .fetchone()
    )


def session_numbers(members):
    """Assign stable small numbers within this bounded batch, never inferred identities."""
    return {name: index + 1 for index, name in enumerate(dict.fromkeys(m["session_id"] for m in members))}


def decode_observations(raw, members, sources, *, rejections=None):
    """Reject malformed candidates individually; valid siblings still need independent support."""
    value = json.loads(raw) if isinstance(raw, str) else raw
    if (
        not isinstance(value, dict)
        or set(value) != {"observations"}
        or not isinstance(value["observations"], list)
        or len(value["observations"]) > MAX_OBSERVATIONS
    ):
        raise ContractError("DERIVATION_INVALID", "observations")
    contents = {(s.ref, s.revision): s.event["content"] for s in sources}
    proposals = []
    for index, item in enumerate(value["observations"]):
        try:
            proposals.append(_decode_observation(item, members, sources, contents))
        except (ContractError, ValueError, TypeError, KeyError) as exc:
            if rejections is not None:
                rejections.append(dict(candidate=index, reason=getattr(exc, "field", None) or type(exc).__name__))
    return proposals


def _decode_observation(item, members, sources, contents):
    """Validate a single exact-quote cross-session induction before paying for support."""
    if not isinstance(item, dict):
        raise ContractError("DERIVATION_INVALID", "observation")
    declared = item.get("sessions")
    proposal: dict = dict(
        {k: v for k, v in item.items() if k != "sessions"},
        kind="observation",
        statement_kind="assertion",
        conditions=item.get("conditions", []),
        valid_from=None,
        valid_to=None,
    )
    envelope = dict(
        protocol_version="1.1",
        source_refs=[f"{m['ref']}@{m['revision']}" for m in members],
        claim_proposals=[proposal],
        resume_proposals=[],
        reference_proposals=[],
    )
    resolve_evidence_quotes(envelope, contents)
    validate_payload("consolidation_result", envelope)
    if structure_contains_secret_like_text(proposal):
        raise ContractError("INPUT_INVALID", "plaintext_secret_rejected")
    validate_positions(proposal, contents)
    for span in proposal["evidence_spans"]:
        content = contents.get((span["source_ref"], span["source_revision"]))
        if "start" not in span and (content is None or content.count(span["quote"]) != 1):
            raise ContractError("DERIVATION_INVALID", "ambiguous_observation_quote")
    align_claim_evidence(proposal, {(source.ref, source.revision): source for source in sources}, index=0)
    validate_positions(proposal, contents)
    cited = {(s["source_ref"], s["source_revision"]) for s in proposal["evidence_spans"]}
    if len({m["session_id"] for m in members if (m["ref"], m["revision"]) in cited}) < 2 or any(
        "start" not in s for s in proposal["evidence_spans"]
    ):
        raise ContractError("DERIVATION_INVALID", "independent_sessions")
    numbered = session_numbers(members)
    cited_sessions = {numbered[m["session_id"]] for m in members if (m["ref"], m["revision"]) in cited}
    if (
        not isinstance(declared, list)
        or any(type(n) is not int for n in declared)
        or set(declared) != cited_sessions
        or len(declared) != len(cited_sessions)
    ):
        raise ContractError("DERIVATION_INVALID", "declared_sessions")
    return proposal


def publish_observation(tx, group, proposal, *, now):
    """Publish only the exact proposal covered by the independent support receipt."""
    members = json.loads(group["manifest_json"])
    sources = live_members(tx, members, now=now)
    contents = {(s.ref, s.revision): s.event["content"] for s in sources}
    verdict = support_for_contents(proposal, contents)
    if verdict is None or verdict.state != "supported" or verdict.action != "new":
        return None
    ctx = tx.context
    old = (
        tx._check()
        .execute(
            "SELECT claim_id FROM claims WHERE slot_key=? AND kind='observation'",
            (claim_slot(group["scope_id"], ctx.project_id, ctx.branch_id, proposal),),
        )
        .fetchone()
    )
    if old is not None and not renew_observation_head(tx, old[0]):
        return None
    previous = tx.claims.slot(group["scope_id"], proposal)
    head = next((v for v in previous if v.revision == v.current_revision), None)
    if head is not None and head.payload.get("kind") == "observation" and reflection_live(tx, head):
        return None
    saved = tx.claims.append(
        group["scope_id"],
        proposal,
        Qualification("active", "derived_summary", "grounded_observation"),
        previous=head,
        recorded_at=now,
    )
    if owner_conflict(tx, saved, members):
        tx.claims.append(
            saved.scope_id,
            saved.payload,
            Qualification("retracted", "derived_summary", "grounded_observation"),
            previous=saved,
            recorded_at=now,
        )
        return None
    tx._check().execute(
        "INSERT INTO observations VALUES(?,?,?,?,0)",
        (saved.ref, saved.revision, group["manifest_json"], group["signature"]),
    )
    return saved


def next_reflection_due(tx, *, now):
    """Wake for one affordable page or group, respecting persisted historical cadence."""
    if private.source_blocked(private.active_intervals(tx, now), None):
        return None
    available = [row for row in reflection_usage(tx, now=now) if row["calls"] < row["limit"]]
    ctx, conn = tx.context, tx._check()
    audience = reflection_audience(tx)
    cursor = conn.execute("SELECT position FROM reflection_cursors WHERE audience=?", (audience,)).fetchone()
    position = cursor[0] if cursor else 0
    scopes = ",".join("?" for _ in ctx.allowed_scope_ids)
    params = (*sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id)
    unseen = conn.execute(
        "SELECT 1 FROM source_events WHERE rowid>? AND scope_id IN ("
        + scopes
        + ") AND project_id IS ? AND branch_id IS ? AND role='user' LIMIT 1",
        (position, *params),
    ).fetchone()
    ready = conn.execute(
        "SELECT DISTINCT scope_id FROM reflection_groups WHERE scope_id IN ("
        + scopes
        + ") AND project_id IS ? AND branch_id IS ? AND "
        + AVAILABLE_GROUP
        + (
            " AND new_sources>=required_sources AND json_array_length(buffered_json)>=8 AND ("
            "SELECT count(DISTINCT json_extract(value,'$.session_id')) FROM json_each(buffere"
            "d_json))>=2"
        ),
        params,
    ).fetchall()
    if (ready or unseen) and not available:
        return reflection_usage(tx, now=now)[0]["resets_at"]
    if any(row[0] in {scope["scope_id"] for scope in available} for row in ready):
        return now
    if unseen:
        cadence = conn.execute("SELECT next_at FROM reflection_discovery WHERE audience=?", (audience,)).fetchone()
        return max(now, cadence[0]) if cadence and cadence[0] else now
    return reflection_usage(tx, now=now)[0]["resets_at"] if ready else None
