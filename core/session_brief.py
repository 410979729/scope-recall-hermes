"""Precomputed scope briefing manifests and authority-checked rendering."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

from ..contracts import ContractError, utc_timestamp
from . import lineage, private
from .claim_storage import parse_source_ref
from .claims import statement_key
from .entry_control import context_fence, entry_changed
from .recall_budget import estimate_tokens
from .retrieval import CandidateRef, SearchContext, SearchLimits
from .retrieval_hydration import RetrievalHydration
from .writer_lease import TruthWriterBusyError

_HEADING = "Session background:"


def _dependencies(conn, items):
    """Bind the shared lineage's source versions to their current lifecycle state."""
    result = []
    for item in items:
        kind = "episode" if item["kind"] == "task" else "claim"
        pairs = lineage.evidence(conn, kind, item["ref"], item["revision"])
        rows = conn.execute(
            "SELECT json_extract(p.value,'$[0]'),json_extract(p.value,'$[1]'), "
            "s.content_sha256,s.read_blocked,s.suppressed,coalesce(guard.state,'released'), "
            "(SELECT max(newer.source_revision) FROM source_events newer "
            " WHERE newer.source_group_key=s.source_group_key) "
            "FROM json_each(?) p LEFT JOIN source_events s "
            "ON s.event_id=json_extract(p.value,'$[0]') AND s.source_revision=json_extract(p.value,'$[1]') "
            "LEFT JOIN source_safety guard ON guard.source_ref=s.event_id AND guard.source_revision=s.source_revision "
            "ORDER BY json_extract(p.value,'$[0]'),json_extract(p.value,'$[1]')",
            (json.dumps(pairs),),
        )
        result.append((kind, item["ref"], item["revision"], [tuple(row) for row in rows]))
    return result


def rebuild_scope(tx, scope_id: str, now: str) -> bool:
    """Replace one scope's manifest when the selected object versions or their rank change."""
    conn = tx._check(write=True)
    cutoff = (datetime.fromisoformat(utc_timestamp(now)) - timedelta(days=14)).isoformat()
    tasks = conn.execute(
        "SELECT e.episode_id,e.current_revision,v.recorded_at FROM episodes e "
        "JOIN episode_versions v ON v.episode_id=e.episode_id AND v.revision=e.current_revision "
        "WHERE e.scope_id=? AND e.anchor_kind='task' AND e.read_blocked=0 AND e.suppressed=0 "
        "AND v.state IN ('open','interrupted') AND v.resume_json IS NOT NULL "
        "ORDER BY v.recorded_at DESC,e.episode_id LIMIT 3",
        (scope_id,),
    ).fetchall()
    decisions = conn.execute(
        "SELECT c.claim_id,c.current_revision,coalesce(v.valid_from,v.recorded_from),c.importance,c.pinned "
        "FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision "
        "WHERE c.scope_id=? AND c.kind='decision' AND c.read_blocked=0 AND c.suppressed=0 "
        "AND v.state='active' AND coalesce(v.valid_from,v.recorded_from)>=? "
        "ORDER BY CASE WHEN c.pinned=1 THEN 5 ELSE c.importance END DESC, "
        "coalesce(v.valid_from,v.recorded_from) DESC,c.claim_id LIMIT 5",
        (scope_id, cutoff),
    ).fetchall()
    preferences = conn.execute(
        "SELECT c.claim_id,c.current_revision,coalesce(v.valid_from,v.recorded_from),c.importance,c.pinned "
        "FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision "
        "WHERE c.scope_id=? AND c.kind IN ('preference','constraint') AND c.read_blocked=0 AND c.suppressed=0 "
        "AND v.state='active' AND (c.importance>=4 OR c.pinned=1) "
        "ORDER BY CASE WHEN c.pinned=1 THEN 5 ELSE c.importance END DESC, "
        "coalesce(v.valid_from,v.recorded_from) DESC,c.claim_id LIMIT 8",
        (scope_id,),
    ).fetchall()
    items = [
        {"kind": kind, "ref": row[0], "revision": row[1], "time": row[2], "rank": list(row[3:])}
        for kind, rows in (("task", tasks), ("decision", decisions), ("preference", preferences))
        for row in rows
    ]
    encoded = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dependencies = json.dumps(_dependencies(conn, items), ensure_ascii=False, separators=(",", ":"))
    signature = hashlib.sha256((encoded + dependencies).encode("utf-8")).hexdigest()
    old = conn.execute("SELECT signature FROM session_briefs WHERE scope_id=?", (scope_id,)).fetchone()
    if old is not None and old[0] == signature:
        return False
    conn.execute(
        "INSERT INTO session_briefs(scope_id,version,signature,generated_at,items_json) VALUES(?,1,?,?,?) "
        "ON CONFLICT(scope_id) DO UPDATE SET version=version+1,signature=excluded.signature, "
        "generated_at=excluded.generated_at,items_json=excluded.items_json",
        (scope_id, signature, now, encoded),
    )
    return True


def _line(tx, item, obj, now: str) -> str | None:
    """Render a current fact or task without copying quoted evidence."""
    if item["kind"] == "task":
        episode = tx.episodes.get(item["ref"])
        if episode is None or episode.state not in {"open", "interrupted"} or not episode.resume:
            return None
        content = episode.resume.get("goal", {}).get("text", "")
        kind = "task"
    else:
        claim = tx.claims.version(item["ref"], item["revision"])
        if claim is None or claim.state != "active" or claim.current_revision != claim.revision:
            return None
        if item["kind"] == "decision":
            cutoff = datetime.fromisoformat(utc_timestamp(now)) - timedelta(days=14)
            if datetime.fromisoformat(utc_timestamp(item["time"])) < cutoff:
                return None
        payload = claim.payload
        content = " ".join(str(payload.get(key, "")) for key in ("subject", "predicate", "value_text"))
        conditions = payload.get("conditions") or []
        if conditions:
            content += " " + json.dumps(conditions, ensure_ascii=False, separators=(",", ":"))
        kind = payload["kind"]
        if (
            kind == "decision"
            and tx._check()
            .execute(
                "SELECT 1 FROM adoption_relations WHERE claim_ref=? AND claim_revision=? AND state='bound' LIMIT 1",
                (claim.ref, claim.revision),
            )
            .fetchone()
        ):
            kind = "decision (owner adopted assistant proposal)"
    content = " ".join(str(content).split())
    if not content:
        return None
    return f"{kind} | {content} | {item['time']} | {item['ref']}@{item['revision']}"


def _entry(context) -> str:
    """The installation-scoped host entry that owns this session's delivery marks."""
    return context.entry_id or context.binding.installation_id


def already_injected(tx, kind: str, ref: str, revision: int) -> bool:
    """Whether this session received this object after the latest compaction."""
    if kind != "claim":
        return False
    context = tx.context
    row = (
        tx._check()
        .execute(
            "SELECT delivery_compacted_seq FROM session_brief_injections WHERE entry_id=? AND session_id=? "
            "AND object_kind=? AND object_ref=? AND revision=?",
            (_entry(context), context.session_id, kind, ref, revision),
        )
        .fetchone()
    )
    return row is not None and row[0] >= _compaction_cutoff(tx, context)


def _compaction_cutoff(tx, context) -> int:
    """Translate a client counter into this session's persisted monotonic sequence."""

    def load():
        conn = tx._check()
        key = (_entry(context), context.session_id)
        row = conn.execute(
            "SELECT compacted_seq FROM session_brief_sessions WHERE entry_id=? AND session_id=?", key
        ).fetchone()
        current = row[0] if row else 0
        if context.compaction_seq is None:
            return current
        # A lost client counter starts a new generation at the committed watermark;
        # retaining older bases makes delayed requests idempotent without comparing clocks.
        epoch = conn.execute(
            "SELECT base_seq FROM session_brief_compaction_epochs WHERE entry_id=? AND session_id=? AND epoch=?",
            (*key, context.compaction_epoch),
        ).fetchone()
        cutoff = max(current, (epoch[0] if epoch else current) + context.compaction_seq)
        if cutoff >= 2**63:
            raise ContractError("IDENTITY_UNBOUND", "compaction_seq")
        return cutoff

    return tx.remembered(
        (
            "session_brief_compaction",
            _entry(context),
            context.session_id,
            context.compaction_seq,
            context.compaction_epoch,
        ),
        load,
    )


def _started(tx, context) -> bool:
    row = (
        tx._check()
        .execute(
            "SELECT started,started_compacted_seq FROM session_brief_sessions WHERE entry_id=? AND session_id=?",
            (_entry(context), context.session_id),
        )
        .fetchone()
    )
    return bool(row and row[0] and row[1] >= _compaction_cutoff(tx, context))


def _reset(tx, context) -> None:
    key = (_entry(context), context.session_id)
    tx._check(write=True).execute("DELETE FROM session_brief_injections WHERE entry_id=? AND session_id=?", key)
    tx._check(write=True).execute("UPDATE session_brief_sessions SET started=0 WHERE entry_id=? AND session_id=?", key)


def _apply_compaction(tx, context) -> None:
    """Persist the translated watermark and forget only older delivery marks."""
    if context.compaction_seq is None:
        return
    cutoff = _compaction_cutoff(tx, context)
    conn = tx._check(write=True)
    key = (_entry(context), context.session_id)
    previous = conn.execute(
        "SELECT base_seq,local_seq FROM session_brief_compaction_epochs WHERE entry_id=? AND session_id=? AND epoch=?",
        (*key, context.compaction_epoch),
    ).fetchone()
    if previous and context.compaction_seq <= previous[1]:
        return
    base = previous[0] if previous else cutoff - context.compaction_seq
    conn.execute(
        "INSERT INTO session_brief_compaction_epochs(entry_id,session_id,epoch,base_seq,local_seq) VALUES(?,?,?,?,?) "
        "ON CONFLICT(entry_id,session_id,epoch) DO UPDATE SET local_seq=excluded.local_seq",
        (*key, context.compaction_epoch, base, context.compaction_seq),
    )
    conn.execute(
        "DELETE FROM session_brief_injections WHERE entry_id=? AND session_id=? AND delivery_compacted_seq<?",
        (*key, cutoff),
    )
    conn.execute(
        "INSERT INTO session_brief_sessions(entry_id,session_id,started,compacted_at,compacted_seq,"
        "compaction_epoch,compaction_local_seq) VALUES(?,?,0,?,?,?,?) "
        "ON CONFLICT(entry_id,session_id) DO UPDATE SET "
        "started=CASE WHEN started_compacted_seq<excluded.compacted_seq THEN 0 ELSE started END, "
        "compacted_at=excluded.compacted_at,compacted_seq=excluded.compacted_seq,"
        "compaction_epoch=excluded.compaction_epoch,compaction_local_seq=excluded.compaction_local_seq",
        (*key, context.compacted_at, cutoff, context.compaction_epoch, context.compaction_seq),
    )


def sync_compaction(storage, context, *, remaining_seconds: float = 0.03) -> bool:
    """Persist a pending host watermark when the bounded writer lease is available."""
    if context.compaction_seq is None:
        return False
    try:
        with storage.write(context, remaining_seconds=remaining_seconds) as tx:
            _apply_compaction(tx, context)
    except (TruthWriterBusyError, sqlite3.OperationalError):
        return False
    return True


def compaction_watermark(storage, context) -> int:
    """Return the committed global compaction sequence for one bound session."""
    with storage.read(context, remaining_seconds=0.05) as tx:
        row = (
            tx._check()
            .execute(
                "SELECT compacted_seq FROM session_brief_sessions WHERE entry_id=? AND session_id=?",
                (_entry(context), context.session_id),
            )
            .fetchone()
        )
        return row[0] if row else 0


def reset_session(storage, context, *, remaining_seconds: float | None = None) -> None:
    """Clear delivery marks when the host signals that context is being compacted."""
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        _reset(tx, context)


def record_recall(storage, clock, context, *, tokens: int, elapsed_ms: int, remaining_seconds: float) -> None:
    """Accumulate the host's delivered question recall without storing its content."""
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        _apply_compaction(tx, context)
        mode, _marker = private.recall_state(tx, context, utc_timestamp(clock.utc_now()))
        if mode == "full":
            return
        tx._check(write=True).execute(
            "INSERT INTO session_brief_sessions(entry_id,session_id,started,recall_tokens,recall_elapsed_ms) "
            "VALUES(?,?,0,?,?) ON CONFLICT(entry_id,session_id) DO UPDATE SET "
            "recall_tokens=recall_tokens+excluded.recall_tokens, "
            "recall_elapsed_ms=recall_elapsed_ms+excluded.recall_elapsed_ms",
            (_entry(context), context.session_id, tokens, elapsed_ms),
        )


def session_stats(storage, context) -> dict:
    """Read separate briefing and question totals for the bound session."""
    with storage.read(context) as tx:
        row = (
            tx._check()
            .execute(
                "SELECT brief_tokens,brief_elapsed_ms,recall_tokens,recall_elapsed_ms "
                "FROM session_brief_sessions WHERE entry_id=? AND session_id=?",
                (_entry(context), context.session_id),
            )
            .fetchone()
        )
    brief_tokens, brief_ms, recall_tokens, recall_ms = tuple(row) if row else (0, 0, 0, 0)
    return {
        "brief_tokens": brief_tokens,
        "brief_elapsed_ms": brief_ms,
        "recall_tokens": recall_tokens,
        "recall_elapsed_ms": recall_ms,
        "total_tokens": brief_tokens + recall_tokens,
        "total_elapsed_ms": brief_ms + recall_ms,
    }


def _manifest(tx, context):
    """Read only the saved manifests, merging scope rankings without joining scopes."""
    result = []
    for scope in sorted(context.allowed_scope_ids):
        row = tx._check().execute("SELECT items_json FROM session_briefs WHERE scope_id=?", (scope,)).fetchone()
        if row:
            result.extend(json.loads(row[0]))
    return result


def _rank(item):
    rank = item["rank"]
    score = (5 if rank[1] else rank[0]) if rank else 0
    return score, item["time"], item["ref"]


def _safe_line(tx, search, item, intervals, hydration):
    """Hydrate the current object and check each evidence root before formatting it."""
    kind = "episode" if item["kind"] == "task" else "claim"
    if kind == "claim":
        saved = tx.claims.version(item["ref"], item["revision"])
        revision = tx.claims.current_revision(item["ref"])
        head = tx.claims.version(item["ref"], revision) if revision is not None else None
        if saved is None or head is None or head.state != "active":
            return None
        if head.revision != saved.revision:
            if statement_key(head.payload) != statement_key(saved.payload):
                return None
            # Rehydrate every current evidence root; an unchanged assertion may
            # acquire more evidence without invalidating its cached selection.
            item = {**item, "revision": head.revision}
    candidate = CandidateRef(kind, item["ref"], item["revision"], "background")
    obj = hydration.hydrate(tx, candidate, search)
    if obj is None or obj.temporal_status != "current" or not obj.evidence_refs:
        return None
    for ref in obj.evidence_refs:
        source = tx.source(*parse_source_ref(ref))
        if source is None or private.source_blocked(intervals, source):
            return None
    line = _line(tx, item, obj, search.now)
    if not line:
        return None
    from .secret_patterns import contains_secret_like_text

    if contains_secret_like_text(line):
        return None
    return candidate, obj, line


def _mark_delivered(tx, context, items, now: str, cutoff: int) -> None:
    pairs = [("claim", item["ref"], item["revision"]) for item in items if item["kind"] != "task"]
    tx._check(write=True).executemany(
        "INSERT INTO session_brief_injections(entry_id,session_id,object_kind,object_ref,revision,delivered_at,"
        "delivery_compacted_at,delivery_compacted_seq) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(entry_id,session_id,object_kind,object_ref,revision) DO UPDATE SET "
        "delivered_at=excluded.delivered_at,delivery_compacted_at=excluded.delivery_compacted_at,"
        "delivery_compacted_seq=excluded.delivery_compacted_seq",
        [(_entry(context), context.session_id, *pair, now, context.compacted_at, cutoff) for pair in pairs],
    )


def _render(tx, search, manifests, intervals, token_limit: int, deliver: bool):
    """Render at most the per-kind limits, never a partial line."""
    limits = {"task": 3, "decision": 5, "preference": 8}
    items, lines = [], []
    hydration = RetrievalHydration()
    for kind, limit in limits.items():
        selected = sorted((entry for entry in manifests if entry["kind"] == kind), key=_rank, reverse=True)
        for item in selected:
            if sum(found["kind"] == kind for found in items) >= limit:
                break
            found = _safe_line(tx, search, item, intervals, hydration)
            if found is None:
                continue
            candidate, obj, line = found
            if deliver and already_injected(tx, candidate.kind, candidate.ref, candidate.revision):
                continue
            if estimate_tokens("\n".join([_HEADING, *lines, line])) > token_limit:
                continue
            lines.append(line)
            items.append({"kind": kind, "ref": candidate.ref, "revision": candidate.revision})
    return items, lines


def _record_brief(tx, context, tokens: int, elapsed_ms: int, now: str, cutoff: int) -> None:
    tx._check(write=True).execute(
        "INSERT INTO session_brief_sessions(entry_id,session_id,started,brief_tokens,brief_elapsed_ms,started_at,"
        "started_compacted_at,started_compacted_seq) VALUES(?,?,1,?,?,?,?,?) "
        "ON CONFLICT(entry_id,session_id) DO UPDATE SET "
        "started=1,started_at=excluded.started_at,started_compacted_at=excluded.started_compacted_at, "
        "started_compacted_seq=excluded.started_compacted_seq, "
        "brief_tokens=brief_tokens+excluded.brief_tokens,brief_elapsed_ms=brief_elapsed_ms+excluded.brief_elapsed_ms",
        (_entry(context), context.session_id, tokens, elapsed_ms, now, context.compacted_at, cutoff),
    )


def remove_question_overlaps(brief: dict, question_text: str) -> None:
    """Omit fact lines already present in the question recall of the same response."""
    if "_lines" not in brief or not question_text:
        return
    kept = [
        (item, line)
        for item, line in zip(brief["items"], brief["_lines"], strict=True)
        if item["kind"] == "task" or item["ref"] not in question_text
    ]
    brief["items"] = [item for item, _line in kept]
    brief["_lines"] = [line for _item, line in kept]
    brief["text"] = "\n".join([_HEADING, *brief["_lines"]]) if kept else ""
    brief["tokens"] = estimate_tokens(brief["text"]) if kept else 0


def read(
    storage,
    clock,
    context,
    *,
    token_limit: int = 1024,
    remaining_seconds: float | None = None,
    deliver: bool = False,
    compact: bool = False,
    defer_confirmation: bool = False,
) -> dict:
    """Read the saved manifests and recheck each version in the session's current authority snapshot."""
    started = clock.monotonic()
    now = utc_timestamp(clock.utc_now())
    empty = {"text": "", "items": [], "tokens": 0, "elapsed_ms": 0}
    search = SearchContext("session briefing", "auto", None, (), SearchLimits(), started + 5, now, context)
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        entry = context_fence(tx, context)
        if entry_changed(tx, entry):
            return {**empty, "delivery_reason": "entry_state:paused"}
        mode, marker = private.recall_state(tx, context, now)
        if compact and context.compaction_seq is not None:
            row = (
                tx._check()
                .execute(
                    "SELECT compacted_seq FROM session_brief_sessions WHERE entry_id=? AND session_id=?",
                    (_entry(context), context.session_id),
                )
                .fetchone()
            )
            compact = _compaction_cutoff(tx, context) > (row[0] if row else 0)
        if (mode == "full" and not compact) or (deliver and not compact and _started(tx, context)):
            return empty
        items, lines = (
            ([], [])
            if mode == "full"
            else _render(
                tx,
                search,
                _manifest(tx, context),
                private.active_intervals(tx, now),
                token_limit,
                deliver and not compact,
            )
        )
        epoch = tx.memory_epoch()
        compaction = _compaction_cutoff(tx, context)
        text = "\n".join([_HEADING, *lines]) if lines else ""
        tokens = estimate_tokens(text) if text else 0
    result = {
        "text": text,
        "items": items,
        "tokens": tokens,
        "elapsed_ms": max(0, round((clock.monotonic() - started) * 1000)),
        "_epoch": epoch,
        "_entry_fence": entry,
        "_privacy_marker": marker,
        "_log_allowed": mode is None,
        "_log_checked": False,
        "_compaction_cutoff": compaction,
        "_lines": lines,
        "_compact": compact,
        "_deliver": deliver and mode != "full",
    }
    if not defer_confirmation and (deliver or compact):
        result["delivery_reason"] = confirm(
            storage, clock, context, result, entry_fence=entry, remaining_seconds=remaining_seconds
        )
        if result["delivery_reason"] in {"brief_invalidated", "brief_safety_unconfirmed", "entry_state:paused"}:
            result.update(empty)
    return result


def _still_valid(tx, clock, context, brief) -> bool:
    """Recheck selected lines if truth or privacy changed after preparation."""
    now = utc_timestamp(clock.utc_now())
    mode, marker = private.recall_state(tx, context, now, after=brief["_privacy_marker"])
    if mode is not None or (
        marker != brief["_privacy_marker"]
        and private.rules_changed(tx, context, after=brief["_privacy_marker"], modes=private.MODES)[0]
    ):
        brief["_log_allowed"] = False
    brief["_log_checked"] = True
    cutoff = _compaction_cutoff(tx, context)
    if cutoff > brief["_compaction_cutoff"]:
        return False
    if (mode == "full" and brief["text"]) or (brief["_deliver"] and not brief["_compact"] and _started(tx, context)):
        return False
    if tx.memory_epoch() == brief["_epoch"] and marker == brief["_privacy_marker"]:
        return True
    search = SearchContext("session briefing", "auto", None, (), SearchLimits(), clock.monotonic() + 5, now, context)
    manifest = {(item["kind"], item["ref"]): item for item in _manifest(tx, context)}
    intervals, hydration = private.active_intervals(tx, now), RetrievalHydration()
    for item, line in zip(brief["items"], brief["_lines"], strict=True):
        saved = manifest.get((item["kind"], item["ref"]))
        found = _safe_line(tx, search, saved, intervals, hydration) if saved else None
        if found is None or found[2] != line:
            return False
    return True


def _entry_invalid(tx, context, fence):
    """Reject a briefing prepared for a changed or different entry."""
    return set(fence) != set(context_fence(tx, context)) or entry_changed(tx, fence)


def _release_refusal(tx, clock, context, brief, fence):
    """Name the first failed entry, retraction or briefing authority fence."""
    from .retractions import retraction_after

    if _entry_invalid(tx, context, fence):
        return "entry_state:paused"
    if retraction_after(tx._check(), context.allowed_scope_ids, brief["_epoch"]) or not _still_valid(
        tx, clock, context, brief
    ):
        return "brief_invalidated"
    return None


def confirm(storage, clock, context, brief: dict, *, entry_fence, remaining_seconds: float | None = None) -> str:
    """Recheck release safety before attempting bounded delivery accounting."""
    if remaining_seconds is not None and remaining_seconds <= 0:
        return "brief_safety_unconfirmed"
    started = clock.monotonic()
    try:
        with storage.read(context, remaining_seconds=remaining_seconds) as tx:
            if refusal := _release_refusal(tx, clock, context, brief, entry_fence):
                return refusal
    except Exception:  # noqa: BLE001 - an unfinished safety check must not release prepared text.
        return "brief_safety_unconfirmed"
    left = None if remaining_seconds is None else remaining_seconds - (clock.monotonic() - started)
    if left is not None and left <= 0:
        return "brief_safety_unconfirmed"
    wait = min(0.03, left) if left is not None else 0.03
    entered = validated = False
    try:
        with storage.write(context, remaining_seconds=wait) as tx:
            entered = True
            if refusal := _release_refusal(tx, clock, context, brief, entry_fence):
                return refusal
            validated = True
            if brief["_compact"] and context.compaction_seq is None:
                _reset(tx, context)
            _apply_compaction(tx, context)
            cutoff = _compaction_cutoff(tx, context)
            brief["_confirmed_seq"] = cutoff
            if brief["_deliver"]:
                now = utc_timestamp(clock.utc_now())
                _mark_delivered(tx, context, brief["items"], now, cutoff)
                _record_brief(tx, context, brief["tokens"], brief["elapsed_ms"], now, cutoff)
    except (TruthWriterBusyError, sqlite3.OperationalError):
        return "brief_safety_unconfirmed" if entered and not validated else "brief_delivery_unconfirmed"
    except Exception:  # noqa: BLE001 - a failed write-time recheck cannot release prepared text.
        return "brief_safety_unconfirmed"
    return "brief_delivered"
