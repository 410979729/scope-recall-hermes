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

from ..contracts import (
    ArtifactVersion,
    ContractError,
    DisplaySnapshot,
    TrustedContext,
    TrustedSourcePrincipal,
)
from .truth_connection import TruthDatabaseConnectionError
from .writer_lease import TruthWriterBusyError
from .capture import CaptureReceipt, record_event
from .events import PreparedCapture, prepare_capture

_TRANSIENT = (sqlite3.Error, TruthDatabaseConnectionError, TruthWriterBusyError)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _context_payload(context):
    if context.import_provenance is not None or context.actor_origin == "imported":
        raise ContractError("ACCESS_DENIED", "ingress_import_attestation")
    return dict(session_id=context.session_id, actor_origin=context.actor_origin,
                allowed_scope_ids=sorted(context.allowed_scope_ids), project_id=context.project_id,
                branch_id=context.branch_id, task_anchor=context.task_anchor,
                environment_revision=context.environment_revision,
                source_principal=(context.source_principal.to_payload()
                                  if context.source_principal is not None else None),
                display_snapshot=context.display_snapshot.to_payload() if context.display_snapshot else None)


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


def _commit(storage, clock, context, token, prepared, scope_id, policy, deadline):
    try:
        receipt = record_event(storage, clock, context, {}, scope_id=scope_id,
                               admission_policy=policy, remaining_seconds=max(.001, deadline-time.monotonic()),
                               _prepared=prepared, _inbox_token=token)
        if receipt.disposition in {"conflict", "cancelled"}:
            with storage.write(context, remaining_seconds=max(.001, deadline-time.monotonic())) as tx:
                tx._check(write=True).execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (receipt.error_code, token))
            return receipt
        if receipt.durability == "persisted":
            return receipt
        code = receipt.error_code or "STORAGE_UNAVAILABLE"
    except ContractError as exc:
        code = exc.code
        # Terminal failures remain inspectable, but are not replayed forever.
        try:
            with storage.write(context, remaining_seconds=max(.001, deadline-time.monotonic())) as tx:
                tx._check(write=True).execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
        except (*_TRANSIENT, ContractError):
            pass
    except _TRANSIENT:
        code = "STORAGE_UNAVAILABLE"
    return CaptureReceipt("queued", (), "queued", "pending", "pending", prepared.gaps, code)


#: Marker spliced into a re-keyed capture's source_event_key. Self-documenting on
#: purpose: the stored key keeps the host's original and appends the content
#: fingerprint, so the collision stays visible to anyone reading the row rather
#: than being filed in a side table nobody queries.
REKEY_MARKER = "#rekey:"


def _rekeyed_event(event: dict) -> dict:
    """Give one capture an identity derived from its own content.

    A host that reuses a turn number sends a second, different message under a
    key that already exists. Storage refuses it — same event id and revision,
    different fingerprint — and neither obvious escape is right: leaving it
    refused loses the message, while storing it as the next revision would brand
    it a revision of an unrelated message and hide the first one, since lexical
    retrieval only returns the newest revision of a group.

    Distinct content therefore earns a distinct identity. The derivation is
    deterministic, so repairing the same payload twice is idempotent.
    """
    original = str(event["source_event_key"])
    if REKEY_MARKER in original:
        return dict(event)
    fingerprint = hashlib.sha256(
        _json([original, event.get("content"), event.get("origin"), event.get("role"),
               event.get("occurred_at")]).encode("utf-8")
    ).hexdigest()[:16]
    return {**event, "source_event_key": f"{original}{REKEY_MARKER}{fingerprint}"}


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
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        rows = tx._check().execute(f"""SELECT * FROM capture_inbox WHERE scope_id IN ({','.join('?' for _ in scopes)})
            AND project_id IS ? AND branch_id IS ? AND last_error_code='VERSION_CONFLICT'
            ORDER BY created_at,token LIMIT ?""", (*scopes, context.project_id, context.branch_id, limit)).fetchall()
    receipts = []
    for row in rows:
        if time.monotonic() >= deadline:
            break
        body = json.loads(row["payload_json"])
        raw = dict(body["context"])
        allowed = frozenset(raw.pop("allowed_scope_ids")) & context.allowed_scope_ids & frozenset(authorize(body["host_scope"]))
        if row["scope_id"] not in allowed:
            with storage.write(context, remaining_seconds=max(.001, deadline-time.monotonic())) as tx:
                tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (row["token"],))
            receipts.append(CaptureReceipt("cancelled", (), "not_persisted", "unchanged", "unchanged", error_code="ACCESS_DENIED"))
            continue
        snapshot = raw.pop("display_snapshot")
        principal = raw.pop("source_principal", None)
        original = TrustedContext(context.binding, allowed_scope_ids=allowed,
            display_snapshot=DisplaySnapshot(snapshot["order"], tuple(ArtifactVersion(**i) for i in snapshot["items"])) if snapshot else None,
            source_principal=TrustedSourcePrincipal(**principal) if principal is not None else None,
            **raw)
        events = []
        for event in body["events"]:
            checked = prepare_capture(_rekeyed_event(event), original)
            if checked.rejection:
                raise ContractError("ACCESS_DENIED", "ingress_payload")
            events.extend(checked.events)
        receipts.append(_commit(storage, clock, original, row["token"], PreparedCapture(tuple(events), tuple(body["gaps"])),
                                row["scope_id"], admission_policy, deadline))
    return tuple(receipts)


def replay_inbox(storage, clock, context, *, authorize, admission_policy=None, limit=8, remaining_seconds=1.0):
    """Replay only the caller's partition; the callback verifies current host ACLs."""
    if not 1 <= limit <= 32:
        raise ContractError("INPUT_INVALID", "ingress_limit")
    deadline = time.monotonic() + remaining_seconds
    scopes = tuple(sorted(context.allowed_scope_ids))
    if not scopes:
        return ()
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        rows = tx._check().execute(f"""SELECT * FROM capture_inbox WHERE scope_id IN ({','.join('?' for _ in scopes)})
            AND project_id IS ? AND branch_id IS ?
            AND (last_error_code IS NULL OR last_error_code IN ('STORAGE_UNAVAILABLE','DEADLINE_EXCEEDED'))
            ORDER BY created_at,token LIMIT ?""", (*scopes, context.project_id, context.branch_id, limit)).fetchall()
    receipts = []
    for row in rows:
        if time.monotonic() >= deadline:
            break
        body = json.loads(row["payload_json"])
        raw = dict(body["context"])
        allowed = frozenset(raw.pop("allowed_scope_ids")) & context.allowed_scope_ids & frozenset(authorize(body["host_scope"]))
        if row["scope_id"] not in allowed:
            with storage.write(context, remaining_seconds=max(.001, deadline-time.monotonic())) as tx:
                tx._check(write=True).execute("DELETE FROM capture_inbox WHERE token=?", (row["token"],))
            receipts.append(CaptureReceipt("cancelled", (), "not_persisted", "unchanged", "unchanged", error_code="ACCESS_DENIED"))
            continue
        snapshot = raw.pop("display_snapshot")
        principal = raw.pop("source_principal", None)
        original = TrustedContext(context.binding, allowed_scope_ids=allowed,
            display_snapshot=DisplaySnapshot(snapshot["order"], tuple(ArtifactVersion(**i) for i in snapshot["items"])) if snapshot else None,
            source_principal=TrustedSourcePrincipal(**principal) if principal is not None else None,
            **raw)
        # Revalidate the stored envelope; trust is from the captured context,
        # never inferred from a payload role or text claiming to be a user.
        events = []
        for event in body["events"]:
            checked = prepare_capture(event, original)
            if checked.rejection:
                raise ContractError("ACCESS_DENIED", "ingress_payload")
            events.extend(checked.events)
        receipts.append(_commit(storage, clock, original, row["token"], PreparedCapture(tuple(events), tuple(body["gaps"])),
                                row["scope_id"], admission_policy, deadline))
    return tuple(receipts)
