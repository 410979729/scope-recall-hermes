"""Persist group safety before admission and fence every dependent content exit."""

from __future__ import annotations

import hashlib
import json
import time

from . import injection_policy
from .injection_policy import SCAN_WINDOWS, ScanResult
from .injection_policy import scan as scan
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


def group_input(rows):
    """Canonical decoded events plus identities, hashing actual bytes, not stored digests."""
    events = [stored_source(row, len(rows)).event for row in rows]
    identity = [(row["event_id"], row["source_revision"], event) for row, event in zip(rows, events, strict=True)]
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return events, digest


def save_checkpoint(tx, rows, cursor, policy, now):
    """Persist a withheld group's exact-input checkpoint and idempotent local work."""
    anchor = rows[0]
    _events, digest = group_input(rows)
    conn = tx._check(write=True)
    old = conn.execute(
        "SELECT * FROM source_scan_progress WHERE source_ref=? AND source_revision=?",
        (anchor["event_id"], anchor["source_revision"]),
    ).fetchone()
    fingerprint = policy.fingerprint if policy is not None else ""
    if old is not None and old["input_sha256"] == digest and old["policy_sha256"] == fingerprint:
        previous = json.loads(old["cursor_json"])
        if (previous.get("value", 0), previous.get("offset", 0)) > (cursor.get("value", 0), cursor.get("offset", 0)):
            cursor = previous
    conn.execute(
        """INSERT INTO source_scan_progress VALUES (?,?,?,?,?)
        ON CONFLICT(source_ref,source_revision) DO UPDATE SET input_sha256=excluded.input_sha256,
        policy_sha256=excluded.policy_sha256,cursor_json=excluded.cursor_json""",
        (anchor["event_id"], anchor["source_revision"], digest, fingerprint, json.dumps(cursor)),
    )
    conn.execute(
        """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at)
        VALUES ('rebuild_projection',?,?,?,?,?,?) ON CONFLICT(work_type,subject_ref,subject_revision)
        DO UPDATE SET state='pending',attempt=0,available_at=excluded.available_at,lease_owner=NULL,lease_until=NULL""",
        (
            anchor["event_id"],
            anchor["source_revision"],
            anchor["scope_id"],
            anchor["project_id"],
            anchor["branch_id"],
            now,
        ),
    )


def record_scan_deadline(tx, rows, verdict, now):
    """Keep content-free fallback counts in the continuation job's retained history."""
    if "scan_time_budget" in verdict.rules:
        tx._check(write=True).execute(
            "INSERT INTO work_error_details(work_id,lease_token,stage,error_code,recorded_at) "
            "SELECT work_id,lease_token,'source_scan','scan_time_budget',? FROM work_items "
            "WHERE work_type='rebuild_projection' AND subject_ref=? AND subject_revision=?",
            (now, rows[0]["event_id"], rows[0]["source_revision"]),
        )


def _scan_events(tx, events, remaining_seconds, policy, max_windows):
    """Join body boundaries once, then check every part's own metadata under one budget."""
    return injection_policy.scan_events(
        tx, events, remaining_seconds=remaining_seconds, policy=policy, max_windows=max_windows
    )


def complete_events(rows):
    """Return a complete capture group, treating masking separately from missing parts."""
    if not rows:
        return None
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
        return None
    return events


def evidence_verdict(tx, events, verdict):
    """Keep content whose evidence is held unavailable at either publication boundary."""
    if verdict.state == "clear":
        evidence = tuple(ref.rsplit("@", 1)[0] for event in events for ref in event.get("evidence_refs", ()))
        if blocked_refs(tx, "event", evidence):
            return ScanResult("pending_review", ("held_evidence",), windows=verdict.windows)
    return verdict


def _group_verdict(tx, rows, remaining_seconds, policy, max_windows, *, expired=False):
    events = complete_events(rows)
    if events is None:
        return ScanResult("pending_review", ("incomplete_scan_group",))
    if expired:
        return ScanResult("pending_review", ("scan_time_budget",), {})
    if policy is None:
        return ScanResult("pending_review", ("invalid_scan_policy",))
    verdict = _scan_events(tx, events, remaining_seconds, policy, max_windows)
    return evidence_verdict(tx, events, verdict)


def _record_verdict(tx, rows, verdict, now, *, fresh=()):
    conn = tx._check(write=True)
    for row in rows:
        key = (row["event_id"], row["source_revision"])
        old = (
            None
            if key in fresh
            else conn.execute(
                "SELECT state FROM source_safety WHERE source_ref=? AND source_revision=?", key
            ).fetchone()
        )
        if old is not None and old[0] in {"quarantined", "released"}:
            continue
        if verdict.state == "clear":
            if old is not None:
                conn.execute("DELETE FROM source_safety WHERE source_ref=? AND source_revision=?", key)
        else:
            conn.execute(
                """INSERT INTO source_safety(source_ref,source_revision,state,rules_json,scanned_at)
                VALUES (?,?,?,?,?) ON CONFLICT(source_ref,source_revision) DO UPDATE
                SET state=excluded.state,rules_json=excluded.rules_json,scanned_at=excluded.scanned_at
                WHERE source_safety.state NOT IN ('quarantined','released')""",
                (*key, verdict.state, json.dumps(verdict.rules), now),
            )


def _remove_held_projections(tx, rows, *, preserve_work_id=None):
    held = blocked_refs(tx, "event", (row["event_id"] for row in rows))
    conn = tx._check(write=True)
    for row in rows:
        if row["event_id"] in held:
            conn.execute("DELETE FROM lexical_postings WHERE source_id=?", (row["source_id"],))
            conn.execute(
                """DELETE FROM work_items WHERE subject_ref=? AND state IN ('pending','leased') AND work_id IS NOT ?
                AND NOT (work_type='rebuild_projection' AND EXISTS(
                    SELECT 1 FROM source_scan_progress p WHERE p.source_ref=work_items.subject_ref
                    AND p.source_revision=work_items.subject_revision))""",
                # A hold cancels derived work, but must not cancel the local job
                # that can finish that hold, including an older group version.
                (row["event_id"], preserve_work_id),
            )


def screen_sources(tx, sources, now, *, remaining_seconds=None):
    """Rescan complete database groups and return versions ready for first admission.

    All pieces are written first. Pending pieces from a previous delivery are
    scanned and admitted in this same transaction when their final piece arrives.
    Existing quarantine and owner release decisions are never overwritten.
    """
    deadline = None if remaining_seconds is None else time.perf_counter() + max(0, remaining_seconds)
    expired = False
    try:
        policy = injection_policy.load_policy(tx._check(), deadline)
    except TimeoutError:
        policy, expired = None, True
    except (ValueError, TypeError, KeyError, RecursionError):
        policy = None
    seen, eligible = set(), []
    inserted = {(s.ref, s.revision) for s in sources if s.disposition == "inserted"}
    windows = SCAN_WINDOWS
    for source in sources:
        if (source.ref, source.revision) in seen:
            continue
        rows = group_rows(tx, source.ref, source.revision)
        seen.update((row["event_id"], row["source_revision"]) for row in rows)
        pending = (
            set()
            if all((r["event_id"], r["source_revision"]) in inserted for r in rows)
            else {
                (r[0], r[1])
                for r in tx._check().execute(
                    "SELECT source_ref,source_revision FROM source_safety WHERE state='pending_review' "
                    "AND source_revision=? "
                    "AND source_ref IN (SELECT value FROM json_each(?))",
                    (source.revision, json.dumps([row["event_id"] for row in rows])),
                )
            }
        )
        left = None if deadline is None else max(0, deadline - time.perf_counter())
        verdict = _group_verdict(tx, rows, left, policy, windows, expired=expired)
        windows -= verdict.windows
        _record_verdict(tx, rows, verdict, now, fresh=inserted)
        held = blocked_refs(tx, "event", (row["event_id"] for row in rows))
        if held:
            _remove_held_projections(tx, group_rows(tx, source.ref))
        if verdict.progress is not None:
            save_checkpoint(tx, rows, verdict.progress, policy, now)
            record_scan_deadline(tx, rows, verdict, now)
        elif pending:
            tx._check(write=True).execute(
                "DELETE FROM source_scan_progress WHERE source_revision=? "
                "AND source_ref IN (SELECT value FROM json_each(?))",
                (source.revision, json.dumps([row["event_id"] for row in rows])),
            )
        for row in rows:
            if row["event_id"] not in held and (row["event_id"], row["source_revision"]) in inserted | pending:
                eligible.append((row["event_id"], row["source_revision"]))
    return eligible


def held_roots_sql(seed: str) -> str:
    """SQL returning held root refs from a trusted SELECT of (root, kind, ref).

    Both bounded collection queries and hydrated reads use this dependency
    walk. The seed owns its parameters; callers never supply user text as SQL.
    An empty hold set skips the walk independently of corpus size.
    """
    # Start from the bounded dependency roots. The unary plus leaves equality
    # unchanged while preventing the session index from replacing the group-key lookup.
    group_match = GROUP_MATCH.replace("h.scope_id", "+h.scope_id").replace("h.session_id", "+h.session_id")
    return f"""WITH RECURSIVE lineage(root,kind,ref) AS (
            SELECT * FROM ({seed}) WHERE EXISTS (
                SELECT 1 FROM source_safety WHERE state!='released')
            UNION
            SELECT l.root,d.dependency_kind,d.dependency_ref FROM lineage l JOIN object_dependencies d
            ON d.object_kind=l.kind AND d.object_ref=l.ref UNION
            SELECT l.root,'event',e.source_ref FROM lineage l JOIN evidence_links e
            ON e.object_kind=l.kind AND e.object_ref=l.ref)
        SELECT DISTINCT l.root FROM lineage l CROSS JOIN source_events e
        CROSS JOIN source_events h CROSS JOIN source_safety s
        WHERE l.kind='event' AND e.event_id=l.ref AND {group_match}
        AND s.source_ref=h.event_id AND s.source_revision=h.source_revision
        AND s.state!='released' AND h.read_blocked=0"""


def blocked_refs(tx, kind, refs) -> frozenset[str]:
    """Return held groups and their dependent objects through the common visibility gate."""
    wanted = tuple(dict.fromkeys(refs))
    if not wanted:
        return frozenset()
    conn = tx._check()
    # A read transaction has a stable snapshot; a write transaction's
    # ``remembered`` deliberately reloads after a hold changes in that transaction.
    held = tx.remembered(
        ("source_safety_has_holds",),
        lambda: (
            conn.execute(
                "SELECT 1 FROM source_safety WHERE state IN ('quarantined','pending_review') LIMIT 1"
            ).fetchone()
            is not None
        ),
    )
    if not held:
        return frozenset()
    rows = conn.execute(
        held_roots_sql("SELECT value,?,value FROM json_each(?)"),
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
