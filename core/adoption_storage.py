"""Adoption bindings in SQLite; authority remains rooted in the owner source.

Fingerprints grant authority only to the exact decision prepared for these live
source versions. Text stays in source_events and the existing evidence links.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from ..contracts import ContractError
from .adoption import fragments


@dataclass(frozen=True)
class AdoptionContext:
    """Adjacent source versions and the assistant's exact numbered slices."""

    confirmation: Any
    assistant: Any
    fragments: tuple[dict, ...]
    assistant_ref: str
    assistant_revision: int


@dataclass(frozen=True)
class PreparedAdoption:
    """A selection revalidated under the publication writer lease."""

    snapshot: AdoptionContext
    selection: dict | None = None
    proposal: dict | None = None
    reason: str | None = None
    support: Any = None
    entry_fence: Any = None


def _owner_bound(tx, owner, assistant):
    """Use the host's shared ownership proof rather than deriving another principal identity."""
    resolver = tx.context.owner_resolver
    shared = tx.context.binding.installation_kind == "shared"
    if resolver is None:
        return not shared
    if assistant is None:
        return False
    sources = (owner, assistant)
    principals = [source.event.get("source_principal") for source in sources]
    if shared and any(
        p is None or p.get("kind") != kind for p, kind in zip(principals, ("human", "assistant"), strict=True)
    ):
        return False
    memo = {}
    identities = [
        resolver(source.entry_id, principal, memo) for source, principal in zip(sources, principals, strict=True)
    ]
    return identities[0] is not None and identities[0] == identities[1]


def adoption_context(tx, owner):
    """Find the preceding assistant occurrence in this session and audience.

    First capture orders occurrences: an edit appended later cannot masquerade
    as a new turn before confirmation. Deleted rows identify an unavailable
    predecessor but lend no words. Imported and reinjected approvals cannot bind.
    """
    if owner.event["origin"] != "human_direct" or owner.event["role"] != "user":
        return None
    conn = tx._check()
    row = conn.execute(
        """SELECT event_id,max(source_revision) AS revision,origin FROM source_events
        WHERE session_id=? AND scope_id=? AND project_id IS ? AND branch_id IS ? AND entry_id=?
        AND event_id!=? GROUP BY event_id HAVING min(source_id)<
        (SELECT min(source_id) FROM source_events WHERE event_id=?)
        ORDER BY min(source_id) DESC LIMIT 1""",
        (owner.session_id, owner.scope_id, owner.project_id, owner.branch_id, owner.entry_id, owner.ref, owner.ref),
    ).fetchone()
    if row is None or row["origin"] != "assistant_visible":
        return None
    revision = conn.execute(
        """SELECT max(source_revision) FROM source_events WHERE event_id=? AND source_id<
        (SELECT min(source_id) FROM source_events WHERE event_id=?)""",
        (row["event_id"], owner.ref),
    ).fetchone()[0]
    assistant = tx.source(row["event_id"], revision)
    if not _owner_bound(tx, owner, assistant):
        return None
    parts = fragments(assistant.event["content"]) if assistant is not None else ()
    return AdoptionContext(owner, assistant, parts, row["event_id"], revision)


def source_reason(tx, snapshot):
    """Reject missing, superseded, held, muted or incomplete source versions."""
    if not _owner_bound(tx, snapshot.confirmation, snapshot.assistant):
        return "owner_unbound"
    for source in (snapshot.confirmation, snapshot.assistant):
        if source is None:
            return "source_missing"
        try:
            tx.claims.require_live_source(source.ref, source.revision)
            current = tx.source(source.ref, source.revision)
        except ContractError as exc:
            return "source_changed" if exc.code == "VERSION_CONFLICT" else "source_missing"
        if current is None or current.suppressed:
            return "source_missing"
        if (
            current.content_sha256 != source.content_sha256
            or current.capture_gaps
            or current.event["capture_state"] != "complete"
        ):
            return "source_incomplete"
    return None


def binding_reason(tx, snapshot, selection):
    """Validate exact slices, owner conditions and session-wide label uniqueness."""
    reason = source_reason(tx, snapshot)
    if reason is not None:
        return reason
    owner, assistant = snapshot.confirmation, snapshot.assistant
    parts = [p for p in snapshot.fragments if p["id"] in selection["fragment_ids"]]
    if not parts or any(assistant.event["content"][p["start"] : p["end"]] != p["text"] for p in parts):
        return "fragment_changed"
    if any(condition not in owner.event["content"] for condition in selection["conditions"]):
        return "condition_unbound"
    labels = _binding_labels(snapshot, selection)
    if labels is None:
        return "reference_unbound"
    rows = (
        tx._check()
        .execute(
            """SELECT e.content FROM source_events e WHERE e.session_id=? AND e.scope_id=? AND e.project_id IS ?
        AND e.branch_id IS ? AND e.entry_id=? AND e.origin='assistant_visible'
        AND e.source_id<(SELECT min(source_id) FROM source_events WHERE event_id=?)
        AND NOT EXISTS(SELECT 1 FROM source_events n WHERE n.event_id=e.event_id
                       AND n.source_revision>e.source_revision)""",
            (owner.session_id, owner.scope_id, owner.project_id, owner.branch_id, owner.entry_id, owner.ref),
        )
        .fetchall()
    )
    if not labels and len(rows) != 1:
        return "ambiguous_reference"
    for label in labels:
        if sum(_label_groups(row[0], label) for row in rows) != 1:
            return "ambiguous_reference"
    quote = selection.get("confirmation_quote")
    if quote is not None and owner.event["content"].count(quote) != 1:
        return "confirmation_unbound"
    return None


def _binding_labels(snapshot, selection):
    """Recover structural labels even when a model omits an option's label.

    Parsing titles and list numbering locates text; it never decides whether
    a person adopted it. Body fragments inherit their preceding section label.
    """
    label = selection["reference_label"]
    chosen = [p for p in snapshot.fragments if p["id"] in selection["fragment_ids"]]
    if label and not any(label in p["text"] for p in chosen):
        return None
    labels = {label} if label else set()
    current = None
    for part in snapshot.fragments:
        title = _section_label(part["text"])
        if title:
            current = title
        if part in chosen and current:
            labels.add(current)
    return tuple(sorted(labels))


def _section_label(text):
    """Read a structural title or list label, retaining the source spelling."""
    title = re.sub(r"^[-*+]\s+", "", text.splitlines()[0].strip())
    heading = re.match(r"^#{1,6}\s+(.+)$", title)
    title = heading[1] if heading else title
    numbered = re.match(r"^(\d+[.)])\s+(.+)$", title)
    title = numbered[2] if numbered else title
    # Display prefixes do not give a repeated named option a different identity.
    prose = re.match(
        r"^((?:方案|选项|计划)\s*[一二三四五六七八九十百零〇两0-9]+|(?:Option|Plan)\s+[A-Z0-9]+)\s*(?:是|为|is\b)",
        title,
        re.I,
    )
    if prose:
        return prose[1].strip()
    colon = re.match(r"^([^:：\n]{1,80})[:：]", title)
    if colon:
        return colon[1].strip()
    return numbered[1] if numbered else title if heading else None


def _label_groups(content, label):
    """Count proposal labels, not repetitions in their bodies or recommendations.

    Without structural titles, literal occurrences remain a conservative
    ambiguity fence. A title annotation can follow its label after punctuation.
    """
    headings = [title for part in fragments(content) if (title := _section_label(part["text"]))]
    if not headings:
        return content.count(label)
    return sum(title == label or (title.startswith(label) and title[len(label)] in " (（[-—") for title in headings)


def proposal_digest(proposal):
    """Bind a proof to one complete proposal without storing a parallel text copy."""
    return hashlib.sha256(
        json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def record_binding(tx, adoption, *, reason, now):
    """Record a validated binding or an owner-visible unresolved relationship."""
    s, selection = adoption.snapshot, dict(adoption.selection or {})
    selection["positions"] = [
        dict(id=p["id"], start=p["start"], end=p["end"])
        for p in s.fragments
        if p["id"] in selection.get("fragment_ids", ())
    ]
    owner = s.confirmation
    tx._check(write=True).execute(
        """INSERT INTO adoption_relations(confirmation_ref,confirmation_revision,assistant_ref,assistant_revision,
        scope_id,project_id,branch_id,scope,selection_json,state,reason,proposal_sha256,recorded_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(confirmation_ref,confirmation_revision) DO UPDATE SET
        selection_json=excluded.selection_json,state=excluded.state,reason=excluded.reason,
        proposal_sha256=excluded.proposal_sha256""",
        (
            owner.ref,
            owner.revision,
            s.assistant_ref,
            s.assistant_revision,
            owner.scope_id,
            owner.project_id,
            owner.branch_id,
            selection.get("scope"),
            json.dumps(selection, ensure_ascii=False),
            "pending_review" if reason else "bound",
            reason,
            proposal_digest(adoption.proposal) if adoption.proposal is not None and reason is None else None,
            now,
        ),
    )


def adoption_qualification(tx, proposal):
    """Grant decision authority only for an exact binding whose sources still hold."""
    from .evidence_support import support_for_contents

    contents = {
        (span["source_ref"], span["source_revision"]): source.event["content"]
        for span in proposal["evidence_spans"]
        if (source := tx.source(span["source_ref"], span["source_revision"])) is not None
    }
    verdict = support_for_contents(proposal, contents)
    if proposal["kind"] != "decision" or verdict is None or verdict.state != "supported":
        return False
    row = (
        tx._check()
        .execute(
            "SELECT * FROM adoption_relations WHERE state='bound' AND proposal_sha256=?",
            (proposal_digest(proposal),),
        )
        .fetchone()
    )
    if row is None:
        return False
    owner = tx.source(row["confirmation_ref"], row["confirmation_revision"])
    if owner is None:
        return False
    snapshot = adoption_context(tx, owner)
    return (
        snapshot is not None
        and (snapshot.assistant_ref, snapshot.assistant_revision) == (row["assistant_ref"], row["assistant_revision"])
        and binding_reason(tx, snapshot, json.loads(row["selection_json"])) is None
    )


def purge_adoptions(conn, kind, ref):
    """Erase relationships when either source or its decision is physically purged."""
    if kind == "event":
        conn.execute("DELETE FROM adoption_processing WHERE confirmation_ref=? OR assistant_ref=?", (ref, ref))
        conn.execute("DELETE FROM adoption_relations WHERE confirmation_ref=? OR assistant_ref=?", (ref, ref))
    elif kind == "claim":
        conn.execute("DELETE FROM adoption_relations WHERE claim_ref=?", (ref,))


def invalidate_adoptions(conn, ref, revision):
    """Expose decisions whose source changed as unresolved; live evidence fences hide them."""
    conn.execute(
        """UPDATE adoption_relations SET state='pending_review',reason='source_changed',proposal_sha256=NULL
        WHERE (confirmation_ref=? AND confirmation_revision<?) OR (assistant_ref=? AND assistant_revision<?)""",
        (ref, revision, ref, revision),
    )


def adopted_source(tx, claim_ref, claim_revision):
    """Read the assistant parent already represented by an adopted decision's selected quotes."""

    def load():
        row = (
            tx._check()
            .execute(
                "SELECT assistant_ref,assistant_revision FROM adoption_relations "
                "WHERE claim_ref=? AND claim_revision=? AND state='bound'",
                (claim_ref, claim_revision),
            )
            .fetchone()
        )
        return tuple(row) if row is not None else None

    return tx.remembered(("adopted_source", claim_ref, claim_revision), load)


def is_adopted_assistant(tx, ref, revision):
    """Whether a raw assistant parent is represented by a bound adoption's selected slices."""
    return (
        tx._check()
        .execute(
            "SELECT 1 FROM adoption_relations WHERE assistant_ref=? AND assistant_revision=? AND state='bound' LIMIT 1",
            (ref, revision),
        )
        .fetchone()
        is not None
    )
