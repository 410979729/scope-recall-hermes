"""Read-time dependency fences for cross-session inductive observations."""

from __future__ import annotations

import hashlib
import json

from ..contracts import ContractError
from .recall_policy import meaningful_query_terms
from .source_qualification import POLARITY
from .summary_validity import group_signature, members_in_scope

OBSERVATION_REASONS = ("reflection_pending", "grounded_observation", "confirmed_observation")


def live_members(tx, members):
    """Resolve exact source versions under current authorization, deletion and safety policy."""
    sources = []
    for member in members:
        source = tx.source(member["ref"], member["revision"])
        tx.claims.require_live_source(member["ref"], member["revision"])
        if source is None or source.suppressed or source.capture_gaps:
            raise ContractError("SOURCE_MISSING", "reflection_source")
        if hashlib.sha256(source.event["content"].encode()).hexdigest() != member["digest"]:
            raise ContractError("VERSION_CONFLICT", "reflection_source")
        sources.append(source)
    return tuple(sources)


def pending_owner_correction(tx, version):
    """Use the core's bound correction candidates, including affirmative replacements."""
    rows = (
        tx._check()
        .execute(
            "SELECT source_ref,source_revision FROM unresolved_updates WHERE state='unresolved' "
            "AND scope_id=? AND project_id IS ? AND branch_id IS ? "
            "AND EXISTS(SELECT 1 FROM json_each(candidate_refs_json) WHERE value=?) LIMIT 65",
            (version.scope_id, version.project_id, version.branch_id, version.ref),
        )
        .fetchall()
    )
    if len(rows) > 64:
        return True
    for row in rows:
        source = tx.source(*row)
        if source is not None and not source.suppressed:
            return True
    return False


def owner_conflict(tx, version, members):
    """Fail closed on later owner denials and same-slot direct reports.

    Negation is the existing language policy. Lexical overlap only suppresses
    an induction; it never establishes a new fact or grants revision authority.
    """
    if pending_owner_correction(tx, version):
        return True
    payload = version.payload
    rows = (
        tx._check()
        .execute(
            "SELECT v.payload_json FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id "
            "AND v.revision=c.current_revision WHERE c.scope_id=? AND c.project_id IS ? AND c.branch_id IS ? "
            "AND c.subject=? AND c.predicate=? AND c.kind!='observation' AND v.state='active' "
            "AND v.basis='direct_report' AND c.read_blocked=0 AND c.suppressed=0 LIMIT 65",
            (version.scope_id, version.project_id, version.branch_id, payload["subject"], payload["predicate"]),
        )
        .fetchall()
    )
    if len(rows) > 64 or any(json.loads(row[0])["value_text"] != payload["value_text"] for row in rows):
        return True
    latest = max(member["position"] for member in members)
    rows = (
        tx._check()
        .execute(
            "SELECT event_id,source_revision,content FROM source_events WHERE rowid>? AND scope_id=? "
            "AND project_id IS ? AND branch_id IS ? AND role='user' AND read_blocked=0 AND suppressed=0 "
            "ORDER BY rowid LIMIT 129",
            (latest, version.scope_id, version.project_id, version.branch_id),
        )
        .fetchall()
    )
    if len(rows) > 128:
        return True
    terms = set(meaningful_query_terms(payload["value_text"]))
    for row in rows:
        source = tx.source(row[0], row[1])
        if source is not None and POLARITY.search(row[2]) and len(terms & set(meaningful_query_terms(row[2]))) >= 2:
            return True
    return False


def reflection_live(tx, version, *, query_scope=None, excluded=(), check_conflicts=True):
    """Release only the current, supported induction with all original dependencies intact."""
    if version.payload.get("kind") != "observation":
        return True
    if version.state != "active" or version.suppressed or tx.claims.current_revision(version.ref) != version.revision:
        return False
    if version.scope_id not in tx.context.allowed_scope_ids or (version.project_id, version.branch_id) != (
        tx.context.project_id,
        tx.context.branch_id,
    ):
        return False
    row = (
        tx._check()
        .execute("SELECT * FROM observations WHERE claim_ref=? AND claim_revision=?", (version.ref, version.revision))
        .fetchone()
    )
    if row is None:
        return False
    members = json.loads(row["manifest_json"])
    if not members_in_scope(members, query_scope) or any(f"{m['ref']}@{m['revision']}" in excluded for m in members):
        return False
    try:
        live_members(tx, members)
        return group_signature(tx, members) == row["signature"] and (
            not check_conflicts or not owner_conflict(tx, version, members)
        )
    except ContractError:
        return False
