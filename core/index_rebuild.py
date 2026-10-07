"""Bounded operator scheduling over existing source truth and work_items.

No second queue and no extraction: migration only schedules eligible current
sources for embedding. Publication still uses the normal worker's fences.
"""

from dataclasses import replace
from datetime import datetime, timezone

from ..contracts import ContractError
from .admission import classify
from .visibility import allowed

#: The roles of an import whose words a person reads back: what the owner said, what they were told, their
#: documents, and the notes an older store kept without a role.  An import queued an embedding only where its source
#: store had one (``maintenance/shared_import.py``), so a store that never had one left its history findable by its
#: words alone: on the pilot one agent's, 1,928 of the owner's messages, 6,552 replies and 3,009 notes.  Tool output is
#: left out: 200,000 imported outputs would cost more to embed than everything else in the store, and a captured
#: one keeps its vector 180 days (``tool_output_retention_days``).
IMPORT_EMBED_ROLES = ("user", "assistant", "document", "unknown")
#: A page is added only while fewer embeddings than this wait, so a message captured now is never queued behind an
#: import's history for its vector.
IMPORT_EMBED_QUEUE_CEILING = 64
#: The queue an import's embeddings are kept to while other work the worker takes after embeddings is ready: half a
#: pass of the default 32 items (``queue_import_embeddings``).
IMPORT_EMBED_YIELD_CEILING = 16


def queue_embedding_page(storage, context, *, after_key=None, limit=128, watermark=None):
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
            if source is None or source.suppressed or not allowed(tx, "event", source.ref, automatic=True):
                continue
            try:
                tx.claims.require_live_source(source.ref, source.revision)
            except ContractError:
                continue
            tx.enqueue_source(source.ref, source.revision, work_type="embed", available_at=now)
            scheduled += 1
    cursor = (rows[-1]["event_id"], rows[-1]["source_revision"]) if rows else watermark
    return dict(
        after_key=cursor,
        watermark=watermark,
        scanned=len(rows),
        eligible=scheduled,
        finished=cursor >= watermark or len(rows) < limit,
    )


def queue_import_embeddings(
    storage,
    context,
    *,
    after_key=None,
    limit: int = 64,
    yield_to: frozenset[str] = frozenset(),
    yield_ceiling: int = IMPORT_EMBED_YIELD_CEILING,
    now: datetime | None = None,
) -> dict:
    """Queue an embedding for imported sources in ``IMPORT_EMBED_ROLES`` that never had one, a page at a time.

    ``after_key`` is where the last page stopped: a source the admission rules keep without one (an
    acknowledgement) is passed over, not looked at again on every page.  A page tops the embedding queue up to
    ``IMPORT_EMBED_QUEUE_CEILING``, and to ``yield_ceiling`` while work of a type in ``yield_to`` is ready; with the
    queue there, nothing is queued, the page is ``held`` and the cursor stays where it was.  The worker claims
    embeddings before candidate evaluations (``work_storage._CLAIM_ORDER``): the backfill kept a page past the
    ceiling waiting (up to 127), every pass took embeddings alone, and on the pilot 251 evaluations waited behind an
    import's history for the hours it ran (rc10).  Stopping for them instead stopped it for as long as they could
    not be done, a model refusing before any request (review of rc11); kept to part of a pass, it still moves.
    Only sources the context's worker would embed are looked at, those of its project and branch: an import kept
    by another's (a store converted from 2.x keeps them) was queued where this worker neither counts nor claims
    it, and every pass queued another page of them past the ceiling.
    """
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ContractError("INPUT_INVALID", "import_embed_page")
    if after_key is not None and (
        type(after_key) not in (list, tuple)
        or len(after_key) != 2
        or type(after_key[0]) is not str
        or type(after_key[1]) is not int
    ):
        raise ContractError("INPUT_INVALID", "import_embed_cursor")
    if type(yield_ceiling) is not int or not 1 <= yield_ceiling <= IMPORT_EMBED_QUEUE_CEILING:
        raise ContractError("INPUT_INVALID", "import_embed_yield_ceiling")
    after_key = tuple(after_key or ("", 0))
    moment = (now or datetime.now(timezone.utc)).isoformat()
    scopes = sorted(context.allowed_scope_ids)
    marks = ",".join("?" for _ in scopes)
    roles = ",".join("?" for _ in IMPORT_EMBED_ROLES)
    with storage.read(context) as tx:
        ceiling = IMPORT_EMBED_QUEUE_CEILING
        if yield_to and tx.work.other_work_ready(now=moment, kinds=frozenset(yield_to)):
            ceiling = yield_ceiling
        room = ceiling - tx.work.pending_depth("embed")
        if room <= 0:
            return dict(after_key=after_key, queued=0, scanned=0, held=True, finished=False)
        # No more sources are looked at than can join the queue: the cursor passes every one looked at.
        scan = min(limit, room)
        rows = (
            tx._check()
            .execute(
                f"""SELECT s.event_id,s.source_revision,s.scope_id,s.project_id,s.branch_id FROM source_events s
                WHERE s.scope_id IN ({marks}) AND (s.event_id,s.source_revision)>(?,?)
                  AND (s.project_id IS NULL OR s.project_id=?) AND (s.branch_id IS NULL OR s.branch_id=?)
                  AND s.import_provenance_sha256 IS NOT NULL AND s.role IN ({roles})
                  AND COALESCE(s.source_original_origin,'')<>'memory_reinjection'
                  AND s.read_blocked=0 AND s.suppressed=0
                  AND NOT EXISTS(SELECT 1 FROM source_events n
                      WHERE n.source_group_key=s.source_group_key AND n.source_revision>s.source_revision)
                  AND NOT EXISTS(SELECT 1 FROM work_items w WHERE w.work_type='embed'
                      AND w.subject_ref=s.event_id AND w.subject_revision=s.source_revision)
                ORDER BY s.event_id,s.source_revision LIMIT ?""",
                (*scopes, *after_key, context.project_id, context.branch_id, *IMPORT_EMBED_ROLES, scan),
            )
            .fetchall()
        )
    groups: dict[tuple, list] = {}
    for row in rows:
        groups.setdefault((row["scope_id"], row["project_id"], row["branch_id"]), []).append(row)
    queued = 0
    for (scope_id, project_id, branch_id), members in groups.items():
        scoped = replace(context, allowed_scope_ids=frozenset({scope_id}), project_id=project_id, branch_id=branch_id)
        with storage.write(scoped) as tx:
            for row in members:
                source = tx.source(row["event_id"], row["source_revision"])
                if source is None or source.suppressed or not allowed(tx, "event", source.ref, automatic=True):
                    continue
                try:
                    tx.claims.require_live_source(source.ref, source.revision)
                except ContractError:
                    continue
                if classify(source.event).disposition != "schedule":
                    continue
                tx.enqueue_source(source.ref, source.revision, work_type="embed", available_at=moment)
                queued += 1
    cursor = (rows[-1]["event_id"], rows[-1]["source_revision"]) if rows else after_key
    return dict(after_key=cursor, queued=queued, scanned=len(rows), held=False, finished=len(rows) < scan)
