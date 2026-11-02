"""Owner-controlled salience metadata, independent of fact truth and recall ranking."""

from __future__ import annotations

import re

from ..contracts import ContractError


def backfill_importance(tx, *, limit=128):
    """Assign the neutral default to a bounded page of legacy facts without model calls."""
    conn = tx._check(write=True)
    rows = conn.execute(
        "SELECT claim_id FROM claims WHERE importance_assigned=0 AND scope_id IN ("
        + ",".join("?" for _ in tx.context.allowed_scope_ids)
        + ") AND project_id IS ? AND branch_id IS ? ORDER BY claim_id LIMIT ?",
        (*sorted(tx.context.allowed_scope_ids), tx.context.project_id, tx.context.branch_id, limit),
    ).fetchall()
    conn.executemany("UPDATE claims SET importance_assigned=1 WHERE claim_id=?", rows)
    return len(rows)


def set_importance(tx, head, *, importance=None, pinned=None):
    """Update metadata under the caller's existing owner authority and head-version fence."""
    tx.claims.require_target(head)
    if tx.claims.current_revision(head.ref) != head.revision:
        raise ContractError("VERSION_CONFLICT", "expected_revision")
    if importance is not None and (type(importance) is not int or not 1 <= importance <= 5):
        raise ContractError("INPUT_INVALID", "importance")
    if pinned is not None and type(pinned) is not bool:
        raise ContractError("INPUT_INVALID", "pinned")
    if importance is None and pinned is None:
        raise ContractError("INPUT_INVALID", "importance")
    score = head.importance if importance is None else importance
    pin = head.pinned if pinned is None else pinned
    tx._check(write=True).execute(
        "UPDATE claims SET importance=?,pinned=?,importance_assigned=1,importance_owner=1 WHERE claim_id=?",
        (5 if pin else score, int(pin), head.ref),
    )
    tx._check().execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")


def require_importance_statement(raw, changes):
    """Bind tool metadata to literal owner wording instead of letting the model choose it."""
    if "importance" in changes:
        score = changes["importance"]
        if not re.search(rf"(?:重要度|importance)\s*(?:为|设为|改为|[=:：]|to|is)?\s*{score}\b", raw, re.I):
            raise ContractError("ACCESS_DENIED", "importance_not_bound")
    if "pinned" in changes:
        pattern = r"取消置顶|不置顶|unpin" if changes["pinned"] is False else r"(?<!取消)(?<!不)置顶|\bpin\b"
        if not re.search(pattern, raw, re.I):
            raise ContractError("ACCESS_DENIED", "pin_not_bound")


def record_importance(tx, ref, proposal, *, advance_head):
    """Accept model salience only while the owner has not supplied an override."""
    if advance_head and "importance" in proposal:
        tx._check(write=True).execute(
            "UPDATE claims SET importance=CASE WHEN pinned=1 THEN 5 ELSE ? END,importance_assigned=1 "
            "WHERE claim_id=? AND importance_owner=0",
            (proposal["importance"], ref),
        )
