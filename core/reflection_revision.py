"""Owner review changes an induction without converting it into a direct report."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from ..contracts import ContractError
from .claims import Qualification
from .reflection_validity import dependency_signature, reviewable_observation
from .secret_patterns import structure_contains_secret_like_text


def revise_observation(tx, clock, head, value, source):
    """Retain both original sessions and dependencies after a literal owner review."""
    row = (
        tx._check()
        .execute("SELECT * FROM observations WHERE claim_ref=? AND claim_revision=?", (head.ref, head.revision))
        .fetchone()
    )
    if row is None or not reviewable_observation(tx, head, check_conflicts=False):
        raise ContractError("SOURCE_MISSING", "observation_dependencies")
    retract = value is None or value == {"state": "retracted"}
    confirm = value == {"confirmed": True}
    if not retract and not confirm and (type(value) is not str or not value.strip() or len(value) > 2048):
        raise ContractError("INPUT_INVALID", "observation_value")
    if type(value) is str and value not in source.event["content"]:
        raise ContractError("DERIVATION_INVALID", "observation_owner_words")
    payload = deepcopy(head.payload)
    if type(value) is str:
        payload["value_text"] = value
    if structure_contains_secret_like_text(payload):
        raise ContractError("INPUT_INVALID", "plaintext_secret_rejected")
    payload["evidence_spans"].append(
        dict(source_ref=source.ref, source_revision=source.revision, quote=source.event["content"])
    )
    saved = tx.claims.append(
        head.scope_id,
        payload,
        Qualification("retracted" if retract else "active", "derived_summary", "confirmed_observation"),
        previous=head,
        expected_revision=head.revision,
        recorded_at=clock.utc_now(),
    )
    members = json.loads(row["manifest_json"])
    position = (
        tx._check()
        .execute(
            "SELECT rowid FROM source_events WHERE event_id=? AND source_revision=?", (source.ref, source.revision)
        )
        .fetchone()[0]
    )
    members.append(
        dict(
            ref=source.ref,
            revision=source.revision,
            position=position,
            digest=hashlib.sha256(source.event["content"].encode()).hexdigest(),
            total=len(source.event["content"]),
            session_id=source.session_id,
            entry_id=source.entry_id,
            occurred_at=source.event["occurred_at"],
        )
    )
    tx._check().execute(
        "INSERT INTO observations VALUES(?,?,?,?,1)",
        (saved.ref, saved.revision, json.dumps(members), dependency_signature(tx, members)),
    )
    tx.claims.resolve_updates(saved.ref, resolved_at=clock.utc_now())
    return saved
