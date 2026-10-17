"""Re-judge stored claims after the rules that judged them have changed.

A claim is judged once, when it is written.  Without this nothing looks at it
again, so a gate repair only ever helps claims captured *after* the repair ships
-- the proposals already in a store (366 in one) would keep their original
verdicts forever, including the ones refused by a rule that has since been
fixed.

This is the bounded way to let a rule change reach them.  It re-runs exactly
the qualification an ordinary write runs, on the payload already stored, and
writes a new version only where the verdict actually differs.  It never
rewrites the assertion: if re-binding the subject would change what the claim
says, the claim is left alone and reported, because that is a different
mutation and belongs to a different, deliberate operation.

Deliberately an operator command rather than background work.  Re-qualification
is the consequence of a *code* change, and code changes are events somebody
decides on; on a timer it would spend forever re-deriving verdicts that have
already settled.  ``--dry-run`` exists because the first thing to do after a
gate change is look at the diff, not apply it.

Not responsible for: judging (``core/claims.qualify``), or persisting the page
cursor (the caller does, exactly as ``repair_frames`` does it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts import ContractError
from .claim_normalization import expand_frames
from .claims import (
    DENIAL_REASON,
    NOT_A_VALUE_REASON,
    Qualification,
    bind_claim_subject,
    evidence_refs,
    is_denial,
    qualify,
    same_assertion,
    select_effective,
    valid_at,
)
from .confirmation import CONFIRMED_REASON
from .corroboration import CORROBORATED_REASON
from .episodes import source_origin
from .evidence_question import DERIVATION_ROOT_ORIGINS, NO_DERIVATION_ROOT_REASON
from .mutate import apply_claim, denial_verdict, register_applied_candidate

#: Page ceiling.  Matches ``repair_frames``: large enough to finish a real
#: store in a few passes, small enough that one pass is an ordinary transaction.
MAX_PAGE = 32

#: States worth re-judging.  ``active`` is included because a rule change can
#: also *withdraw* support, and a repair that could only ever promote would be
#: a ratchet rather than a re-judgement.
REQUALIFIABLE_STATES = frozenset({"proposed", "active", "disputed"})


def _preserved_reasons() -> frozenset[str]:
    """Verdicts that did not come from reading the text, and so cannot be re-read.

    ``qualify`` answers one question: does *this* source prove the claim?  Two
    verdicts in this system deliberately answer a different one -- a person
    vouched for it, or two independent witnesses did -- and re-running the text
    gate over a single stored payload would return "unproved" for both and
    silently undo them.  Every user confirmation would be withdrawn by the next
    maintenance pass.  A denial's verdict answers another still: whether it is
    its slot's value, given what the slot held when it was written.  The text
    gate alone would promote it.

    Imported lazily so this module stays free of import cycles with ``mutate``.
    """
    return frozenset({CONFIRMED_REASON, CORROBORATED_REASON, DENIAL_REASON, NOT_A_VALUE_REASON})


@dataclass
class RequalifyReport:
    examined: int = 0
    changed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    last_ref: str = ""
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "changed": list(self.changed),
            "skipped": list(self.skipped),
            "last_ref": self.last_ref,
            "applied": self.applied,
        }


def _page(tx, after_ref: str, limit: int) -> list:
    """The refs of one page of the claims this context may read and nothing blocks, after ``after_ref`` in ref order.

    Authorization filters before pagination, so a visited prefix or another audience cannot starve the records behind
    it.
    """
    if type(limit) is not int or type(limit) is bool or not 1 <= limit <= MAX_PAGE:
        raise ContractError("INPUT_INVALID", "requalify_limit")
    if type(after_ref) is not str:
        raise ContractError("INPUT_INVALID", "requalify_cursor")
    scopes = sorted(tx.context.allowed_scope_ids)
    rows = (
        tx._check()
        .execute(
            f"""SELECT claim_id FROM claims WHERE claim_id>? AND read_blocked=0 AND suppressed=0
            AND scope_id IN ({",".join("?" for _ in scopes)})
            AND project_id IS ? AND branch_id IS ?
            ORDER BY claim_id LIMIT ?""",
            (after_ref, *scopes, tx.context.project_id, tx.context.branch_id, limit),
        )
        .fetchall()
    )
    return [row[0] for row in rows]


def _head(tx, ref: str):
    """The claim's current version, or None."""
    return next((v for v in tx.claims.versions(ref) if v.revision == v.current_revision), None)


def requalify_claims(tx, *, now: str, after_ref: str = "", limit: int = 16, dry_run: bool = True) -> RequalifyReport:
    """Re-judge one bounded page of stored claims.  Returns what moved."""
    refs = _page(tx, after_ref, limit)
    preserved = _preserved_reasons()
    report = RequalifyReport(applied=not dry_run)
    for ref in refs:
        report.last_ref = ref
        head = _head(tx, ref)
        if head is None or head.state not in REQUALIFIABLE_STATES:
            continue
        if head.reason in preserved:
            report.skipped.append({"ref": ref, "why": f"not_text_derived:{head.reason}"})
            continue
        report.examined += 1
        try:
            roots = tx.claims.roots(evidence_refs(head.payload))
            proposal, subject_bound, binding_issue = bind_claim_subject(head.payload, roots)
            verdict = (
                Qualification("proposed", "inferred_suggestion", binding_issue)
                if binding_issue is not None
                else qualify(proposal, roots, project_id=tx.context.project_id, _subject_bound=subject_bound)
            )
        except ContractError as exc:
            report.skipped.append({"ref": ref, "why": f"qualification_failed:{exc.code}"})
            continue
        if not same_assertion(head.payload, proposal):
            # Re-binding changed what the claim says.  That is a rewrite, not a
            # re-judgement, and it is not this operation's decision to make.
            report.skipped.append({"ref": ref, "why": "assertion_would_change"})
            continue
        if (verdict.state, verdict.reason) == (head.state, head.reason):
            continue
        entry = {
            "ref": ref,
            "was": f"{head.state}:{head.reason}",
            "now": f"{verdict.state}:{verdict.reason}",
            "subject": head.payload.get("subject"),
            "predicate": head.payload.get("predicate"),
            "value": head.payload.get("value_text"),
        }
        if not dry_run:
            saved = tx.claims.append(
                head.scope_id, proposal, verdict, recorded_at=now, previous=head, advance_head=True
            )
            entry["revision"] = saved.revision
        report.changed.append(entry)
    return report


#: The reason a retired proposal carries: none of its evidence is a source consolidation derives from.
ROOTLESS_REASON = NO_DERIVATION_ROOT_REASON


def retire_rootless_proposals(
    tx, *, now: str, after_ref: str = "", limit: int = 16, dry_run: bool = True
) -> RequalifyReport:
    """Retire one bounded page of proposed claims that no derivation root supports.

    Consolidation derives claims only from ``DERIVATION_ROOT_ORIGINS``, which tool output is not in.
    A proposal whose every evidence source is of another origin is one consolidation would not
    derive, and it is still waiting for a proof nobody asked for: it gets a retracted version with
    ``ROOTLESS_REASON``, registered as the candidate's new head so its queued evaluations end.  Its
    sources and its earlier versions stay.  Active and disputed claims are left alone -- a claim that
    was proved stands on that proof -- and nothing is re-judged here; ``requalify_claims`` does that.
    Nor is a proposal a person has restated since (``restated_by_a_root``): they are heard in its
    evaluation, not in its own evidence, and the verdict on their words decides.

    Separate from ``requalify_claims`` on purpose: a re-judgement moves claims for every rule that
    changed since they were written (154 of them in one store, promotions included), and retiring
    these must not bring that along.  The report names refs and verdicts only, never claim text.
    """
    refs = _page(tx, after_ref, limit)
    report = RequalifyReport(applied=not dry_run)
    for ref in refs:
        report.last_ref = ref
        head = _head(tx, ref)
        if head is None or head.state != "proposed":
            continue
        report.examined += 1
        origins = set()
        for evidence in evidence_refs(head.payload):
            source_ref, _, revision = evidence.rpartition("@")
            source = tx.source(source_ref, int(revision)) if revision.isdigit() else None
            origins.add(source_origin(source) if source is not None else None)
        if not origins or None in origins:
            report.skipped.append({"ref": ref, "why": "evidence_unreadable"})
            continue
        if origins & DERIVATION_ROOT_ORIGINS:
            continue
        if tx.candidates.restated_by_a_root(ref, head.revision, head.payload):
            # A person (or a document) has said it since; the verdict on their words decides.
            report.skipped.append({"ref": ref, "why": "restated_in_evaluation"})
            continue
        entry = {
            "ref": ref,
            "was": f"{head.state}:{head.reason}",
            "now": f"retracted:{ROOTLESS_REASON}",
            "origins": sorted(origins),
        }
        if not dry_run:
            retired = tx.claims.append(
                head.scope_id,
                head.payload,
                Qualification("retracted", head.basis, ROOTLESS_REASON),
                recorded_at=now,
                previous=head,
            )
            tx.candidates.register(retired.ref, retired.revision, observed_at=now, schedule_initial=False)
            entry["revision"] = retired.revision
        report.changed.append(entry)
    return report


def _denial_standing(tx, head, versions):
    """What the slot held when ``head``, a denial, was written (``select_effective``), and why the denial is no value
    of its slot there (``mutate.denial_verdict``, as writing it now would judge), or None where it is one."""
    earlier = tuple(v for v in versions if v.revision < head.revision)
    held = select_effective(earlier, head.recorded_from) if earlier else None
    reason, _ends = denial_verdict(head.payload, held, tx.claims.roots(evidence_refs(head.payload)))
    return held, reason


def _settle_denial(tx, head, held, reason: str, *, now: str, dry_run: bool) -> dict[str, Any]:
    """Withdraw ``head``, a denial at its claim's head that is no value of its slot (``_denial_standing``), with a
    retracted version, so no read at any instant takes it for the value; and where it denied another value than the
    one held, re-instate that value as it was, if it still holds (``valid_at``): an expired one is not.  Every version
    stays."""
    displaced = held if reason == DENIAL_REASON else None
    restores = displaced if displaced is not None and valid_at(displaced, now, historical=False) else None
    entry = {
        "ref": head.ref,
        "was": f"{head.state}:{head.reason}",
        "now": f"retracted:{reason}",
        "restores": None if restores is None else restores.revision,
    }
    if not dry_run:
        saved = tx.claims.append(
            head.scope_id, head.payload, Qualification("retracted", head.basis, reason), recorded_at=now, previous=head
        )
        if restores is not None:
            verdict = Qualification(restores.state, restores.basis, restores.reason)
            saved = tx.claims.append(head.scope_id, restores.payload, verdict, recorded_at=now, previous=saved)
        tx.candidates.register(saved.ref, saved.revision, observed_at=now, schedule_initial=False)
        entry["revision"] = saved.revision
    return entry


def restore_displaced_values(
    tx, *, now: str, after_ref: str = "", limit: int = 16, dry_run: bool = True
) -> RequalifyReport:
    """Withdraw each denial of one bounded page that holds its claim's head without being its slot's value, and give
    back the value it displaced.

    A denial is its slot's value only as the person's own answer (``mutate.denial_verdict``): any other at the head
    ("not CircleCI" over Jenkins, "not us-east-2" as a project's region) reads as the slot's value, and is settled as
    ``_settle_denial`` says; the value a denial of another value displaced comes back if it was the slot's value when
    the denial was written and holds still.  ``requalify_claims`` settles them the same way.  The report names refs and
    revisions only, never claim text.
    """
    refs = _page(tx, after_ref, limit)
    report = RequalifyReport(applied=not dry_run)
    for ref in refs:
        report.last_ref = ref
        versions = tx.claims.versions(ref)
        head = next((v for v in versions if v.revision == v.current_revision), None)
        if head is None or head.state not in {"active", "disputed"} or not is_denial(head.payload):
            continue
        report.examined += 1
        held, reason = _denial_standing(tx, head, versions)
        if reason is not None:
            report.changed.append(_settle_denial(tx, head, held, reason, now=now, dry_run=dry_run))
    return report


__all__ = [
    "MAX_PAGE",
    "REQUALIFIABLE_STATES",
    "ROOTLESS_REASON",
    "RequalifyReport",
    "requalify_claims",
    "restore_displaced_values",
    "retire_rootless_proposals",
]


def repair_frames(tx, *, now, after_ref="", limit=16):
    """Bounded upgrade repair through the ordinary evidence/application path.

    The caller persists the cursor; old versions and original sources remain.
    No model call, manual approval, or fabricated claim payload is involved.
    """
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ContractError("INPUT_INVALID", "normalization_limit")
    # Filter authorization before pagination so an already-visited prefix or
    # another audience cannot starve later records.
    scopes = sorted(tx.context.allowed_scope_ids)
    rows = (
        tx._check()
        .execute(
            f"""SELECT claim_id FROM claims WHERE claim_id>? AND read_blocked=0 AND suppressed=0
        AND scope_id IN ({",".join("?" for _ in scopes)}) AND project_id IS ? AND branch_id IS ?
        ORDER BY claim_id LIMIT ?""",
            (after_ref, *scopes, tx.context.project_id, tx.context.branch_id, limit),
        )
        .fetchall()
    )
    repaired, errors = [], []
    for row in rows:
        versions = tx.claims.versions(row[0])
        head = next((v for v in versions if v.revision == v.current_revision), None)
        if head is None or head.state not in {"active", "proposed", "disputed"}:
            continue
        try:
            with tx.savepoint():
                roots = tx.claims.roots(evidence_refs(head.payload))
                frames = expand_frames(head.payload, roots)
                if len(frames) == 1 and same_assertion(head.payload, frames[0]):
                    continue
                mutation = apply_claim(tx, frames[0], head.scope_id, now)
                register_applied_candidate(tx, mutation, now)
                for sibling in frames[1:]:
                    sibling_mutation = apply_claim(tx, sibling, head.scope_id, now)
                    register_applied_candidate(tx, sibling_mutation, now)
                    repaired.append(
                        {
                            "original_ref": head.ref,
                            "ref": sibling_mutation.ref,
                            "revision": sibling_mutation.revision,
                            "state": sibling_mutation.state,
                            "disposition": sibling_mutation.disposition,
                        }
                    )
                if mutation.ref != head.ref and mutation.state == "active" and head.state in {"active", "disputed"}:
                    # Retire a duplicate legacy frame only after its corrected
                    # canonical replacement passed ordinary evidence admission.
                    # Append a version; never rewrite the original payload.
                    retired = tx.claims.append(
                        head.scope_id,
                        head.payload,
                        Qualification("retracted", head.basis, "canonical_frame_reconciled"),
                        recorded_at=now,
                        previous=head,
                    )
                    tx.candidates.register(retired.ref, retired.revision, observed_at=now, schedule_initial=False)
                if mutation.ref != head.ref and head.state == "proposed":
                    conn = tx._check(write=True)
                    conn.execute(
                        "UPDATE candidate_lifecycle SET processing_state='archived',reason='canonical_frame_reconciled',updated_at=? WHERE candidate_ref=?",
                        (now, head.ref),
                    )
                    conn.execute(
                        "UPDATE work_items SET state='obsolete',last_error_code='canonical_frame_reconciled' WHERE work_type='evaluate_candidate' AND subject_ref=? AND state IN ('pending','leased')",
                        ("candidate:" + head.ref,),
                    )
                    conn.execute(
                        "UPDATE candidate_evaluations SET state='obsolete',reason='canonical_frame_reconciled',completed_at=? WHERE candidate_ref=? AND state='queued'",
                        (now, head.ref),
                    )
                repaired.append(
                    {
                        "original_ref": head.ref,
                        "ref": mutation.ref,
                        "revision": mutation.revision,
                        "state": mutation.state,
                        "disposition": mutation.disposition,
                    }
                )
        except ContractError as exc:
            errors.append({"ref": head.ref, "code": exc.code, "field": exc.field})
    return {
        "cursor": rows[-1][0] if rows else after_ref,
        "scanned": len(rows),
        "items": repaired,
        "errors": errors,
        "done": len(rows) < limit,
    }
