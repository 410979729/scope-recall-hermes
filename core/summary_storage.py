"""Bounded summary discovery, durable coverage and scoped raw-message substitution."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from ..contracts import ContractError
from . import private
from .claims import canonical_time
from .handoff import close_drafts
from .language_resources import language_rules
from .recall_scope import QueryScope
from .summary_parts import queue_summary_part, reuse_completed_parts
from .summary_validity import group_members, group_signature, members_in_scope, open_task, summary_live

IDLE_SECONDS = 1800


def previous_conversation_scope(tx, query, *, now):
    """Resolve a previous-conversation question to one visible source session, preserving raw fallback."""
    if not language_rules("query_time")["previous_conversation"].fullmatch(query.strip()):
        return None
    ctx = tx.context
    rows = (
        tx._check()
        .execute(
            "SELECT event_id,source_revision,session_id,entry_id FROM source_events WHERE scope_id IN ("
            + ",".join("?" for _ in ctx.allowed_scope_ids)
            + ") AND project_id IS ? AND branch_id IS ? AND session_id!=? AND occurred_at<=? "
            "AND role='user' AND read_blocked=0 AND suppressed=0 ORDER BY occurred_at DESC,rowid DESC LIMIT 64",
            (*sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id, ctx.session_id, now),
        )
        .fetchall()
    )
    for row in rows:
        source = tx.source(row[0], row[1])
        if source is None or source.suppressed:
            continue
        stamps = (
            tx._check()
            .execute(
                "SELECT min(occurred_at),max(occurred_at) FROM source_events WHERE session_id=? "
                "AND scope_id=? AND entry_id=? AND project_id IS ? AND branch_id IS ?",
                (row[2], source.scope_id, row[3], ctx.project_id, ctx.branch_id),
            )
            .fetchone()
        )
        if stamps[0] and stamps[1]:
            end = (datetime.fromisoformat(stamps[1]) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            return QueryScope(((stamps[0], end),), (row[3],), query, (row[2],))
    return None


def note_source(tx, source, *, now, historical=False):
    """Register conversation and task partitions without copying text or requesting a model."""
    if source is None or source.event["role"] not in {"user", "assistant", "tool"}:
        return
    groups = [("session", source.session_id)] if source.event["role"] != "tool" else []
    # Capture already attached this source; only the task identity and terminal state are needed here.
    episode = (
        tx._check()
        .execute(
            "SELECT e.episode_id,e.anchor_kind,v.state FROM episode_events ee "
            "JOIN episodes e ON e.episode_id=ee.episode_id "
            "JOIN episode_versions v ON v.episode_id=e.episode_id AND v.revision=e.current_revision "
            "WHERE ee.source_ref=? AND ee.source_revision=?",
            (source.ref, source.revision),
        )
        .fetchone()
    )
    if episode is not None and episode["anchor_kind"] == "task":
        groups.append(("task", episode["episode_id"]))
    if source.event["role"] == "tool" and episode is not None and episode["state"] == "completed":
        tx._check(write=True).execute(
            "UPDATE summary_groups SET state='waiting' WHERE kind='session' AND subject_ref=? "
            "AND scope_id=? AND project_id IS ? AND branch_id IS ? AND state='open_task'",
            (source.session_id, source.scope_id, source.project_id, source.branch_id),
        )
    for kind, subject in groups:
        values = [kind, subject, source.scope_id, source.project_id, source.branch_id, source.entry_id]
        key = hashlib.sha256(json.dumps(values).encode()).hexdigest()
        tx._check(write=True).execute(
            "INSERT INTO "
            "summary_groups(group_key,kind,subject_ref,scope_id,project_id,branch_id,entry_id,last_activity) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(group_key) DO UPDATE SET "
            "last_activity=max(last_activity,excluded.last_activity),closed=CASE WHEN ? THEN closed ELSE 0 END,"
            "state=CASE WHEN ? THEN state ELSE 'waiting' END",
            (key, *values, now, int(historical), int(historical)),
        )
        if historical:
            # Safety review can restore sources after a part could not even be
            # queued. Retry only source failures; model failures keep their
            # existing retry budget and complete summaries remain readable.
            tx._check().execute(
                "UPDATE summary_groups SET state='waiting',signature='',failure=NULL WHERE group_key=? "
                "AND state='partial' AND failure IN ('source_coverage','source_unavailable','summary_source')",
                (key,),
            )


def close_session(tx, session_id, *, now):
    """Record an explicit end signal for this bound session; new capture reopens it."""
    if session_id != tx.context.session_id:
        raise ContractError("ACCESS_DENIED", "session")
    close_drafts(tx, session_id, now)
    tx._check(write=True).execute(
        "UPDATE summary_groups SET closed=1 WHERE kind='session' AND subject_ref=? AND scope_id IN ("
        + ",".join("?" for _ in tx.context.allowed_scope_ids)
        + ") AND project_id IS ? AND branch_id IS ?",
        (session_id, *sorted(tx.context.allowed_scope_ids), tx.context.project_id, tx.context.branch_id),
    )


def _discover(tx, limit):
    ctx = tx.context
    audience = hashlib.sha256(
        json.dumps([sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id]).encode()
    ).hexdigest()
    conn = tx._check(write=True)
    conn.execute("INSERT OR IGNORE INTO summary_backfill(audience) VALUES(?)", (audience,))
    position = conn.execute("SELECT position FROM summary_backfill WHERE audience=?", (audience,)).fetchone()[0]
    rows = conn.execute(
        "SELECT rowid,event_id,source_revision FROM source_events WHERE rowid>? AND scope_id IN ("
        + ",".join("?" for _ in ctx.allowed_scope_ids)
        + ") AND project_id IS ? AND branch_id IS ? ORDER BY rowid LIMIT ?",
        (position, *sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id, limit),
    ).fetchall()
    for row in rows:
        source = tx.source(row[1], row[2])
        if source is not None:
            # Historical sources keep their recorded activity; importing them does not make an old conversation active.
            note_source(tx, source, now=source.event["recorded_at"], historical=True)
    if rows:
        conn.execute("UPDATE summary_backfill SET position=? WHERE audience=?", (rows[-1][0], audience))


def _ended(tx, group, now):
    if group["kind"] == "task":
        episode = tx.episodes.get(group["subject_ref"])
        return episode is not None and episode.state == "completed" and not episode.suppressed
    if open_task(tx, group):
        return False
    if group["closed"]:
        return True
    last, current = canonical_time(group["last_activity"]), canonical_time(now)
    if last is None or current is None:
        return False
    return datetime.fromisoformat(current) - datetime.fromisoformat(last) >= timedelta(seconds=IDLE_SECONDS)


def queue_summaries(tx, *, now, limit=128, enabled=True):
    """Discover one old-source page and queue at most eight ended groups for the budgeted worker."""
    if not enabled:
        return 0
    _discover(tx, limit)
    ctx = tx.context
    audience = (
        "groups:"
        + hashlib.sha256(
            json.dumps([sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id]).encode()
        ).hexdigest()
    )
    conn = tx._check(write=True)
    conn.execute("INSERT OR IGNORE INTO summary_backfill(audience) VALUES(?)", (audience,))
    position = conn.execute("SELECT position FROM summary_backfill WHERE audience=?", (audience,)).fetchone()[0]
    rows = conn.execute(
        "SELECT rowid AS position,* FROM summary_groups WHERE rowid>? AND scope_id IN ("
        + ",".join("?" for _ in ctx.allowed_scope_ids)
        + ") AND project_id IS ? AND branch_id IS ? AND state IN ('waiting','complete') ORDER BY rowid LIMIT 64",
        (position, *sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id),
    ).fetchall()
    conn.execute(
        "UPDATE summary_backfill SET position=? WHERE audience=?", (rows[-1]["position"] if rows else 0, audience)
    )
    queued = 0
    for row in rows:
        if row["kind"] == "task" or open_task(tx, row):
            if not _ended(tx, row, now):
                tx._check().execute(
                    "UPDATE summary_groups SET state='open_task' WHERE group_key=?", (row["group_key"],)
                )
                continue
        if queued == 8 or not _ended(tx, row, now):
            continue
        members = group_members(tx, row)
        if not members or len(members) > 2048 or any(m["read_blocked"] or m["suppressed"] for m in members):
            tx._check().execute(
                "UPDATE summary_groups SET state='partial',failure='source_coverage' WHERE group_key=?",
                (row["group_key"],),
            )
            continue
        signature = group_signature(tx, members)
        if row["signature"] == signature:
            if row["total_units"] > 0 and row["covered_units"] == row["total_units"]:
                tx._check().execute("UPDATE summary_groups SET state='complete' WHERE group_key=?", (row["group_key"],))
            continue
        group = dict(
            row,
            generation=row["generation"] + 1,
            manifest_json=json.dumps(members),
            state="pending",
            total_units=sum(m["total"] for m in members),
            covered_units=0,
            next_offset=0,
            signature=signature,
        )
        tx._check(write=True).execute(
            "UPDATE summary_groups SET generation=?,manifest_json=?,state='pending',total_units=?,covered_units=0,"
            "next_offset=0,signature=?,failure=NULL WHERE group_key=?",
            (group["generation"], group["manifest_json"], group["total_units"], signature, group["group_key"]),
        )
        try:
            group = reuse_completed_parts(tx, row, group, now=now)
            if group["state"] != "complete":
                ordinal = (
                    tx._check()
                    .execute(
                        "SELECT COALESCE(max(ordinal),-1)+1 FROM summary_parts WHERE group_key=? AND generation=?",
                        (group["group_key"], group["generation"]),
                    )
                    .fetchone()[0]
                )
                queue_summary_part(tx, group, ordinal, now)
        except ContractError:
            tx._check().execute(
                "UPDATE summary_groups SET state='partial',failure='source_unavailable' WHERE group_key=?",
                (row["group_key"],),
            )
        else:
            queued += 1
    return queued


def summarized_rows(tx, context, rows, *, limit):
    """Offer valid summaries first; remove only raw messages literally covered in full."""
    if context.mode in {"history", "as_of"}:
        return rows
    raw = {(r["event_id"], r["source_revision"]) for r in rows}
    if not raw:
        return rows
    raw_json = json.dumps(sorted(raw))
    groups = (
        tx._check()
        .execute(
            "SELECT g.* FROM summary_groups g WHERE g.state='complete' AND g.scope_id IN ("
            + ",".join("?" for _ in tx.context.allowed_scope_ids)
            + ") AND g.project_id IS ? AND g.branch_id IS ? AND EXISTS("
            "SELECT 1 FROM json_each(?) r JOIN source_events s ON s.event_id=json_extract(r.value,'$[0]') "
            "AND s.source_revision=json_extract(r.value,'$[1]') WHERE s.scope_id=g.scope_id "
            "AND s.entry_id=g.entry_id AND ((g.kind='session' AND g.subject_ref=s.session_id) OR "
            "(g.kind='task' AND EXISTS(SELECT 1 FROM episode_events ee WHERE ee.episode_id=g.subject_ref "
            "AND ee.source_ref=s.event_id AND ee.source_revision=s.source_revision)))) "
            "ORDER BY g.last_activity DESC LIMIT 64",
            (*sorted(tx.context.allowed_scope_ids), tx.context.project_id, tx.context.branch_id, raw_json),
        )
        .fetchall()
    )
    replacements, covered = [], set()
    for group in groups:
        manifest = json.loads(group["manifest_json"])
        members = {(m["ref"], m["revision"]) for m in manifest if m.get("role") in {"user", "assistant"}}
        if (
            not members
            or not members <= raw
            or members & covered
            or not members_in_scope(manifest, context.scope)
            or any(f"{m['ref']}@{m['revision']}" in context.current_source_refs for m in manifest)
        ):
            continue
        parts = (
            tx._check()
            .execute(
                "SELECT * FROM summary_parts WHERE group_key=? AND generation=? ORDER BY ordinal",
                (group["group_key"], group["generation"]),
            )
            .fetchall()
        )
        versions = [(p, tx.claims.version(p["claim_ref"], p["claim_revision"])) for p in parts]
        active = [(p, v) for p, v in versions if v is not None and v.state == "active"]
        if (
            not active
            or len(active) + len(replacements) > limit
            or not all(summary_live(tx, p["claim_ref"], p["claim_revision"]) for p, _v in active)
        ):
            continue
        if all(not version.suppressed for _p, version in active):
            replacements.extend(
                dict(
                    event_id=p["claim_ref"],
                    source_revision=p["claim_revision"],
                    role="summary",
                    size=0,
                    short=None,
                    candidate_kind="claim",
                )
                for p, _v in active
            )
            covered.update(fully_quoted_sources(tx, [version for _p, version in active], members))
    return [*replacements, *(r for r in rows if (r["event_id"], r["source_revision"]) not in covered)]


def fully_quoted_sources(tx, versions, members):
    """Identify raw entries whose every character is present in positioned exact excerpts."""
    spans = {}
    for version in versions:
        for span in version.payload["evidence_spans"]:
            if "start" in span and "end" in span:
                spans.setdefault((span["source_ref"], span["source_revision"]), []).append(span)
    covered = set()
    for key in members:
        source = tx.source(*key)
        if source is None:
            continue
        content, position = source.event["content"], 0
        for span in sorted(spans.get(key, ()), key=lambda span: span["start"]):
            if span["start"] > position or content[span["start"] : span["end"]] != span["quote"]:
                break
            position = max(position, span["end"])
        if position == len(content):
            covered.add(key)
    return covered


def next_summary_due(tx, *, now):
    """Advise the existing supervisor about old-source discovery and idle closure; grant no write authority."""
    if private.source_blocked(private.active_intervals(tx, now), None):
        return None
    ctx = tx.context
    audience = hashlib.sha256(
        json.dumps([sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id]).encode()
    ).hexdigest()
    cursor = tx._check().execute("SELECT position FROM summary_backfill WHERE audience=?", (audience,)).fetchone()
    position = cursor[0] if cursor else 0
    scopes = ",".join("?" for _ in ctx.allowed_scope_ids)
    params = (*sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id)
    if (
        tx._check()
        .execute(
            "SELECT 1 FROM source_events WHERE rowid>? AND scope_id IN ("
            + scopes
            + ") AND project_id IS ? AND branch_id IS ? LIMIT 1",
            (position, *params),
        )
        .fetchone()
    ):
        return now
    groups = (
        tx._check()
        .execute(
            "SELECT * FROM summary_groups WHERE state='waiting' AND scope_id IN ("
            + scopes
            + ") AND project_id IS ? AND branch_id IS ? ORDER BY last_activity LIMIT 64",
            params,
        )
        .fetchall()
    )
    due = []
    for group in groups:
        if _ended(tx, group, now):
            due.append(now)
        elif group["kind"] == "session" and not open_task(tx, group):
            stamp = canonical_time(group["last_activity"])
            if stamp:
                due.append(
                    (datetime.fromisoformat(stamp) + timedelta(seconds=IDLE_SECONDS)).isoformat().replace("+00:00", "Z")
                )
    return min(due, default=None)
