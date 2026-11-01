"""Persist group safety before admission and fence every dependent content exit."""

from __future__ import annotations

import json
import time

from .injection_policy import SCAN_SECONDS, ScanResult, scan
from .source_records import stored_source

# A ref changes when segment counts change. This identity remains stable across
# versions, within the source's authorization and conversation partition.
GROUP_MATCH = """h.source_group_key=e.source_group_key AND h.scope_id=e.scope_id
    AND h.project_id IS e.project_id AND h.branch_id IS e.branch_id AND h.session_id=e.session_id"""


def group_rows(tx, ref, revision=None):
    """Read the message group for an already-authorized source, including older versions."""
    return (
        tx._check()
        .execute(
            f"""SELECT DISTINCT h.* FROM source_events e JOIN source_events h ON {GROUP_MATCH}
        WHERE e.event_id=? AND (? IS NULL OR h.source_revision=?)
        ORDER BY h.source_revision,h.segment_index,h.source_id""",
            (ref, revision, revision),
        )
        .fetchall()
    )


def _scan_events(tx, events, remaining_seconds):
    """Join body boundaries once, then check every part's own metadata under one budget."""
    deadline = time.perf_counter() + remaining_seconds
    content = "".join(event["content"] for event in events)
    for index, event in enumerate(events):
        verdict = scan(
            tx,
            dict(event, content=content if index == 0 else ""),
            remaining_seconds=max(0, deadline - time.perf_counter()),
        )
        if verdict.state != "clear":
            return verdict
    return verdict


def _group_verdict(tx, rows, remaining_seconds):
    total = rows[0]["segment_total"] or 1
    events = [stored_source(row, len(rows)).event for row in rows]
    indices = {row["segment_index"] for row in rows}
    # Masking replaces bytes but does not omit a segment of the captured message.
    if (
        any("segment" in e and e["segment"]["total"] is None for e in events)
        or len(rows) != total
        or (total > 1 and indices != set(range(total)))
        or any(
            e.get("segment", {}).get("truncated")
            and set(json.loads(row["capture_gaps_json"])) != {"secret_values_masked"}
            for row, e in zip(rows, events, strict=True)
        )
    ):
        return ScanResult("pending_review", ("incomplete_scan_group",))
    verdict = _scan_events(tx, events, remaining_seconds)
    if verdict.state == "clear":
        evidence = tuple(ref.rsplit("@", 1)[0] for event in events for ref in event.get("evidence_refs", ()))
        if blocked_refs(tx, "event", evidence):
            return ScanResult("pending_review", ("held_evidence",))
    return verdict


def _record_verdict(tx, rows, verdict, now):
    conn = tx._check(write=True)
    for row in rows:
        key = (row["event_id"], row["source_revision"])
        old = conn.execute("SELECT state FROM source_safety WHERE source_ref=? AND source_revision=?", key).fetchone()
        if old is not None and old[0] in {"quarantined", "released"}:
            continue
        if verdict.state == "clear":
            conn.execute("DELETE FROM source_safety WHERE source_ref=? AND source_revision=?", key)
        else:
            conn.execute(
                """INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at)
                VALUES (?,?,?,?,?) ON CONFLICT(source_ref,source_revision) DO UPDATE
                SET state=excluded.state,rules_json=excluded.rules_json,scanned_at=excluded.scanned_at""",
                (*key, verdict.state, json.dumps(verdict.rules), now),
            )


def _remove_held_projections(tx, rows):
    held = blocked_refs(tx, "event", (row["event_id"] for row in rows))
    conn = tx._check(write=True)
    for row in rows:
        if row["event_id"] in held:
            conn.execute("DELETE FROM lexical_postings WHERE source_id=?", (row["source_id"],))
            conn.execute(
                "DELETE FROM work_items WHERE subject_ref=? AND state IN ('pending','leased')", (row["event_id"],)
            )


def screen_sources(tx, sources, now, *, remaining_seconds=SCAN_SECONDS):
    """Rescan complete database groups and return versions ready for first admission.

    All pieces are written first. Pending pieces from a previous delivery are
    scanned and admitted in this same transaction when their final piece arrives.
    Existing quarantine and owner release decisions are never overwritten.
    """
    deadline = time.perf_counter() + min(SCAN_SECONDS, max(0, remaining_seconds))
    seen, eligible = set(), []
    inserted = {(s.ref, s.revision) for s in sources if s.disposition == "inserted"}
    for source in sources:
        if (source.ref, source.revision) in seen:
            continue
        rows = group_rows(tx, source.ref, source.revision)
        seen.update((row["event_id"], row["source_revision"]) for row in rows)
        pending = {
            (r[0], r[1])
            for r in tx._check().execute(
                "SELECT source_ref,source_revision FROM source_safety WHERE state='pending_review' "
                "AND source_revision=? "
                "AND source_ref IN (SELECT value FROM json_each(?))",
                (source.revision, json.dumps([row["event_id"] for row in rows])),
            )
        }
        verdict = _group_verdict(tx, rows, max(0, deadline - time.perf_counter()))
        _record_verdict(tx, rows, verdict, now)
        held = blocked_refs(tx, "event", (row["event_id"] for row in rows))
        if held:
            _remove_held_projections(tx, group_rows(tx, source.ref))
        for row in rows:
            if row["event_id"] not in held and (row["event_id"], row["source_revision"]) in inserted | pending:
                eligible.append((row["event_id"], row["source_revision"]))
    return eligible


def blocked_refs(tx, kind, refs) -> frozenset[str]:
    """Return held groups and their dependent objects through the common visibility gate."""
    wanted = tuple(dict.fromkeys(refs))
    if not wanted:
        return frozenset()
    rows = tx._check().execute(
        f"""WITH RECURSIVE lineage(root,kind,ref) AS (
            SELECT value,?,value FROM json_each(?) WHERE EXISTS (
                SELECT 1 FROM source_safety WHERE state!='released')
            UNION
            SELECT l.root,d.dependency_kind,d.dependency_ref FROM lineage l JOIN object_dependencies d
            ON d.object_kind=l.kind AND d.object_ref=l.ref UNION
            SELECT l.root,'event',e.source_ref FROM lineage l JOIN evidence_links e
            ON e.object_kind=l.kind AND e.object_ref=l.ref)
        SELECT DISTINCT l.root FROM lineage l JOIN source_events e ON l.kind='event' AND e.event_id=l.ref
        JOIN source_events h ON {GROUP_MATCH}
        JOIN source_safety s ON s.source_ref=h.event_id AND s.source_revision=h.source_revision
        WHERE s.state!='released' AND h.read_blocked=0""",
        (kind, json.dumps(wanted)),
    )
    return frozenset(row[0] for row in rows)


def notice(tx, ref, revision=None):
    """Return a content-free hold inside the ordinary scope, project and deletion fence."""
    scopes = sorted(tx.context.allowed_scope_ids)
    row = (
        tx._check()
        .execute(
            f"""SELECT s.state,s.rules_json,e.source_revision FROM source_events e
        JOIN source_events h ON {GROUP_MATCH}
        JOIN source_safety s ON s.source_ref=h.event_id AND s.source_revision=h.source_revision
        WHERE e.event_id=? AND s.state!='released' AND (? IS NULL OR e.source_revision=?)
        AND e.read_blocked=0 AND h.read_blocked=0 AND e.scope_id IN ({",".join("?" for _ in scopes)})
        AND (e.project_id IS NULL OR e.project_id=?) AND (e.branch_id IS NULL OR e.branch_id=?)
        AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event' AND b.object_ref=e.event_id
            AND b.read_blocked=1)
        AND NOT EXISTS(SELECT 1 FROM restored_absence_blocks a WHERE a.object_kind='event' AND a.object_ref=e.event_id)
        ORDER BY e.source_revision DESC,s.state DESC,h.source_revision DESC LIMIT 1""",
            (ref, revision, revision, *scopes, tx.context.project_id, tx.context.branch_id),
        )
        .fetchone()
    )
    return (
        None
        if row is None
        else dict(state=row["state"], rules=json.loads(row["rules_json"]), revision=row["source_revision"])
    )
