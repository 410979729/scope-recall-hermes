"""Re-judge stored claims after the rules that judged them have changed.

A claim is judged once, when it is written.  Nothing ever looks at it again, so
a gate repair only ever helps claims captured *after* the repair ships -- the
366 proposals already in one instance's store would keep their original verdicts
forever, including the ones refused by a rule that has since been fixed.

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
from .evidence_question import DERIVATION_ROOT_ORIGINS, NO_DERIVATION_ROOT_REASON

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
    maintenance pass.

    Imported lazily so this module stays free of import cycles with ``mutate``.
    """
    from .confirmation import CONFIRMED_REASON
    from .corroboration import CORROBORATED_REASON

    return frozenset({CONFIRMED_REASON, CORROBORATED_REASON})


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


def requalify_claims(tx, *, now: str, after_ref: str = "", limit: int = 16,
                     dry_run: bool = True) -> RequalifyReport:
    """Re-judge one bounded page of stored claims.  Returns what moved."""
    from .claims import Qualification, bind_claim_subject, qualify, same_assertion
    from .mutate import evidence_refs

    if type(limit) is not int or type(limit) is bool or not 1 <= limit <= MAX_PAGE:
        raise ContractError("INPUT_INVALID", "requalify_limit")
    if type(after_ref) is not str:
        raise ContractError("INPUT_INVALID", "requalify_cursor")

    # Authorization filters before pagination, so a visited prefix or another
    # audience cannot starve the records behind it.
    scopes = sorted(tx.context.allowed_scope_ids)
    rows = tx._check().execute(
        f"""SELECT claim_id FROM claims WHERE claim_id>? AND read_blocked=0 AND suppressed=0
            AND scope_id IN ({','.join('?' for _ in scopes)})
            AND project_id IS ? AND branch_id IS ?
            ORDER BY claim_id LIMIT ?""",
        (after_ref, *scopes, tx.context.project_id, tx.context.branch_id, limit),
    ).fetchall()

    preserved = _preserved_reasons()
    report = RequalifyReport(applied=not dry_run)
    for row in rows:
        ref = row[0]
        report.last_ref = ref
        head = next((v for v in tx.claims.versions(ref) if v.revision == v.current_revision), None)
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
                else qualify(proposal, roots, project_id=tx.context.project_id,
                             _subject_bound=subject_bound)
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
            saved = tx.claims.append(head.scope_id, proposal, verdict, recorded_at=now,
                                     previous=head, advance_head=True)
            entry["revision"] = saved.revision
        report.changed.append(entry)
    return report


#: The reason a retired proposal carries: none of its evidence is a source consolidation derives from.
ROOTLESS_REASON = NO_DERIVATION_ROOT_REASON


def retire_rootless_proposals(tx, *, now: str, after_ref: str = "", limit: int = 16,
                              dry_run: bool = True) -> RequalifyReport:
    """Retire one bounded page of proposed claims that no derivation root supports.

    Consolidation derives claims only from ``DERIVATION_ROOT_ORIGINS``; tool output left that set in
    3.2.0rc6.  A proposal whose every evidence source is of another origin would not be derived
    today, and it is still waiting for a proof nobody asked for: it gets a retracted version with
    ``ROOTLESS_REASON``, registered as the candidate's new head so its queued evaluations end.  Its
    sources and its earlier versions stay.  Active and disputed claims are left alone -- a claim that
    was proved stands on that proof -- and nothing is re-judged here; ``requalify_claims`` does that.
    Nor is a proposal a person has restated since (``restated_by_a_root``): they are heard in its
    evaluation, not in its own evidence, and the verdict on their words decides.

    Separate from ``requalify_claims`` on purpose: a re-judgement moves claims for every rule that
    changed since they were written (on the pilot, 154 of them, promotions included), and retiring
    these must not bring that along.  The report names refs and verdicts only, never claim text.
    """
    from .claims import Qualification
    from .episodes import source_origin
    from .mutate import evidence_refs

    if type(limit) is not int or type(limit) is bool or not 1 <= limit <= MAX_PAGE:
        raise ContractError("INPUT_INVALID", "requalify_limit")
    if type(after_ref) is not str:
        raise ContractError("INPUT_INVALID", "requalify_cursor")
    scopes = sorted(tx.context.allowed_scope_ids)
    rows = tx._check().execute(
        f"""SELECT claim_id FROM claims WHERE claim_id>? AND read_blocked=0 AND suppressed=0
            AND scope_id IN ({','.join('?' for _ in scopes)})
            AND project_id IS ? AND branch_id IS ?
            ORDER BY claim_id LIMIT ?""",
        (after_ref, *scopes, tx.context.project_id, tx.context.branch_id, limit),
    ).fetchall()
    report = RequalifyReport(applied=not dry_run)
    for row in rows:
        ref = row[0]
        report.last_ref = ref
        head = next((v for v in tx.claims.versions(ref) if v.revision == v.current_revision), None)
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
        entry = {"ref": ref, "was": f"{head.state}:{head.reason}", "now": f"retracted:{ROOTLESS_REASON}",
                 "origins": sorted(origins)}
        if not dry_run:
            retired = tx.claims.append(head.scope_id, head.payload,
                                       Qualification("retracted", head.basis, ROOTLESS_REASON),
                                       recorded_at=now, previous=head)
            tx.candidates.register(retired.ref, retired.revision, observed_at=now, schedule_initial=False)
            entry["revision"] = retired.revision
        report.changed.append(entry)
    return report


__all__ = ["MAX_PAGE", "REQUALIFIABLE_STATES", "ROOTLESS_REASON", "RequalifyReport", "requalify_claims",
           "retire_rootless_proposals"]
