"""Bounded entity candidates over the fact merge lookup and existing vector port."""

from __future__ import annotations

from .claims import evidence_refs, valid_at


def entity_candidates(tx, source, *, now: str, vector_hits=(), name=None):
    """Return source-backed facts from the exact audience, nearest names first.

    Similarity only nominates candidates. Neither spelling nor a vector score
    authorizes a merge, including when two people have exactly the same name.
    """
    name = source.payload["subject"] if name is None else name
    vector_refs = []
    for hit in vector_hits:
        if hit.kind == "claim":
            vector_refs.append(hit.ref)
        elif hit.kind == "event":
            vector_refs.extend(
                row[0]
                for row in tx._check().execute(
                    "SELECT object_ref FROM evidence_links WHERE object_kind='claim' "
                    "AND source_ref=? AND source_revision=? LIMIT 16",
                    (hit.ref, hit.revision),
                )
            )
    refs = tx.claims.merge_lookup(
        source.scope_id,
        kind="fact",
        options=dict(
            entity=(
                name,
                tuple(vector_refs[:64]),
                " ".join(span["quote"] for span in source.payload["evidence_spans"]),
            )
        ),
    )
    source_row = tx._check().execute("SELECT rowid FROM claims WHERE claim_id=?", (source.ref,)).fetchone()[0]
    found = []
    for ref in refs:
        versions = tx.claims.versions(ref)
        head = next((v for v in versions if v.revision == v.current_revision), None)
        if head is None or head.ref == source.ref or head.state != "active" or head.suppressed:
            continue
        # One deterministic direction prevents concurrent passes from building cycles.
        target_row = tx._check().execute("SELECT rowid FROM claims WHERE claim_id=?", (head.ref,)).fetchone()[0]
        if target_row >= source_row or head.payload["subject"] == name:
            continue
        if not valid_at(head, now, historical=False) or head.payload.get("conditions"):
            continue
        tx.claims.live_sources(evidence_refs(head.payload), head.scope_id)
        found.append(dict(ref=head.ref, revision=head.revision, payload=head.payload))
    return tuple(found)
