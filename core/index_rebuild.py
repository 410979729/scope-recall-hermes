"""Bounded operator scheduling over existing source truth and work_items.

No second queue and no extraction: migration only schedules eligible current
sources for embedding. Publication still uses the normal worker's fences.
"""

from dataclasses import replace
from datetime import datetime, timezone

from ..contracts import ContractError
from .visibility import allowed


def queue_claim_embedding_page(
    storage, context, *, after_key=None, limit=128, watermark=None
):
    """Schedule existing claim heads for embedding, one bounded page at a time.

    A new claim head enqueues its own embed work when written, so this exists
    only for instances that already had a derived layer before it was
    indexable: on those, every existing claim stays invisible to search until
    it is backfilled. Same guard, cursor discipline and fences as the source
    page — an operator action that schedules work, never a second queue.
    """
    if context.actor_origin not in {"host_generated", "human_direct"}:
        raise ContractError("ACCESS_DENIED", "index_rebuild")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ContractError("INPUT_INVALID", "index_page")
    for key in (after_key, watermark):
        if key is not None and (
            type(key) not in (list, tuple) or len(key) != 2
            or type(key[0]) is not str or type(key[1]) is not int or key[1] < 0
        ):
            raise ContractError("INPUT_INVALID", "index_cursor")
    after_key = tuple(after_key or ("", 0))
    with storage.read(context) as tx:
        conn = tx._check()
        scopes = sorted(context.allowed_scope_ids)
        marks = ",".join("?" for _ in scopes)
        if watermark is None:
            last = conn.execute(
                f"""SELECT c.claim_id,c.current_revision AS revision FROM claims c
                    WHERE c.scope_id IN ({marks}) ORDER BY c.claim_id DESC LIMIT 1""",
                scopes,
            ).fetchone()
            watermark = tuple(last) if last else ("", 0)
        watermark = tuple(watermark)
        if after_key > watermark:
            raise ContractError("INPUT_INVALID", "index_cursor")
        # Current heads only, read straight off `claims`: scope, project and branch
        # live there, not on claim_versions, and a superseded revision has no
        # reason to occupy the index — its vector would compete with the version
        # that replaced it.
        rows = conn.execute(
            f"""SELECT c.claim_id,c.current_revision AS revision,c.scope_id,c.project_id,c.branch_id
            FROM claims c
            WHERE c.scope_id IN ({marks}) AND (c.claim_id,c.current_revision)>(?,?)
              AND (c.claim_id,c.current_revision)<=(?,?)
              AND c.read_blocked=0 AND c.suppressed=0
            ORDER BY c.claim_id,c.current_revision LIMIT ?""",
            (*scopes, *after_key, *watermark, limit),
        ).fetchall()
    scheduled = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        scoped = replace(
            context,
            allowed_scope_ids=frozenset({row["scope_id"]}),
            project_id=row["project_id"],
            branch_id=row["branch_id"],
        )
        with storage.write(scoped) as tx:
            versions = tx.claims.versions(row["claim_id"])
            version = next((v for v in versions if v.revision == row["revision"]), None)
            if version is None:
                continue
            try:
                tx.claims.require_target(version)
            except ContractError:
                continue
            if tx.work.enqueue("embed", row["claim_id"], row["revision"], available_at=now):
                scheduled += 1
    cursor = (rows[-1]["claim_id"], rows[-1]["revision"]) if rows else watermark
    return dict(
        after_key=cursor,
        watermark=watermark,
        scanned=len(rows),
        eligible=scheduled,
        finished=cursor >= watermark or len(rows) < limit,
    )


def queue_embedding_page(
    storage, context, *, after_key=None, limit=128, watermark=None
):
    if context.actor_origin not in {"host_generated", "human_direct"}:
        raise ContractError("ACCESS_DENIED", "index_rebuild")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ContractError("INPUT_INVALID", "index_page")
    for key in (after_key, watermark):
        if key is not None and (
            type(key) not in (list, tuple)
            or len(key) != 2
            or type(key[0]) is not str
            or type(key[1]) is not int
            or key[1] < 0
        ):
            raise ContractError("INPUT_INVALID", "index_cursor")
    after_key = tuple(after_key or ("", 0))
    with storage.read(context) as tx:
        conn = tx._check()
        scopes = sorted(context.allowed_scope_ids)
        marks = ",".join("?" for _ in scopes)
        if watermark is None:
            last = conn.execute(
                f"SELECT event_id,source_revision FROM source_events WHERE scope_id IN ({marks}) ORDER BY event_id DESC,source_revision DESC LIMIT 1",
                scopes,
            ).fetchone()
            watermark = tuple(last) if last else ("", 0)
        watermark = tuple(watermark)
        if after_key > watermark:
            raise ContractError("INPUT_INVALID", "index_cursor")
        # Stable primary-key pagination survives deletion/VACUUM; SQLite rowid
        # is not a durable cursor. New live captures use their normal enqueue.
        rows = conn.execute(
            f"""SELECT event_id,source_revision,scope_id,project_id,branch_id
            FROM source_events WHERE scope_id IN ({marks}) AND (event_id,source_revision)>(?,?) AND (event_id,source_revision)<=(?,?)
            ORDER BY event_id,source_revision LIMIT ?""",
            (*scopes, *after_key, *watermark, limit),
        ).fetchall()
    scheduled = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        scoped = replace(
            context,
            allowed_scope_ids=frozenset({row["scope_id"]}),
            project_id=row["project_id"],
            branch_id=row["branch_id"],
        )
        with storage.write(scoped) as tx:
            source = tx.source(row["event_id"], row["source_revision"])
            if (
                source is None
                or source.suppressed
                or not allowed(tx, "event", source.ref, automatic=True)
            ):
                continue
            try:
                tx.claims.require_live_source(source.ref, source.revision)
            except ContractError:
                continue
            tx.enqueue_source(
                source.ref, source.revision, work_type="embed", available_at=now
            )
            scheduled += 1
    cursor = (rows[-1]["event_id"], rows[-1]["source_revision"]) if rows else watermark
    return dict(
        after_key=cursor,
        watermark=watermark,
        scanned=len(rows),
        eligible=scheduled,
        finished=cursor >= watermark or len(rows) < limit,
    )
