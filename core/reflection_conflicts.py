"""Bounded owner-conflict scans with durable progress independent of model budgets."""

from __future__ import annotations

import json

from .recall_policy import meaningful_query_terms
from .source_qualification import POLARITY


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


def _source_page(tx, version, members):
    conn = tx._check()
    prior = conn.execute(
        "SELECT position,conflicted FROM observation_conflicts WHERE claim_ref=? AND claim_revision=?",
        (version.ref, version.revision),
    ).fetchone()
    position = prior[0] if prior else max(m["position"] for m in members)
    if prior and prior[1]:
        return position, True
    rows = conn.execute(
        (
            "SELECT rowid AS position,event_id,source_revision,content FROM source_events WHERE"
            " rowid>? AND scope_id=? AND project_id IS ? AND branch_id IS ? AND role='user' AND"
            " read_blocked=0 AND suppressed=0 ORDER BY rowid LIMIT 128"
        ),
        (position, version.scope_id, version.project_id, version.branch_id),
    ).fetchall()
    terms = set(meaningful_query_terms(version.payload["value_text"]))
    conflict = any(
        tx.source(row["event_id"], row["source_revision"]) is not None
        and POLARITY.search(row["content"])
        and len(terms & set(meaningful_query_terms(row["content"]))) >= 2
        for row in rows
    )
    return rows[-1]["position"] if rows else position, bool(conflict)


def owner_conflict(tx, version, members):
    """An actual owner statement can suppress an induction; a scan limit cannot.

    A read checks the next bounded page without writing. Idle worker pages
    persist the same cursor, retaining the last conclusion until more is seen.
    """
    if pending_owner_correction(tx, version):
        return True
    payload = version.payload
    direct = (
        tx._check()
        .execute(
            (
                "SELECT 1 FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revisi"
                "on=c.current_revision WHERE c.scope_id=? AND c.project_id IS ? AND c.branch_id IS "
                "? AND c.subject=? AND c.predicate=? AND c.kind!='observation' AND v.state='active'"
                " AND v.basis='direct_report' AND c.read_blocked=0 AND c.suppressed=0 AND json_extr"
                "act(v.payload_json,'$.value_text')!=? LIMIT 1"
            ),
            (
                version.scope_id,
                version.project_id,
                version.branch_id,
                payload["subject"],
                payload["predicate"],
                payload["value_text"],
            ),
        )
        .fetchone()
    )
    return direct is not None or _source_page(tx, version, members)[1]


def conflict_candidates(tx):
    """Current observations whose authorized partition contains unchecked owner messages."""
    ctx = tx.context
    return (
        tx._check()
        .execute(
            (
                "SELECT o.claim_ref,o.claim_revision,o.manifest_json FROM observations o JOIN claim"
                "s c ON c.claim_id=o.claim_ref AND c.current_revision=o.claim_revision LEFT JOIN ob"
                "servation_conflicts p ON p.claim_ref=o.claim_ref AND p.claim_revision=o.claim_revi"
                "sion WHERE c.scope_id IN ("
            )
            + ",".join("?" for _ in ctx.allowed_scope_ids)
            + (
                ") AND c.project_id IS ? AND c.branch_id IS ? AND c.read_blocked=0 AND c.suppressed"
                "=0 AND coalesce(p.conflicted,0)=0 AND EXISTS(SELECT 1 FROM source_events s WHERE s"
                ".rowid>coalesce(p.position,(SELECT max(json_extract(value,'$.position')) FROM json"
                "_each(o.manifest_json))) AND s.scope_id=c.scope_id AND s.project_id IS c.project_i"
                "d AND s.branch_id IS c.branch_id AND s.role='user' AND s.read_blocked=0 AND s.supp"
                "ressed=0) ORDER BY o.claim_ref LIMIT 16"
            ),
            (*sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id),
        )
        .fetchall()
    )


def advance_conflicts(tx):
    """Persist at most sixteen 128-source pages inside the existing idle writer."""
    conn = tx._check(write=True)
    for row in conflict_candidates(tx):
        version = tx.claims.version(row["claim_ref"], row["claim_revision"])
        if version is None:
            continue
        position, conflict = _source_page(tx, version, json.loads(row["manifest_json"]))
        conn.execute(
            (
                "INSERT INTO observation_conflicts VALUES(?,?,?,?) ON CONFLICT(claim_ref,claim_revi"
                "sion) DO UPDATE SET position=excluded.position,conflicted=excluded.conflicted"
            ),
            (version.ref, version.revision, position, int(conflict)),
        )
