"""Replay completed legacy privacy purges as Core deletion operations.

A purge names journal entries; the replay must fence everything Core would
consider derived from them: episodes, memories, fact records, and whole
procedure archive groups. The closure is planned from the archived sources,
then applied inside the single write transaction.
"""
from __future__ import annotations

from typing import Any

from .legacy_plan import Conversion, Row
from .legacy_sources import (
    _anchor_ref, _evidence_items, _json_list, _resolve, _safe, journal_links,
)
from .migration_records import _canon, _digest, _json, _recorded, _stable

_OBJECT_TABLES = {
    "event": ("source_events", "event_id"),
    "episode": ("episodes", "episode_id"),
    "claim": ("claims", "claim_id"),
}
_PENDING_LAYERS = {
    "sqlite_active": "pending",
    "vector_active": "pending",
    "attachments": "pending",
    "historical_storage": "pending",
}


def _procedure_archive_groups(cv: Conversion) -> list[tuple[set[str], set[str]]]:
    """A procedure and all its snapshots form one deletion object.

    A deleted source cited by any snapshot affects the complete archive group,
    including versions no longer used as current evidence.
    """
    groups: list[tuple[set[str], set[str]]] = []
    for playbook in cv.rows["procedural_playbooks"]:
        old_id = str(playbook.get("id") or "")
        group: set[str] = set()
        if ("procedural_playbooks", old_id) in cv.archives:
            group.add(cv.archives[("procedural_playbooks", old_id)])
        records: list[Row] = [playbook]
        for snapshot in cv.rows["playbook_versions"]:
            if str(snapshot.get("playbook_id")) != old_id:
                continue
            ref = cv.archives.get(("playbook_versions", str(snapshot.get("id"))))
            if ref:
                group.add(ref)
            data = _json(snapshot.get("snapshot"), {})
            if isinstance(data, dict):
                records.append(data)
        dependencies: set[str] = set()
        for record in records:
            for anchor in _evidence_items(record.get("evidence_anchors")):
                ref = _resolve(_anchor_ref(anchor), anchor.get("source_type"), cv.journal_refs, cv.memory_refs, cv.archives)
                if ref:
                    dependencies.add(ref)
        groups.append((group, dependencies))
    return groups


def _closure(
    cv: Conversion,
    journal_ids: list[str],
    links: dict[str, list[str]],
    groups: list[tuple[set[str], set[str]]],
) -> set[str]:
    """Every archived object Core would treat as derived from the purged journals."""
    refs = {cv.journal_refs[x] for x in journal_ids if x in cv.journal_refs}
    for episode in cv.rows["task_episodes"]:
        members = [str(x) for x in _json_list(episode.get("journal_entry_ids"))]
        if any(x in journal_ids for x in members):
            refs.add(cv.episode_refs[str(episode.get("id"))])
    for memory in cv.rows["memories"]:
        mid = str(memory.get("id"))
        if mid in cv.memory_refs and any(x in journal_ids for x in links.get(mid, [])):
            refs.add(cv.memory_refs[mid])
    previous = -1
    while len(refs) != previous:
        previous = len(refs)
        for evidence in cv.rows["fact_claim_evidence"]:
            resolved = _resolve(evidence.get("source_ref"), evidence.get("source_type"), cv.journal_refs, cv.memory_refs, cv.archives)
            if resolved not in refs:
                continue
            for table, column in (("fact_claims", "claim_id"), ("fact_claim_evidence", "evidence_id")):
                ref = cv.archives.get((table, str(evidence.get(column) or "")))
                if ref:
                    refs.add(ref)
        for group, dependencies in groups:
            if refs & (group | dependencies):
                refs.update(group)
    return refs


def plan_deletions(cv: Conversion) -> None:
    """Turn each completed legacy purge into a deletion spec over resolved refs."""
    groups = _procedure_archive_groups(cv)
    links = journal_links(cv.rows)
    scope_by_journal: dict[str, Any] = {}
    for row in cv.rows["journal_entries"]:
        scope_by_journal.setdefault(str(row.get("id")), row.get("scope_id"))
    for row in cv.rows["privacy_purge_operations"]:
        op = str(row.get("operation_id") or "")
        if str(row.get("status") or "") != "completed":
            cv.unmapped("privacy_purge_operations", op, "non_completed_tombstone_not_replayed_blocks_cutover", redacted_row=_safe(row))
            continue
        journal_ids = [
            str(item.get("journal_entry_id"))
            for item in cv.rows["privacy_purge_source_tombstones"]
            if str(item.get("operation_id")) == op
        ]
        default_scope = sorted(cv.requested)[0]
        scopes = sorted({str(scope_by_journal.get(x, default_scope)) for x in journal_ids})
        cv.deletion_specs.append({
            "operation_id": _stable("deletion", op),
            "legacy_operation_id": op,
            "refs": sorted(_closure(cv, journal_ids, links, groups)),
            "scopes": scopes or [default_scope],
            "created_at": _recorded(row.get("created_at")),
        })
    for row in cv.rows["privacy_purge_source_tombstones"]:
        if str(row.get("journal_entry_id")) not in cv.journal_refs:
            cv.unmapped("privacy_purge_source_tombstones", str(row.get("journal_entry_id")), "source_missing_unknown_blocks_cutover", redacted_row=_safe(row))


def _insert_operation(conn: Any, spec: Row, batch_key: str) -> None:
    epoch = int(conn.execute("SELECT memory_epoch FROM instance_meta WHERE singleton=1").fetchone()[0]) + 1
    request = {"legacy_operation_id": spec["legacy_operation_id"], "batch_key": batch_key, "refs": spec["refs"]}
    conn.execute(
        "INSERT INTO deletion_operations(operation_id,request_sha256,mode,scope_ids_json,project_id,branch_id,requested_refs_json,expected_revisions_json,created_at,memory_epoch,layers_json,active_content_removed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            spec["operation_id"],
            _digest(request),
            "delete",
            _canon(spec["scopes"]),
            None,
            None,
            _canon(spec["refs"]),
            _canon({x: 1 for x in spec["refs"]}),
            spec["created_at"],
            epoch,
            _canon(_PENDING_LAYERS),
            0,
        ),
    )
    conn.execute("UPDATE instance_meta SET memory_epoch=max(memory_epoch,?) WHERE singleton=1", (epoch,))


def _block(conn: Any, op_id: str, kind: str, ref: str, owner: tuple[Any, ...]) -> None:
    """Add one object to the operation and fence it in place."""
    table, column = _OBJECT_TABLES[kind]
    conn.execute(
        "INSERT INTO deletion_members(operation_id,object_kind,object_ref) VALUES (?,?,?) ON CONFLICT DO NOTHING",
        (op_id, kind, ref),
    )
    conn.execute(
        "INSERT INTO object_blocks(object_kind,object_ref,scope_id,project_id,branch_id,read_blocked,suppressed,operation_id) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(object_kind,object_ref) DO UPDATE SET read_blocked=1,suppressed=1,operation_id=excluded.operation_id",
        (kind, ref, owner[0], owner[1], owner[2], 1, 1, op_id),
    )
    conn.execute(f"UPDATE {table} SET read_blocked=1,suppressed=1 WHERE {column}=?", (ref,))


def write_deletions(cv: Conversion, tx: Any) -> None:
    conn = tx._check(write=True)
    for spec in cv.deletion_specs:
        op_id = spec["operation_id"]
        if conn.execute("SELECT memory_epoch FROM deletion_operations WHERE operation_id=?", (op_id,)).fetchone() is None:
            _insert_operation(conn, spec, cv.batch_key)
        for ref in spec["refs"]:
            event = conn.execute("SELECT scope_id,project_id,branch_id FROM source_events WHERE event_id=?", (ref,)).fetchone()
            episode = conn.execute("SELECT scope_id,project_id,branch_id FROM episodes WHERE episode_id=?", (ref,)).fetchone()
            owner = event or episode
            if owner is not None:
                _block(conn, op_id, "episode" if episode else "event", ref, tuple(owner))
        # Claims whose evidence was purged are fenced too, not rewritten.
        derived = conn.execute(
            "SELECT DISTINCT object_ref FROM evidence_links WHERE object_kind='claim' AND source_ref IN (SELECT object_ref FROM deletion_members WHERE operation_id=? AND object_kind='event')",
            (op_id,),
        ).fetchall()
        for (claim_id,) in derived:
            owner = conn.execute("SELECT scope_id,project_id,branch_id FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
            if owner:
                _block(conn, op_id, "claim", claim_id, tuple(owner))
        tx.deletions.purge_sqlite(op_id)
