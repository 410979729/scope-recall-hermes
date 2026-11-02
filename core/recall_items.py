"""Public item projection shared by retrieval admission and packet compilation."""

from __future__ import annotations

from typing import Literal, cast

from ..contracts import Basis, RecallItem, bounded_entry_labels, bounded_source_contexts
from .recall_budget import canonical_render_json
from .resume_compaction import resume_evidence_refs
from .retrieval import RetrievedObject, optional_json

PacketKind = Literal["event", "episode", "claim", "procedure", "artifact"]
_MAX_OCCURRED_AT_CHARS = 64
_BASIS = frozenset({"direct_report", "observed", "derived_summary", "inferred_suggestion", "unknown"})
_CLAIM_STATES = frozenset({"proposed", "active", "superseded", "disputed", "retracted"})


def packet_kind(obj: RetrievedObject) -> PacketKind:
    """Map a retrieved kind to the legacy public item vocabulary."""
    if obj.kind in {"event", "episode", "claim", "procedure", "artifact"}:
        return cast(PacketKind, obj.kind)
    return "event"


def packet_item(obj: RetrievedObject, *, content: dict | None = None) -> RecallItem:
    """The public item for one released object.

    ``content`` replaces an episode resume with a compact field selection; the
    item then carries every ref of every retained field, so a cross-field
    packet stays provable rather than merely plausible-looking.
    """
    evidence = list(dict.fromkeys(obj.evidence_refs)) if content is None else list(resume_evidence_refs(content))
    if not evidence:
        evidence = [f"{obj.ref}@{obj.revision}"]
    item: RecallItem = {
        "ref": obj.ref,
        "revision": obj.revision,
        "kind": packet_kind(obj),
        "content": obj.content if content is None else canonical_render_json(content),
        "temporal_status": obj.temporal_status,
        "origin": obj.origin,
        "applicability": obj.applicability,
        "evidence_refs": evidence,
        "expandable": bool(obj.expandable),
        "basis": cast(Basis, obj.basis if obj.basis in _BASIS else "unknown"),
    }
    metadata = dict(obj.metadata)
    # An unpromoted claim reaches recall labelled: temporal_status alone cannot
    # tell a lead from a retired truth, since both look "historical".
    if metadata.get("state") in _CLAIM_STATES:
        item["claim_state"] = cast(
            Literal["proposed", "active", "superseded", "disputed", "retracted"], metadata["state"]
        )
    reason = metadata.get("restatement_qualification") or metadata.get("qualification_reason")
    if reason:
        item["qualification_reason"] = str(reason)[:120]
    contexts = bounded_source_contexts(optional_json(metadata.get("source_contexts")))
    if contexts:
        item["source_contexts"] = contexts
    occurred = metadata.get("occurred_at")
    if type(occurred) is str and 0 < len(occurred) <= _MAX_OCCURRED_AT_CHARS:
        item["occurred_at"] = occurred
    entries = bounded_entry_labels(optional_json(metadata.get("entries")))
    if entries:
        item["entries"] = entries
    return item
