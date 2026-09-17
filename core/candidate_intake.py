"""Bringing candidates in: registration, source triggers and evaluation scheduling.

A claim version becomes a candidate when C2 registers it; sources that mention
it afterwards add evidence and, once that evidence settles, one evaluation is
queued per open question.  Nothing here writes a claim or grants authority.
"""
from __future__ import annotations

from ..contracts import ContractError
from .candidate_lifecycle import (
    RULE_VERSION, SOURCE_MATCH_LIMIT, CandidateRegistration, CandidateSourceTrigger,
)
from .candidate_tables import (
    ECHO_ORIGINS, EMPTY_FINGERPRINT, JUDGEABLE_STATES, CandidateTables, encode_refs, is_reachable, reachable_sql,
    rule, settled_reason, utc,
)
from .events import lexical_terms
from .evidence_question import (
    AUTOMATIC_VERDICTS,
    PERSON_ABSENT_REASON,
    REPEAT_WITHOUT_RESTATEMENT_REASON,
    evidence_text,
    needs_absent_person,
    unanswerable_reason,
)

#: Truncated source triggers still owe a page of candidates, joined to their
#: source so the audience filter can apply.
_TRUNCATED_TRIGGERS = """FROM candidate_source_triggers t
    JOIN source_events s ON s.event_id=t.source_ref AND s.source_revision=t.source_revision
    WHERE t.truncated=1 AND {context}"""


class CandidateIntake(CandidateTables):
    """Register claim versions, index them for triggers and queue evaluations."""

    def register(
        self,
        candidate_ref: str,
        candidate_revision: int,
        *,
        observed_at: str,
        rule_version: str = RULE_VERSION,
        schedule_initial: bool = True,
    ) -> CandidateRegistration:
        """Observe one already-written fact version and register lifecycle metadata.

        Performs no claim write: C2 calls it inside the same transaction after
        its authoritative fact application.
        """
        now = utc(observed_at)
        rule_version = rule(rule_version)
        if type(schedule_initial) is not bool:
            raise ContractError("INPUT_INVALID", "candidate_schedule_initial")
        if type(candidate_ref) is not str or not candidate_ref or type(candidate_revision) is not int or candidate_revision < 1:
            raise ContractError("INPUT_INVALID", "candidate_ref")
        candidate = self._tx.claims.version(candidate_ref, candidate_revision)
        if candidate is None:
            raise ContractError("SOURCE_MISSING", "candidate")
        self._tx.claims.require_target(candidate)
        if candidate.current_revision != candidate_revision:
            raise ContractError("VERSION_CONFLICT", "candidate_revision")
        prior = self._lifecycle_row(candidate_ref, candidate_revision)
        state, reason, updated_at = _registration_target(candidate, prior, rule_version, now)
        if state == "pending_evaluation" and needs_absent_person(
                candidate.payload, self._cited_origins(candidate.ref, candidate.revision)):
            # See core/evidence_question.py: only the person's own words can
            # promote it, and their consolidation proposes it with that authority.
            state, reason, updated_at = "archived", PERSON_ABSENT_REASON, now
            self._retire_evaluations(candidate.ref, candidate.revision, PERSON_ABSENT_REASON, now)
        self._write_lifecycle(candidate, state, reason, rule_version, now, updated_at)
        evaluation_id, queued = None, False
        if is_reachable(state, reason):
            self._index(candidate, now)
            if state == "pending_evaluation" and schedule_initial:
                evaluation_id, queued = self._schedule(candidate, now=now, rule_version=rule_version)
            elif state == "pending_evaluation":
                self._move(candidate.ref, candidate.revision, "waiting_evidence", "evaluated_waiting_evidence",
                           now=now, evaluated_at=now)
            current = self._lifecycle_row(candidate.ref, candidate.revision)
            state, reason = current["processing_state"], current["reason"]
        disposition = "inserted" if prior is None else "updated"
        if prior is not None and (prior["processing_state"], prior["reason"]) == (state, reason) and not queued:
            disposition = "unchanged"
        return CandidateRegistration(candidate.ref, candidate.revision, state, reason, disposition, evaluation_id, queued)

    def _write_lifecycle(self, candidate, state: str, reason: str, rule_version: str, now: str, updated_at: str) -> None:
        """Upsert the candidate's row; older revisions of the claim can no longer be judged."""
        conn = self._write()
        conn.execute(
            """UPDATE candidate_lifecycle SET processing_state='archived',reason='superseded_by_candidate_revision',
               dormant_at=?,updated_at=? WHERE candidate_ref=? AND candidate_revision<>?
               AND processing_state NOT IN ('resolved','blocked')""",
            (now, now, candidate.ref, candidate.revision),
        )
        self._retire_evaluations(candidate.ref, candidate.revision, "candidate_revision_changed", now, others=True)
        conn.execute(
            """INSERT INTO candidate_lifecycle(
                candidate_ref,candidate_revision,scope_id,project_id,branch_id,processing_state,reason,
                rule_version,evidence_fingerprint,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_ref,candidate_revision) DO UPDATE SET
                    processing_state=excluded.processing_state,reason=excluded.reason,
                    rule_version=excluded.rule_version,updated_at=excluded.updated_at""",
            (candidate.ref, candidate.revision, candidate.scope_id, candidate.project_id, candidate.branch_id,
             state, reason, rule_version, EMPTY_FINGERPRINT, now, updated_at),
        )
        conn.execute(
            "DELETE FROM candidate_trigger_terms WHERE candidate_ref=? AND candidate_revision=?",
            (candidate.ref, candidate.revision),
        )

    def _index(self, candidate, now: str) -> None:
        """Make the candidate findable by later sources and seed it from its own evidence links."""
        conn = self._write()
        conn.executemany(
            "INSERT INTO candidate_trigger_terms(term,candidate_ref,candidate_revision) VALUES (?,?,?)",
            ((term, candidate.ref, candidate.revision) for term in _trigger_terms(candidate)),
        )
        for link in conn.execute(
            """SELECT source_ref,source_revision FROM evidence_links
               WHERE object_kind='claim' AND object_ref=? AND object_revision=?
               ORDER BY source_ref,source_revision""",
            (candidate.ref, candidate.revision),
        ).fetchall():
            self._add_evidence(candidate, link["source_ref"], link["source_revision"], now)

    def _add_evidence(self, candidate, source_ref: str, source_revision: int, now: str) -> bool:
        source = self._tx.source(source_ref, source_revision)
        if source is None or source.suppressed or (source.event or {}).get("origin") in ECHO_ORIGINS:
            return False
        if (source.scope_id, source.project_id, source.branch_id) != (
            candidate.scope_id, candidate.project_id, candidate.branch_id,
        ):
            return False
        return self._write().execute(
            """INSERT INTO candidate_evidence(
                candidate_ref,candidate_revision,source_ref,source_revision,observed_at)
                VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (candidate.ref, candidate.revision, source_ref, source_revision, now),
        ).rowcount == 1

    def observe_source(
        self,
        source_ref: str,
        source_revision: int,
        *,
        observed_at: str,
        rule_version: str = RULE_VERSION,
        limit: int = SOURCE_MATCH_LIMIT,
        _resume: bool = False,
    ) -> CandidateSourceTrigger:
        """Wake a bounded set of indexed, same-audience candidates with this source."""
        if type(limit) is not int or not 1 <= limit <= SOURCE_MATCH_LIMIT:
            raise ContractError("INPUT_INVALID", "candidate_match_limit")
        now = utc(observed_at)
        rule_version = rule(rule_version)
        source = self._tx.source(source_ref, source_revision)
        current = self._tx.source_current(source_ref)
        if source is None or source.suppressed or current is None or current.revision != source_revision:
            raise ContractError("SOURCE_MISSING", "candidate_trigger_source")
        conn = self._write()
        prior = conn.execute(
            "SELECT * FROM candidate_source_triggers WHERE source_ref=? AND source_revision=?",
            (source_ref, source_revision),
        ).fetchone()
        if prior is not None and not (_resume and prior["truncated"]):
            return CandidateSourceTrigger(source_ref, source_revision, "duplicate", prior["matched_count"],
                                          prior["scheduled_count"], bool(prior["truncated"]))
        rows = self._candidates_mentioned_by(source, limit + 1)
        truncated = len(rows) > limit
        matched = scheduled = 0
        for row in rows[:limit]:
            candidate = self._tx.claims.version(row["candidate_ref"], row["candidate_revision"])
            if candidate is None or candidate.current_revision != candidate.revision:
                continue
            if not self._add_evidence(candidate, source_ref, source_revision, now):
                continue
            matched += 1
            conn.execute(
                f"""UPDATE candidate_lifecycle SET processing_state='pending_evaluation',reason='new_evidence',
                    last_evidence_at=?,updated_at=?,dormant_at=NULL
                    WHERE candidate_ref=? AND candidate_revision=? AND {reachable_sql()}""",
                (now, now, candidate.ref, candidate.revision),
            )
            _, queued = self._schedule_when_settled(candidate, now=now, rule_version=rule_version)
            scheduled += int(queued)
        # A resumed page adds to the counts the first page recorded.
        conn.execute(
            """INSERT INTO candidate_source_triggers(
                source_ref,source_revision,matched_count,scheduled_count,truncated,processed_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(source_ref,source_revision) DO UPDATE SET
                    matched_count=candidate_source_triggers.matched_count+excluded.matched_count,
                    scheduled_count=candidate_source_triggers.scheduled_count+excluded.scheduled_count,
                    truncated=excluded.truncated,processed_at=excluded.processed_at""",
            (source_ref, source_revision, matched, scheduled, int(truncated), now),
        )
        return CandidateSourceTrigger(source_ref, source_revision, "processed", matched, scheduled, truncated)

    def _candidates_mentioned_by(self, source, limit: int) -> list:
        """Reachable same-audience candidate heads sharing a term with the source, not yet holding it."""
        terms = lexical_terms(source.event["content"])
        if not terms:
            return []
        context, params = self._context("l.")
        return self._read().execute(
            f"""SELECT DISTINCT l.candidate_ref,l.candidate_revision
                FROM candidate_trigger_terms t
                JOIN candidate_lifecycle l USING(candidate_ref,candidate_revision)
                JOIN claims c ON c.claim_id=l.candidate_ref
                WHERE t.term IN ({','.join('?' for _ in terms)}) AND {context}
                  AND l.scope_id=? AND l.project_id IS ? AND l.branch_id IS ?
                  AND c.current_revision=l.candidate_revision AND c.read_blocked=0 AND c.suppressed=0
                  AND {reachable_sql('l.')}
                  AND NOT EXISTS(SELECT 1 FROM candidate_evidence e
                      WHERE e.candidate_ref=l.candidate_ref AND e.candidate_revision=l.candidate_revision
                        AND e.source_ref=? AND e.source_revision=?)
                ORDER BY l.updated_at,l.candidate_ref,l.candidate_revision LIMIT ?""",
            (*terms, *params, source.scope_id, source.project_id, source.branch_id,
             source.ref, source.revision, limit),
        ).fetchall()

    def _schedule_when_settled(self, candidate, *, now: str, rule_version: str) -> tuple[int | None, bool]:
        """Schedule only once this candidate has stopped collecting evidence.

        One schedule per arriving source, each retiring the ones still waiting,
        is where the evaluation pile-up came from; ``core/candidate_debounce.py``
        holds the window.  The evidence is recorded either way, so nothing is
        dropped by waiting.
        """
        row = self._lifecycle_row(candidate.ref, candidate.revision)
        if row is None:
            return None, False
        queued = self._read().execute(
            "SELECT 1 FROM candidate_evaluations WHERE candidate_ref=? AND candidate_revision=? AND state='queued'",
            (candidate.ref, candidate.revision),
        ).fetchone() is not None
        if settled_reason(row, now, has_queued_evaluation=queued) is None:
            return None, False
        return self._schedule(candidate, now=now, rule_version=rule_version)

    def _schedule(self, candidate, *, now: str, rule_version: str) -> tuple[int | None, bool]:
        """Queue one evaluation for the candidate's current question.

        Returns ``(evaluation_id, queued)``; ``queued`` is False when the
        candidate cannot be reached, holds no admissible evidence, or the same
        question was already asked under this rule version.
        """
        conn = self._write()
        row = self._lifecycle_row(candidate.ref, candidate.revision)
        if row is None or not is_reachable(row["processing_state"], row["reason"]):
            return None, False
        payload = getattr(candidate, "payload", None)
        if needs_absent_person(payload, self._cited_origins(candidate.ref, candidate.revision)):
            # A candidate registered before this rule: archive it the way registration now would.
            self._retire_evaluations(candidate.ref, candidate.revision, PERSON_ABSENT_REASON, now)
            self._move(candidate.ref, candidate.revision, "archived", PERSON_ABSENT_REASON, now=now, dormant_at=now)
            conn.execute("DELETE FROM candidate_trigger_terms WHERE candidate_ref=? AND candidate_revision=?",
                         (candidate.ref, candidate.revision))
            return None, False
        refs, digest = self._question(candidate.ref, candidate.revision)
        if not refs:
            self._move(candidate.ref, candidate.revision, "waiting_evidence", "no_valid_evidence", now=now)
            return None, False
        epoch = int(conn.execute("SELECT memory_epoch FROM instance_meta WHERE singleton=1").fetchone()[0])
        unanswerable = self._unanswerable(candidate, refs)
        if unanswerable is None:
            verdicts, judged = self._model_verdicts(candidate.ref, candidate.revision)
            if verdicts >= AUTOMATIC_VERDICTS and not self._restated_in_unjudged(payload, refs, judged):
                unanswerable = REPEAT_WITHOUT_RESTATEMENT_REASON
        if unanswerable is not None:
            self._record_unanswerable(candidate, refs, digest, unanswerable, epoch=epoch, now=now,
                                      rule_version=rule_version)
            return None, False
        inserted = conn.execute(
            """INSERT INTO candidate_evaluations(
                candidate_ref,candidate_revision,evidence_fingerprint,evidence_refs_json,rule_version,
                memory_epoch,state,reason,created_at)
                VALUES (?,?,?,?,?,?,'queued','new_evidence',?)
                ON CONFLICT(candidate_ref,candidate_revision,evidence_fingerprint,rule_version) DO NOTHING""",
            (candidate.ref, candidate.revision, digest, encode_refs(refs), rule_version, epoch, now),
        )
        if inserted.rowcount != 1:
            conn.execute(
                "UPDATE candidate_lifecycle SET evidence_fingerprint=? WHERE candidate_ref=? AND candidate_revision=?",
                (digest, candidate.ref, candidate.revision),
            )
            return None, False
        evaluation_id = int(inserted.lastrowid)
        self._supersede_unstarted(candidate, evaluation_id, now)
        work_id = self._enqueue(candidate.ref, evaluation_id, now)
        conn.execute("UPDATE candidate_evaluations SET work_id=? WHERE evaluation_id=?", (work_id, evaluation_id))
        conn.execute(
            """UPDATE candidate_lifecycle SET processing_state='pending_evaluation',reason='new_evidence',
               rule_version=?,evidence_fingerprint=?,updated_at=?
               WHERE candidate_ref=? AND candidate_revision=?""",
            (rule_version, digest, now, candidate.ref, candidate.revision),
        )
        return evaluation_id, True

    def _unanswerable(self, candidate, refs) -> str | None:
        """Why no verdict on this evidence could promote the candidate; see ``core/evidence_question.py``."""
        payload = getattr(candidate, "payload", None)
        evidence = []
        for ref, revision in refs:
            source = self._tx.source(ref, revision)
            if source is None or source.suppressed:
                # Unreadable evidence is the worker's fence to judge, not this one.
                return None
            evidence.append(evidence_text(source))
        return unanswerable_reason(payload, evidence)

    def _record_unanswerable(self, candidate, refs, digest: str, reason: str, *, epoch: int, now: str,
                             rule_version: str) -> None:
        """Answer the question without a model: recorded like a verdict, so it is never asked again.

        The row carries no work item and no model attempt.  Its fingerprint is
        what stops the settle sweep from selecting the same question every
        drain; new first-hand evidence poses a new question and is checked anew.
        A candidate that still holds a queued evaluation keeps its state, so
        that evaluation can run on the evidence it was queued with.
        """
        conn = self._write()
        inserted = conn.execute(
            """INSERT INTO candidate_evaluations(
                candidate_ref,candidate_revision,evidence_fingerprint,evidence_refs_json,rule_version,
                memory_epoch,state,reason,created_at,completed_at)
                VALUES (?,?,?,?,?,?,'waiting_evidence',?,?,?)
                ON CONFLICT(candidate_ref,candidate_revision,evidence_fingerprint,rule_version) DO NOTHING""",
            (candidate.ref, candidate.revision, digest, encode_refs(refs), rule_version, epoch, reason, now, now),
        )
        conn.execute(
            "UPDATE candidate_lifecycle SET evidence_fingerprint=? WHERE candidate_ref=? AND candidate_revision=?",
            (digest, candidate.ref, candidate.revision),
        )
        if inserted.rowcount != 1:
            return
        queued = conn.execute(
            "SELECT 1 FROM candidate_evaluations WHERE candidate_ref=? AND candidate_revision=? AND state='queued'",
            (candidate.ref, candidate.revision),
        ).fetchone()
        if queued is None:
            self._move(candidate.ref, candidate.revision, "waiting_evidence", reason, now=now, evaluated_at=now)

    def _supersede_unstarted(self, candidate, evaluation_id: int, now: str) -> None:
        """An older queued evaluation would judge a strictly smaller evidence set.

        Only evaluations that have not begun a model attempt are superseded:
        ``model_attempted_at IS NULL`` is the at-most-once fence, and touching a
        started one would race the worker holding it.  Their work items are
        retired only while still ``pending`` for the same reason.
        """
        conn = self._write()
        conn.execute(
            """UPDATE work_items SET state='obsolete' WHERE state='pending' AND work_id IN (
                   SELECT work_id FROM candidate_evaluations
                   WHERE candidate_ref=? AND candidate_revision=? AND evaluation_id<>?
                     AND state='queued' AND model_attempted_at IS NULL AND work_id IS NOT NULL)""",
            (candidate.ref, candidate.revision, evaluation_id),
        )
        conn.execute(
            """UPDATE candidate_evaluations SET state='obsolete',reason='superseded_by_new_evidence',completed_at=?
               WHERE candidate_ref=? AND candidate_revision=? AND evaluation_id<>?
                 AND state='queued' AND model_attempted_at IS NULL""",
            (now, candidate.ref, candidate.revision, evaluation_id),
        )

    def _enqueue(self, candidate_ref: str, evaluation_id: int, now: str) -> int:
        subject = "candidate:" + candidate_ref
        if not self._tx.work.enqueue("evaluate_candidate", subject, evaluation_id, available_at=now):
            raise ContractError("STORAGE_UNAVAILABLE", "candidate_work_missing")
        return self._read().execute(
            """SELECT work_id FROM work_items WHERE work_type='evaluate_candidate'
               AND subject_ref=? AND subject_revision=?""",
            (subject, evaluation_id),
        ).fetchone()[0]

    def pending_source_pages(self) -> int:
        """The scoped durable remainder, including pages with no work yet."""
        context, params = self._context("s.")
        return self._read().execute(
            f"SELECT count(*) {_TRUNCATED_TRIGGERS.format(context=context)}", params,
        ).fetchone()[0]

    def resume_source_pages(self, *, now: str) -> int:
        """Continue one bounded page using persisted evidence membership as cursor."""
        context, params = self._context("s.")
        row = self._read().execute(
            f"""SELECT t.source_ref,t.source_revision {_TRUNCATED_TRIGGERS.format(context=context)}
                ORDER BY t.processed_at,t.source_ref,t.source_revision LIMIT 1""", params,
        ).fetchone()
        if row is None:
            return 0
        try:
            return self.observe_source(row["source_ref"], row["source_revision"], observed_at=now, _resume=True).matched
        except ContractError as exc:
            if exc.code != "SOURCE_MISSING":
                raise
            # Deleted, suppressed or superseded evidence cannot wake more claims.
            self._write().execute(
                "UPDATE candidate_source_triggers SET truncated=0,processed_at=? WHERE source_ref=? AND source_revision=?",
                (now, row["source_ref"], row["source_revision"]),
            )
            return 0

    def backfill(self, *, now: str, limit: int = 8) -> int:
        """Bounded one-way cursor over proposed/disputed claim heads registered before the lifecycle existed."""
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ContractError("INPUT_INVALID", "candidate_backfill_limit")
        now = utc(now)
        conn = self._write()
        cursor = conn.execute(
            "SELECT position_ref,position_revision,processed_count FROM candidate_scan_cursors WHERE cursor_name='claim_backfill_1108'"
        ).fetchone()
        position_ref = cursor["position_ref"] if cursor else ""
        position_revision = int(cursor["position_revision"] or 0) if cursor else 0
        context, params = self._context("c.")
        rows = conn.execute(
            f"""SELECT c.claim_id,v.revision FROM claims c JOIN claim_versions v
                 ON v.claim_id=c.claim_id AND v.revision=c.current_revision
                WHERE {context} AND c.read_blocked=0 AND c.suppressed=0
                  AND v.state IN ('proposed','disputed')
                  AND (c.claim_id>? OR (c.claim_id=? AND v.revision>?))
                  AND NOT EXISTS(SELECT 1 FROM candidate_lifecycle l
                      WHERE l.candidate_ref=c.claim_id AND l.candidate_revision=v.revision)
                ORDER BY c.claim_id,v.revision LIMIT ?""",
            (*params, position_ref, position_ref, position_revision, limit),
        ).fetchall()
        for row in rows:
            self.register(row["claim_id"], row["revision"], observed_at=now)
            position_ref, position_revision = row["claim_id"], int(row["revision"])
        processed = int(cursor["processed_count"] if cursor else 0) + len(rows)
        conn.execute(
            """INSERT INTO candidate_scan_cursors(
                cursor_name,position_ref,position_revision,processed_count,completed,updated_at)
                VALUES ('claim_backfill_1108',?,?,?,?,?)
                ON CONFLICT(cursor_name) DO UPDATE SET position_ref=excluded.position_ref,
                    position_revision=excluded.position_revision,processed_count=excluded.processed_count,
                    completed=excluded.completed,updated_at=excluded.updated_at""",
            (position_ref or None, position_revision or None, processed, int(len(rows) < limit), now),
        )
        return len(rows)


def _registration_target(candidate, prior, rule_version: str, now: str) -> tuple[str, str, str]:
    """Where registration puts the candidate: ``(processing_state, reason, updated_at)``."""
    judgeable = candidate.state in JUDGEABLE_STATES
    if judgeable:
        state, reason = "pending_evaluation", "candidate_registered"
    elif candidate.state == "active":
        state, reason = "resolved", "fact_active"
    else:
        state, reason = "archived", f"fact_{candidate.state}"
    if prior is None:
        return state, reason, now
    if prior["processing_state"] == "blocked":
        return "blocked", prior["reason"], now
    if judgeable and prior["rule_version"] == rule_version:
        # A replayed registration is a no-op: it cannot turn waiting or dormant
        # back into pending without a new evidence fingerprint.
        return prior["processing_state"], prior["reason"], prior["updated_at"]
    if judgeable:
        return "pending_evaluation", "rule_version_changed", now
    return state, reason, now


def _trigger_terms(candidate) -> tuple[str, ...]:
    payload = candidate.payload
    conditions = payload.get("conditions")
    text = " ".join(str(value) for value in (
        payload.get("subject", ""), payload.get("predicate", ""), payload.get("value_text", ""),
        " ".join(conditions) if isinstance(conditions, list) else "",
    ) if value)
    return lexical_terms(text)[:64]


__all__ = ["CandidateIntake"]
