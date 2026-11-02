"""Final host-text budgets, delivery accounting and session correction notices.

Preparation reads authority only. Confirmation rechecks the chosen text before
one bounded transaction records references, notice acknowledgements and usage.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from ..contracts import utc_timestamp
from . import lineage, private
from . import session_brief as briefing
from .claim_storage import parse_source_ref
from .recall_budget import canonical_render_json, estimate_tokens
from .retrieval import CandidateRef, SearchContext, SearchLimits
from .retrieval_hydration import RetrievalHydration
from .secret_patterns import contains_secret_like_text
from .writer_lease import TruthWriterBusyError

NOTICE_TOKEN_CAP = 300
NOTICE_ITEM_CAP = 3


def _tokens(text):
    return estimate_tokens(text) if text else 0


def _usage(tx, context):
    row = (
        tx._check()
        .execute(
            "SELECT brief_tokens+recall_tokens,compacted_seq FROM session_brief_sessions "
            "WHERE entry_id=? AND session_id=?",
            (briefing._entry(context), context.session_id),
        )
        .fetchone()
    )
    return row[0] if row and row[1] >= briefing._compaction_cutoff(tx, context) else 0


def _search(clock, context):
    return SearchContext(
        "session delivery",
        "auto",
        None,
        (),
        SearchLimits(),
        clock.monotonic() + 5,
        utc_timestamp(clock.utc_now()),
        context,
    )


def _current(tx, search, kind, ref, revision):
    kind = "claim" if kind == "procedure" else kind
    obj = RetrievalHydration().hydrate(tx, CandidateRef(kind, ref, revision, "background"), search)
    if obj is None or dict(obj.metadata).get("replaced_by"):
        return None
    intervals = private.active_intervals(tx, search.now)
    for cited in obj.evidence_refs:
        source = tx.source(*parse_source_ref(cited))
        if source is None or private.source_blocked(intervals, source):
            return None
    return obj


def _head(tx, kind, ref):
    kind = "claim" if kind == "procedure" else kind
    table, column = {
        "claim": ("claims", "claim_id"),
        "episode": ("episodes", "episode_id"),
        "artifact": ("artifacts", "artifact_id"),
        "reference": ("reference_bindings", "reference_id"),
    }[kind]
    row = tx._check().execute(f"SELECT current_revision FROM {table} WHERE {column}=?", (ref,)).fetchone()
    return row[0] if row else None


def _replacement(tx, search, row):
    kind, ref, revision = row["object_kind"], row["object_ref"], row["revision"]
    if kind == "event":
        # A delivered original can state a corrected fact even when its own
        # immutable source revision is unchanged. Follow the shared lineage.
        supported = lineage.supported_claims(tx._check(), ((ref, revision),)).get((ref, revision), ())
        for claim_ref, claim_revision in supported:
            head = _head(tx, "claim", claim_ref)
            if head and head != claim_revision:
                return _current(tx, search, "claim", claim_ref, head)
        source = tx.source(ref, revision)
        if source is None:
            return None
        head = (
            tx._check()
            .execute(
                "SELECT max(source_revision) FROM source_events WHERE source_group_key="
                "(SELECT source_group_key FROM source_events WHERE event_id=? AND source_revision=?)",
                (ref, revision),
            )
            .fetchone()[0]
        )
    else:
        head = _head(tx, kind, ref)
    return _current(tx, search, kind, ref, head) if head else None


def _notice(tx, search, row, reveal):
    kind, ref, revision = row["object_kind"], row["object_ref"], row["revision"]
    current = _current(tx, search, kind, ref, revision)
    if reveal and current is not None and (kind != "event" or current.temporal_status == "current"):
        return None
    replacement = _replacement(tx, search, row) if reveal else None
    # Only the current value is eligible for release. A blocked dependency,
    # privacy rule or tombstone produces a reference-only withdrawal.
    value = None
    if replacement is not None and replacement.temporal_status == "current":
        if replacement.kind in {"claim", "procedure"}:
            claim = tx.claims.version(replacement.ref, replacement.revision)
            value = claim.payload["value_text"] if claim and claim.state == "active" else None
        else:
            value = replacement.content
    if value and contains_secret_like_text(value):
        value = None
    marker = hashlib.sha256(
        canonical_render_json(
            [replacement.ref, replacement.revision, value]
            if value and replacement is not None
            else [kind, ref, "withdrawn"]
        ).encode()
    ).hexdigest()
    if marker == row["notice_marker"]:
        return None
    prefix = f"{ref}@{revision}: "
    text = prefix + (f"corrected; current value: {value}" if value else "withdrawn; do not rely on it.")
    return {
        "kind": kind,
        "ref": ref,
        "revision": revision,
        "marker": marker,
        "corrected": bool(value),
        "text": text,
        "targets": [(kind, ref, revision)],
    }


def _notices(tx, search, limit):
    rows = (
        tx._check()
        .execute(
            "SELECT i.* FROM session_brief_injections i WHERE entry_id=? AND session_id=? "
            "AND delivery_compacted_seq>=? AND revision=(SELECT max(j.revision) "
            "FROM session_brief_injections j WHERE j.entry_id=i.entry_id AND j.session_id=i.session_id "
            "AND j.object_kind=i.object_kind AND j.object_ref=i.object_ref) ORDER BY delivered_at,object_ref",
            (
                briefing._entry(search.trusted_context),
                search.trusted_context.session_id,
                briefing._compaction_cutoff(tx, search.trusted_context),
            ),
        )
        .fetchall()
    )
    selected = []
    reveal = private.recall_state(tx, search.trusted_context, search.now)[0] != "full"
    for row in rows:
        notice = _notice(tx, search, row, reveal)
        if notice is None:
            continue
        duplicate = next((saved for saved in selected if saved["marker"] == notice["marker"]), None)
        if duplicate is not None:
            duplicate["targets"].extend(notice["targets"])
            continue
        if len(selected) == NOTICE_ITEM_CAP:
            continue
        # Truncate only new values, retaining the complete old reference. The
        # notice still directs a reader to the current object via that ref.
        prefix = f"{notice['ref']}@{notice['revision']}: "
        while _tokens("\n".join(["Memory updates:", *(n["text"] for n in selected), notice["text"]])) > limit:
            if len(notice["text"]) <= len(prefix) + 48:
                break
            notice["text"] = notice["text"][:-17] + "…"
        if _tokens("\n".join(["Memory updates:", *(n["text"] for n in selected), notice["text"]])) <= limit:
            selected.append(notice)
    return selected


def _question(text):
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("schema") == "scope-recall.recall_context/1.1":
            return line, payload
    return None, None


def _refresh_brief(brief):
    brief["text"] = "\n".join(["Session background:", *brief["_lines"]]) if brief.get("_lines") else ""
    brief["tokens"] = _tokens(brief["text"])


def _item_live(tx, search, item):
    obj = _current(tx, search, item["kind"], item["ref"], item["revision"])
    return obj is not None and obj.temporal_status == item["temporal_status"]


def _fit(text, brief, limit, tx, search):
    line, payload = _question(text)
    items = payload["items"] if payload else []
    kept = [item for item in items if _item_live(tx, search, item)]
    original = list(items)
    reason = None
    if kept != items:
        reason = "injection_invalidated"
    base = text.replace(brief["text"], "", 1).strip() if brief["text"] else text

    def render():
        question = base
        if payload and kept != original:
            changed = dict(
                payload,
                items=kept,
                gaps=[*payload["gaps"], reason or "injection_token_cap"],
                coverage="unknown",
                answerability="unknown",
            )
            question = base.replace(line, canonical_render_json(changed)) if kept else ""
        return "\n\n".join(part for part in (brief["text"], question) if part)

    rendered = render()
    while _tokens(rendered) > limit:
        reason = "injection_token_cap"
        if kept:
            kept.pop()
        elif brief.get("_lines"):
            brief["_lines"].pop()
            brief["items"].pop()
            _refresh_brief(brief)
        else:
            return "", [], reason
        rendered = render()
    return rendered, kept, reason


def prepare(core, context, text, *, brief=None, remaining_seconds=0.2):
    """Read and fit complete host text, retaining only references actually included."""
    if brief is None or "_epoch" not in brief:
        generation = brief.get("_generation") if brief else None
        brief = briefing.read(
            core.storage,
            core.clock,
            context,
            token_limit=0,
            deliver=False,
            remaining_seconds=remaining_seconds,
            defer_confirmation=True,
        )
        if generation is not None:
            brief["_generation"] = generation
    with core.storage.read(context, remaining_seconds=remaining_seconds) as tx:
        search = _search(core.clock, context)
        used = _usage(tx, context)
        mode, marker = private.recall_state(tx, context, search.now)
        brief.setdefault("_epoch", tx.memory_epoch())
        brief.setdefault("_privacy_marker", marker)
        brief.setdefault("_compaction_cutoff", briefing._compaction_cutoff(tx, context))
        brief.setdefault("_deliver", False)
        brief.setdefault("_compact", False)
        brief.setdefault("_lines", [])
        brief.setdefault("_log_allowed", mode is None)
        notices = _notices(tx, search, min(NOTICE_TOKEN_CAP, core.config.injection_token_cap))
        notice_text = "\n".join(["Memory updates:", *(n["text"] for n in notices)]) if notices else ""
        left = None if core.config.session_token_cap is None else max(0, core.config.session_token_cap - used)
        limit = core.config.injection_token_cap - _tokens(notice_text) - (1 if notice_text else 0)
        exhausted = left is not None and left <= 0
        if left is not None:
            limit = min(limit, left)
        if mode == "full":
            text = "" if _question(text)[1] else text
            brief.update(text="", items=[], tokens=0, _lines=[])
        ordinary, items, reason = _fit(text, brief, max(0, limit), tx, search)
        output = "\n\n".join(part for part in (notice_text, ordinary) if part)
        brief["_injection"] = {
            "text": output,
            "items": items,
            "notices": notices,
            "tokens": _tokens(output),
            "session_tokens": used,
            "injection_token_cap": core.config.injection_token_cap,
            "session_token_cap": core.config.session_token_cap,
            "reason": "session_injection_budget_exhausted" if exhausted else reason,
            "ordinary_tokens": _tokens(ordinary),
            "session_before": used,
        }
    return brief


def _valid(tx, clock, context, brief):
    if not briefing._still_valid(tx, clock, context, brief):
        return False
    injection = brief["_injection"]
    search = _search(clock, context)
    mode, _marker = private.recall_state(tx, context, search.now, after=brief["_privacy_marker"])
    if mode == "full" and (injection["items"] or brief["text"] or any(n["corrected"] for n in injection["notices"])):
        return False
    # A concurrent delivery may consume the remaining session budget. It must
    # invalidate this response rather than charge two packets against one slot.
    cap = injection["session_token_cap"]
    if cap is not None and injection["ordinary_tokens"] > max(0, cap - _usage(tx, context)):
        return False
    for item in injection["items"]:
        if not _item_live(tx, search, item):
            return False
    current = _notices(tx, search, min(NOTICE_TOKEN_CAP, injection["injection_token_cap"]))
    return all(notice in current for notice in injection["notices"])


def _record(tx, clock, context, brief):
    injection = brief["_injection"]
    now = utc_timestamp(clock.utc_now())
    if brief["_compact"] and context.compaction_seq is None:
        briefing._reset(tx, context)
    briefing._apply_compaction(tx, context)
    cutoff = briefing._compaction_cutoff(tx, context)
    brief["_confirmed_seq"] = cutoff
    if brief["_deliver"]:
        briefing._mark_delivered(tx, context, brief["items"], now, cutoff)
        briefing._record_brief(tx, context, brief["tokens"], brief["elapsed_ms"], now, cutoff)
    briefing._mark_delivered(tx, context, injection["items"], now, cutoff, "question")
    for notice in injection["notices"]:
        tx._check(write=True).executemany(
            "UPDATE session_brief_injections SET notice_marker=? WHERE entry_id=? AND session_id=? "
            "AND object_kind=? AND object_ref=? AND revision<=?",
            [(notice["marker"], briefing._entry(context), context.session_id, *target) for target in notice["targets"]],
        )
    tokens = injection["tokens"] - (brief["tokens"] if brief["_deliver"] else 0)
    tx._check(write=True).execute(
        "INSERT INTO session_brief_sessions(entry_id,session_id,started,recall_tokens,recall_elapsed_ms) "
        "VALUES(?,?,0,?,?) "
        "ON CONFLICT(entry_id,session_id) DO UPDATE SET recall_tokens=recall_tokens+excluded.recall_tokens, "
        "recall_elapsed_ms=recall_elapsed_ms+excluded.recall_elapsed_ms",
        (briefing._entry(context), context.session_id, max(0, tokens), injection.get("elapsed_ms", 0)),
    )
    injection["session_tokens"] = _usage(tx, context)


def confirm(storage, clock, context, brief, *, remaining_seconds=None):
    """Fence release first; a busy writer returns safe text without delivery marks."""
    if remaining_seconds is not None and remaining_seconds <= 0:
        return "brief_safety_unconfirmed"
    started = clock.monotonic()
    try:
        with storage.read(context, remaining_seconds=remaining_seconds) as tx:
            if not _valid(tx, clock, context, brief):
                return "brief_invalidated"
    except Exception:  # noqa: BLE001 - unfinished safety checks cannot release content.
        return "brief_safety_unconfirmed"
    left = 0.03 if remaining_seconds is None else min(0.03, remaining_seconds - (clock.monotonic() - started))
    if left <= 0:
        return "brief_safety_unconfirmed"
    entered = validated = False
    try:
        with storage.write(context, remaining_seconds=max(0, left)) as tx:
            entered = True
            if not _valid(tx, clock, context, brief):
                return "brief_invalidated"
            validated = True
            _record(tx, clock, context, brief)
    except (TruthWriterBusyError, sqlite3.OperationalError):
        return (
            "brief_safety_unconfirmed"
            if entered and not validated
            else "brief_delivery_unconfirmed"
            if brief["_deliver"]
            else "injection_delivery_unconfirmed"
        )
    except Exception:  # noqa: BLE001 - no partial accounting or unsafe release on storage failure.
        return "brief_safety_unconfirmed"
    return "brief_delivered"
