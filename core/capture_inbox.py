"""Bounded, sanitized host ingress in the authoritative backup/restore domain.

Only an authenticated adapter may create an ingress row. Replay retains its
original actor and occurrence and narrows authority against the current host.
The inbox removal and all source effects commit in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

from ..contracts import (
    ArtifactVersion,
    ContractError,
    DisplaySnapshot,
    TrustedContext,
    TrustedSourcePrincipal,
)
from .capture import CaptureReceipt, record_event
from .delete_storage import canonical
from .events import PreparedCapture, prepare_capture, segment_key
from .inbox_rules import (
    GAVE_UP,
    LEGACY_SOURCE_MISSING,
    REKEY_MARKER,
    REPLAY_CANDIDATES,
    RETRIED,
    deferral,
    deferred_path,
    replayable,
)
from .truth_connection import TruthDatabaseConnectionError
from .writer_lease import TruthWriterBusyError

_TRANSIENT = (sqlite3.Error, TruthDatabaseConnectionError, TruthWriterBusyError)
#: What a capture already given a new key leaves when it conflicts again: another try would conflict the same way.
_REKEYED_CONFLICT = "VERSION_CONFLICT:rekeyed"
#: What a replay's last receipt carries when a busy store stopped its page: the rest waits for the next pass.
INGRESS_PENDING_GAP = "capture_gap:durable_ingress_pending"
#: A commit's failures that leave its row for the next pass.
_PASSING = frozenset({"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED"})


def _terminal_code(exc: ContractError) -> str:
    """The code a terminal failure leaves on its row: a missing source says which, never the bare legacy code."""
    if exc.code == LEGACY_SOURCE_MISSING:
        return f"{exc.code}:{exc.field or 'unnamed'}"
    return exc.code


def _context_payload(context):
    if context.import_provenance is not None or context.actor_origin == "imported":
        raise ContractError("ACCESS_DENIED", "ingress_import_attestation")
    payload = dict(
        session_id=context.session_id,
        actor_origin=context.actor_origin,
        allowed_scope_ids=sorted(context.allowed_scope_ids),
        project_id=context.project_id,
        branch_id=context.branch_id,
        task_anchor=context.task_anchor,
        environment_revision=context.environment_revision,
        source_principal=(context.source_principal.to_payload() if context.source_principal is not None else None),
        display_snapshot=context.display_snapshot.to_payload() if context.display_snapshot else None,
    )
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
    encoded = canonical(body)
    if len(encoded.encode("utf-8")) > 2097152:
        raise ContractError("INPUT_INVALID", "ingress_item_budget")
    # The words are part of a capture's place here.  Codex gives a message sent into a running turn that turn's id, so
    # a second such message comes under the key of the first while the first still waits for its new key
    # (``resolve_conflicted_ingress``): without its words in the token it would meet that row and be refused as
    # changed evidence.  A retried hook sends the same words and meets its own row.
    token = hashlib.sha256(
        canonical(
            [
                context.binding.installation_id,
                scope_id,
                context.session_id,
                context.project_id,
                context.branch_id,
                [
                    (
                        e["source_event_key"],
                        e["source_revision"],
                        hashlib.sha256(e["content"].encode("utf-8")).hexdigest(),
                    )
                    for e in prepared.events
                ],
            ]
        ).encode()
    ).hexdigest()
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

            if (
                normalize(previous["events"]) != normalize(body["events"])
                or previous["context"] != body["context"]
                or previous["host_scope"] != host_scope
            ):
                raise ContractError("VERSION_CONFLICT", "ingress_identity")
            return token, PreparedCapture(tuple(previous["events"]), tuple(previous["gaps"]))
        count, size = conn.execute(
            "SELECT count(*),coalesce(sum(length(CAST(payload_json AS BLOB))),0) FROM capture_inbox"
        ).fetchone()
        if count >= 256 or size + len(encoded.encode("utf-8")) > 67108864:
            raise ContractError("STORAGE_UNAVAILABLE", "ingress_capacity")
        conn.execute(
            "INSERT INTO capture_inbox(token,scope_id,project_id,branch_id,created_at,payload_json) VALUES (?,?,?,?,?,?)",
            (token, scope_id, context.project_id, context.branch_id, clock.utc_now(), encoded),
        )
    return token, prepared


def durable_record_event(
    storage, clock, context, value, *, scope_id, host_scope, admission_policy=None, remaining_seconds=1.0
):
    deadline = time.monotonic() + remaining_seconds
    token, prepared = enqueue(
        storage, clock, context, value, scope_id=scope_id, host_scope=host_scope, remaining_seconds=remaining_seconds
    )
    if token is None:
        return CaptureReceipt(
            "rejected", (), "not_persisted", "not_indexed", "not_scheduled", prepared.gaps, prepared.rejection
        )
    return _commit(storage, clock, context, token, prepared, scope_id, admission_policy, deadline)


def _commit(storage, clock, context, token, prepared, scope_id, policy, deadline, *, rekeyed=False):
    try:
        receipt = record_event(
            storage,
            clock,
            context,
            {},
            scope_id=scope_id,
            admission_policy=policy,
            remaining_seconds=max(0.001, deadline - time.monotonic()),
            _prepared=prepared,
            _inbox_token=token,
        )
        if receipt.disposition in {"conflict", "cancelled"}:
            # A capture that conflicts under the key it was given for its content stays final: written back as a
            # bare conflict, it was given the same key and refused again on every pass.
            code = _REKEYED_CONFLICT if rekeyed and receipt.disposition == "conflict" else receipt.error_code
            with storage.write(context, remaining_seconds=max(0.001, deadline - time.monotonic())) as tx:
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
        # up) leaves the row's code as it was: written over, a collision would leave its path, and a row put off its
        # tries and its place across a delete.
        if code not in _PASSING:
            try:
                with storage.write(context, remaining_seconds=max(0.001, deadline - time.monotonic())) as tx:
                    tx._check(write=True).execute(
                        "UPDATE capture_inbox SET last_error_code=? WHERE token=?", (_terminal_code(exc), token)
                    )
            except (*_TRANSIENT, ContractError):
                pass
    except _TRANSIENT:
        code = "STORAGE_UNAVAILABLE"
    # A row the next pass takes again says so, or a pass that met a busy writer here would say nothing.
    pending = (INGRESS_PENDING_GAP,) if code in _PASSING else ()
    return CaptureReceipt("queued", (), "queued", "pending", "pending", (*prepared.gaps, *pending), code)


#: How storage refuses a copy of a deleted message under that message's key, or a later version or part of the deleted
#: one (``Sources.refuse_under_a_deleted_key``): the code and field of its ContractError, refused for good.
DELETED_KEY = ("ACCESS_DENIED", "source_unavailable")
#: What the receipt of such a capture carries when it left the inbox.
SOURCE_DELETED_GAP = "capture_gap:source_deleted"


def _refused_for_a_delete(storage, context, token, prepared, deadline) -> CaptureReceipt:
    """A copy of a deleted message under that message's key is refused for good; another message under the key is a
    key collision, stored under a key of its own (``source_storage.Sources.refuse_under_a_deleted_key``).  Left in the
    inbox with its code, the copy would keep the doctor's ``capture_ingress_blocked`` up until someone removed it by
    hand; it leaves the inbox, and the pass counts it among the rows it cancelled."""
    try:
        with storage.write(context, remaining_seconds=max(0.001, deadline - time.monotonic())) as tx:
            tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (token,))
    except (*_TRANSIENT, ContractError):
        # Not removed: the next pass meets the same refusal and removes it then.
        pass
    return CaptureReceipt(
        "cancelled",
        (),
        "not_persisted",
        "unchanged",
        "unchanged",
        (*prepared.gaps, SOURCE_DELETED_GAP),
        "ACCESS_DENIED",
    )


def _capture_fingerprint(events) -> str:
    """One fingerprint of a whole capture, which every segment of a long message shares."""
    return hashlib.sha256(
        canonical(
            [
                [
                    event["source_event_key"],
                    event.get("content"),
                    event.get("origin"),
                    event.get("role"),
                    event.get("occurred_at"),
                ]
                for event in events
            ]
        ).encode("utf-8")
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
        return {
            **event,
            "source_event_key": segment_key(group, segment["index"]),
            "segment": {**segment, "group_key": group},
        }
    original = str(event["source_event_key"])
    if REKEY_MARKER in original:
        return dict(event)
    fingerprint = hashlib.sha256(
        canonical(
            [original, event.get("content"), event.get("origin"), event.get("role"), event.get("occurred_at")]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return {**event, "source_event_key": _rekey(original, fingerprint)}


#: A source key, and a segment's group key, is at most this long (``contracts/source_event.schema.json``).
_KEY_LIMIT = 512


def _rekey(key: str, fingerprint: str) -> str:
    """``key`` with the marker and the fingerprint, its original part cut to fit the key limit: a key of 490 characters
    or more went past it and was refused on every pass.  The fingerprint covers the whole original key, so the cut
    one stays unique."""
    suffix = f"{REKEY_MARKER}{fingerprint}"
    return key[: _KEY_LIMIT - len(suffix)] + suffix


def resolve_conflicted_ingress(
    storage, clock, context, *, authorize, admission_policy=None, limit=8, remaining_seconds=1.0
):
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
    page = _page(
        storage,
        clock,
        context,
        limit,
        remaining_seconds,
        "(last_error_code='VERSION_CONFLICT' OR last_error_code LIKE 'DEFERRED|%')",
        (),
        lambda code, now: code == "VERSION_CONFLICT" or (deferred_path(code) == "rekey" and replayable(code, now)),
    )
    if page is None:
        return ()
    rows, deadline = page
    return _replay_rows(storage, clock, context, rows, authorize, admission_policy, deadline, rekey=True)


def _page(storage, clock, context, limit, remaining_seconds, condition, params, takes):
    """The rows of one page of the caller's inbox partition that ``condition`` (with ``params``) selects and
    ``takes(code, now)`` keeps, oldest first, and the deadline the page's replay has; None when the context reaches no
    scope.

    A row put off is passed over, not the head of the page.  Its code is read first and its payload only when it is
    taken: the inbox holds up to 256 rows and 64 MB.
    """
    if not 1 <= limit <= 32:
        raise ContractError("INPUT_INVALID", "ingress_limit")
    deadline = time.monotonic() + remaining_seconds
    scopes = tuple(sorted(context.allowed_scope_ids))
    if not scopes:
        return None
    now = _utc(clock)
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        conn = tx._check()
        tokens = [
            token
            for token, code in conn.execute(
                f"""SELECT token,last_error_code FROM capture_inbox WHERE scope_id IN ({",".join("?" for _ in scopes)})
            AND project_id IS ? AND branch_id IS ? AND {condition}
            ORDER BY created_at,token""",
                (*scopes, context.project_id, context.branch_id, *params),
            )
            if takes(code, now)
        ][:limit]
        rows = [
            row
            for token in tokens
            if (row := conn.execute("SELECT * FROM capture_inbox WHERE token=?", (token,)).fetchone()) is not None
        ]
    return rows, deadline


def _replay_rows(storage, clock, context, rows, authorize, admission_policy, deadline, *, rekey):
    receipts = []
    for row in rows:
        if time.monotonic() >= deadline:
            break
        try:
            revalidated = _revalidated(storage, context, row, authorize, deadline, rekey=rekey)
        except _TRANSIENT:
            # The store itself: the rest of the page waits for the next pass.  Raised, it would lose what this replay
            # had done, and the rekey replay after it would not run.
            receipts.append(
                CaptureReceipt(
                    "queued", (), "queued", "pending", "pending", (INGRESS_PENDING_GAP,), "STORAGE_UNAVAILABLE"
                )
            )
            break
        except (ContractError, KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
            # A host's check that raises (Hermes' identity errors are RuntimeErrors) put off this row only.
            receipts.append(_defer(storage, clock, context, row, exc, deadline, path="rekey" if rekey else "replay"))
            continue
        if revalidated is None:
            receipts.append(
                CaptureReceipt("cancelled", (), "not_persisted", "unchanged", "unchanged", error_code="ACCESS_DENIED")
            )
            continue
        original, prepared = revalidated
        receipts.append(
            _commit(
                storage,
                clock,
                original,
                row["token"],
                prepared,
                row["scope_id"],
                admission_policy,
                deadline,
                rekeyed=rekey,
            )
        )
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
        with storage.write(context, remaining_seconds=max(0.001, deadline - time.monotonic())) as tx:
            tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (row["token"],))
        return None
    try:
        snapshot = raw.pop("display_snapshot")
        principal = raw.pop("source_principal", None)
        original = TrustedContext(
            context.binding,
            allowed_scope_ids=allowed,
            display_snapshot=DisplaySnapshot(snapshot["order"], tuple(ArtifactVersion(**i) for i in snapshot["items"]))
            if snapshot
            else None,
            source_principal=TrustedSourcePrincipal(**principal) if principal is not None else None,
            **raw,
        )
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        # A field a newer release wrote into the stored context, or one this release needs and it lacks: named, where
        # a bare TypeError would say nothing of where to look.
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
    """Put off a row whose stored capture a replay could not check again (``DEFERRED``), or give it up."""
    code = deferral(row["last_error_code"], exc, _utc(clock), path=path)
    try:
        with storage.write(context, remaining_seconds=max(0.001, deadline - time.monotonic())) as tx:
            tx._check(write=True).execute(
                "UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, row["token"])
            )
    except (*_TRANSIENT, ContractError):
        # Not written: the row keeps its code and a later pass takes it again.  Said as put off or given up, a pass
        # would report a give-up the store never saw, and the next would report it again.
        return CaptureReceipt(
            "queued", (), "queued", "pending", "pending", (INGRESS_PENDING_GAP,), "STORAGE_UNAVAILABLE"
        )
    return CaptureReceipt(
        "queued", (), "queued", "pending", "pending", error_code="GAVE_UP" if code.startswith(GAVE_UP) else "DEFERRED"
    )


def replay_inbox(storage, clock, context, *, authorize, admission_policy=None, limit=8, remaining_seconds=1.0):
    """Replay only the caller's partition; the callback verifies current host ACLs."""
    page = _page(
        storage,
        clock,
        context,
        limit,
        remaining_seconds,
        REPLAY_CANDIDATES,
        RETRIED,
        lambda code, now: replayable(code, now) and deferred_path(code) != "rekey",
    )
    if page is None:
        return ()
    rows, deadline = page
    return _replay_rows(storage, clock, context, rows, authorize, admission_policy, deadline, rekey=False)
