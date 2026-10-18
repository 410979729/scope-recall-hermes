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

from dataclasses import dataclass, field, replace
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
from .storage import Transaction

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
    gate alone would promote it.  None of them is above the denial rule, which
    is no text gate: a vouched-for denial that is no value is still withdrawn.

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
    """Re-judge one bounded page of stored claims.  Returns what moved.

    A denial is judged as writing judges it (``mutate.denial_verdict``): one at the head that is no value of its slot
    is withdrawn (``_settle_denial``), whatever made it active, a second witness or a confirmation too; and one the
    text gate alone would promote stays a proposal."""
    refs = _page(tx, after_ref, limit)
    preserved = _preserved_reasons()
    report = RequalifyReport(applied=not dry_run)
    for ref in refs:
        report.last_ref = ref
        versions = tx.claims.versions(ref)
        head = next((v for v in versions if v.revision == v.current_revision), None)
        if head is None or head.state not in REQUALIFIABLE_STATES:
            continue
        held, denial_reason = _denial_standing(tx, head, versions)
        settled_as = denial_reason if head.state != "proposed" else None
        if head.reason in preserved and settled_as is None:
            report.skipped.append({"ref": ref, "why": f"not_text_derived:{head.reason}"})
            continue
        report.examined += 1
        if settled_as is not None:
            report.changed.append(_settle_denial(tx, head, held, settled_as, now=now, dry_run=dry_run))
            continue
        try:
            roots = tx.claims.roots(evidence_refs(head.payload))
            proposal, subject_bound, binding_issue = bind_claim_subject(head.payload, roots, stored=True)
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
        if denial_reason is not None and verdict.state != "proposed":
            verdict = Qualification("proposed", "inferred_suggestion", denial_reason)
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
    """What the slot held when ``head``, a denial, was written (``_value_under``), and why the denial is no value of
    its slot there (``mutate.denial_verdict``, as writing it now would judge), or None where it is one."""
    earlier = tuple(v for v in versions if v.revision < head.revision)
    if not is_denial(head.payload, (v.payload for v in earlier if v.state != "proposed")):
        return None, None
    held = _value_under(tx, head, versions)
    reason, _ends = denial_verdict(head.payload, held, tx.claims.roots(evidence_refs(head.payload)), earlier)
    return held, reason


def _value_under(tx, version, versions):
    """The value the slot held when ``version`` was written, of the versions before it (``select_effective``), passing
    over one that is a denial and no value of its slot itself: an older rule stacked denials as values, and "not
    Travis" over "not CircleCI" over Jenkins displaced Jenkins, "not Jenkins" over "not CircleCI" alone nothing."""
    earlier = tuple(v for v in versions if v.revision < version.revision)
    while earlier:
        held = select_effective(earlier, version.recorded_from)
        if held is None or _denial_standing(tx, held, earlier)[1] is None:
            return held
        earlier = tuple(v for v in earlier if v.revision != held.revision)
    return None


def _settle_denial(tx, head, held, reason: str, *, now: str, dry_run: bool) -> dict[str, Any]:
    """Withdraw ``head``, a denial at its claim's head that is no value of its slot (``_denial_standing``), with a
    retracted version, so no read at any instant takes it for the value; and where it denied another value than the
    one held, re-instate that value as it was, if it still holds (``valid_at``): an expired one is not.  Every version
    stays.  The entry names the claim, its subject, predicate and current value, and the revision and value it gives
    back (``restores``, ``gives_back``), both None where it will hold none, so a preview can be read before it is
    applied."""
    displaced = held if reason == DENIAL_REASON else None
    restores = displaced if displaced is not None and valid_at(displaced, now, historical=False) else None
    entry = {
        "ref": head.ref,
        "was": f"{head.state}:{head.reason}",
        "now": f"retracted:{reason}",
        "subject": head.payload.get("subject"),
        "predicate": head.payload.get("predicate"),
        "value": head.payload.get("value_text"),
        "restores": None if restores is None else restores.revision,
        "gives_back": None if restores is None else restores.payload.get("value_text"),
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
    the denial was written and holds still.  ``requalify_claims`` settles them the same way.  The report names each
    claim it changes, or as a preview would change, by ref, subject, predicate and current value, with the value it
    gives back or None where the claim will hold none (``_settle_denial``).
    """
    refs = _page(tx, after_ref, limit)
    report = RequalifyReport(applied=not dry_run, last_ref=refs[-1] if refs else "")
    report.examined, report.changed = settle_denials(tx, refs, now=now, dry_run=dry_run)
    return report


def settle_denials(tx, refs, *, now: str, dry_run: bool = False) -> tuple[int, list[dict[str, Any]]]:
    """Settle each of ``refs`` in ``tx``'s project and branch whose head is a denial that holds it without being its
    slot's value (``_denial_standing``, ``_settle_denial``): how many heads were denials, and what moved."""
    examined, changed = 0, []
    for ref in refs:
        versions = tx.claims.versions(ref)
        head = next((v for v in versions if v.revision == v.current_revision), None)
        if (
            head is None
            or head.state not in {"active", "disputed"}
            or not is_denial(
                head.payload, (v.payload for v in versions if v.revision < head.revision and v.state != "proposed")
            )
        ):
            continue
        examined += 1
        held, reason = _denial_standing(tx, head, versions)
        if reason is not None:
            changed.append(_settle_denial(tx, head, held, reason, now=now, dry_run=dry_run))
    return examined, changed


def settle_brought_denials(tx, refs, *, now: str) -> int:
    """Settle ``refs`` as ``settle_denials`` does, each in its own project and branch, and say how many moved: the
    claims a conversion or an import brings in whole, as a writer judged them that took a denial for its slot's
    value, settled in the operation's own transaction (``tx``) before anything reads them.  A transaction reads one
    project and branch, so each is settled in one scoped to it, on ``tx``'s connection."""
    conn = tx._check(write=True)
    partitions: dict[tuple[str | None, str | None], list[str]] = {}
    for ref in refs:
        row = conn.execute("SELECT project_id, branch_id FROM claims WHERE claim_id=?", (ref,)).fetchone()
        if row is not None:
            partitions.setdefault((row[0], row[1]), []).append(ref)
    settled = 0
    for (project, branch), claim_refs in sorted(partitions.items(), key=repr):
        scoped = Transaction(conn, replace(tx.context, project_id=project, branch_id=branch), writable=True)
        try:
            settled += len(settle_denials(scoped, claim_refs, now=now)[1])
        finally:
            scoped._finish()
    return settled


__all__ = [
    "MAX_PAGE",
    "REQUALIFIABLE_STATES",
    "ROOTLESS_REASON",
    "RequalifyReport",
    "requalify_claims",
    "restore_displaced_values",
    "retire_rootless_proposals",
    "settle_brought_denials",
    "settle_denials",
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
