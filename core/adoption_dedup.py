"""Conservative deduplication limited to one bound adoption clause and decision topic."""

from __future__ import annotations

import json

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
    """Retire earlier ordinary duplicates through the append-only fact writer."""
    owner = adoption.snapshot.confirmation
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
            tx.claims.append(
                head.scope_id,
                head.payload,
                Qualification("retracted", head.basis, "adoption_duplicate"),
                recorded_at=now,
                previous=head,
            )
