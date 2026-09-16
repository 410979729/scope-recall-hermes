"""Legacy facts and playbooks become Core claim revisions with exact evidence.

A legacy status is preserved as a Core state only when live, exact, same-scope
evidence backs it; otherwise the row stays a proposal and the report says why.
Slot conflicts are never resolved by arrival order: the whole batch is planned
first and conflicting rows stay archived with an explicit conversion gap.
"""
from __future__ import annotations

from collections import defaultdict
import sqlite3
from typing import Any, NamedTuple, cast

from scope_recall.contracts import ClaimProposal
from scope_recall.core.claims import claim_slot

from .legacy_plan import Conversion, Row
from .legacy_sources import (
    _anchor_ref, _evidence_items, _json, _resolve, _safe, _safe_list, _safe_text, _scope, _text_or_none,
)
from .migration_records import MigrationError, _canon, _digest, _recorded, _stable, _time

Evidence = list[tuple[str, str, str | None]]

_FACT_STATES = {
    "superseded": "superseded",
    "retracted": "retracted",
    "uncertain": "disputed",
    "current": "active",
}
_CLAIM_PAYLOAD_LIMIT = 131072
_HISTORY_LINK_TABLES = ("experience_runs", "reflection_events", "skill_anchors", "skill_conflicts")


def _procedure_claim_id(old_id: str) -> str:
    """Use the Core claim namespace for imported procedures."""
    return _stable("claim", f"procedure:{old_id}")


def _claim_basis(assertion: object, origin: str) -> str:
    value = str(assertion or "").lower()
    if value == "direct":
        return "direct_report"
    if value == "validated":
        return "observed" if origin == "tool_observation" else "direct_report"
    if value == "inferred":
        return "inferred_suggestion"
    return "unknown"


def _claim_payload(
    kind: str,
    subject: str,
    predicate: str,
    value: str,
    conditions: list[str],
    legacy: dict[str, Any],
    procedure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # ClaimProposal is an existing closed schema.  Migration provenance lives
    # in the sanitized source archive and history links, never in authority
    # payload extensions that would make ordinary Core revise invalid.
    payload: dict[str, Any] = {
        "kind": kind,
        "subject": subject or "legacy:unknown-subject",
        "predicate": predicate or "legacy:unknown-predicate",
        "value_text": value,
        "conditions": conditions,
        "statement_kind": "assertion",
        "valid_from": legacy.get("valid_from"),
        "valid_to": legacy.get("valid_to"),
        "evidence_spans": [],
    }
    if procedure is not None:
        payload["procedure"] = procedure
    return payload


def _exact_evidence(item: Row | None, quote: str, scope_id: str, project: str | None, branch: str | None) -> bool:
    """Evidence counts only if the quote is live in a same-context, unblocked source."""
    return bool(
        item
        and quote
        and quote[:4096] in item["content"]
        and item["scope_id"] == scope_id
        and item.get("project_id") == project
        and item.get("branch_id") == branch
        and not item.get("scope_gap")
    )


def _sources_by_event(cv: Conversion) -> dict[str, Row]:
    index: dict[str, Row] = {}
    for item in cv.sources:
        index.setdefault(item["event_id"], item)
    return index


def _current_revision(conn: sqlite3.Connection, claim_id: str) -> int:
    return int(conn.execute("SELECT current_revision FROM claims WHERE claim_id=?", (claim_id,)).fetchone()[0])


def _insert_version(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    scope_id: str,
    project_id: str | None,
    branch_id: str | None,
    payload: dict[str, Any],
    state: str,
    basis: str,
    reason: str,
    recorded: str,
    valid_from: str | None,
    valid_to: str | None,
    evidence: Evidence,
    counts: defaultdict[str, int],
) -> int:
    slot = claim_slot(scope_id, project_id, branch_id, cast(ClaimProposal, payload))
    head = conn.execute("SELECT claim_id FROM claims WHERE slot_key=?", (slot,)).fetchone()
    if head is None:
        conn.execute(
            "INSERT INTO claims(claim_id,scope_id,project_id,branch_id,subject,predicate,kind,slot_key,current_revision) VALUES (?,?,?,?,?,?,?,?,1)",
            (claim_id, scope_id, project_id, branch_id, payload["subject"], payload["predicate"], payload["kind"], slot),
        )
        revision = 1
    else:
        if str(head[0]) != claim_id:
            raise MigrationError(f"claim slot collision: {slot}")
        revision = int(
            conn.execute("SELECT COALESCE(max(revision),0)+1 FROM claim_versions WHERE claim_id=?", (claim_id,)).fetchone()[0]
        )
        conn.execute("UPDATE claim_versions SET recorded_to=? WHERE claim_id=? AND revision=?", (recorded, claim_id, revision - 1))
        conn.execute("UPDATE claims SET current_revision=? WHERE claim_id=?", (revision, claim_id))
    serialized = _canon(payload)
    if len(serialized.encode()) > _CLAIM_PAYLOAD_LIMIT:
        raise MigrationError(f"claim payload exceeds Core limit: {claim_id}")
    conn.execute(
        "INSERT INTO claim_versions(claim_id,revision,payload_json,state,basis,qualification_reason,valid_from,valid_to,recorded_from,replaces_revision,conflict_revisions_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (claim_id, revision, serialized, state, basis, reason, valid_from, valid_to, recorded, revision - 1 if revision > 1 else None, "[]"),
    )
    for ref, quote, location in evidence:
        conn.execute(
            "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote,location) VALUES ('claim',?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (claim_id, revision, ref, 1, "supports", quote[:4096], location),
        )
    counts["claims"] += int(revision == 1)
    counts["claim_versions"] += 1
    return revision


def _link_archive(cv: Conversion, conn: sqlite3.Connection, claim_id: str, revision: int, archive_ref: str | None) -> None:
    """Tie a claim revision back to the archived legacy row it came from."""
    if not archive_ref:
        return
    conn.execute(
        "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote) VALUES ('claim',?,?,?,?,?,'') ON CONFLICT DO NOTHING",
        (claim_id, revision, archive_ref, 1, "derived_from"),
    )
    cv.inserted["history_links"] += 1


# --- facts -----------------------------------------------------------------

class _FactSlots(NamedTuple):
    """Legacy fact rows that cannot map to one Core slot without an operator."""

    collisions: set[tuple[Any, ...]]        # several current single-valued rows on one raw key
    natural_conflicts: set[tuple[Any, ...]]  # different legacy fact_keys on one natural slot
    unordered_current: set[tuple[Any, ...]]  # the current row is not the latest recorded


def _fact_slots(rows: list[Row]) -> _FactSlots:
    # A legacy fact_key is an identifier, not a Core semantic condition.  Two
    # single-valued legacy keys for the same natural subject/predicate cannot
    # be selected without an explicit operator decision, so both stay in the
    # archive and cutover blocks instead of merging them into one slot.
    current_counts: defaultdict[tuple[Any, ...], int] = defaultdict(int)
    natural_keys: defaultdict[tuple[Any, ...], set[str]] = defaultdict(set)
    history: defaultdict[tuple[Any, ...], list[Row]] = defaultdict(list)
    for row in rows:
        context = (row.get("scope_id"), row.get("project_id"), row.get("branch_id"))
        single = str(row.get("cardinality") or "single").lower() == "single"
        if single and str(row.get("status") or "").lower() == "current":
            current_counts[(*context, row.get("subject_key"), row.get("predicate_key"), row.get("fact_key"))] += 1
        if not single:
            continue
        subject, predicate, fact_key = (_safe_text(row.get(key))[0] for key in ("subject_key", "predicate_key", "fact_key"))
        natural_keys[(*context, subject, predicate)].add(fact_key)
        history[(*context, subject, predicate, fact_key)].append(row)
    unordered: set[tuple[Any, ...]] = set()
    for key, candidates in history.items():
        current = [c for c in candidates if str(c.get("status") or "").lower() == "current" and not c.get("retired_at")]
        if len(current) != 1:
            continue
        recorded = _recorded(current[0].get("recorded_at"))
        if any(str(c.get("status") or "").lower() != "current" and _recorded(c.get("recorded_at")) > recorded for c in candidates):
            unordered.add(key)
    return _FactSlots(
        {key for key, count in current_counts.items() if count > 1},
        {key for key, keys in natural_keys.items() if len(keys) > 1},
        unordered,
    )


def _fact_evidence(
    cv: Conversion, by_event: dict[str, Row], rows: list[Row], scope_id: str, project: str | None, branch: str | None
) -> tuple[Evidence, str]:
    """Exact evidence links for a fact, and the origin of the last one that held."""
    evidence: Evidence = []
    origin = ""
    for row in rows:
        ref = _resolve(row.get("source_ref"), row.get("source_type"), cv.journal_refs, cv.memory_refs, cv.archives)
        quote, _ = _safe_text(row.get("excerpt"))
        if ref and _exact_evidence(by_event.get(ref), quote, scope_id, project, branch):
            origin = by_event[ref].get("source_original_origin") or ""
            evidence.append((ref, quote, _text_or_none(row.get("location"))))
    return evidence, origin


def _fact_sort_key(row: Row) -> tuple[str, int, str]:
    return (_recorded(row.get("recorded_at")), int(str(row.get("status") or "").lower() == "current"), str(row.get("claim_id")))


def write_fact_claims(cv: Conversion, conn: sqlite3.Connection) -> None:
    rows = cv.rows["fact_claims"]
    slots = _fact_slots(rows)
    by_event = _sources_by_event(cv)
    evidence_by_claim: defaultdict[str, list[Row]] = defaultdict(list)
    for row in cv.rows["fact_claim_evidence"]:
        evidence_by_claim[str(row.get("claim_id"))].append(row)
    # Existing claims at entry are from an earlier migration.  A claim created
    # earlier in this same pass must still receive later legacy rows as
    # revisions.  This distinction also makes a post-delete rerun idempotent:
    # scrubbed payloads are not reconstructed from the immutable snapshot.
    preexisting = {str(item[0]) for item in conn.execute("SELECT claim_id FROM claims").fetchall()}
    current_revisions: dict[str, int] = {}
    for row in sorted(rows, key=_fact_sort_key):
        old_id = str(row.get("claim_id") or "")
        if not old_id:
            cv.unmapped("fact_claims", "<missing>", "missing_fact_claim_id")
            continue
        status = str(row.get("status") or "unknown")
        archive_ref = cv.archives.get(("fact_claims", old_id))
        context = (row.get("scope_id"), row.get("project_id"), row.get("branch_id"))
        (subject, sr), (predicate, pr), (fact_key, fr) = (_safe_text(row.get(key)) for key in ("subject_key", "predicate_key", "fact_key"))
        value, vr = _safe_text(row.get("value") or row.get("normalized_value"))
        if (*context, subject, predicate, fact_key) in slots.unordered_current:
            cv.unmapped("fact_claims", old_id, "fact_current_not_latest_recorded_blocks_cutover", status=status, archive_source_ref=archive_ref)
            continue
        if (*context, subject, predicate) in slots.natural_conflicts:
            cv.unmapped("fact_claims", old_id, "fact_slot_conflict_different_legacy_fact_key", status=status, legacy_fact_key=fact_key, archive_source_ref=archive_ref)
            continue
        scope_id = str(row.get("scope_id") or "legacy-scope")
        project, branch = _text_or_none(row.get("project_id")), _text_or_none(row.get("branch_id"))
        conditions: list[str] = []
        claim_id = _stable("claim", f"{scope_id}:{project}:{branch}:fact:{subject}:{predicate}:{conditions}")
        if claim_id in preexisting:
            cv.mapped_facts.add(old_id)
            cv.fact_claim_refs[old_id] = claim_id
            continue
        cardinality = str(row.get("cardinality") or "single").lower()
        if cardinality == "multi":
            cv.unmapped("fact_claims", old_id, "multi_value_fact_not_losslessly_mapped_to_core_slot", status=status, archive_source_ref=archive_ref)
            continue
        if (*context, row.get("subject_key"), row.get("predicate_key"), row.get("fact_key")) in slots.collisions:
            cv.unmapped("fact_claims", old_id, "single_slot_collision_not_losslessly_mapped", status=status, archive_source_ref=archive_ref)
            continue
        evidence_rows = evidence_by_claim.get(old_id, [])
        evidence, origin = _fact_evidence(cv, by_event, evidence_rows, scope_id, project, branch)
        old_status = status.lower()
        state, reason = _FACT_STATES.get(old_status, "proposed"), "legacy_status_preserved"
        if row.get("retired_at") and state == "active":
            state, reason = "superseded", "legacy_retired_at_not_active"
        if state == "active" and not evidence:
            state, reason = "proposed", "legacy_current_without_live_exact_evidence"
        payload = _claim_payload("fact", subject, predicate, value, conditions, {
            "claim_id": old_id,
            "memory_id": row.get("memory_id"),
            "fact_key": fact_key,
            "normalized_value": row.get("normalized_value"),
            "value_fingerprint": str(row.get("value_fingerprint") or _digest(value)),
            "cardinality": cardinality,
            "assertion_kind": row.get("assertion_kind"),
            "status": old_status,
            "retired_at": row.get("retired_at"),
            "confidence": row.get("confidence"),
            "superseded_by_claim_id": row.get("superseded_by_claim_id"),
            "source_type": row.get("source_type"),
            "source_ref": row.get("source_ref"),
            "evidence_hash": row.get("evidence_hash"),
            "evidence": _safe(evidence_rows),
            "scope_authorization": _scope(row),
            "valid_from": _time(row.get("valid_from"))[0],
            "valid_to": _time(row.get("valid_to"))[0],
        })
        revision = _insert_version(
            conn, claim_id=claim_id, scope_id=scope_id, project_id=project, branch_id=branch,
            payload=payload, state=state, basis=_claim_basis(row.get("assertion_kind"), origin), reason=reason,
            recorded=_recorded(row.get("recorded_at")), valid_from=payload["valid_from"], valid_to=payload["valid_to"],
            evidence=evidence, counts=cv.inserted,
        )
        _link_archive(cv, conn, claim_id, revision, archive_ref)
        cv.mapped_facts.add(old_id)
        cv.fact_claim_refs[old_id] = claim_id
        cv.redactions += int(sr or pr or vr or fr)
        if old_status == "current":
            current_revisions[claim_id] = revision
            if state != "active":
                cv.unmapped("fact_claims", old_id, "current_fact_without_live_exact_evidence", claim_ref=claim_id)
    # Legacy timestamps are not authoritative ordering when a source repaired a
    # row in place.  The explicit current marker selects the head after the
    # complete history has been appended.
    for claim_id, revision in current_revisions.items():
        conn.execute("UPDATE claims SET current_revision=? WHERE claim_id=?", (revision, claim_id))


# --- procedures ------------------------------------------------------------

class _Version(NamedTuple):
    """One playbook snapshot, read into the fields a procedure claim needs."""

    snapshot: Row
    data: dict[str, Any]
    title: str
    goal: str
    status: str
    subject: str
    conditions: list[str]
    method: list[str]
    non_applicable: list[str]


def versions_by_playbook(rows: list[Row]) -> dict[str, list[Row]]:
    versions: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        versions[str(row.get("playbook_id"))].append(row)
    return versions


def _snapshot_data(snapshot: Row, playbook: Row) -> dict[str, Any]:
    data = _json(snapshot.get("snapshot"), playbook)
    return data if isinstance(data, dict) else playbook


def _procedure_slot_collisions(
    playbooks: list[Row], versions: dict[str, list[Row]]
) -> dict[str, tuple[str, ...]]:
    """Find different legacy identities competing for one Core procedure slot.

    Plan the complete batch before inserting any authority: arrival order must
    not select a winner. History remains in same-scope source_events; old IDs
    are provenance, never invented semantic conditions or replacement chains.
    Histories changing slots already have their own explicit conversion gap.
    """
    owners: defaultdict[str, set[str]] = defaultdict(set)
    for playbook in playbooks:
        old_id = str(playbook.get("id") or "")
        if not old_id:
            continue
        slots: set[str] = set()
        for snapshot in versions.get(old_id) or [{"snapshot": _canon(playbook)}]:
            data = _snapshot_data(snapshot, playbook)
            title, _ = _safe_text(data.get("title") or playbook.get("title") or old_id)
            proposal = cast(ClaimProposal, {
                "kind": "procedure", "subject": str(playbook.get("task_class") or old_id),
                "predicate": title or "procedure", "conditions": _safe_list(data.get("preconditions")),
            })
            slots.add(claim_slot(
                _scope(playbook)["row_scope_id"],
                _text_or_none(playbook.get("project_id")),
                _text_or_none(playbook.get("branch_id")),
                proposal,
            ))
        if len(slots) == 1:
            owners[next(iter(slots))].add(old_id)
    return {
        old_id: tuple(sorted(ids))
        for ids in owners.values() if len(ids) > 1
        for old_id in ids
    }


def _versions(playbook: Row, snapshots: list[Row]) -> list[_Version]:
    old_id = str(playbook.get("id") or "")
    prepared: list[_Version] = []
    for snapshot in sorted(snapshots, key=lambda x: (int(x.get("version") or 0), str(x.get("created_at") or ""))):
        data = _snapshot_data(snapshot, playbook)
        title, _ = _safe_text(data.get("title") or playbook.get("title") or old_id)
        goal, _ = _safe_text(data.get("goal") or playbook.get("goal"))
        prepared.append(_Version(
            snapshot,
            data,
            title or "procedure",
            goal,
            str(data.get("status") or playbook.get("status") or "candidate").lower(),
            str(playbook.get("task_class") or old_id),
            _safe_list(data.get("preconditions")),
            _safe_list(data.get("steps")),
            _safe_list(data.get("pitfalls")),
        ))
    return prepared


def _procedure_evidence(cv: Conversion, by_event: dict[str, Row], version: _Version, playbook: Row, scope_id: str, project: str | None, branch: str | None) -> Evidence:
    evidence: Evidence = []
    for anchor in _evidence_items(version.data.get("evidence_anchors") or playbook.get("evidence_anchors")):
        ref = _resolve(_anchor_ref(anchor), "", cv.journal_refs, cv.memory_refs, cv.archives)
        quote, _ = _safe_text(anchor.get("quote") or anchor.get("excerpt"))
        if ref and _exact_evidence(by_event.get(ref), quote, scope_id, project, branch):
            evidence.append((ref, quote, None))
    return evidence


def _write_procedure_versions(cv: Conversion, conn: sqlite3.Connection, by_event: dict[str, Row], playbook: Row, claim_id: str, prepared: list[_Version]) -> None:
    old_id = str(playbook.get("id") or "")
    scope = _scope(playbook)
    scope_id = scope["row_scope_id"]
    project, branch = _text_or_none(playbook.get("project_id")), _text_or_none(playbook.get("branch_id"))
    for version in prepared:
        evidence = _procedure_evidence(cv, by_event, version, playbook, scope_id, project, branch)
        promoted = version.status == "promoted"
        active = promoted and bool(evidence)
        procedure = {
            "conditions": version.conditions,
            "non_applicable": version.non_applicable,
            "method": version.method,
            "verification_basis": "user_accepted" if promoted else "inferred_suggestion",
            "counterexample_refs": [],
        }
        payload = _claim_payload("procedure", version.subject, version.title, version.goal, version.conditions, {
            "playbook_id": old_id,
            "version": version.snapshot.get("version"),
            "status": version.status,
            "snapshot": _safe(version.data),
            "scope_authorization": scope,
        }, procedure)
        revision = _insert_version(
            conn, claim_id=claim_id, scope_id=scope_id, project_id=project, branch_id=branch,
            payload=payload,
            state="active" if active else "proposed",
            basis="observed" if active else "inferred_suggestion",
            reason=(
                "legacy_promoted_with_exact_evidence" if active
                else "legacy_promoted_without_same_scope_exact_evidence" if promoted
                else "legacy_candidate_or_missing_exact_evidence"
            ),
            recorded=_recorded(version.snapshot.get("created_at") or playbook.get("updated_at")),
            valid_from=None, valid_to=None, evidence=evidence, counts=cv.inserted,
        )
        archive_ref = cv.archives.get(("playbook_versions", str(version.snapshot.get("id")))) or cv.archives.get(("procedural_playbooks", old_id))
        _link_archive(cv, conn, claim_id, revision, archive_ref)
        if promoted and not active:
            cv.unmapped("procedural_playbooks", old_id, "promoted_procedure_without_live_exact_evidence")


def write_procedures(cv: Conversion, conn: sqlite3.Connection) -> None:
    playbooks = cv.rows["procedural_playbooks"]
    versions = versions_by_playbook(cv.rows["playbook_versions"])
    by_event = _sources_by_event(cv)
    preexisting = {str(item[0]) for item in conn.execute("SELECT claim_id FROM claims").fetchall()}
    cv.procedure_collisions = _procedure_slot_collisions(playbooks, versions)
    for playbook in playbooks:
        old_id = str(playbook.get("id") or "")
        if not old_id:
            continue
        if old_id in cv.procedure_collisions:
            # All originals and snapshots were already imported as same-scope
            # legacy_history source_events.  Do not silently turn unrelated
            # playbooks into revisions of each other.
            cv.unmapped(
                "procedural_playbooks", old_id, "procedure_slot_conflict_different_legacy_playbook",
                archive_source_ref=cv.archives.get(("procedural_playbooks", old_id)),
                version_source_refs=[
                    cv.archives[("playbook_versions", str(snap.get("id")))]
                    for snap in versions.get(old_id, [])
                    if ("playbook_versions", str(snap.get("id"))) in cv.archives
                ],
                conflicting_playbook_ids=list(cv.procedure_collisions[old_id]),
                preservation="same_scope_sqlite_history_sources",
                automatic_procedure_eligible=False,
            )
            continue
        snapshots = versions.get(old_id) or [{"version": 1, "snapshot": _canon(playbook), "created_at": playbook.get("created_at")}]
        prepared = _versions(playbook, snapshots)
        scope = _scope(playbook)
        slots = {
            (scope["row_scope_id"], _text_or_none(playbook.get("project_id")), _text_or_none(playbook.get("branch_id")),
             version.subject, version.title, tuple(sorted(set(version.conditions))))
            for version in prepared
        }
        if len(slots) > 1:
            table = "playbook_versions" if versions.get(old_id) else "procedural_playbooks"
            for version in prepared:
                cv.unmapped(
                    table, str(version.snapshot.get("id") or old_id), "procedure_version_slot_changed_blocks_cutover",
                    archive_source_ref=cv.archives.get(("playbook_versions", str(version.snapshot.get("id")))),
                )
            continue
        claim_id = _procedure_claim_id(old_id)
        if claim_id not in preexisting:
            _write_procedure_versions(cv, conn, by_event, playbook, claim_id, prepared)
        cv.mapped_procedures.add(old_id)


# --- history links ---------------------------------------------------------

def _link_history(cv: Conversion, conn: sqlite3.Connection, kind: str, ref: str, revision: int, source_ref: str) -> None:
    conn.execute(
        "INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote) VALUES (?,?,?,?,?,'derived_from','') ON CONFLICT DO NOTHING",
        (kind, ref, revision, source_ref, 1),
    )
    cv.inserted["history_links"] += 1


def link_history_records(cv: Conversion, conn: sqlite3.Connection) -> None:
    """Old action receipts and experience records stay historical imported
    sources, but their relationship to the new authority is retained where the
    old IDs make it unambiguous."""
    for row in cv.rows["fact_action_receipts"]:
        source_ref = cv.archives.get(("fact_action_receipts", str(row.get("action_id"))))
        if not source_ref:
            continue
        raw = str(row.get("receipt_json") or "")
        for old_id, claim_ref in cv.fact_claim_refs.items():
            if old_id in raw:
                _link_history(cv, conn, "claim", claim_ref, _current_revision(conn, claim_ref), source_ref)
    for table in _HISTORY_LINK_TABLES:
        for row in cv.rows[table]:
            source_ref = cv.archives.get((table, str(row.get("id") or "unknown")))
            if not source_ref:
                continue
            playbook_id = str(row.get("playbook_id") or "")
            if playbook_id and playbook_id in cv.mapped_procedures:
                claim_ref = _procedure_claim_id(playbook_id)
                _link_history(cv, conn, "claim", claim_ref, _current_revision(conn, claim_ref), source_ref)
            episode_id = str(row.get("episode_id") or "")
            if episode_id in cv.episode_refs:
                _link_history(cv, conn, "episode", cv.episode_refs[episode_id], 1, source_ref)
