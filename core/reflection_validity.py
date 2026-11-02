"""Read-time dependency fences for cross-session inductive observations."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from ..contracts import ContractError
from . import private
from .reflection_conflicts import owner_conflict
from .summary_validity import group_signature, members_in_scope

OBSERVATION_REASONS = ("reflection_pending", "grounded_observation", "confirmed_observation")


def live_members(tx, members, *, now=None):
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
    intervals = private.active_intervals(tx, now or datetime.now(UTC).isoformat())
    if private.source_blocked(intervals, None) or any(private.source_blocked(intervals, s) for s in sources):
        raise ContractError("ACCESS_DENIED", "private_mode")
    return tuple(sources)


def dependency_signature(tx, members, *, sources=None):
    """Include relevant full-private rule changes, including rules closed before release."""
    sources = sources if sources is not None else live_members(tx, members)
    rules = private.source_rules(tx, sources)
    original = group_signature(tx, members)
    return hashlib.sha256(json.dumps([original, rules], sort_keys=True).encode()).hexdigest() if rules else original


def check_dependencies(tx, members, signature=None, *, now=None):
    """One fence for dispatch, publication, hydration, inspection and final release."""
    sources = live_members(tx, members, now=now)
    if signature is not None and dependency_signature(tx, members, sources=sources) != signature:
        raise ContractError("VERSION_CONFLICT", "reflection_dependencies")
    return sources


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
        check_dependencies(tx, members, row["signature"])
        return not check_conflicts or not owner_conflict(tx, version, members)
    except ContractError:
        return False
