"""Bounded, sanitized host ingress in the authoritative backup/restore domain.

Only an authenticated adapter may create an ingress row. Replay retains its
original actor and occurrence and narrows authority against the current host.
The inbox removal and all source effects commit in the same transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import re
import time

from ..contracts import (
    ArtifactVersion,
    ContractError,
    DisplaySnapshot,
    TrustedContext,
    TrustedSourcePrincipal,
)
from .._version import __version__
from .truth_connection import TruthDatabaseConnectionError
from .writer_lease import TruthWriterBusyError
from .capture import CaptureReceipt, record_event
from .events import PreparedCapture, prepare_capture, segment_key

_TRANSIENT = (sqlite3.Error, TruthDatabaseConnectionError, TruthWriterBusyError)
#: What a release before 3.4.0rc10 wrote for every missing source, which names none.  One of its causes is gone: a
#: task whose episode was deleted refused every later capture of the task (``EpisodeStore.attach``).  Such a row is
#: replayed once more; a missing source is now written with its field (``_terminal_code``), so a failure after that
#: replay stays final.
_LEGACY_SOURCE_MISSING = "SOURCE_MISSING"
#: Inbox rows a replay still stores, besides those never tried: a passing failure (``replay_inbox``), a row from
#: before the missing source was named, or a key another message already took (``resolve_conflicted_ingress``
#: stores it under a new key).  Any other code is terminal; the row stays for inspection only.
STILL_REPLAYED = frozenset({"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED", _LEGACY_SOURCE_MISSING, "VERSION_CONFLICT"})
#: The codes ``replay_inbox`` itself retries; a ``VERSION_CONFLICT`` row is ``resolve_conflicted_ingress``'s.
_RETRIED = tuple(sorted(STILL_REPLAYED - {"VERSION_CONFLICT"}))
#: What a capture already given a new key leaves when it conflicts again: another try would conflict the same way.
_REKEYED_CONFLICT = "VERSION_CONFLICT:rekeyed"


#: A row whose stored capture a replay could not check again is put off:
#: ``DEFERRED|<release>|<until>|<attempt>|<path>|<code>``.  A newer release's field in its context, a host whose check
#: fails for now, a secret screen that differs between releases: each can clear.  Given a final code at once, such a
#: row was never stored, and a Claude Code Stop that had counted it as waiting did not store the words either; left in
#: place, it stopped every row after it on every pass (reviews of 3.4.0rc10).  It is tried again after a minute,
#: doubling to an hour, by whichever release runs, and the tries are counted across releases: when its
#: ``DEFER_ATTEMPTS``-th try again fails the row is given up (``GAVE_UP|<release>|<failures>|<path>|<code>``), where
#: doctor and the patrol show it, and ``retry-failures --apply`` returns it to the replay once its cause is fixed.  ``path`` is the replay
#: that put it off: a key collision's new key (``rekey``) is retried only by ``resolve_conflicted_ingress``, since a
#: plain replay would only meet the collision again.
_DEFERRED = "DEFERRED|"
#: What a replay's last receipt carries when a busy store stopped its page: the rest waits for the next pass.
INGRESS_PENDING_GAP = "capture_gap:durable_ingress_pending"
#: A commit's failures that leave its row for the next pass.
_PASSING = frozenset({"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED"})
_GAVE_UP = "GAVE_UP|"
_PATHS = ("replay", "rekey")
DEFER_FIRST_SECONDS = 60.0
DEFER_MAX_SECONDS = 3600.0
#: About nineteen hours of tries.
DEFER_ATTEMPTS = 24
#: What a query takes for the rows ``replay_inbox`` may store: never tried, a code in ``_RETRIED``, or put off.
REPLAY_CANDIDATES = (f"(last_error_code IS NULL OR last_error_code IN ({','.join('?' for _ in _RETRIED)})"
                     " OR last_error_code LIKE 'DEFERRED|%')")


def _kind(exc: BaseException) -> str:
    """What failed, for an operator: a contract error's code and field, or the exception's class."""
    if isinstance(exc, ContractError):
        text = f"{exc.code}:{exc.field}" if exc.field else exc.code
    else:
        text = type(exc).__name__
    return text.replace("|", "/")[:80]


def _parsed_deferral(code: object) -> tuple[datetime | None, int, str] | None:
    """``(until, attempt, path)`` of a ``DEFERRED`` code, or None for any other code.  A time that cannot be read as a
    UTC time is None: the row is due."""
    if type(code) is not str or not code.startswith(_DEFERRED):
        return None
    parts = code.split("|", 5)
    if len(parts) != 6:
        return None, 0, "replay"
    _marker, _release, until, attempt, path, _kind_ = parts
    try:
        moment = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is not None and moment.tzinfo is None:
        moment = None
    return moment, int(attempt) if attempt.isdigit() else 0, path if path in _PATHS else "replay"


def _deferral(previous: object, exc: BaseException, now: datetime, *, path: str) -> str:
    parsed = _parsed_deferral(previous)
    attempt = (parsed[1] if parsed is not None else 0) + 1
    if attempt > DEFER_ATTEMPTS:
        return f"{_GAVE_UP}{__version__}|{attempt}|{path}|{_kind(exc)}"
    delay = min(DEFER_MAX_SECONDS, DEFER_FIRST_SECONDS * 2 ** (attempt - 1))
    until = (now + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{_DEFERRED}{__version__}|{until}|{attempt}|{path}|{_kind(exc)}"


def deferred_until(code: object, now: datetime) -> datetime | None:
    """When a row put off is tried again, if that is still to come; None when it is due or not put off."""
    parsed = _parsed_deferral(code)
    if parsed is None or parsed[0] is None:
        return None
    until = parsed[0]
    # A time further off than any backoff was written while the clock ran ahead: due now.
    if until <= now or until - now > timedelta(seconds=2 * DEFER_MAX_SECONDS):
        return None
    return until


def deferred_path(code: object) -> str | None:
    """The replay a row was put off or given up by (``replay`` or ``rekey``); None for any other code."""
    parsed = _parsed_deferral(code)
    if parsed is not None:
        return parsed[2]
    if type(code) is str and code.startswith(_GAVE_UP):
        parts = code.split("|", 4)
        return parts[3] if len(parts) == 5 and parts[3] in _PATHS else "replay"
    return None


def replayable(code: object, now: datetime) -> bool:
    """Whether a pass now stores a row with this code: never tried, a passing failure, a bare ``SOURCE_MISSING`` an
    older release left, or a row put off whose time has come.

    What wakes the worker, and all the doctor does not call blocked, is read from here: the wake had counted two of
    the three retried codes, so a row an older release left as ``SOURCE_MISSING`` waited for a pass something else
    started, and the doctor called it blocked.  A key collision (``VERSION_CONFLICT``) is taken by the next pass's
    ``resolve_conflicted_ingress``, which stores it under a new key, finds it final (``VERSION_CONFLICT:rekeyed``) or
    puts it off, so its wake is never in vain; counted blocked, a collision, and a given-up row returned to the rekey
    path, waited for a pass something else started (review of rc10)."""
    if code is None or code in _RETRIED or code == "VERSION_CONFLICT":
        return True
    return type(code) is str and code.startswith(_DEFERRED) and deferred_until(code, now) is None


def given_up(code: object) -> bool:
    """Whether a replay gave a row up (``_GAVE_UP``): blocked until ``retry-failures --apply``."""
    return type(code) is str and code.startswith(_GAVE_UP)


def waiting(code: object) -> bool:
    """Whether some pass will still store a row with this code: what a hook's record read counts as already said.  A
    row given up is not."""
    return code is None or code in STILL_REPLAYED or (type(code) is str and code.startswith(_DEFERRED))


def taking_a_new_key(code: object) -> bool:
    """Whether a row is another message that took a stored one's key: a collision, or one the rekey path put off."""
    return code == "VERSION_CONFLICT" or deferred_path(code) == "rekey"


def without_whitespace(text: object) -> str:
    """``text`` with no whitespace at all: how a delete compares a message's text (``holds``)."""
    return "".join(str(text).split())


def letters_and_digits(text: object) -> str:
    """``text`` with its letters and digits alone, in one case: how a delete compares a near copy (``holds``)."""
    return _NOT_A_LETTER.sub("", str(text)).casefold()


_NOT_A_LETTER = re.compile(r"[\W_]+")
#: Characters, whitespace aside, from which a deleted message's text is its own: a row holding all of it is a copy,
#: whatever else it says.  A shorter one ("好", "ok") is found inside unrelated messages, and deleting it cancelled
#: every waiting row that held it (review of rc10).
DISTINCT_TEXT = 24
#: Letters and digits from which a row with the same ones and at most a tenth more is a copy though its punctuation or
#: case differ ("我要辞职了。").  Below that, different messages compare the same ("C++" and "C#", "+1" and "-1").
NEAR_COPY = 4


def deleted_text(text: object) -> tuple[str, str]:
    """A deleted message's text as ``holds`` compares it: without whitespace, and its letters and digits."""
    bare = without_whitespace(text)
    return bare, letters_and_digits(bare)


def deleted_forms(text: object) -> frozenset[str]:
    """What a purge keeps of a deleted message's words to know a copy by once they are gone: digests of them without
    whitespace, and of their letters and digits when there are ``NEAR_COPY`` or more of them.  The same words spaced,
    cased or punctuated otherwise have the same forms; words added or taken away do not (review of rc13)."""
    bare, letters = deleted_text(text)
    forms = {"bare:" + hashlib.sha256(bare.encode("utf-8")).hexdigest()} if bare else set()
    if len(letters) >= NEAR_COPY:
        forms.add("letters:" + hashlib.sha256(letters.encode("utf-8")).hexdigest())
    return frozenset(forms)


def holds(payload_json: object, digests: frozenset[str], groups: frozenset[str],
          texts: frozenset[tuple[str, str]] = frozenset(), *, rekeyed: bool = False) -> bool:
    """Whether an inbox row's capture holds a deleted message.  A row that cannot be read is taken to (a delete then
    cancels it, as it cancels every row it cannot look into).  It holds one when:

    - one of its segments is a deleted one as stored (``content_sha256``), or it is of the deleted source's group
      (not for a row being given a new key, ``rekeyed``: another message that took a stored one's key, whose group
      is not its own; deleting the first message cancelled the second);
    - whitespace aside, it holds all of a deleted text of ``DISTINCT_TEXT`` characters or more, or is one with at most
      a tenth more; or, letters and digits compared, it is a deleted text of ``NEAR_COPY`` or more of them with at most
      a tenth more.  A message that quotes a short deleted one among other words, only part of a long one, or a long
      one written otherwise than whitespace (a quote reformatted, its punctuation changed), is kept.

    Compared by digest alone, the same words with a line break or a full stop more were kept and stored after the
    delete; compared more loosely, deleting a short message cancelled unrelated rows, and a character-by-character
    normalisation of every waiting row held the writer lease for seconds (reviews of rc10)."""
    try:
        return holds_events(json.loads(payload_json)["events"], digests, groups, texts, rekeyed=rekeyed)
    except (ValueError, KeyError, TypeError, AttributeError):
        return True


def holds_events(events, digests: frozenset[str], groups: frozenset[str],
                 texts: frozenset[tuple[str, str]] = frozenset(), *, rekeyed: bool = False) -> bool:
    """``holds`` for a capture's events already read: the parts of one message, or one of them.  Storage asks it of a
    message under a deleted message's key (``storage.put_source``)."""
    from .events import stored_content_digest

    for event in events:
        segment = event.get("segment")
        group = segment.get("group_key") if isinstance(segment, dict) else event.get("source_event_key")
        if (not rekeyed and group in groups) or stored_content_digest(event["content"]) in digests:
            return True
    texts = [text for text in texts if text[0]]
    if not texts:
        return False
    ordered = sorted(events, key=lambda event: (event.get("segment") or {}).get("index", 0))
    bare = without_whitespace("".join(event["content"] for event in ordered))
    letters = None
    for text, text_letters in texts:
        if ((len(text) >= DISTINCT_TEXT or len(bare) - len(text) <= len(bare) // 10) and text in bare):
            return True
        if len(text_letters) >= NEAR_COPY:
            letters = letters_and_digits(bare) if letters is None else letters
            if len(letters) - len(text_letters) <= len(letters) // 10 and text_letters in letters:
                return True
    return False


def _terminal_code(exc: ContractError) -> str:
    """The code a terminal failure leaves on its row: a missing source says which, never the bare legacy code."""
    if exc.code == _LEGACY_SOURCE_MISSING:
        return f"{exc.code}:{exc.field or 'unnamed'}"
    return exc.code


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _context_payload(context):
    if context.import_provenance is not None or context.actor_origin == "imported":
        raise ContractError("ACCESS_DENIED", "ingress_import_attestation")
    payload = dict(session_id=context.session_id, actor_origin=context.actor_origin,
                   allowed_scope_ids=sorted(context.allowed_scope_ids), project_id=context.project_id,
                   branch_id=context.branch_id, task_anchor=context.task_anchor,
                   environment_revision=context.environment_revision,
                   source_principal=(context.source_principal.to_payload()
                                     if context.source_principal is not None else None),
                   display_snapshot=context.display_snapshot.to_payload() if context.display_snapshot else None)
    # The entry is part of the original actor a replay keeps.  Whoever replays --
    # the shared worker, or another entry's provider -- otherwise files the
    # capture under its own name, and a busy shared store sends more captures
    # through here, not fewer.  Only present in a shared store, so a local
    # store's payload is byte-for-byte what it was and an old row still matches.
    if context.entry_id is not None:
        payload["entry_id"] = context.entry_id
    return payload


def enqueue(storage, clock, context, value, *, scope_id, host_scope, remaining_seconds=1.0):
    storage._context_check(context)
    if scope_id not in context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED")
    prepared = prepare_capture(value, context)
    if prepared.rejection:
        return None, prepared
    if host_scope is None and not context.binding.test_mode:
        raise ContractError("ACCESS_DENIED", "ingress_host_authority")
    body = dict(events=prepared.events, gaps=prepared.gaps, context=_context_payload(context), host_scope=host_scope)
    encoded = _json(body)
    if len(encoded.encode("utf-8")) > 2097152:
        raise ContractError("INPUT_INVALID", "ingress_item_budget")
    token = hashlib.sha256(_json([context.binding.installation_id, scope_id, context.session_id,
                                context.project_id, context.branch_id,
                                [(e["source_event_key"], e["source_revision"]) for e in prepared.events]]).encode()).hexdigest()
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        conn = tx._check(write=True)
        prior = conn.execute("SELECT payload_json FROM capture_inbox WHERE token=?", (token,)).fetchone()
        if prior:
            previous = json.loads(prior[0])
            # Retried host hooks may report a new receipt timestamp, but never
            # replace the first occurrence or quietly change its evidence.
            def normalize(events):
                return [
                    {
                        key: value
                        for key, value in event.items()
                        if key not in {"recorded_at", "occurred_at", "time_precision"}
                    }
                    for event in events
                ]

            if normalize(previous["events"]) != normalize(body["events"]) or previous["context"] != body["context"] or previous["host_scope"] != host_scope:
                raise ContractError("VERSION_CONFLICT", "ingress_identity")
            return token, PreparedCapture(tuple(previous["events"]), tuple(previous["gaps"]))
        count, size = conn.execute("SELECT count(*),coalesce(sum(length(CAST(payload_json AS BLOB))),0) FROM capture_inbox").fetchone()
        if count >= 256 or size + len(encoded.encode("utf-8")) > 67108864:
            raise ContractError("STORAGE_UNAVAILABLE", "ingress_capacity")
        conn.execute("INSERT INTO capture_inbox(token,scope_id,project_id,branch_id,created_at,payload_json) VALUES (?,?,?,?,?,?)",
                     (token, scope_id, context.project_id, context.branch_id, clock.utc_now(), encoded))
    return token, prepared


def durable_record_event(storage, clock, context, value, *, scope_id, host_scope, admission_policy=None, remaining_seconds=1.0):
    deadline = time.monotonic() + remaining_seconds
    token, prepared = enqueue(storage, clock, context, value, scope_id=scope_id, host_scope=host_scope,
                              remaining_seconds=remaining_seconds)
    if token is None:
        return CaptureReceipt("rejected", (), "not_persisted", "not_indexed", "not_scheduled", prepared.gaps, prepared.rejection)
    return _commit(storage, clock, context, token, prepared, scope_id, admission_policy, deadline)


def _commit(storage, clock, context, token, prepared, scope_id, policy, deadline, *, rekeyed=False):
    try:
        receipt = record_event(storage, clock, context, {}, scope_id=scope_id,
                               admission_policy=policy, remaining_seconds=max(.001, deadline-time.monotonic()),
                               _prepared=prepared, _inbox_token=token)
        if receipt.disposition in {"conflict", "cancelled"}:
            # A capture that conflicts under the key it was given for its content stays final: written back as a
            # bare conflict, it was given the same key and refused again on every pass.
            code = _REKEYED_CONFLICT if rekeyed and receipt.disposition == "conflict" else receipt.error_code
            with storage.write(context, remaining_seconds=max(.001, deadline-time.monotonic())) as tx:
                tx._check(write=True).execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
            return receipt
        if receipt.durability == "persisted":
            return receipt
        code = receipt.error_code or "STORAGE_UNAVAILABLE"
    except ContractError as exc:
        code = exc.code
        if (exc.code, exc.field) == DELETED_KEY:
            return _refused_for_a_delete(storage, context, token, prepared, deadline)
        # Terminal failures remain inspectable, but are not replayed forever.  A passing one (the store busy, the time
        # up) leaves the row's code as it was: written over, a collision left its path, and a row put off its tries
        # and its place across a delete (review of rc10).
        if code not in _PASSING:
            try:
                with storage.write(context, remaining_seconds=max(.001, deadline-time.monotonic())) as tx:
                    tx._check(write=True).execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?",
                                                  (_terminal_code(exc), token))
            except (*_TRANSIENT, ContractError):
                pass
    except _TRANSIENT:
        code = "STORAGE_UNAVAILABLE"
    # A row the next pass takes again says so: a pass that met a busy writer here said nothing (review of rc10).
    pending = (INGRESS_PENDING_GAP,) if code in _PASSING else ()
    return CaptureReceipt("queued", (), "queued", "pending", "pending", (*prepared.gaps, *pending), code)


#: How storage refuses a copy of a deleted message under that message's key, or a later version or part of the deleted
#: one (``storage.Transaction.refuse_under_a_deleted_key``): the code and field of its ContractError, refused for good.
DELETED_KEY = ("ACCESS_DENIED", "source_unavailable")
#: What the receipt of such a capture carries when it left the inbox.
SOURCE_DELETED_GAP = "capture_gap:source_deleted"


def _refused_for_a_delete(storage, context, token, prepared, deadline) -> CaptureReceipt:
    """A copy of a deleted message under that message's key is refused for good; another message under the key is a
    key collision, stored under a key of its own (``storage.Transaction.refuse_under_a_deleted_key``).  Left in the
    inbox with its code, the copy kept the doctor's ``capture_ingress_blocked`` and the patrol's line up until someone
    removed it by hand (rc13); it leaves the inbox, and the pass counts it among the rows it cancelled."""
    try:
        with storage.write(context, remaining_seconds=max(.001, deadline-time.monotonic())) as tx:
            tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (token,))
    except (*_TRANSIENT, ContractError):
        # Not removed: the next pass meets the same refusal and removes it then.
        pass
    return CaptureReceipt("cancelled", (), "not_persisted", "unchanged", "unchanged",
                          (*prepared.gaps, SOURCE_DELETED_GAP), "ACCESS_DENIED")


#: Marker spliced into a re-keyed capture's source_event_key. Self-documenting on
#: purpose: the stored key keeps the host's original and appends the content
#: fingerprint, so the collision stays visible to anyone reading the row rather
#: than being filed in a side table nobody queries.
REKEY_MARKER = "#rekey:"


def _capture_fingerprint(events) -> str:
    """One fingerprint of a whole capture, which every segment of a long message shares."""
    return hashlib.sha256(_json([[event["source_event_key"], event.get("content"), event.get("origin"),
                                  event.get("role"), event.get("occurred_at")] for event in events]).encode("utf-8")
                          ).hexdigest()[:16]


def _rekeyed_event(event: dict, capture: str = "") -> dict:
    """Give one capture an identity derived from its own content.

    A host that reuses a turn number sends a second, different message under a
    key that already exists. Storage refuses it — same event id and revision,
    different fingerprint — and neither obvious escape is right: leaving it
    refused loses the message, while storing it as the next revision would brand
    it a revision of an unrelated message and hide the first one, since lexical
    retrieval only returns the newest revision of a group.

    Distinct content therefore earns a distinct identity. The derivation is
    deterministic, so repairing the same payload twice is idempotent.

    A segment of a long message moves with its group: the group key takes the
    marker and the fingerprint of the whole capture (``capture``), and the
    segment's own key is derived from the new group key as when it was split
    (``events.prepare_capture``).  Only the segment's key had changed, so the
    segments met the first message's group again and were never stored.
    """
    segment = event.get("segment")
    if segment:
        group = str(segment["group_key"])
        if REKEY_MARKER in group:
            return dict(event)
        group = _rekey(group, capture)
        return {**event, "source_event_key": segment_key(group, segment["index"]),
                "segment": {**segment, "group_key": group}}
    original = str(event["source_event_key"])
    if REKEY_MARKER in original:
        return dict(event)
    fingerprint = hashlib.sha256(
        _json([original, event.get("content"), event.get("origin"), event.get("role"),
               event.get("occurred_at")]).encode("utf-8")
    ).hexdigest()[:16]
    return {**event, "source_event_key": _rekey(original, fingerprint)}


#: A source key, and a segment's group key, is at most this long (``contracts/source_event.schema.json``).
_KEY_LIMIT = 512


def _rekey(key: str, fingerprint: str) -> str:
    """``key`` with the marker and the fingerprint, its original part cut to fit the key limit: a key of 490 characters
    or more went past it and was refused on every pass.  The fingerprint covers the whole original key, so the cut
    one stays unique."""
    suffix = f"{REKEY_MARKER}{fingerprint}"
    return key[:_KEY_LIMIT - len(suffix)] + suffix


def resolve_conflicted_ingress(storage, clock, context, *, authorize, admission_policy=None,
                               limit=8, remaining_seconds=1.0):
    """Store captures whose host key collided, one bounded page at a time.

    ``replay_inbox`` retries only failures that could plausibly clear on their
    own, so a ``VERSION_CONFLICT`` row is never touched again: its payload sits
    in the inbox for good, with nothing but a doctor gap to show for it. Nor
    would replaying it unchanged help — the identity collides by construction —
    so this re-keys by content first.

    Same authorization and revalidation as a replay: the stored envelope is
    re-checked against the captured context, never trusted merely for having
    been in the inbox already.
    """
    if not 1 <= limit <= 32:
        raise ContractError("INPUT_INVALID", "ingress_limit")
    deadline = time.monotonic() + remaining_seconds
    scopes = tuple(sorted(context.allowed_scope_ids))
    if not scopes:
        return ()
    now = _utc(clock)
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        conn = tx._check()
        tokens = [token for token, code in conn.execute(
            f"""SELECT token,last_error_code FROM capture_inbox WHERE scope_id IN ({','.join('?' for _ in scopes)})
            AND project_id IS ? AND branch_id IS ?
            AND (last_error_code='VERSION_CONFLICT' OR last_error_code LIKE 'DEFERRED|%')
            ORDER BY created_at,token""", (*scopes, context.project_id, context.branch_id))
            if code == "VERSION_CONFLICT" or (deferred_path(code) == "rekey" and replayable(code, now))][:limit]
        rows = [row for token in tokens
                if (row := conn.execute("SELECT * FROM capture_inbox WHERE token=?", (token,)).fetchone()) is not None]
    return _replay_rows(storage, clock, context, rows, authorize, admission_policy, deadline, rekey=True)


def _replay_rows(storage, clock, context, rows, authorize, admission_policy, deadline, *, rekey):
    receipts = []
    for row in rows:
        if time.monotonic() >= deadline:
            break
        try:
            revalidated = _revalidated(storage, context, row, authorize, deadline, rekey=rekey)
        except _TRANSIENT:
            # The store itself: the rest of the page waits for the next pass.  Raised, it lost what this replay had
            # done, and the rekey replay after it did not run (review of rc10).
            receipts.append(CaptureReceipt("queued", (), "queued", "pending", "pending", (INGRESS_PENDING_GAP,),
                                           "STORAGE_UNAVAILABLE"))
            break
        except (ContractError, KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
            # A host's check that raises (Hermes' identity errors are RuntimeErrors) put off this row only.
            receipts.append(_defer(storage, clock, context, row, exc, deadline, path="rekey" if rekey else "replay"))
            continue
        if revalidated is None:
            receipts.append(CaptureReceipt("cancelled", (), "not_persisted", "unchanged", "unchanged", error_code="ACCESS_DENIED"))
            continue
        original, prepared = revalidated
        receipts.append(_commit(storage, clock, original, row["token"], prepared, row["scope_id"], admission_policy,
                                deadline, rekeyed=rekey))
    return tuple(receipts)


def _revalidated(storage, context, row, authorize, deadline, *, rekey):
    """One row's stored capture, checked again: ``(its context, the capture)``, or None when the host no longer grants
    its scope (the row is then removed).  Raises when the stored envelope no longer passes."""
    body = json.loads(row["payload_json"])
    try:
        raw = dict(body["context"])
        stored = frozenset(raw.pop("allowed_scope_ids"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("INPUT_INVALID", "ingress_context") from exc
    allowed = stored & context.allowed_scope_ids & frozenset(authorize(body["host_scope"]))
    if row["scope_id"] not in allowed:
        with storage.write(context, remaining_seconds=max(.001, deadline-time.monotonic())) as tx:
            tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (row["token"],))
        return None
    try:
        snapshot = raw.pop("display_snapshot")
        principal = raw.pop("source_principal", None)
        original = TrustedContext(context.binding, allowed_scope_ids=allowed,
            display_snapshot=DisplaySnapshot(snapshot["order"], tuple(ArtifactVersion(**i) for i in snapshot["items"])) if snapshot else None,
            source_principal=TrustedSourcePrincipal(**principal) if principal is not None else None,
            **raw)
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        # A field a newer release wrote into the stored context, or one this release needs and it lacks: named, where
        # a bare TypeError said nothing of where to look (review of rc10).
        raise ContractError("INPUT_INVALID", "ingress_context") from exc
    # Revalidate the stored envelope; trust is from the captured context,
    # never inferred from a payload role or text claiming to be a user.
    capture = _capture_fingerprint(body["events"]) if rekey else ""
    events = []
    for event in body["events"]:
        checked = prepare_capture(_rekeyed_event(event, capture) if rekey else event, original)
        if checked.rejection:
            raise ContractError("ACCESS_DENIED", "ingress_payload")
        events.extend(checked.events)
    return original, PreparedCapture(tuple(events), tuple(body["gaps"]))


def _utc(clock) -> datetime:
    try:
        moment = datetime.fromisoformat(str(clock.utc_now()).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _defer(storage, clock, context, row, exc, deadline, *, path) -> CaptureReceipt:
    """Put off a row whose stored capture a replay could not check again (``_DEFERRED``), or give it up."""
    code = _deferral(row["last_error_code"], exc, _utc(clock), path=path)
    try:
        with storage.write(context, remaining_seconds=max(.001, deadline-time.monotonic())) as tx:
            tx._check(write=True).execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, row["token"]))
    except (*_TRANSIENT, ContractError):
        # Not written: the row keeps its code and a later pass takes it again.  Said as put off or given up, a pass
        # reported a give-up the store never saw, and the next reported it again (review of rc10).
        return CaptureReceipt("queued", (), "queued", "pending", "pending", (INGRESS_PENDING_GAP,),
                              "STORAGE_UNAVAILABLE")
    return CaptureReceipt("queued", (), "queued", "pending", "pending",
                          error_code="GAVE_UP" if code.startswith(_GAVE_UP) else "DEFERRED")


def replay_inbox(storage, clock, context, *, authorize, admission_policy=None, limit=8, remaining_seconds=1.0):
    """Replay only the caller's partition; the callback verifies current host ACLs."""
    if not 1 <= limit <= 32:
        raise ContractError("INPUT_INVALID", "ingress_limit")
    deadline = time.monotonic() + remaining_seconds
    scopes = tuple(sorted(context.allowed_scope_ids))
    if not scopes:
        return ()
    # A row put off is passed over, not the head of the page.  Its code is read first and its payload only when it
    # is taken: the inbox holds up to 256 rows and 64 MB.
    now = _utc(clock)
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        conn = tx._check()
        tokens = [token for token, code in conn.execute(
            f"""SELECT token,last_error_code FROM capture_inbox WHERE scope_id IN ({','.join('?' for _ in scopes)})
            AND project_id IS ? AND branch_id IS ? AND {REPLAY_CANDIDATES}
            ORDER BY created_at,token""",
            (*scopes, context.project_id, context.branch_id, *_RETRIED))
            if replayable(code, now) and deferred_path(code) != "rekey"][:limit]
        rows = [row for token in tokens
                if (row := conn.execute("SELECT * FROM capture_inbox WHERE token=?", (token,)).fetchone()) is not None]
    return _replay_rows(storage, clock, context, rows, authorize, admission_policy, deadline, rekey=False)
