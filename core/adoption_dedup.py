"""Conservative deduplication limited to one bound adoption clause and decision topic."""

from __future__ import annotations

import json
from typing import Any

from ..contracts import ContractError
from .adoption_storage import PreparedAdoption, adoption_context, binding_reason
from .claims import Qualification
from .fact_identity import normalize_fact_component


def same_adoption(proposal, adoption):
    """Require both exact owner-clause coverage and the same decision matter.

    A whole-message citation cannot erase a separate owner decision. When a
    model omits a clause location or uses a different topic, preserve the proposal
    unless its value literally names the selected option in that same clause.
    """
    if proposal["kind"] != "decision" or sorted(proposal["conditions"]) != sorted(adoption.proposal["conditions"]):
        return False
    owner = adoption.snapshot.confirmation
    content = owner.event["content"]
    quote = adoption.selection.get("confirmation_quote", content)
    if content.count(quote) != 1:
        return False
    start, end = content.index(quote), content.index(quote) + len(quote)
    spans = proposal["evidence_spans"]
    if not spans or any(
        (s["source_ref"], s["source_revision"]) != (owner.ref, owner.revision)
        or not start <= s.get("start", -1) < s.get("end", -1) <= end
        for s in spans
    ):
        return False
    same_topic = normalize_fact_component(proposal["predicate"]) == normalize_fact_component(
        adoption.proposal["predicate"]
    )
    label = adoption.selection["reference_label"]
    named_value = bool(label and label in proposal["value_text"] and proposal["value_text"] in quote)
    return (same_topic and "confirmation_quote" in adoption.selection) or named_value


def bound_adoptions(tx, proposal):
    """Load only still-live bound decisions relevant to these owner evidence sources."""
    for span in proposal["evidence_spans"]:
        row = (
            tx._check()
            .execute(
                "SELECT * FROM adoption_relations WHERE confirmation_ref=? "
                "AND confirmation_revision=? AND state='bound'",
                (span["source_ref"], span["source_revision"]),
            )
            .fetchone()
        )
        if row is None or row["claim_ref"] is None:
            continue
        owner = tx.source(row["confirmation_ref"], row["confirmation_revision"])
        snapshot = adoption_context(tx, owner) if owner else None
        selection = json.loads(row["selection_json"])
        if snapshot is None or binding_reason(tx, snapshot, selection) is not None:
            continue
        claim = next(
            (
                v
                for v in tx.claims.versions(row["claim_ref"])
                if v.revision == v.current_revision and v.state == "active"
            ),
            None,
        )
        if claim is not None:
            yield PreparedAdoption(snapshot, selection, claim.payload)


def is_bound_duplicate(tx, proposal):
    """Filter ordinary proposals only after a supported adoption has been published."""
    return any(same_adoption(proposal, a) for a in bound_adoptions(tx, proposal))


def retire_duplicates(tx, adoption, adopted_ref, *, now):
    """Retire earlier ordinary duplicates and return their reversible version receipts."""
    owner = adoption.snapshot.confirmation
    retired = []
    rows = (
        tx._check()
        .execute(
            "SELECT DISTINCT object_ref FROM evidence_links WHERE source_ref=? "
            "AND source_revision=? AND object_kind='claim'",
            (owner.ref, owner.revision),
        )
        .fetchall()
    )
    for row in rows:
        if row[0] == adopted_ref:
            continue
        head = next((v for v in tx.claims.versions(row[0]) if v.revision == v.current_revision), None)
        if (
            head is not None
            and head.state in {"active", "proposed", "disputed"}
            and same_adoption(head.payload, adoption)
        ):
            saved = tx.claims.append(
                head.scope_id,
                head.payload,
                Qualification("retracted", head.basis, "adoption_duplicate"),
                recorded_at=now,
                previous=head,
            )
            retired.append(dict(ref=head.ref, before=head.revision, revision=saved.revision))
    return retired


def approve_deferred_adoption(tx, pending, approved, *, now) -> dict[str, Any]:
    """Apply adoption deduplication only after the owner publishes its held claim."""
    row = (
        tx._check()
        .execute(
            "SELECT confirmation_ref,confirmation_revision FROM adoption_relations "
            "WHERE state='bound' AND claim_ref=? AND claim_revision=?",
            (pending.ref, pending.revision),
        )
        .fetchone()
    )
    if row is None:
        return {}
    adoption = next(
        (
            item
            for item in bound_adoptions(tx, approved.payload)
            if item.snapshot.confirmation.ref == row[0] and item.proposal == approved.payload
        ),
        None,
    )
    if adoption is None:
        raise ContractError("VERSION_CONFLICT", "adoption_dependencies")
    retired = retire_duplicates(tx, adoption, approved.ref, now=now)
    tx._check(write=True).execute(
        "UPDATE adoption_relations SET claim_revision=? WHERE confirmation_ref=? AND confirmation_revision=?",
        (approved.revision, row[0], row[1]),
    )
    from .session_brief import rebuild_scope

    rebuild_scope(tx, pending.scope_id, now)
    return dict(
        heads={v["ref"]: v["revision"] for v in retired},
        before_heads={v["ref"]: v["before"] for v in retired},
        adoption_bindings=[
            dict(
                confirmation_ref=row[0],
                confirmation_revision=row[1],
                claim_ref=approved.ref,
                before=pending.revision,
                revision=approved.revision,
            )
        ],
    )


def restore_reviewed_adoptions(tx, bindings, *, now):
    """Restore the exact adoption pointers together with the approval's fact heads."""
    for item in bindings:
        changed = (
            tx._check(write=True)
            .execute(
                "UPDATE adoption_relations SET claim_revision=? WHERE confirmation_ref=? AND confirmation_revision=? "
                "AND claim_ref=? AND claim_revision=? AND state='bound'",
                (
                    item["before"],
                    item["confirmation_ref"],
                    item["confirmation_revision"],
                    item["claim_ref"],
                    item["revision"],
                ),
            )
            .rowcount
        )
        if changed != 1:
            raise ContractError("VERSION_CONFLICT", "adoption_revision")
    if bindings:
        from .session_brief import rebuild_scope

        version = tx.claims.version(bindings[0]["claim_ref"], bindings[0]["before"])
        rebuild_scope(tx, version.scope_id, now)
