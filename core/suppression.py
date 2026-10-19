"""Reversible suppression, separate from permanent deletion tombstones."""

from __future__ import annotations

from dataclasses import replace

from ..contracts import ContractError
from .delete_storage import OBJECT_TABLES, group_digest


def release(tx, targets, *, now):
    """Clear the reviewed suppression closure; deleted content can never be revived."""
    conn = tx._check(write=True)
    for target in targets:
        table, key = OBJECT_TABLES[target.kind]
        row = conn.execute(f"SELECT max(read_blocked) FROM {table} WHERE {key}=?", (target.ref,)).fetchone()
        absent = conn.execute(
            "SELECT 1 FROM restored_absence_blocks WHERE object_kind=? AND object_ref=?", (target.kind, target.ref)
        ).fetchone()
        if row is None or row[0] is None or row[0] or absent:
            raise ContractError("ACCESS_DENIED", "deleted_content")
    for target in targets:
        table, key = OBJECT_TABLES[target.kind]
        conn.execute(
            "DELETE FROM restored_suppression_blocks WHERE object_kind=? AND object_ref=?",
            (target.kind, target.ref),
        )
        conn.execute(f"UPDATE {table} SET suppressed=0 WHERE {key}=? AND read_blocked=0", (target.ref,))
        conn.execute(
            "UPDATE object_blocks SET suppressed=0 WHERE object_kind=? AND object_ref=? AND read_blocked=0",
            (target.kind, target.ref),
        )
        if target.kind == "event":
            for row in conn.execute(
                "SELECT DISTINCT source_group_key FROM source_events WHERE event_id=?", (target.ref,)
            ):
                digest = group_digest(tx.context.binding, target.scope_id, target.project_id, target.branch_id, row[0])
                conn.execute(
                    "UPDATE source_group_blocks SET suppressed=0 WHERE group_sha256=? AND read_blocked=0", (digest,)
                )
    conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
    resume_candidates(tx, now=now)


def snapshot(conn):
    """Export all live suppression identities, including inherited source flags."""
    queries = [
        f"SELECT DISTINCT '{kind}' AS object_kind,{key} AS object_ref,"
        f"{'source_revision' if kind == 'event' else '0'} AS revision,scope_id,project_id,branch_id "
        f"FROM {table} WHERE suppressed=1 AND read_blocked=0"
        for kind, (table, key) in OBJECT_TABLES.items()
    ]
    queries.append(
        "SELECT object_kind,object_ref,revision,scope_id,project_id,branch_id FROM restored_suppression_blocks"
    )
    return [dict(row) for row in conn.execute(" UNION ".join(queries) + " ORDER BY object_kind,object_ref,revision")]


def reset_snapshot_suppression(conn, scopes, objects):
    """Let a complete latest ledger replace reversible state from an older snapshot.

    Deletion markers remain monotone. Suppression may have been released since
    the snapshot, so replay starts from unsuppressed, nondeleted objects only.
    """
    marks = ",".join("?" for _ in scopes)
    conn.execute(f"DELETE FROM restored_suppression_blocks WHERE scope_id IN ({marks})", tuple(scopes))
    tables = [table for table, _key in OBJECT_TABLES.values()] + ["object_blocks", "source_group_blocks"]
    for table in tables:
        conn.execute(f"UPDATE {table} SET suppressed=0 WHERE read_blocked=0 AND scope_id IN ({marks})", tuple(scopes))
    for obj in objects:
        if (
            type(obj) is not dict
            or set(obj) != {"object_kind", "object_ref", "revision", "scope_id", "project_id", "branch_id"}
            or obj["object_kind"] not in OBJECT_TABLES
            or obj["scope_id"] not in scopes
            or type(obj["revision"]) is not int
            or (obj["revision"] < 1 if obj["object_kind"] == "event" else obj["revision"] != 0)
        ):
            raise ContractError("RESTORE_UNVERIFIED", "suppression_shape")
        table, key = OBJECT_TABLES[obj["object_kind"]]
        conn.execute(
            "INSERT INTO restored_suppression_blocks VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            tuple(obj[k] for k in ("object_kind", "object_ref", "revision", "scope_id", "project_id", "branch_id")),
        )
        version = "AND source_revision=?" if obj["object_kind"] == "event" else ""
        conn.execute(
            f"UPDATE {table} SET suppressed=1 WHERE {key}=? AND scope_id=? "
            f"AND project_id IS ? AND branch_id IS ? AND read_blocked=0 {version}",
            (obj["object_ref"], obj["scope_id"], obj["project_id"], obj["branch_id"])
            + ((obj["revision"],) if version else ()),
        )


def resume_candidates(tx, *, now):
    """Reconcile released candidate lifecycles in the caller's authorized partitions."""
    conn = tx._check(write=True)
    scopes = tx.context.allowed_scope_ids
    marks = ",".join("?" for _ in scopes)
    rows = conn.execute(
        f"""SELECT c.claim_id,c.current_revision,c.project_id,c.branch_id FROM claims c
        JOIN candidate_lifecycle l ON l.candidate_ref=c.claim_id AND l.candidate_revision=c.current_revision
        WHERE c.scope_id IN ({marks}) AND c.read_blocked=0 AND c.suppressed=0
        AND l.processing_state='blocked' AND l.reason LIKE 'suppressed:%'""",
        tuple(scopes),
    ).fetchall()
    for row in rows:
        context = replace(tx.context, project_id=row["project_id"], branch_id=row["branch_id"])
        view = tx.with_context(context, writable=True)
        view.candidates.register(row["claim_id"], row["current_revision"], observed_at=now)
