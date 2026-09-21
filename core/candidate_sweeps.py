"""Bounded maintenance passes over candidates, and the counts the doctor reports.

Each sweep takes one page, is idempotent, and either schedules through
``CandidateIntake`` or moves rows nothing else will move: candidates whose
evidence settled after their conversation ended, candidates that went quiet
for a month, and evaluations that died only on an input bound that has since
changed.
"""
from __future__ import annotations

from datetime import timedelta

from ..contracts import ContractError
from .candidate_debounce import MAX_DEFERRAL_SECONDS, QUIET_SECONDS
from .candidate_intake import CandidateIntake
from .candidate_lifecycle import DORMANCY_DAYS, RULE_VERSION, CandidateSummary
from .candidate_tables import (
    HEAD_COLUMNS, HEAD_JOINS, is_live_head, parse_refs, parse_time, rule, settled_reason, snapshot, stamp,
)
from .schema import SCHEMA_VERSION


class CandidateSweeps(CandidateIntake):
    """Page-bounded repairs and reports over the candidate tables."""

    def archive_dormant(self, *, now: str, days: int = DORMANCY_DAYS, limit: int = 8) -> int:
        if type(days) is not int or not 1 <= days <= 3650 or type(limit) is not int or not 1 <= limit <= 8:
            raise ContractError("INPUT_INVALID", "candidate_dormancy")
        moment = parse_time(now)
        now, cutoff = stamp(moment), stamp(moment - timedelta(days=days))
        context, params = self._context()
        rows = self._read().execute(
            f"""SELECT candidate_ref,candidate_revision FROM candidate_lifecycle
                WHERE {context} AND processing_state='waiting_evidence'
                  AND COALESCE(last_evidence_at,updated_at)<=?
                ORDER BY COALESCE(last_evidence_at,updated_at),candidate_ref,candidate_revision LIMIT ?""",
            (*params, cutoff, limit),
        ).fetchall()
        for row in rows:
            self._move(row["candidate_ref"], row["candidate_revision"], "archived", "dormant_no_evidence",
                       now=now, dormant_at=now)
        return len(rows)

    def summary(self, *, include_all_projects: bool = False) -> CandidateSummary:
        if type(include_all_projects) is not bool:
            raise ContractError("INPUT_INVALID", "candidate_summary_scope")
        conn = self._read()
        context, params = self._context(all_projects=include_all_projects)
        counts = dict(conn.execute(
            f"SELECT processing_state,count(*) FROM candidate_lifecycle WHERE {context} GROUP BY processing_state",
            params,
        ).fetchall())
        dormant, oldest, capability = conn.execute(
            f"""SELECT sum(processing_state='archived' AND reason='dormant_no_evidence'),
                       min(CASE WHEN processing_state IN ('pending_evaluation','waiting_evidence') THEN updated_at END),
                       sum(processing_state='pending_evaluation' AND reason='capability_unavailable')
                FROM candidate_lifecycle WHERE {context}""",
            params,
        ).fetchone()
        work_context, work_params = self._context("w.", all_projects=include_all_projects)
        failed, budget = conn.execute(
            f"""SELECT sum(w.state='failed'),
                       sum(w.state='pending' AND (w.last_error_code IN ('budget_exhausted','budget_unavailable')
                           OR w.last_error_code LIKE '%|budget_exhausted'
                           OR w.last_error_code LIKE '%|budget_unavailable'))
                FROM work_items w WHERE {work_context} AND w.work_type='evaluate_candidate'""",
            work_params,
        ).fetchone()
        dormant = int(dormant or 0)
        return CandidateSummary(
            pending_evaluation=int(counts.get("pending_evaluation", 0)),
            waiting_evidence=int(counts.get("waiting_evidence", 0)),
            dormant=dormant,
            blocked=int(counts.get("blocked", 0)),
            resolved=int(counts.get("resolved", 0)),
            archived_other=max(0, int(counts.get("archived", 0)) - dormant),
            failed=int(failed or 0),
            budget_paused=int(budget or 0),
            capability_unavailable=int(capability or 0),
            oldest_waiting_at=oldest,
        )

    def mark_capability_unavailable(self) -> int:
        context, params = self._context()
        return self._write().execute(
            f"""UPDATE candidate_lifecycle SET reason='capability_unavailable'
                WHERE {context} AND processing_state='pending_evaluation'
                  AND EXISTS(SELECT 1 FROM candidate_evaluations e JOIN work_items w ON w.work_id=e.work_id
                      WHERE e.candidate_ref=candidate_lifecycle.candidate_ref
                        AND e.candidate_revision=candidate_lifecycle.candidate_revision
                        AND e.state='queued' AND w.state='pending')""",
            params,
        ).rowcount

    def _settling_rows(self):
        """Live candidates holding evidence, for the settle sweep and the doctor alike.

        ``waiting_evidence`` is included, not just ``pending_evaluation``: a
        candidate judged "not enough" while more evidence already sat in the
        table would otherwise never be looked at again.  ``authority_revoked``
        is the one reason that must not be revived -- that is a decision, not
        a shortage.
        """
        context, params = self._context("l.")
        return self._read().execute(
            f"""SELECT {HEAD_COLUMNS},l.last_evidence_at,l.last_evaluated_at,l.created_at,
                       EXISTS(SELECT 1 FROM candidate_evaluations e WHERE e.candidate_ref=l.candidate_ref
                         AND e.candidate_revision=l.candidate_revision AND e.state='queued') AS queued
                FROM candidate_lifecycle l {HEAD_JOINS}
                WHERE l.processing_state IN ('pending_evaluation','waiting_evidence')
                  AND l.reason<>'authority_revoked' AND {context}
                  AND c.current_revision=l.candidate_revision AND c.read_blocked=0 AND c.suppressed=0
                  AND v.state IN ('proposed','disputed')
                  AND EXISTS(SELECT 1 FROM candidate_evidence ev
                      WHERE ev.candidate_ref=l.candidate_ref AND ev.candidate_revision=l.candidate_revision)
                ORDER BY l.last_evidence_at,l.candidate_ref,l.candidate_revision""",
            params,
        ).fetchall()

    def _settling_eligible(self, row, *, now: str, rule_version: str) -> str | None:
        """The settle reason, but only when scheduling would ask a new question."""
        if row["queued"]:
            return None
        reason = settled_reason(row, now)
        if reason is None:
            return None
        refs, digest = self._question(row["candidate_ref"], row["candidate_revision"])
        if not refs:
            return None
        asked = self._read().execute(
            """SELECT 1 FROM candidate_evaluations WHERE candidate_ref=? AND candidate_revision=?
               AND evidence_fingerprint=? AND rule_version=?""",
            (row["candidate_ref"], row["candidate_revision"], digest, rule_version),
        ).fetchone()
        return None if asked else reason

    def schedule_settled_candidates(self, *, now: str, rule_version: str = RULE_VERSION, limit: int = 16) -> int:
        """Queue one evaluation for each candidate whose evidence has settled.

        ``observe_source`` does not schedule while evidence is still arriving,
        so something has to notice when it stops; this runs once at the start
        of a drain.  Oldest evidence first is also most-settled first, so a
        page can never fill with candidates still collecting while a settled
        one waits behind them.  Idempotent: a queued evaluation excludes its
        candidate, and an unchanged evidence set collides on its fingerprint.
        """
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ContractError("INPUT_INVALID", "settle_limit")
        rule_version = rule(rule_version)
        scheduled = 0
        for row in self._settling_rows():
            if scheduled >= limit:
                break
            reason = self._settling_eligible(row, now=now, rule_version=rule_version)
            if reason is None:
                continue
            _, queued = self._schedule(snapshot(row, processing_state="pending_evaluation"),
                                       now=now, rule_version=rule_version)
            if queued:
                self._move(row["candidate_ref"], row["candidate_revision"], "pending_evaluation", reason, now=now)
                scheduled += 1
        return scheduled

    def settling_summary(self, *, now: str) -> dict[str, int]:
        """Candidates waiting inside their window versus waiting for the sweep.

        Debouncing deliberately grows ``candidate_pending_evaluation``, so the
        doctor has to tell "waiting on purpose" from "stuck".  There is a
        fourth state, and it is the large one on a live store: evidence that
        has settled and that scheduling would ask nothing new about, because
        the same evidence was already put to the evaluator (whatever came of
        it) or none of it can be read.  Such a candidate is not work.  It
        waits for new evidence, and counted as pending it made a queue that
        never drains out of a thousand candidates nothing was ever going to
        evaluate.
        """
        summary = {"queued": 0, "collecting": 0, "settled_waiting_sweep": 0, "settled_nothing_to_ask": 0}
        for row in self._settling_rows():
            if row["queued"]:
                summary["queued"] += 1
            elif self._settling_eligible(row, now=now, rule_version=RULE_VERSION):
                summary["settled_waiting_sweep"] += 1
            elif settled_reason(row, now) is None:
                summary["collecting"] += 1
            else:
                summary["settled_nothing_to_ask"] += 1
        summary["quiet_seconds"] = QUIET_SECONDS
        summary["max_deferral_seconds"] = MAX_DEFERRAL_SECONDS
        return summary

    def reschedule_budget_blocked_candidates(self, *, now: str, rule_version: str = RULE_VERSION, limit: int = 8) -> int:
        """Give candidates a fresh evaluation after the evidence bound changed.

        ``recover_oversized_evaluations`` re-runs the *same* evidence set, so a
        candidate whose set is genuinely too large keeps its ``budget_checked``
        marker and is never retried by it.  Evidence is now bounded by bytes
        and prefers sources that fit, so the same candidate yields a different
        set, a new fingerprint and a genuinely new evaluation; an unchanged
        set collides and inserts nothing, so this cannot loop.
        """
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ContractError("INPUT_INVALID", "repair_limit")
        rule_version = rule(rule_version)
        conn = self._write()
        # A rebounded marker has had its re-look; left ``failed`` it keeps the
        # doctor reporting work nobody can act on, which hides a real failure.
        conn.execute(
            """UPDATE work_items SET state='obsolete'
               WHERE work_type='evaluate_candidate' AND state='failed' AND last_error_code LIKE 'budget_rebounded:%'"""
        )
        context, params = self._context("l.")
        rows = conn.execute(
            f"""SELECT DISTINCT {HEAD_COLUMNS}
                FROM work_items w
                JOIN candidate_evaluations e ON e.evaluation_id=w.subject_revision
                JOIN candidate_lifecycle l ON l.candidate_ref=e.candidate_ref AND l.candidate_revision=e.candidate_revision
                {HEAD_JOINS}
                WHERE w.work_type='evaluate_candidate' AND w.state='failed'
                  AND w.last_error_code LIKE 'budget_checked:%'
                  AND l.processing_state NOT IN ('resolved','blocked') AND {context}
                ORDER BY l.candidate_ref LIMIT ?""",
            (*params, limit),
        ).fetchall()
        rescheduled = 0
        for row in rows:
            # Retire the marker first: whether or not this candidate schedules,
            # it has now had its one look under the new bound.
            conn.execute(
                """UPDATE work_items SET state='obsolete',last_error_code='budget_rebounded:'||?||'|input_invalid'
                   WHERE work_type='evaluate_candidate' AND state='failed' AND last_error_code LIKE 'budget_checked:%'
                     AND subject_revision IN (SELECT evaluation_id FROM candidate_evaluations
                                              WHERE candidate_ref=? AND candidate_revision=?)""",
                (str(SCHEMA_VERSION), row["candidate_ref"], row["candidate_revision"]),
            )
            if not is_live_head(row):
                continue
            self._move(row["candidate_ref"], row["candidate_revision"], "pending_evaluation", "evidence_bound_changed", now=now)
            _, queued = self._schedule(snapshot(row, processing_state="pending_evaluation"),
                                       now=now, rule_version=rule_version)
            rescheduled += int(queued)
        return rescheduled

    def recover_oversized_evaluations(self, *, now: str, formatter, limit: int = 8) -> int:
        """Re-queue evaluations that died only on the old input budget.

        The real formatter decides.  An evaluation that fits moves all three
        rows back together; one still oversized keeps its failed state and
        gets a durable marker, so it cannot occupy this page twice.
        """
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ContractError("INPUT_INVALID", "repair_limit")
        context, params = self._context("l.")
        rows = self._read().execute(
            f"""SELECT e.evaluation_id,e.evidence_refs_json,w.work_id,{HEAD_COLUMNS}
                FROM candidate_evaluations e
                JOIN candidate_lifecycle l ON l.candidate_ref=e.candidate_ref AND l.candidate_revision=e.candidate_revision
                {HEAD_JOINS}
                JOIN work_items w ON w.work_id=e.work_id
                WHERE e.state='failed' AND UPPER(e.failure_code)='INPUT_INVALID'
                  AND w.work_type='evaluate_candidate' AND w.state='failed'
                  AND UPPER(w.last_error_code)='INPUT_INVALID' AND {context}
                ORDER BY e.evaluation_id LIMIT ?""",
            (*params, limit),
        ).fetchall()
        recovered = 0
        for row in rows:
            fits = self._fits_budget(row, formatter)
            moved = self._tx.work.reopen_oversized_candidate_evaluation(row["work_id"], now=now, fits=fits)
            if not (fits and moved):
                continue
            self._requeue_failed(row["evaluation_id"], "budget_upgrade")
            self._move(row["candidate_ref"], row["candidate_revision"], "pending_evaluation", "budget_upgrade", now=now)
            recovered += 1
        return recovered

    def _fits_budget(self, row, formatter) -> bool:
        """Whether the formatter now accepts this evaluation's stored evidence set as it is on disk."""
        if not is_live_head(row):
            return False
        try:
            sources = tuple(self._tx.source(ref, revision) for ref, revision in parse_refs(row["evidence_refs_json"]))
            if any(source is None or source.suppressed for source in sources):
                return False
            formatter(snapshot(row), sources)
        except ContractError as exc:
            if exc.code in {"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED"}:
                raise
            return False
        except (ValueError, TypeError, AttributeError):
            return False
        return True


__all__ = ["CandidateSweeps"]
