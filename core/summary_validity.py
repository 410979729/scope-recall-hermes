"""Version and audience checks shared by summary hydration and final release."""

from __future__ import annotations

import hashlib
import json

from ..contracts import ContractError
from .episodes import source_origin

CONVERSATION_SUMMARY_REASONS = ("summary_pending", "grounded_session_summary", "grounded_task_summary")
DEPENDENCY_EXCLUDED_REASONS = (
    *CONVERSATION_SUMMARY_REASONS,
    "reflection_pending",
    "grounded_observation",
    "confirmed_observation",
)


def is_conversation_summary(version):
    """Identify this derived kind without changing other users of the shared summary basis."""
    return version.reason in CONVERSATION_SUMMARY_REASONS


def group_members(tx, group):
    """Return a bounded, ordered manifest of current conversation source versions."""
    task = group["kind"] == "task"
    join = "JOIN episode_events ee ON ee.source_ref=s.event_id AND ee.source_revision=s.source_revision" if task else ""
    roles = "('user','assistant','tool')" if task else "('user','assistant')"
    origins = "'human_direct','assistant_visible','tool_observation'" if task else "'human_direct','assistant_visible'"
    target = "ee.episode_id=? AND s.entry_id=?" if task else "s.session_id=? AND s.entry_id=?"
    params = (group["subject_ref"], group["entry_id"])
    return [
        dict(row)
        for row in tx._check().execute(
            f"""SELECT s.event_id AS ref,s.source_revision AS revision,s.content_sha256 AS digest,
             length(s.content) AS total,s.role,s.entry_id,s.session_id,s.occurred_at,s.recorded_at,
             s.read_blocked,s.suppressed
          FROM source_events s {join} WHERE {target} AND s.scope_id=?
          AND s.project_id IS ? AND s.branch_id IS ? AND s.role IN {roles}
          AND (s.origin IN ({origins}) OR (s.origin='imported'
               AND s.import_provenance_sha256 IS NOT NULL
               AND s.source_original_origin IN ({origins})))
          AND NOT EXISTS(SELECT 1 FROM source_events n WHERE n.source_group_key=s.source_group_key
              AND n.source_revision>s.source_revision)
          ORDER BY s.occurred_at,s.event_id,s.source_revision LIMIT 2049""",
            (*params, group["scope_id"], group["project_id"], group["branch_id"]),
        )
    ]


def obsolete_ranges(tx, members):
    """Locate evidence belonging to corrected or withdrawn fact versions.

    Positioned evidence excludes only the overlapping sentence. Legacy evidence
    without positions conservatively excludes its whole source, never a guessed
    occurrence. Conversation summaries do not invalidate each other.
    """
    ranges = {}
    by_ref = {(m["ref"], m["revision"]): m for m in members}
    for start in range(0, len(members), 128):
        page = members[start : start + 128]
        rows = tx._check().execute(
            "SELECT DISTINCT l.source_ref,l.source_revision,v.payload_json FROM evidence_links l "
            "JOIN claims c ON c.claim_id=l.object_ref JOIN claim_versions v ON v.claim_id=c.claim_id "
            "AND v.revision=l.object_revision JOIN claim_versions h ON h.claim_id=c.claim_id "
            "AND h.revision=c.current_revision WHERE l.object_kind='claim' "
            "AND ((v.revision!=c.current_revision AND v.payload_json!=h.payload_json) "
            "OR h.state IN ('retracted','superseded')) "
            "AND v.qualification_reason NOT IN (?,?,?,?,?,?) AND (l.source_ref,l.source_revision) IN ("
            + ",".join("(?,?)" for _ in page)
            + ")",
            [*DEPENDENCY_EXCLUDED_REASONS, *(value for m in page for value in (m["ref"], m["revision"]))],
        )
        for row in rows:
            key = (row[0], row[1])
            spans = [
                span
                for span in json.loads(row[2])["evidence_spans"]
                if (span["source_ref"], span["source_revision"]) == key
            ]
            for span in spans or [{}]:
                positioned = type(span.get("start")) is int and type(span.get("end")) is int
                bounds = (span["start"], span["end"]) if positioned else (0, by_ref[key]["total"])
                ranges.setdefault(key, set()).add(bounds)
    return {key: sorted(value) for key, value in ranges.items()}


def group_signature(tx, members):
    """Bind raw versions and all facts derived from them, including later corrections."""
    facts = []
    for start in range(0, len(members), 128):
        page = members[start : start + 128]
        if not page:
            continue
        rows = (
            tx._check()
            .execute(
                "SELECT DISTINCT c.claim_id,c.current_revision,c.read_blocked,c.suppressed,v.state "
                "FROM evidence_links l "
                "JOIN claims c ON c.claim_id=l.object_ref JOIN claim_versions v "
                "ON v.claim_id=c.claim_id AND v.revision=c.current_revision "
                "WHERE l.object_kind='claim' AND v.qualification_reason NOT IN (?,?,?,?,?,?) "
                "AND (l.source_ref,l.source_revision) IN (" + ",".join("(?,?)" for _ in page) + ") ORDER BY c.claim_id",
                [*DEPENDENCY_EXCLUDED_REASONS, *(value for m in page for value in (m["ref"], m["revision"]))],
            )
            .fetchall()
        )
        facts.extend(tuple(row) for row in rows)
    excluded = [(ref, rev, spans) for (ref, rev), spans in sorted(obsolete_ranges(tx, members).items())]
    body = json.dumps(["whole_sentences", members, sorted(set(facts)), excluded], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def open_task(tx, group):
    """Confirmed unfinished work keeps its conversation on the original-message path.

    Hosts may assign automatic task anchors to ordinary conversation. An
    unknown anchor alone proves no unfinished work and does not end a task;
    task summaries independently require a completed episode.
    """
    row = (
        tx._check()
        .execute(
            "SELECT 1 FROM source_events s JOIN episode_events ee ON ee.source_ref=s.event_id AND "
            "ee.source_revision=s.source_revision "
            "JOIN episodes e ON e.episode_id=ee.episode_id JOIN episode_versions v ON "
            "v.episode_id=e.episode_id AND v.revision=e.current_revision "
            "WHERE s.session_id=? AND s.scope_id=? AND s.entry_id=? AND e.anchor_kind='task' AND "
            "v.state IN ('open','interrupted') LIMIT 1",
            (group["subject_ref"], group["scope_id"], group["entry_id"]),
        )
        .fetchone()
    )
    return row is not None


def members_in_scope(members, scope):
    """Apply a conversation query's date, entry and session limits to every evidence member."""
    return scope is None or all(
        all(key in member for key in ("entry_id", "session_id", "occurred_at"))
        and any(start <= member["occurred_at"] < end for start, end in scope.windows)
        and (not scope.entry_ids or member["entry_id"] in scope.entry_ids)
        and (not scope.session_ids or member["session_id"] in scope.session_ids)
        for member in members
    )


def summary_live(tx, ref, revision, *, query_scope=None, excluded=()):
    """Fail closed until full coverage exists and every dependency is still current and visible."""
    row = (
        tx._check()
        .execute(
            "SELECT g.*,p.coverage_json,p.dependencies_json FROM summary_parts p JOIN "
            "summary_groups g USING(group_key) "
            "WHERE p.claim_ref=? AND p.claim_revision=? AND p.generation=g.generation",
            (ref, revision),
        )
        .fetchone()
    )
    if row is None or row["state"] != "complete" or row["covered_units"] != row["total_units"]:
        return False
    members = json.loads(row["manifest_json"])
    if not members_in_scope(members, query_scope) or any(
        f"{member['ref']}@{member['revision']}" in excluded for member in members
    ):
        return False
    ctx = tx.context
    if row["scope_id"] not in ctx.allowed_scope_ids or (row["project_id"], row["branch_id"]) != (
        ctx.project_id,
        ctx.branch_id,
    ):
        return False
    task = tx.episodes.get(row["subject_ref"]) if row["kind"] == "task" else None
    ended = (
        (task is not None and task.state == "completed" and not task.suppressed)
        if row["kind"] == "task"
        else not open_task(tx, row)
    )
    head = tx.claims.version(ref, revision)
    if head is None or head.current_revision != revision or head.state != "active" or not ended:
        return False
    current = tx.remembered(
        ("summary_signature", row["group_key"], row["generation"]), lambda: group_signature(tx, group_members(tx, row))
    )
    if current != row["signature"] or current != row["dependencies_json"]:
        return False
    try:
        coverage = json.loads(row["coverage_json"])
        for member in json.loads(row["manifest_json"]):
            source_ref, source_revision = member["ref"], member["revision"]
            source = tx.source(source_ref, source_revision)
            tx.claims.require_live_source(source_ref, source_revision)
            if source is None or source.suppressed or source.capture_gaps:
                return False
    except ContractError:
        return False
    return bool(coverage)


def summary_origin(tx, ref, revision):
    """Label a summary by the least trusted member, resolving verified imports.

    The whole manifest is authoritative, including members on other pages or
    omitted from the excerpts. This label never grants derivation authority:
    summaries are claims, while fact evidence roots must be source events.
    """
    row = (
        tx._check()
        .execute(
            "SELECT g.manifest_json FROM summary_parts p JOIN summary_groups g USING(group_key) "
            "WHERE p.claim_ref=? AND p.claim_revision=? AND p.generation=g.generation",
            (ref, revision),
        )
        .fetchone()
    )
    if row is None:
        return "origin_unknown"
    origins = []
    for member in json.loads(row[0]):
        source = tx.source(member["ref"], member["revision"])
        origins.append(source_origin(source) if source is not None else "origin_unknown")
    trust = {"tool_observation": 0, "assistant_visible": 1, "human_direct": 2}
    return min(origins, key=lambda origin: trust.get(origin, -1), default="origin_unknown")
