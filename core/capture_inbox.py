"""Bounded, sanitized host ingress in the authoritative backup/restore domain.

Only an authenticated adapter may create an ingress row. Replay retains its
original actor and occurrence and narrows authority against the current host.
The inbox removal and all source effects commit in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

from ..contracts import (
    ArtifactVersion,
    ContractError,
    DisplaySnapshot,
    TrustedContext,
    TrustedSourcePrincipal,
)
from . import private
from .capture import CaptureReceipt, record_event
from .capture_filters import redact_secret_text
from .deadline import fits
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
from .source_storage import source_reference
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


def context_payload(context):
    """Serialize exact authority and a masked display name; replay supplies and revalidates the binding."""
    if context.import_provenance is not None or context.actor_origin == "imported":
        raise ContractError("ACCESS_DENIED", "ingress_import_attestation")
    principal = context.source_principal.to_payload() if context.source_principal is not None else None
    if principal is not None and "display_name" in principal:
        principal["display_name"] = redact_secret_text(principal["display_name"])[0]
    payload = dict(
        session_id=context.session_id,
        actor_origin=context.actor_origin,
        allowed_scope_ids=sorted(context.allowed_scope_ids),
        project_id=context.project_id,
        branch_id=context.branch_id,
        task_anchor=context.task_anchor,
        environment_revision=context.environment_revision,
        source_principal=principal,
        display_snapshot=context.display_snapshot.to_payload() if context.display_snapshot else None,
    )
    # A local store's old inbox rows have no project root.  Omitting it when absent
    # keeps their envelope byte-for-byte equal to a retried capture's.
    if context.project_root is not None:
        payload["project_root"] = context.project_root
    if context.project_identity != "local":
        payload["project_identity"] = context.project_identity
    # The entry is part of the original actor a replay keeps.  Whoever replays --
    # the shared worker, or another entry's provider -- otherwise files the
    # capture under its own name, and a busy shared store sends more captures
    # through here, not fewer.  Only present in a shared store, so a local
    # store's payload is byte-for-byte what it was and an old row still matches.
    if context.entry_id is not None:
        payload["entry_id"] = context.entry_id
    return payload


def _prepared_ingress(value, context):
    """Revalidate masked retry segments while retaining their original count and capture gaps."""
    if not isinstance(value, PreparedCapture):
        return prepare_capture(value, context)
    if not value.rejection:
        if not value.events:
            raise ContractError("INPUT_INVALID", "unprepared_source")
        for event in value.events:
            checked = prepare_capture(event, context)
            if checked.rejection or checked.events != (event,):
                raise ContractError("INPUT_INVALID", "unprepared_source")
    return value


def enqueue(storage, clock, context, value, *, scope_id, host_scope, remaining_seconds=1.0):
    storage._context_check(context)
    if scope_id not in context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED")
    prepared = _prepared_ingress(value, context)
    if prepared.rejection:
        return None, prepared
    if host_scope is None and not context.binding.test_mode:
        raise ContractError("ACCESS_DENIED", "ingress_host_authority")
    body: dict = dict(
        events=prepared.events, gaps=prepared.gaps, context=context_payload(context), host_scope=host_scope
    )
    if prepared.masked_secret_count:
        body["masked_secret_count"] = prepared.masked_secret_count
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
        # Before the inbox row: a private capture must not leave its words in
        # durable ingress either, where a worker's later replay would meet them.
        intervals = private.matching_intervals(tx, context, events=prepared.events)
        if private.capture_mode(intervals, prepared.events) is not None:
            return None, prepared
        prepared = replace(prepared, private_revision=private.rule_revision(tx))
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

            # A row written before the project-root metadata existed is the same
            # capture, not a changed envelope: retries must not conflict on a field
            # the old row could not carry.
            old_context, new_context = dict(previous["context"]), dict(body["context"])
            if "project_root" not in old_context:
                new_context.pop("project_root", None)
            if "project_identity" not in old_context:
                new_context.pop("project_identity", None)
            if (
                normalize(previous["events"]) != normalize(body["events"])
                or old_context != new_context
                or previous["host_scope"] != host_scope
            ):
                raise ContractError("VERSION_CONFLICT", "ingress_identity")
            # The queued copy keeps its own moment; the private check made above was for this retry's, so the commit
            # checks the copy again.
            return token, replace(
                prepared,
                events=tuple(previous["events"]),
                gaps=tuple(previous["gaps"]),
                masked_secret_count=previous.get("masked_secret_count", 0),
                private_revision=None,
            )
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


class PendingCapture:
    """A durably enqueued capture's latest receipt, published by its immediate commit thread.

    The thread owns no open transaction at handoff. Process exit can discard it;
    the inbox row remains until record_event commits all effects atomically.
    """

    def __init__(self, prepared, installation_id):
        self.receipt = _waiting(prepared, None)
        self.done = threading.Event()
        self.current_refs = tuple(
            f"{source_reference(installation_id, e['source_event_key'])}@{e['source_revision']}"
            for e in prepared.events
        )

    def run(self, commit):
        """Publish the terminal receipt; an unexpected failure leaves durable replay responsible."""
        try:
            self.receipt = commit()
        except Exception:  # noqa: BLE001 - durable ingress retains the capture for replay
            pass
        finally:
            self.done.set()


def durable_record_event(
    storage,
    clock,
    context,
    value,
    *,
    scope_id,
    host_scope,
    admission_policy=None,
    remaining_seconds=1.0,
):
    started = time.monotonic()
    deadline = started + remaining_seconds
    token, prepared = enqueue(
        storage, clock, context, value, scope_id=scope_id, host_scope=host_scope, remaining_seconds=remaining_seconds
    )
    if token is None:
        return _refused(prepared)
    # The commit is a second write.  Started with less time left than the inbox write took, on a store that slow it
    # would run past the capture's time: the capture is durable in the inbox already, and the worker's replay, or the
    # next one a host runs, commits it.
    if not fits(deadline, time.monotonic() - started):
        return _waiting(prepared, "DEADLINE_EXCEEDED")
    return _commit(storage, clock, context, token, prepared, scope_id, admission_policy, deadline)


def _refused(prepared):
    refused = prepared.rejection is None
    return CaptureReceipt(
        "rejected",
        (),
        "not_persisted",
        "not_indexed",
        "not_scheduled",
        (*prepared.gaps, "private_mode") if refused else prepared.gaps,
        "PRIVATE_MODE" if refused else prepared.rejection,
        masked_secret_count=prepared.masked_secret_count,
    )


def start_record_event(
    storage, clock, context, value, *, scope_id, host_scope, admission_policy=None, remaining_seconds=1.0
):
    """Durably enqueue a host capture and immediately commit it on a daemon thread.

    Only a long-lived server uses this path; the commit keeps its own lease and
    deadline, independent of the prompt's recall and the lifetime of its handler.
    """
    token, prepared = enqueue(
        storage, clock, context, value, scope_id=scope_id, host_scope=host_scope, remaining_seconds=remaining_seconds
    )
    if token is None:
        return _refused(prepared)
    pending = PendingCapture(prepared, context.binding.installation_id)
    threading.Thread(
        target=pending.run,
        args=(
            lambda: _commit(
                storage,
                clock,
                context,
                token,
                prepared,
                scope_id,
                admission_policy,
                time.monotonic() + remaining_seconds,
            ),
        ),
        name="scope-recall-prompt-commit",
        daemon=True,
    ).start()
    return pending


def _waiting(prepared, code: str | None) -> CaptureReceipt:
    """The receipt of a capture the inbox keeps for the next pass, which says so: without the gap a pass that met a
    busy writer, or ran out of time, would say nothing."""
    return CaptureReceipt(
        "queued",
        (),
        "queued",
        "pending",
        "pending",
        (*prepared.gaps, INGRESS_PENDING_GAP),
        code,
        masked_secret_count=prepared.masked_secret_count,
    )


def _commit(storage, clock, context, token, prepared, scope_id, policy, deadline, *, path="original"):
    try:
        receipt = record_event(
            storage,
            clock,
            context,
            {},
            scope_id=scope_id,
            admission_policy=policy,
            remaining_seconds=deadline - time.monotonic(),
            _prepared=prepared,
            _inbox_token=token,
        )
        if receipt.disposition == "conflict" and path != "normalized":
            stored = _as_stored(storage, clock, context, prepared, (token, scope_id), policy, deadline)
            if stored is not None:
                return stored
        if receipt.disposition in {"conflict", "cancelled"}:
            # A capture that conflicts under the key it was given for its content stays final: written back as a
            # bare conflict, it would be given the same key and refused again on every pass.  With no time left for
            # that write the next pass meets the same refusal and writes it then.
            code = _REKEYED_CONFLICT if path == "rekeyed" and receipt.disposition == "conflict" else receipt.error_code
            try:
                with storage.write(context, remaining_seconds=deadline - time.monotonic()) as tx:
                    tx._check(write=True).execute(
                        "UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token)
                    )
            except (*_TRANSIENT, ContractError):
                pass
            return replace(receipt, durability="queued") if receipt.disposition == "conflict" else receipt
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
                with storage.write(context, remaining_seconds=deadline - time.monotonic()) as tx:
                    tx._check(write=True).execute(
                        "UPDATE capture_inbox SET last_error_code=? WHERE token=?", (_terminal_code(exc), token)
                    )
            except (*_TRANSIENT, ContractError):
                pass
    except _TRANSIENT:
        code = "STORAGE_UNAVAILABLE"
    if code in _PASSING:
        return _waiting(prepared, code)
    return CaptureReceipt(
        "queued",
        (),
        "queued",
        "pending",
        "pending",
        prepared.gaps,
        code,
        masked_secret_count=prepared.masked_secret_count,
    )


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
        with storage.write(context, remaining_seconds=deadline - time.monotonic()) as tx:
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
        masked_secret_count=prepared.masked_secret_count,
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
    (``events.prepare_capture``).  Were only the segment's key changed, the
    segments would meet the first message's group again and never be stored.
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
    or more would go past it and be refused on every pass.  The fingerprint covers the whole original key, so the
    cut one stays unique."""
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
    took = 0.0
    for row in rows:
        # A row is reckoned by the one before it: started with less time left than that one took, on a store that slow
        # its commit would run past the replay's time.  The rest of the page waits for the next pass.
        if not fits(deadline, took):
            break
        began = time.monotonic()
        receipt, stop = _replay_row(storage, clock, context, row, authorize, admission_policy, deadline, rekey=rekey)
        receipts.append(receipt)
        if stop:
            break
        took = time.monotonic() - began
    return tuple(receipts)


def _replay_row(storage, clock, context, row, authorize, admission_policy, deadline, *, rekey):
    """One row of a replay's page: its receipt, and whether the rest of the page waits for the next pass."""
    try:
        revalidated = _revalidated(storage, context, row, authorize, deadline, rekey=False)
        stored = None
        if rekey and revalidated is not None:
            stored = _as_stored(
                storage, clock, *revalidated, (row["token"], row["scope_id"]), admission_policy, deadline
            )
            if stored is None:
                revalidated = _revalidated(storage, context, row, authorize, deadline, rekey=True)
    except _TRANSIENT:
        # The store itself: the rest of the page waits for the next pass.  Raised, it would lose what this replay had
        # done, and the rekey replay after it would not run.
        return _waiting(PreparedCapture((), ()), "STORAGE_UNAVAILABLE"), True
    except ContractError as exc:
        if exc.code == "DEADLINE_EXCEEDED":
            # The store refused a call the replay had no time left for: this row waits with the rest, its tries
            # untouched, as it would behind a busy writer.
            return _waiting(PreparedCapture((), ()), exc.code), True
        return _defer(storage, clock, context, row, exc, deadline, path="rekey" if rekey else "replay"), False
    except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
        # A host's check that raises (Hermes' identity errors are RuntimeErrors) puts off this row only.
        return _defer(storage, clock, context, row, exc, deadline, path="rekey" if rekey else "replay"), False
    if stored is not None:
        return stored, False
    if revalidated is None:
        return (
            CaptureReceipt("cancelled", (), "not_persisted", "unchanged", "unchanged", error_code="ACCESS_DENIED"),
            False,
        )
    original, prepared = revalidated
    receipt = _commit(
        storage,
        clock,
        original,
        row["token"],
        prepared,
        row["scope_id"],
        admission_policy,
        deadline,
        path="rekeyed" if rekey else "original",
    )
    return receipt, False


def _as_stored(storage, clock, context, prepared, ingress, policy, deadline) -> CaptureReceipt | None:
    """A conflicted capture committed as the message its key holds, when it is that message dated anew.

    A host can send a message again with a moment of its own (dsh's plugin sends a turn's reply with each later Stop
    that carries its line): the same words under the same key, refused on the moment alone.  Each part whose key holds
    a message of its role, origin and words takes that message's moment, and the capture commits as the message already
    stored and leaves the inbox; re-keyed, it would be stored a second time.  Its receipt, or None when a part's key
    holds nothing or other words, or the capture differs in more than its moment: a key collision, which is re-keyed.
    """
    token, scope_id = ingress
    events = []
    with storage.read(context, remaining_seconds=deadline - time.monotonic()) as tx:
        for event in prepared.events:
            stored = tx.sources.source_by_event_key(event["source_event_key"], event["source_revision"])
            words = hashlib.sha256(event["content"].encode("utf-8")).hexdigest()
            if stored is None or (stored.event["role"], stored.event["origin"], stored.content_sha256) != (
                event["role"],
                event["origin"],
                words,
            ):
                return None
            events.append(
                {**event, "occurred_at": stored.event["occurred_at"], "time_precision": stored.event["time_precision"]}
            )
    receipt = _commit(
        storage,
        clock,
        context,
        token,
        # Dated with the stored moment, the parts are checked against private intervals at that moment.
        replace(prepared, events=tuple(events), private_revision=None),
        scope_id,
        policy,
        deadline,
        path="normalized",
    )
    return None if receipt.disposition == "conflict" else receipt


def stored_context(binding, payload, *, allowed_scope_ids: frozenset[str] | None = None) -> TrustedContext:
    """The context a waiting capture was said in, from what ``context_payload`` kept, under ``binding``, with the
    scopes ``allowed_scope_ids`` names (those it kept, when None)."""
    try:
        raw = dict(payload)
        kept = frozenset(raw.pop("allowed_scope_ids"))
        snapshot = raw.pop("display_snapshot")
        principal = raw.pop("source_principal", None)
        return TrustedContext(
            binding,
            allowed_scope_ids=kept if allowed_scope_ids is None else allowed_scope_ids,
            display_snapshot=DisplaySnapshot(snapshot["order"], tuple(ArtifactVersion(**i) for i in snapshot["items"]))
            if snapshot
            else None,
            source_principal=TrustedSourcePrincipal(**principal) if principal is not None else None,
            project_root=raw.pop("project_root", None),
            **raw,
        )
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        # A field a newer release wrote into the stored context, or one this release needs and it lacks: named, where
        # a bare TypeError would say nothing of where to look.
        raise ContractError("INPUT_INVALID", "ingress_context") from exc


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
        with storage.write(context, remaining_seconds=deadline - time.monotonic()) as tx:
            tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (row["token"],))
        return None
    original = stored_context(context.binding, body["context"], allowed_scope_ids=allowed)
    # Revalidate the stored envelope; trust is from the captured context,
    # never inferred from a payload role or text claiming to be a user.
    capture = _capture_fingerprint(body["events"]) if rekey else ""
    events = []
    for event in body["events"]:
        checked = prepare_capture(_rekeyed_event(event, capture) if rekey else event, original)
        if checked.rejection:
            raise ContractError("ACCESS_DENIED", "ingress_payload")
        events.extend(checked.events)
    count = body.get("masked_secret_count", 0)
    if type(count) is not int or count < 0:
        raise ContractError("INPUT_INVALID", "masked_secret_count")
    return original, PreparedCapture(tuple(events), tuple(body["gaps"]), masked_secret_count=count)


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
        with storage.write(context, remaining_seconds=deadline - time.monotonic()) as tx:
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
