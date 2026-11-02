"""Final host-text budgets, delivery accounting and session correction notices.

Preparation reads authority only. Confirmation rechecks the chosen text before
one bounded transaction records references, notice acknowledgements and usage.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from ..contracts import ContractError, utc_timestamp
from . import lineage, private
from . import session_brief as briefing
from .claims import statement_key
from .entry_control import context_fence
from .recall_budget import canonical_render_json, estimate_tokens
from .retrieval import SearchContext, SearchLimits
from .review_policy import PENDING, PENDING_OBSERVATION
from .secret_patterns import contains_secret_like_text
from .visibility import check_claim_release
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
            (briefing.session_entry(context), context.session_id),
        )
        .fetchone()
    )
    return row[0] if row and row[1] >= briefing.compaction_cutoff(tx, context) else 0


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


def _current(tx, search, kind, ref, revision, batch=None):
    obj = briefing.visible_object(tx, search, kind, ref, revision, batch=batch)
    return None if obj is not None and dict(obj.metadata).get("replaced_by") else obj


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


def _markers(row):
    value = row["notice_marker"]
    if not value:
        return {}
    return json.loads(value)


def _make_notice(tx, row, key, replacement):
    kind, ref, revision = row["object_kind"], row["object_ref"], row["revision"]
    value = None
    if replacement is not None and replacement.temporal_status == "current":
        if replacement.kind in {"claim", "procedure"}:
            claim = tx.claims.version(replacement.ref, replacement.revision)
            if claim is not None:
                try:
                    check_claim_release(tx, claim, history=False, owner_review=False)
                except ContractError:
                    return None
            value = claim.payload["value_text"] if claim and claim.state == "active" else None
        else:
            value = replacement.content
    if value and contains_secret_like_text(value):
        value = None
    # A new evidence revision must not replay an acknowledged assertion change.
    identity = (
        [key.rsplit("@", 1)[0], replacement.ref, statement_key(claim.payload)]
        if value and replacement and replacement.kind in {"claim", "procedure"} and claim
        else [replacement.ref, replacement.revision, value]
        if value and replacement
        else [kind, ref, "withdrawn"]
        if key == "object" or kind == "claim"
        else [kind, ref, key, "withdrawn"]
    )
    marker = hashlib.sha256(canonical_render_json(identity).encode()).hexdigest()
    saved = _markers(row)
    if marker == saved.get(key):
        return None
    text = f"{ref}@{revision}: " + (f"corrected; current value: {value}" if value else "withdrawn; do not rely on it.")
    return {
        "kind": kind,
        "ref": ref,
        "revision": revision,
        "marker": marker,
        "corrected": bool(value),
        "text": text,
        "targets": [(kind, ref, revision, key)],
    }


def _claim_change(tx, search, ref, revision, batch):
    """Resolve reviewed replacements; evidence alone does not change an assertion."""
    # A held correction has no publishable value or withdrawal notice. Once
    # decided, the ordinary change detector compares the effective assertion.
    if any(v.reason in {PENDING, PENDING_OBSERVATION} for v in tx.claims.versions(ref)):
        return False, None
    head = _head(tx, "claim", ref)
    version = tx.claims.version(ref, head) if head else None
    target = (
        lineage.resolve_replacement(tx, ref, search.now, rules=batch["intervals"])
        if version and version.state == "superseded" and version.reason == "health_superseded"
        else None
    )
    replacement = (
        _current(tx, search, "claim", target.ref, target.revision, batch)
        if target
        else (_current(tx, search, "claim", ref, head, batch) if head else None)
    )
    if replacement is None:
        return True, None
    saved = tx.claims.version(ref, revision)
    current = tx.claims.version(replacement.ref, replacement.revision)
    changed = replacement.ref != ref or saved is None or statement_key(saved.payload) != statement_key(current.payload)
    return changed, replacement


def _event_changes(tx, search, row, batch, keys=None):
    ref, revision = row["object_ref"], row["revision"]
    supported = lineage.supported_claims(tx._check(), ((ref, revision),)).get((ref, revision), ())
    # One original may support several independent facts. No first replacement
    # can stand in for the others, even when the original remains current.
    baselines = {}
    for claim_ref, baseline in supported:
        baselines[claim_ref] = max(baseline, baselines.get(claim_ref, 0))
    for claim_ref, baseline in sorted(baselines.items()):
        key = f"claim:{claim_ref}@{baseline}"
        if keys is not None and key not in keys:
            continue
        changed, replacement = _claim_change(tx, search, claim_ref, baseline, batch)
        if changed:
            yield key, replacement
    if keys is not None and "object" not in keys:
        return
    head = (
        tx._check()
        .execute(
            "SELECT max(source_revision) FROM source_events WHERE source_group_key="
            "(SELECT source_group_key FROM source_events WHERE event_id=? AND source_revision=?)",
            (ref, revision),
        )
        .fetchone()[0]
    )
    if head and head != revision:
        yield "object", _current(tx, search, "event", ref, head, batch)


def _row_notices(tx, search, row, batch, reveal, keys=None):
    kind, ref, revision = row["object_kind"], row["object_ref"], row["revision"]
    original = briefing.visible_object(tx, search, kind, ref, revision, history=True, batch=batch)
    if not reveal or original is None:
        changes = [("object", None)]
    elif kind == "event":
        changes = _event_changes(tx, search, row, batch, keys)
    elif kind == "claim":
        changed, replacement = _claim_change(tx, search, ref, revision, batch)
        if not changed:
            return
        changes = [(f"claim:{ref}@{revision}", replacement)]
    else:
        current = _current(tx, search, kind, ref, revision, batch)
        if current is not None and current.temporal_status == "current":
            return
        head = _head(tx, kind, ref)
        changes = [("object", _current(tx, search, kind, ref, head, batch) if head else None)]
    for key, replacement in changes:
        notice = _make_notice(tx, row, key, replacement)
        if notice is not None:
            yield notice


def _delivery_rows(tx, context):
    return (
        tx._check()
        .execute(
            "SELECT i.* FROM session_brief_injections i WHERE entry_id=? AND session_id=? "
            "AND delivery_compacted_seq>=? AND revision=(SELECT max(j.revision) "
            "FROM session_brief_injections j WHERE j.entry_id=i.entry_id AND j.session_id=i.session_id "
            "AND j.object_kind=i.object_kind AND j.object_ref=i.object_ref) ORDER BY delivered_at,object_ref",
            (briefing.session_entry(context), context.session_id, briefing.compaction_cutoff(tx, context)),
        )
        .fetchall()
    )


def _select_notices(candidates, limit):
    selected = []
    for notice in candidates:
        duplicate = next((saved for saved in selected if saved["marker"] == notice["marker"]), None)
        if duplicate is not None:
            duplicate["targets"].extend(notice["targets"])
            continue
        if len(selected) == NOTICE_ITEM_CAP:
            continue
        prefix = f"{notice['ref']}@{notice['revision']}: "
        while _tokens("\n".join(["Memory updates:", *(n["text"] for n in selected), notice["text"]])) > limit:
            if len(notice["text"]) <= len(prefix) + 48:
                break
            notice["text"] = notice["text"][:-17] + "\u2026"
        if _tokens("\n".join(["Memory updates:", *(n["text"] for n in selected), notice["text"]])) <= limit:
            selected.append(notice)
    return selected


def _notices(tx, search, limit, *, batch=None, reveal=None):
    batch = briefing.visibility_batch(tx, search) if batch is None else batch
    if reveal is None:
        reveal = private.recall_state(tx, search.trusted_context, search.now)[0] != "full"
    rows = _delivery_rows(tx, search.trusted_context)
    candidates = (notice for row in rows for notice in _row_notices(tx, search, row, batch, reveal))
    return _select_notices(candidates, limit)


def _notices_valid(tx, search, brief, batch, reveal):
    injection = brief["_injection"]
    wanted = {}
    for notice in injection["notices"]:
        for kind, ref, revision, key in notice["targets"]:
            wanted.setdefault((kind, ref, revision), set()).add(key)
    loaded = {}
    for notice in injection["notices"]:
        for kind, ref, revision, key in notice["targets"]:
            pair = kind, ref, revision
            if pair not in loaded:
                row = (
                    tx._check()
                    .execute(
                        "SELECT * FROM session_brief_injections WHERE entry_id=? AND session_id=? "
                        "AND object_kind=? AND object_ref=? AND revision=?",
                        (briefing.session_entry(search.trusted_context), search.trusted_context.session_id, *pair),
                    )
                    .fetchone()
                )
                candidates = [] if row is None else list(_row_notices(tx, search, row, batch, reveal, wanted[pair]))
                loaded[pair] = row, candidates
            row, candidates = loaded[pair]
            if row is None or notice["marker"] == _markers(row).get(key):
                return False
            if not any(n["marker"] == notice["marker"] and (*pair, key) in n["targets"] for n in candidates):
                return False
    return True


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


def _item_live(tx, search, item, batch=None):
    obj = _current(tx, search, item["kind"], item["ref"], item["revision"], batch)
    return obj is not None and obj.temporal_status == item["temporal_status"]


def _fit(text, brief, limit, tx, search, batch):
    line, payload = _question(text)
    items = payload["items"] if payload else []
    kept = [item for item in items if _item_live(tx, search, item, batch)]
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


def prepare(core, context, text, *, brief=None, entry_fence=None, remaining_seconds=0.2):
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
        brief["_entry_fence"] = context_fence(tx, context) if entry_fence is None else entry_fence
        paused = briefing.entry_invalid(tx, context, brief["_entry_fence"])
        mode, marker = (None, 0) if paused else private.recall_state(tx, context, search.now)
        brief.setdefault("_epoch", tx.memory_epoch())
        brief.setdefault("_privacy_marker", marker)
        brief.setdefault("_compaction_cutoff", briefing.compaction_cutoff(tx, context))
        brief.setdefault("_deliver", False)
        brief.setdefault("_compact", False)
        brief.setdefault("_lines", [])
        brief.setdefault("_log_allowed", mode is None)
        batch = None if paused else briefing.visibility_batch(tx, search)
        # Compaction discards the old context and its notice targets together.
        notices = (
            []
            if paused or brief["_compact"]
            else _notices(
                tx, search, min(NOTICE_TOKEN_CAP, core.config.injection_token_cap), batch=batch, reveal=mode != "full"
            )
        )
        notice_text = "\n".join(["Memory updates:", *(n["text"] for n in notices)]) if notices else ""
        left = None if core.config.session_token_cap is None else max(0, core.config.session_token_cap - used)
        limit = core.config.injection_token_cap - _tokens(notice_text) - (1 if notice_text else 0)
        exhausted = left is not None and left <= 0
        if left is not None:
            limit = min(limit, left)
        if paused:
            text = ""
            brief.update(text="", items=[], tokens=0, _lines=[], _deliver=False)
        elif mode == "full":
            brief["_discarded"] = bool(brief["text"] or _question(text)[1])
            text = private.notice_for_mode("full") or ""
            brief.update(text="", items=[], tokens=0, _lines=[], _deliver=False)
        ordinary, items, reason = _fit(text, brief, max(0, limit), tx, search, batch)
        output = "\n\n".join(part for part in (notice_text, ordinary) if part)
        brief["_injection"] = {
            "text": output,
            "items": items,
            "notices": notices,
            "tokens": _tokens(output),
            "session_tokens": used,
            "injection_token_cap": core.config.injection_token_cap,
            "session_token_cap": core.config.session_token_cap,
            "reason": "entry_state:paused" if paused else "session_injection_budget_exhausted" if exhausted else reason,
            "ordinary_tokens": _tokens(ordinary),
            "session_before": used,
        }
    return brief


def _valid(tx, clock, context, brief, *, deadline=None):
    if not briefing.validate_prepared(tx, clock, context, brief, deadline=deadline):
        return False
    injection = brief["_injection"]
    search = _search(clock, context)
    mode, _marker = private.recall_state(tx, context, search.now, after=brief["_privacy_marker"])
    if mode == "full":
        notices = (
            "\n".join(["Memory updates:", *(n["text"] for n in injection["notices"])]) if injection["notices"] else ""
        )
        allowed = "\n\n".join(part for part in (notices, private.notice_for_mode("full")) if part)
        if any(n["corrected"] for n in injection["notices"]) or injection["text"] not in ("", notices, allowed):
            return False
    # A concurrent delivery may consume the remaining session budget. It must
    # invalidate this response rather than charge two packets against one slot.
    cap = injection["session_token_cap"]
    if cap is not None and injection["ordinary_tokens"] > max(0, cap - _usage(tx, context)):
        return False
    batch = briefing.visibility_batch(tx, search)
    for item in injection["items"]:
        if not _item_live(tx, search, item, batch):
            return False
    return _notices_valid(tx, search, brief, batch, mode != "full")


def _record(tx, clock, context, brief):
    injection = brief["_injection"]
    now = utc_timestamp(clock.utc_now())
    if brief["_compact"] and context.compaction_seq is None:
        briefing.clear_session(tx, context)
    briefing.apply_compaction(tx, context)
    cutoff = briefing.compaction_cutoff(tx, context)
    brief["_confirmed_seq"] = cutoff
    if brief["_deliver"]:
        briefing.mark_delivered(tx, context, brief["items"], now, cutoff)
        briefing.record_brief_usage(tx, context, brief["tokens"], brief["elapsed_ms"], now, cutoff)
    briefing.mark_delivered(tx, context, injection["items"], now, cutoff, "question")
    acknowledgements = {}
    for notice in injection["notices"]:
        for kind, ref, revision, key in notice["targets"]:
            pair = kind, ref, revision
            if pair not in acknowledgements:
                row = (
                    tx._check()
                    .execute(
                        "SELECT notice_marker FROM session_brief_injections WHERE entry_id=? AND session_id=? "
                        "AND object_kind=? AND object_ref=? AND revision=?",
                        (briefing.session_entry(context), context.session_id, *pair),
                    )
                    .fetchone()
                )
                acknowledgements[pair] = _markers(row)
            acknowledgements[pair][key] = notice["marker"]
    tx._check(write=True).executemany(
        "UPDATE session_brief_injections SET notice_marker=? WHERE entry_id=? AND session_id=? "
        "AND object_kind=? AND object_ref=? AND revision=?",
        [
            (canonical_render_json(markers), briefing.session_entry(context), context.session_id, *pair)
            for pair, markers in acknowledgements.items()
        ],
    )
    tokens = injection["tokens"] - (brief["tokens"] if brief["_deliver"] else 0)
    tx._check(write=True).execute(
        "INSERT INTO session_brief_sessions(entry_id,session_id,started,recall_tokens,recall_elapsed_ms) "
        "VALUES(?,?,0,?,?) "
        "ON CONFLICT(entry_id,session_id) DO UPDATE SET recall_tokens=recall_tokens+excluded.recall_tokens, "
        "recall_elapsed_ms=recall_elapsed_ms+excluded.recall_elapsed_ms",
        (briefing.session_entry(context), context.session_id, max(0, tokens), injection.get("elapsed_ms", 0)),
    )
    injection["session_tokens"] = _usage(tx, context)


def _release_refusal(tx, clock, context, brief, entry_fence, *, deadline=None):
    """Apply entry authority before content and accounting checks in each snapshot."""
    if briefing.entry_invalid(tx, context, entry_fence):
        return "entry_state:paused"
    return None if _valid(tx, clock, context, brief, deadline=deadline) else "brief_invalidated"


def confirm(storage, clock, context, brief, *, entry_fence, remaining_seconds=None):
    """Fence release first; a busy writer returns safe text without delivery marks."""
    if remaining_seconds is not None and remaining_seconds <= 0:
        return "brief_safety_unconfirmed"
    started = clock.monotonic()
    deadline = started + (5 if remaining_seconds is None else remaining_seconds)
    try:
        with storage.read(context, remaining_seconds=remaining_seconds) as tx:
            if refusal := _release_refusal(tx, clock, context, brief, entry_fence, deadline=deadline):
                return refusal
    except Exception:  # noqa: BLE001 - unfinished safety checks cannot release content.
        return "brief_safety_unconfirmed"
    left = 0.03 if remaining_seconds is None else min(0.03, remaining_seconds - (clock.monotonic() - started))
    if left <= 0:
        return "brief_safety_unconfirmed"
    entered = validated = False
    try:
        with storage.write(context, remaining_seconds=max(0, left)) as tx:
            entered = True
            if refusal := _release_refusal(tx, clock, context, brief, entry_fence, deadline=deadline):
                return refusal
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
    return "injection_delivered" if brief.get("_discarded") else "brief_delivered"
