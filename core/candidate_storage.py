"""Transactional storage for the C3 candidate processing service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from ..contracts import ContractError
from .candidate_debounce import MAX_DEFERRAL_SECONDS, QUIET_SECONDS, settle_reason
from .evidence_question import question_digest
from .candidate_lifecycle import (
    DORMANCY_DAYS,
    RULE_VERSION,
    SOURCE_MATCH_LIMIT,
    CandidateEvaluationSnapshot,
    CandidateRegistration,
    CandidateSnapshot,
    CandidateSourceTrigger,
    CandidateSummary,
)
from .events import lexical_terms
from .schema import SCHEMA_VERSION


#: Characters of evidence content one candidate evaluation may carry.
#:
#: CANDIDATE_EVALUATION_INPUT_BUDGET is 64,000 bytes and roughly 12 KB of every
#: request is fixed prompt overhead, so this leaves comfortable room even when
#: every character is three UTF-8 bytes. The old bound was a count of sixteen
#: sources with no size at all, which is why 268 evaluations on tianshu died
#: terminal on the input budget after their candidates accumulated evidence.
CANDIDATE_EVIDENCE_BUDGET_CHARS = 14000


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ContractError("INPUT_INVALID", "candidate_timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError("INPUT_INVALID", "candidate_timestamp")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint(refs: tuple[tuple[str, int], ...]) -> str:
    return hashlib.sha256(_json([f"{ref}@{revision}" for ref, revision in refs]).encode("utf-8")).hexdigest()


def _rule(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 64 or not value.strip() or "\x00" in value:
        raise ContractError("INPUT_INVALID", "candidate_rule_version")
    return value


class CandidateLifecycle:
    """Candidate metadata borrowed from the existing owning transaction."""

    def __init__(self, transaction) -> None:
        self._tx = transaction

    def _context(self, prefix: str = "") -> tuple[str, tuple]:
        def column(name: str) -> str:
            return f"{prefix}{name}"
        scopes = sorted(self._tx.context.allowed_scope_ids)
        marks = ",".join("?" for _ in scopes)
        clause = (
            f"{column('scope_id')} IN ({marks}) AND "
            f"({column('project_id')} IS NULL OR {column('project_id')}=?) AND "
            f"({column('branch_id')} IS NULL OR {column('branch_id')}=?)"
        )
        return clause, (*scopes, self._tx.context.project_id, self._tx.context.branch_id)

    def _trigger_terms(self, candidate) -> tuple[str, ...]:
        payload = candidate.payload
        text = " ".join(str(value) for value in (
            payload.get("subject", ""), payload.get("predicate", ""), payload.get("value_text", ""),
            " ".join(payload.get("conditions", ())) if isinstance(payload.get("conditions"), list) else "",
        ) if value)
        return lexical_terms(text)[:64]

    #: Recalled memory delivered back into a conversation. It is this system's
    #: own output, so admitting it as candidate evidence is a feedback loop: it
    #: adds no information the store did not already hold, it keeps a candidate
    #: from ever going quiet, and on TianShu it accounted for 594 of 3,669
    #: evidence rows (16%) behind the re-judgement churn.
    ECHO_ORIGINS = frozenset({"memory_reinjection"})

    def _add_evidence(self, candidate, source_ref: str, source_revision: int, now: str) -> bool:
        source = self._tx.source(source_ref, source_revision)
        if source is None or source.suppressed:
            return False
        if (source.event or {}).get("origin") in self.ECHO_ORIGINS:
            return False
        if (source.scope_id, source.project_id, source.branch_id) != (
            candidate.scope_id, candidate.project_id, candidate.branch_id,
        ):
            return False
        conn = self._tx._check(write=True)
        cursor = conn.execute(
            """INSERT INTO candidate_evidence(
                candidate_ref,candidate_revision,source_ref,source_revision,observed_at)
                VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (candidate.ref, candidate.revision, source_ref, source_revision, now),
        )
        return cursor.rowcount == 1

    def _queued_evaluation_count(self, candidate) -> int:
        """Evaluations already waiting for this candidate that nobody has begun."""
        return int(self._tx._check().execute(
            """SELECT COUNT(*) FROM candidate_evaluations
               WHERE candidate_ref=? AND candidate_revision=? AND state='queued'""",
            (candidate.ref, candidate.revision),
        ).fetchone()[0])

    def _schedule_when_settled(self, candidate, *, now: str, rule_version: str) -> tuple[int | None, bool]:
        """Schedule only once this candidate has stopped collecting evidence.

        Called from ``observe_source``, which is where the pile-up came from:
        one schedule per arriving source, each retiring the ones still waiting.
        See ``core/candidate_debounce.py`` for the measurements behind the
        window; the evidence itself is already recorded either way, so nothing
        is dropped by waiting.
        """
        row = self._tx._check().execute(
            """SELECT last_evidence_at,last_evaluated_at,created_at FROM candidate_lifecycle
               WHERE candidate_ref=? AND candidate_revision=?""",
            (candidate.ref, candidate.revision),
        ).fetchone()
        if row is None:
            return None, False
        reason = settle_reason(
            now=now,
            last_evidence_at=row["last_evidence_at"],
            last_evaluated_at=row["last_evaluated_at"],
            created_at=row["created_at"],
            has_queued_evaluation=self._queued_evaluation_count(candidate) > 0,
        )
        if reason is None:
            return None, False
        return self._schedule(candidate, now=now, rule_version=rule_version)

    def _evaluation_evidence(self, candidate_ref: str, candidate_revision: int):
        """Select the same live, bounded evidence for preview and enqueue."""
        conn = self._tx._check()
        evidence = conn.execute(
            """SELECT e.source_ref,e.source_revision,e.observed_at,s.origin,
                      LENGTH(s.content) AS content_length
               FROM candidate_evidence e JOIN source_events s
                 ON s.event_id=e.source_ref AND s.source_revision=e.source_revision
               WHERE e.candidate_ref=? AND e.candidate_revision=?
                 AND s.read_blocked=0 AND s.suppressed=0
                 AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                     AND b.object_ref=e.source_ref AND (b.read_blocked=1 OR b.suppressed=1))
               ORDER BY (s.origin='human_direct') DESC,
                        e.observed_at DESC,e.source_ref,e.source_revision DESC LIMIT 16""",
            (candidate_ref, candidate_revision),
        ).fetchall()
        # Sixteen sources is a count, not a size. A candidate that accumulates
        # evidence collects sixteen of whatever length, and the evaluation prompt
        # then exceeds any budget and dies terminal: 268 of them on tianshu, each
        # a candidate that will never be judged. Bound the bytes here, where the
        # evidence set is chosen, so what the evaluation declares is what the
        # prompt can actually carry — trimming later would leave the model
        # returning fewer source_refs than the evaluation committed to.
        #
        # Newest first, because recent evidence is what triggered this round —
        # but only among sources that fit. Keeping the newest unconditionally
        # sounds fair and is not: one 52,451-character tool transcript at the
        # head of the list guarantees an oversized prompt and a terminal failure
        # while the same candidate has a 131-character source right behind it.
        # On tianshu every one of the 121 blocked candidates held at least one
        # source under this budget.
        selected, budget_left = [], CANDIDATE_EVIDENCE_BUDGET_CHARS
        for item in evidence:
            length = int(item["content_length"] or 0)
            if length > budget_left:
                continue
            selected.append(item)
            budget_left -= length
        if not selected and evidence:
            # Every source is oversized on its own. Judge the smallest rather
            # than nothing: the alternative is a candidate no evidence can reach.
            selected = [min(evidence, key=lambda item: int(item["content_length"] or 0))]
        return selected

    def _schedule(self, candidate, *, now: str, rule_version: str) -> tuple[int | None, bool]:
        conn = self._tx._check(write=True)
        row = conn.execute(
            """SELECT processing_state,reason FROM candidate_lifecycle
               WHERE candidate_ref=? AND candidate_revision=?""",
            (candidate.ref, candidate.revision),
        ).fetchone()
        if row is None or row["processing_state"] in {"resolved", "blocked"}:
            return None, False
        if row["processing_state"] == "archived" and row["reason"] != "dormant_no_evidence":
            return None, False
        selected = self._evaluation_evidence(candidate.ref, candidate.revision)
        refs = tuple(sorted((str(item["source_ref"]), int(item["source_revision"])) for item in selected))
        if not refs:
            conn.execute(
                """UPDATE candidate_lifecycle SET processing_state='waiting_evidence',
                   reason='no_valid_evidence',updated_at=? WHERE candidate_ref=? AND candidate_revision=?""",
                (now, candidate.ref, candidate.revision),
            )
            return None, False
        # The dedup key is the *question*, not the byte-exact selection: one
        # more tool observation displacing an older one is not a new question,
        # and keying on that is what let the UNIQUE guard below never collide.
        digest = question_digest(
            (str(item["source_ref"]), int(item["source_revision"]), item["origin"])
            for item in selected
        )
        epoch = int(conn.execute("SELECT memory_epoch FROM instance_meta WHERE singleton=1").fetchone()[0])
        inserted = conn.execute(
            """INSERT INTO candidate_evaluations(
                candidate_ref,candidate_revision,evidence_fingerprint,evidence_refs_json,rule_version,
                memory_epoch,state,reason,created_at)
                VALUES (?,?,?,?,?,?,'queued','new_evidence',?)
                ON CONFLICT(candidate_ref,candidate_revision,evidence_fingerprint,rule_version) DO NOTHING""",
            (candidate.ref, candidate.revision, digest, _json([f"{ref}@{revision}" for ref, revision in refs]),
             rule_version, epoch, now),
        )
        if inserted.rowcount != 1:
            conn.execute(
                """UPDATE candidate_lifecycle SET evidence_fingerprint=?
                   WHERE candidate_ref=? AND candidate_revision=?""",
                (digest, candidate.ref, candidate.revision),
            )
            return None, False
        evaluation_id = int(inserted.lastrowid)
        # Every new piece of evidence changes the fingerprint, so it queues a
        # fresh evaluation while the older ones stay queued — on tianshu that
        # left 1,424 evaluations waiting for 113 candidates, a 12.6x pile-up,
        # and 335 already-obsolete rows showing what happens when they finally
        # run. An older evaluation would judge a strictly smaller evidence set,
        # so its verdict is stale before it starts.
        #
        # Only evaluations that have not begun a model attempt are superseded:
        # `model_attempted_at IS NULL` is the at-most-once fence, and touching a
        # started one would race the worker holding it. Their work items are
        # retired only while still `pending` for the same reason.
        superseded = conn.execute(
            """SELECT evaluation_id,work_id FROM candidate_evaluations
               WHERE candidate_ref=? AND candidate_revision=? AND evaluation_id<>?
                 AND state='queued' AND model_attempted_at IS NULL""",
            (candidate.ref, candidate.revision, evaluation_id),
        ).fetchall()
        if superseded:
            conn.executemany(
                """UPDATE candidate_evaluations SET state='obsolete',reason='superseded_by_new_evidence',
                   completed_at=? WHERE evaluation_id=? AND state='queued' AND model_attempted_at IS NULL""",
                ((now, row["evaluation_id"]) for row in superseded),
            )
            conn.executemany(
                "UPDATE work_items SET state='obsolete' WHERE work_id=? AND state='pending'",
                ((row["work_id"],) for row in superseded if row["work_id"] is not None),
            )
        work_subject = "candidate:" + candidate.ref
        queued = self._tx.work.enqueue(
            "evaluate_candidate", work_subject, evaluation_id, available_at=now,
        )
        if not queued:
            raise ContractError("STORAGE_UNAVAILABLE", "candidate_work_missing")
        work_id = conn.execute(
            """SELECT work_id FROM work_items WHERE work_type='evaluate_candidate'
               AND subject_ref=? AND subject_revision=?""",
            (work_subject, evaluation_id),
        ).fetchone()[0]
        conn.execute("UPDATE candidate_evaluations SET work_id=? WHERE evaluation_id=?", (work_id, evaluation_id))
        conn.execute(
            """UPDATE candidate_lifecycle SET processing_state='pending_evaluation',reason='new_evidence',
               rule_version=?,evidence_fingerprint=?,updated_at=?
               WHERE candidate_ref=? AND candidate_revision=?""",
            (rule_version, digest, now, candidate.ref, candidate.revision),
        )
        return evaluation_id, True

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

        This method intentionally performs no claim write.  Thread 1 calls it
        inside the same transaction after its authoritative fact application.
        """
        now = _stamp(_time(observed_at))
        rule_version = _rule(rule_version)
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
        if candidate.state in {"proposed", "disputed"}:
            processing_state, reason = "pending_evaluation", "candidate_registered"
        elif candidate.state == "active":
            processing_state, reason = "resolved", "fact_active"
        else:
            processing_state, reason = "archived", f"fact_{candidate.state}"
        conn = self._tx._check(write=True)
        prior = conn.execute(
            """SELECT processing_state,reason,rule_version,updated_at FROM candidate_lifecycle
               WHERE candidate_ref=? AND candidate_revision=?""",
            (candidate_ref, candidate_revision),
        ).fetchone()
        rule_changed = prior is not None and prior["rule_version"] != rule_version
        lifecycle_updated_at = now
        if prior is not None:
            if prior["processing_state"] == "blocked":
                processing_state, reason = "blocked", prior["reason"]
            elif candidate.state in {"proposed", "disputed"} and not rule_changed:
                # A replayed fact-registration callback is a no-op. In
                # particular it cannot turn waiting/dormant back into pending
                # without a new evidence fingerprint.
                processing_state, reason = prior["processing_state"], prior["reason"]
                lifecycle_updated_at = prior["updated_at"]
            elif candidate.state in {"proposed", "disputed"}:
                processing_state, reason = "pending_evaluation", "rule_version_changed"
        conn.execute(
            """UPDATE candidate_lifecycle SET processing_state='archived',reason='superseded_by_candidate_revision',
               dormant_at=?,updated_at=? WHERE candidate_ref=? AND candidate_revision<>?
               AND processing_state NOT IN ('resolved','blocked')""",
            (now, now, candidate_ref, candidate_revision),
        )
        conn.execute(
            """UPDATE work_items SET state='obsolete',lease_token=lease_token+1,lease_owner=NULL,lease_until=NULL,
               last_error_code='candidate_revision_changed' WHERE work_id IN (
                   SELECT work_id FROM candidate_evaluations WHERE candidate_ref=? AND candidate_revision<>?
                   AND work_id IS NOT NULL) AND state IN ('pending','leased')""",
            (candidate_ref, candidate_revision),
        )
        conn.execute(
            """UPDATE candidate_evaluations SET state='obsolete',reason='candidate_revision_changed',completed_at=?
               WHERE candidate_ref=? AND candidate_revision<>? AND state='queued'""",
            (now, candidate_ref, candidate_revision),
        )
        empty = hashlib.sha256(b"[]").hexdigest()
        conn.execute(
            """INSERT INTO candidate_lifecycle(
                candidate_ref,candidate_revision,scope_id,project_id,branch_id,processing_state,reason,
                rule_version,evidence_fingerprint,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_ref,candidate_revision) DO UPDATE SET
                    processing_state=excluded.processing_state,reason=excluded.reason,
                    rule_version=excluded.rule_version,updated_at=excluded.updated_at""",
            (candidate.ref, candidate.revision, candidate.scope_id, candidate.project_id, candidate.branch_id,
             processing_state, reason, rule_version, empty, now, lifecycle_updated_at),
        )
        conn.execute(
            "DELETE FROM candidate_trigger_terms WHERE candidate_ref=? AND candidate_revision=?",
            (candidate.ref, candidate.revision),
        )
        triggerable = processing_state in {"pending_evaluation", "waiting_evidence"} or (
            processing_state == "archived" and reason == "dormant_no_evidence"
        )
        if triggerable:
            conn.executemany(
                "INSERT INTO candidate_trigger_terms(term,candidate_ref,candidate_revision) VALUES (?,?,?)",
                ((term, candidate.ref, candidate.revision) for term in self._trigger_terms(candidate)),
            )
            for evidence in conn.execute(
                """SELECT source_ref,source_revision FROM evidence_links
                   WHERE object_kind='claim' AND object_ref=? AND object_revision=?
                   ORDER BY source_ref,source_revision""",
                (candidate.ref, candidate.revision),
            ).fetchall():
                self._add_evidence(candidate, evidence["source_ref"], evidence["source_revision"], now)
            if schedule_initial and processing_state == "pending_evaluation":
                evaluation_id, queued = self._schedule(candidate, now=now, rule_version=rule_version)
            elif not schedule_initial and processing_state == "pending_evaluation":
                evaluation_id, queued = None, False
                conn.execute(
                    """UPDATE candidate_lifecycle SET processing_state='waiting_evidence',
                       reason='evaluated_waiting_evidence',last_evaluated_at=?,updated_at=?
                       WHERE candidate_ref=? AND candidate_revision=?""",
                    (now, now, candidate.ref, candidate.revision),
                )
            else:
                evaluation_id, queued = None, False
            current = conn.execute(
                "SELECT processing_state,reason FROM candidate_lifecycle WHERE candidate_ref=? AND candidate_revision=?",
                (candidate.ref, candidate.revision),
            ).fetchone()
            processing_state, reason = current["processing_state"], current["reason"]
        else:
            evaluation_id, queued = None, False
        disposition = "inserted" if prior is None else "updated"
        if prior is not None and prior["processing_state"] == processing_state and prior["reason"] == reason and not queued:
            disposition = "unchanged"
        return CandidateRegistration(candidate.ref, candidate.revision, processing_state, reason,
                                     disposition, evaluation_id, queued)

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
        """Schedule a bounded set of indexed, same-audience candidates."""
        if type(limit) is not int or not 1 <= limit <= SOURCE_MATCH_LIMIT:
            raise ContractError("INPUT_INVALID", "candidate_match_limit")
        now = _stamp(_time(observed_at))
        rule_version = _rule(rule_version)
        source = self._tx.source(source_ref, source_revision)
        current = self._tx.source_current(source_ref)
        if source is None or source.suppressed or current is None or current.revision != source_revision:
            raise ContractError("SOURCE_MISSING", "candidate_trigger_source")
        conn = self._tx._check(write=True)
        prior = conn.execute(
            "SELECT * FROM candidate_source_triggers WHERE source_ref=? AND source_revision=?",
            (source_ref, source_revision),
        ).fetchone()
        if prior is not None and not (_resume and prior["truncated"]):
            return CandidateSourceTrigger(source_ref, source_revision, "duplicate", prior["matched_count"],
                                          prior["scheduled_count"], bool(prior["truncated"]))
        terms = lexical_terms(source.event["content"])
        rows = []
        if terms:
            marks = ",".join("?" for _ in terms)
            context, params = self._context("l.")
            rows = conn.execute(
                f"""SELECT DISTINCT l.candidate_ref,l.candidate_revision
                    FROM candidate_trigger_terms t
                    JOIN candidate_lifecycle l USING(candidate_ref,candidate_revision)
                    JOIN claims c ON c.claim_id=l.candidate_ref
                    WHERE t.term IN ({marks}) AND {context}
                      AND l.scope_id=? AND l.project_id IS ? AND l.branch_id IS ?
                      AND c.current_revision=l.candidate_revision AND c.read_blocked=0 AND c.suppressed=0
                      AND (l.processing_state IN ('pending_evaluation','waiting_evidence') OR
                           (l.processing_state='archived' AND l.reason='dormant_no_evidence'))
                      AND NOT EXISTS(SELECT 1 FROM candidate_evidence e
                          WHERE e.candidate_ref=l.candidate_ref AND e.candidate_revision=l.candidate_revision
                            AND e.source_ref=? AND e.source_revision=?)
                    ORDER BY l.updated_at,l.candidate_ref,l.candidate_revision LIMIT ?""",
                (*terms, *params, source.scope_id, source.project_id, source.branch_id,
                 source_ref, source_revision, limit + 1),
            ).fetchall()
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
                """UPDATE candidate_lifecycle SET processing_state='pending_evaluation',reason='new_evidence',
                   last_evidence_at=?,updated_at=?,dormant_at=NULL WHERE candidate_ref=? AND candidate_revision=?
                   AND (processing_state IN ('pending_evaluation','waiting_evidence') OR
                        (processing_state='archived' AND reason='dormant_no_evidence'))""",
                (now, now, candidate.ref, candidate.revision),
            )
            _, queued = self._schedule_when_settled(candidate, now=now, rule_version=rule_version)
            scheduled += int(queued)
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

    def pending_source_pages(self) -> int:
        """Read the scoped durable remainder, including pages with no work yet."""
        context, params = self._context("s.")
        return self._tx._check().execute(
            f"""SELECT count(*) FROM candidate_source_triggers t
                JOIN source_events s ON s.event_id=t.source_ref AND s.source_revision=t.source_revision
                WHERE t.truncated=1 AND {context}""", params,
        ).fetchone()[0]

    def resume_source_pages(self, *, now: str) -> int:
        """Continue one bounded page using persisted evidence membership as cursor."""
        context, params = self._context("s.")
        conn = self._tx._check(write=True)
        row = conn.execute(
            f"""SELECT t.source_ref,t.source_revision FROM candidate_source_triggers t
                JOIN source_events s ON s.event_id=t.source_ref AND s.source_revision=t.source_revision
                WHERE t.truncated=1 AND {context}
                ORDER BY t.processed_at,t.source_ref,t.source_revision LIMIT 1""", params,
        ).fetchone()
        if row is None:
            return 0
        try:
            result = self.observe_source(row["source_ref"], row["source_revision"], observed_at=now, _resume=True)
            return result.matched
        except ContractError as exc:
            if exc.code != "SOURCE_MISSING":
                raise
            # Deleted, suppressed or superseded evidence cannot wake more claims.
            conn.execute(
                "UPDATE candidate_source_triggers SET truncated=0,processed_at=? WHERE source_ref=? AND source_revision=?",
                (now, row["source_ref"], row["source_revision"]),
            )
            return 0

    def backfill(self, *, now: str, limit: int = 8) -> int:
        """Bounded one-way cursor for pre-1108 proposed/disputed claim heads."""
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ContractError("INPUT_INVALID", "candidate_backfill_limit")
        now = _stamp(_time(now))
        conn = self._tx._check(write=True)
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

    def archive_dormant(self, *, now: str, days: int = DORMANCY_DAYS, limit: int = 8) -> int:
        if type(days) is not int or not 1 <= days <= 3650 or type(limit) is not int or not 1 <= limit <= 8:
            raise ContractError("INPUT_INVALID", "candidate_dormancy")
        current = _time(now)
        stamp = _stamp(current)
        cutoff = _stamp(current - timedelta(days=days))
        conn = self._tx._check(write=True)
        context, params = self._context()
        rows = conn.execute(
            f"""SELECT candidate_ref,candidate_revision FROM candidate_lifecycle
                WHERE {context} AND processing_state='waiting_evidence'
                  AND COALESCE(last_evidence_at,updated_at)<=?
                ORDER BY COALESCE(last_evidence_at,updated_at),candidate_ref,candidate_revision LIMIT ?""",
            (*params, cutoff, limit),
        ).fetchall()
        for row in rows:
            conn.execute(
                """UPDATE candidate_lifecycle SET processing_state='archived',reason='dormant_no_evidence',
                   dormant_at=?,updated_at=? WHERE candidate_ref=? AND candidate_revision=?
                   AND processing_state='waiting_evidence'""",
                (stamp, stamp, row["candidate_ref"], row["candidate_revision"]),
            )
        return len(rows)

    def summary(self, *, include_all_projects: bool = False) -> CandidateSummary:
        if type(include_all_projects) is not bool:
            raise ContractError("INPUT_INVALID", "candidate_summary_scope")
        conn = self._tx._check()
        if include_all_projects:
            scopes = sorted(self._tx.context.allowed_scope_ids)
            context = f"scope_id IN ({','.join('?' for _ in scopes)})"
            params = tuple(scopes)
        else:
            context, params = self._context()
        counts = dict(conn.execute(
            f"""SELECT processing_state,count(*) FROM candidate_lifecycle
                WHERE {context} GROUP BY processing_state""",
            params,
        ).fetchall())
        dormant = int(conn.execute(
            f"""SELECT count(*) FROM candidate_lifecycle WHERE {context}
                AND processing_state='archived' AND reason='dormant_no_evidence'""", params,
        ).fetchone()[0])
        oldest = conn.execute(
            f"""SELECT min(updated_at) FROM candidate_lifecycle WHERE {context}
                AND processing_state IN ('pending_evaluation','waiting_evidence')""", params,
        ).fetchone()[0]
        if include_all_projects:
            work_context = f"w.scope_id IN ({','.join('?' for _ in scopes)})"
            work_params = tuple(scopes)
        else:
            work_context, work_params = self._context("w.")
        failed = int(conn.execute(
            f"""SELECT count(*) FROM work_items w WHERE {work_context}
                AND w.work_type='evaluate_candidate' AND w.state='failed'""", work_params,
        ).fetchone()[0])
        budget = int(conn.execute(
            f"""SELECT count(*) FROM work_items w WHERE {work_context}
                AND w.work_type='evaluate_candidate' AND w.state='pending'
                AND (w.last_error_code IN ('budget_exhausted','budget_unavailable')
                     OR w.last_error_code LIKE '%|budget_exhausted'
                     OR w.last_error_code LIKE '%|budget_unavailable')""", work_params,
        ).fetchone()[0])
        capability = int(conn.execute(
            f"""SELECT count(*) FROM candidate_lifecycle WHERE {context}
                AND processing_state='pending_evaluation' AND reason='capability_unavailable'""", params,
        ).fetchone()[0])
        archived = int(counts.get("archived", 0))
        return CandidateSummary(
            pending_evaluation=int(counts.get("pending_evaluation", 0)),
            waiting_evidence=int(counts.get("waiting_evidence", 0)),
            dormant=dormant,
            blocked=int(counts.get("blocked", 0)),
            resolved=int(counts.get("resolved", 0)),
            archived_other=max(0, archived - dormant),
            failed=failed,
            budget_paused=budget,
            capability_unavailable=capability,
            oldest_waiting_at=oldest,
        )

    def mark_capability_unavailable(self) -> int:
        conn = self._tx._check(write=True)
        context, params = self._context()
        return conn.execute(
            f"""UPDATE candidate_lifecycle SET reason='capability_unavailable'
                WHERE {context} AND processing_state='pending_evaluation'
                  AND EXISTS(SELECT 1 FROM candidate_evaluations e JOIN work_items w ON w.work_id=e.work_id
                      WHERE e.candidate_ref=candidate_lifecycle.candidate_ref
                        AND e.candidate_revision=candidate_lifecycle.candidate_revision
                        AND e.state='queued' AND w.state='pending')""",
            params,
        ).rowcount

    def evaluation(self, evaluation_id: int) -> CandidateEvaluationSnapshot | None:
        if type(evaluation_id) is not int or evaluation_id < 1:
            raise ContractError("INPUT_INVALID", "candidate_evaluation")
        conn = self._tx._check()
        context, params = self._context("l.")
        row = conn.execute(
            f"""SELECT e.*,l.processing_state,l.reason AS lifecycle_reason,l.rule_version AS lifecycle_rule,
                       l.scope_id,l.project_id,l.branch_id,v.state AS fact_state,v.payload_json,c.current_revision,
                       c.read_blocked,c.suppressed
                FROM candidate_evaluations e
                JOIN candidate_lifecycle l USING(candidate_ref,candidate_revision)
                JOIN claims c ON c.claim_id=e.candidate_ref
                JOIN claim_versions v ON v.claim_id=e.candidate_ref AND v.revision=e.candidate_revision
                WHERE e.evaluation_id=? AND {context}""",
            (evaluation_id, *params),
        ).fetchone()
        if row is None or row["state"] != "queued" or row["processing_state"] != "pending_evaluation":
            return None
        if row["read_blocked"] or row["suppressed"] or row["current_revision"] != row["candidate_revision"]:
            return None
        if row["fact_state"] not in {"proposed", "disputed"}:
            return None
        try:
            refs = tuple(
                (text.rsplit("@", 1)[0], int(text.rsplit("@", 1)[1]))
                for text in json.loads(row["evidence_refs_json"])
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContractError("STORAGE_UNAVAILABLE", "candidate_evidence_shape") from exc
        snapshot = CandidateSnapshot(
            row["candidate_ref"], row["candidate_revision"], row["scope_id"], row["project_id"], row["branch_id"],
            row["fact_state"], json.loads(row["payload_json"]), row["processing_state"], row["lifecycle_reason"],
            row["lifecycle_rule"],
        )
        return CandidateEvaluationSnapshot(
            row["evaluation_id"], snapshot, refs, row["evidence_fingerprint"], row["memory_epoch"], row["state"],
            row["model_attempted_at"],
        )

    def begin_model_attempt(self, evaluation_id: int, work_id: int, lease_token: int, owner: str, *, now: str) -> bool:
        conn = self._tx._check(write=True)
        if not self._tx.work._verify_lease(work_id, lease_token, owner, now=now):
            return False
        row = conn.execute(
            "SELECT state,model_attempted_at,work_id FROM candidate_evaluations WHERE evaluation_id=?",
            (evaluation_id,),
        ).fetchone()
        if row is None or row["state"] != "queued" or row["work_id"] != work_id or row["model_attempted_at"] is not None:
            return False
        return conn.execute(
            """UPDATE candidate_evaluations SET model_attempted_at=?,
               memory_epoch=(SELECT memory_epoch FROM instance_meta WHERE singleton=1)
               WHERE evaluation_id=? AND model_attempted_at IS NULL""",
            (now, evaluation_id),
        ).rowcount == 1

    def _record_error(self, work_id: int, lease_token: int, code: str, now: str, field: str | None = None) -> None:
        conn = self._tx._check(write=True)
        conn.execute(
            """INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at)
               VALUES (?,?,'candidate_evaluation',?,?,?)""",
            (work_id, lease_token, code, field, now),
        )

    def defer_budget(self, evaluation_id: int, work, *, now: str, code: str):
        self._record_error(work.work_id, work.lease_token, code, now)
        conn = self._tx._check(write=True)
        conn.execute(
            """UPDATE candidate_evaluations SET model_attempted_at=NULL,reason='budget_paused',failure_code=?
               WHERE evaluation_id=? AND state='queued'""",
            (code, evaluation_id),
        )
        conn.execute(
            """UPDATE candidate_lifecycle SET processing_state='pending_evaluation',reason='budget_paused'
               WHERE (candidate_ref,candidate_revision)=(
                   SELECT candidate_ref,candidate_revision FROM candidate_evaluations WHERE evaluation_id=?)""",
            (evaluation_id,),
        )
        return self._tx.work.defer_without_attempt(
            work.work_id, work.lease_token, work.lease_owner, now=now, error_code=code, seconds=3600,
        )

    def fail(self, evaluation_id: int, work, *, now: str, code: str, field: str | None = None,
             validation_code: str | None = None):
        self._record_error(work.work_id, work.lease_token, validation_code or code, now, field)
        # An explicit provider rejection has no usable result to replay.
        # Provider failures use the existing limit; invalid output has one
        # explicit extra attempt. Uncertain timeout/crash never clears the fence.
        recoverable = code in {"http_429", "rate_limited", "http_500", "http_502", "http_503", "http_504"}
        mutation = self._tx.work.fail(
            work.work_id, work.lease_token, work.lease_owner, error_code=code, now=now, recoverable=recoverable,
        )
        conn = self._tx._check(write=True)
        if mutation.disposition == "retry":
            conn.execute(
                "UPDATE candidate_evaluations SET model_attempted_at=NULL,reason='retry_scheduled',failure_code=? WHERE evaluation_id=? AND state='queued'",
                (code, evaluation_id),
            )
            conn.execute(
                """UPDATE candidate_lifecycle SET processing_state='pending_evaluation',reason='retry_scheduled',
                   updated_at=? WHERE (candidate_ref,candidate_revision)=(
                       SELECT candidate_ref,candidate_revision FROM candidate_evaluations WHERE evaluation_id=?)
                   AND processing_state<>'blocked'""",
                (now, evaluation_id),
            )
            return mutation
        conn.execute(
            """UPDATE candidate_evaluations SET state='failed',reason='evaluation_failed',
               completed_at=?,failure_code=? WHERE evaluation_id=? AND state='queued'""",
            (now, code, evaluation_id),
        )
        conn.execute(
            """UPDATE candidate_lifecycle SET processing_state='waiting_evidence',reason='evaluation_failed',
               last_evaluated_at=?,updated_at=? WHERE (candidate_ref,candidate_revision)=(
                   SELECT candidate_ref,candidate_revision FROM candidate_evaluations WHERE evaluation_id=?)
               AND processing_state<>'blocked'""",
            (now, now, evaluation_id),
        )
        return mutation

    def _settling_rows(self):
        """Shared live revision/authority/scope filter for scheduler and doctor."""
        conn = self._tx._check()
        context, params = self._context("l.")
        return conn.execute(
            f"""SELECT l.candidate_ref,l.candidate_revision,l.scope_id,l.project_id,l.branch_id,
                       l.reason,l.rule_version AS lifecycle_rule,
                       l.last_evidence_at,l.last_evaluated_at,l.created_at,
                       v.state AS fact_state,v.payload_json,
                       EXISTS(SELECT 1 FROM candidate_evaluations e WHERE e.candidate_ref=l.candidate_ref
                         AND e.candidate_revision=l.candidate_revision AND e.state='queued') AS queued
                FROM candidate_lifecycle l
                JOIN claims c ON c.claim_id=l.candidate_ref
                JOIN claim_versions v ON v.claim_id=l.candidate_ref AND v.revision=l.candidate_revision
                WHERE l.processing_state IN ('pending_evaluation','waiting_evidence')
                  AND l.reason<>'authority_revoked' AND {context}
                  AND c.current_revision=l.candidate_revision AND c.read_blocked=0 AND c.suppressed=0
                  AND v.state IN ('proposed','disputed')
                  AND EXISTS(SELECT 1 FROM candidate_evidence ev
                      WHERE ev.candidate_ref=l.candidate_ref
                        AND ev.candidate_revision=l.candidate_revision)
                ORDER BY l.last_evidence_at,l.candidate_ref,l.candidate_revision""",
            params,
        ).fetchall()

    def _settling_eligible(self, row, *, now: str, rule_version: str):
        """Return settle reason only when enqueue can create a new question."""
        if row["queued"]:
            return None
        reason = settle_reason(now=now, last_evidence_at=row["last_evidence_at"],
                               last_evaluated_at=row["last_evaluated_at"], created_at=row["created_at"])
        if reason is None:
            return None
        selected = self._evaluation_evidence(row["candidate_ref"], row["candidate_revision"])
        if not selected:
            return None
        digest = question_digest((str(item["source_ref"]), int(item["source_revision"]), item["origin"])
                                 for item in selected)
        exists = self._tx._check().execute(
            "SELECT 1 FROM candidate_evaluations WHERE candidate_ref=? AND candidate_revision=? "
            "AND evidence_fingerprint=? AND rule_version=?",
            (row["candidate_ref"], row["candidate_revision"], digest, _rule(rule_version)),
        ).fetchone()
        return None if exists else reason

    def schedule_settled_candidates(self, *, now: str, rule_version: str = RULE_VERSION,
                                    limit: int = 16) -> int:
        """Queue one evaluation for each candidate whose evidence has settled.

        ``observe_source`` no longer schedules while evidence is still arriving,
        so something has to notice when it stops; otherwise a candidate whose
        conversation simply ended would wait forever.  This is that something,
        run once at the start of a drain.

        Bounded and ordered by oldest evidence first, which is also
        most-settled first, so a page can never be filled by candidates that are
        still collecting while a settled one waits behind them.

        ``waiting_evidence`` is included, not just ``pending_evaluation``.  A
        candidate judged "not enough evidence" while more evidence was already
        sitting in the table would otherwise never be looked at again: nothing
        further arrives to move it back, so the evidence it already has stays
        unjudged forever.  ``authority_revoked`` is the one reason that must not
        be revived -- that state is a decision, not a shortage.

        Idempotent by construction: a candidate with a queued evaluation is
        excluded here, and an evidence set that has not changed collides on its
        fingerprint and inserts nothing.  So a candidate can only be scheduled
        again once its evidence genuinely differs from what was judged.
        """
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ContractError("INPUT_INVALID", "settle_limit")
        conn = self._tx._check(write=True)
        rows = self._settling_rows()
        scheduled = 0
        for row in rows:
            if scheduled >= limit:
                break
            reason = self._settling_eligible(row, now=now, rule_version=rule_version)
            if reason is None:
                continue
            snapshot = CandidateSnapshot(
                row["candidate_ref"], row["candidate_revision"], row["scope_id"],
                row["project_id"], row["branch_id"], row["fact_state"],
                json.loads(row["payload_json"]), "pending_evaluation",
                row["reason"], row["lifecycle_rule"],
            )
            _evaluation_id, queued = self._schedule(snapshot, now=now, rule_version=_rule(rule_version))
            if queued:
                conn.execute(
                    """UPDATE candidate_lifecycle SET processing_state='pending_evaluation',
                       reason=?,updated_at=? WHERE candidate_ref=? AND candidate_revision=?
                         AND processing_state IN ('pending_evaluation','waiting_evidence')""",
                    (reason, now, row["candidate_ref"], row["candidate_revision"]),
                )
                scheduled += 1
        return scheduled

    def settling_summary(self, *, now: str) -> dict[str, int]:
        """How many candidates are waiting inside their window versus overdue.

        Debouncing deliberately grows ``candidate_pending_evaluation``, so the
        doctor needs to be able to tell "waiting on purpose" from "stuck"; a
        number that goes up for a good reason is indistinguishable from one
        going up for a bad one unless it is split.
        """
        rows = self._settling_rows()
        summary = {"queued": 0, "collecting": 0, "settled_waiting_sweep": 0}
        for row in rows:
            if row["queued"]:
                summary["queued"] += 1
                continue
            reason = self._settling_eligible(row, now=now, rule_version=RULE_VERSION)
            if reason:
                summary["settled_waiting_sweep"] += 1
            elif settle_reason(now=now, last_evidence_at=row["last_evidence_at"],
                               last_evaluated_at=row["last_evaluated_at"], created_at=row["created_at"]) is None:
                summary["collecting"] += 1
        summary["quiet_seconds"] = QUIET_SECONDS
        summary["max_deferral_seconds"] = MAX_DEFERRAL_SECONDS
        return summary

    def reschedule_budget_blocked_candidates(self, *, now: str, rule_version: str = RULE_VERSION,
                                             limit: int = 8) -> int:
        """Give candidates a fresh evaluation after the evidence bound changed.

        ``recover_oversized_evaluations`` re-runs the *same* evidence set through
        the formatter, so a candidate whose set is genuinely too large keeps its
        durable ``budget_checked`` marker and is deliberately never retried —
        that marker is what stops it looping.

        But the set is no longer chosen the same way: evidence is now bounded by
        bytes and prefers sources that fit, so the same candidate yields a
        different, smaller set. Re-scheduling produces a new fingerprint and
        therefore a genuinely new evaluation; it cannot loop, because a candidate
        whose set has not changed collides on the fingerprint and inserts
        nothing.

        Bounded, and the durable marker is retired as it goes so one candidate
        cannot occupy the repair page twice.
        """
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ContractError("INPUT_INVALID", "repair_limit")
        conn = self._tx._check(write=True)
        context, params = self._context("l.")
        # A row already carrying the rebound marker has had its re-look and its
        # candidate has moved on, so it is superseded work, not an outstanding
        # failure. Left as `failed` it keeps doctor reporting `work_failed` for
        # items nobody can act on — the permanently-degraded signal that hides a
        # real one.
        conn.execute(
            """UPDATE work_items SET state='obsolete'
               WHERE work_type='evaluate_candidate' AND state='failed'
                 AND last_error_code LIKE 'budget_rebounded:%'"""
        )
        rows = conn.execute(
            f"""SELECT DISTINCT e.candidate_ref,e.candidate_revision,l.processing_state,l.reason,
                       l.rule_version AS lifecycle_rule,l.scope_id,l.project_id,l.branch_id,
                       v.state AS fact_state,v.payload_json,c.current_revision,c.read_blocked,c.suppressed
                FROM work_items w
                JOIN candidate_evaluations e ON e.evaluation_id=w.subject_revision
                JOIN candidate_lifecycle l USING(candidate_ref,candidate_revision)
                JOIN claims c ON c.claim_id=e.candidate_ref
                JOIN claim_versions v ON v.claim_id=e.candidate_ref AND v.revision=e.candidate_revision
                WHERE w.work_type='evaluate_candidate' AND w.state='failed'
                  AND w.last_error_code LIKE 'budget_checked:%'
                  AND l.processing_state NOT IN ('resolved','blocked')
                  AND {context}
                ORDER BY e.candidate_ref LIMIT ?""",
            (*params, limit),
        ).fetchall()
        rescheduled = 0
        for row in rows:
            # Retire the marker first: whether or not this candidate schedules,
            # it has now had its one look under the new bound.
            # Obsolete, not failed: the candidate is being re-scheduled into a
            # fresh evaluation, so the old row is superseded work rather than an
            # outstanding failure. Leaving it `failed` kept doctor reporting
            # `work_failed` for hundreds of items nobody could act on, which is
            # exactly the permanently-degraded signal that hides a real one.
            conn.execute(
                """UPDATE work_items SET state='obsolete',
                   last_error_code='budget_rebounded:'||?||'|input_invalid'
                   WHERE work_type='evaluate_candidate' AND state='failed'
                     AND last_error_code LIKE 'budget_checked:%'
                     AND subject_revision IN (SELECT evaluation_id FROM candidate_evaluations
                                              WHERE candidate_ref=? AND candidate_revision=?)""",
                (str(SCHEMA_VERSION), row["candidate_ref"], row["candidate_revision"]),
            )
            if row["read_blocked"] or row["suppressed"] or row["current_revision"] != row["candidate_revision"]:
                continue
            if row["fact_state"] not in {"proposed", "disputed"}:
                continue
            snapshot = CandidateSnapshot(
                row["candidate_ref"], row["candidate_revision"], row["scope_id"],
                row["project_id"], row["branch_id"], row["fact_state"],
                json.loads(row["payload_json"]), "pending_evaluation",
                row["reason"], row["lifecycle_rule"],
            )
            conn.execute(
                """UPDATE candidate_lifecycle SET processing_state='pending_evaluation',
                   reason='evidence_bound_changed',updated_at=?
                   WHERE candidate_ref=? AND candidate_revision=? AND processing_state<>'blocked'""",
                (now, row["candidate_ref"], row["candidate_revision"]),
            )
            _evaluation_id, queued = self._schedule(snapshot, now=now, rule_version=_rule(rule_version))
            if queued:
                rescheduled += 1
        return rescheduled

    def reopen_evaluation(self, work_id: int, *, now: str, reason: str = "operator_retry") -> bool:
        """Put a failed evaluation and its lifecycle back where a retry can run.

        ``fail()`` writes three tables at once, so a retry that only re-queues
        the work item dies immediately as ``candidate_attempt_interrupted``:
        ``begin_model_attempt`` requires ``state='queued'``, a matching work id
        and a null ``model_attempted_at``.  This moves the other two.

        Returns False when there is nothing to move, so the caller leaves the
        work item alone rather than creating the mismatch it is avoiding.
        """
        conn = self._tx._check(write=True)
        row = conn.execute(
            """SELECT evaluation_id,candidate_ref,candidate_revision FROM candidate_evaluations
               WHERE work_id=? AND state='failed'""",
            (work_id,),
        ).fetchone()
        if row is None:
            return False
        moved = conn.execute(
            """UPDATE candidate_evaluations SET state='queued',reason=?,
               completed_at=NULL,model_attempted_at=NULL,failure_code=NULL
               WHERE evaluation_id=? AND state='failed'""",
            (reason, row["evaluation_id"]),
        ).rowcount
        if moved != 1:
            return False
        conn.execute(
            """UPDATE candidate_lifecycle SET processing_state='pending_evaluation',
               reason=?,updated_at=?
               WHERE (candidate_ref,candidate_revision)=(?,?) AND processing_state<>'blocked'""",
            (reason, now, row["candidate_ref"], row["candidate_revision"]),
        )
        return True

    def recover_oversized_evaluations(self, *, now: str, formatter, limit: int = 8) -> int:
        """Re-queue candidate evaluations that died only on the old input budget.

        ``fail()`` writes three tables at once — the evaluation goes ``failed``
        with ``completed_at`` set, the lifecycle goes ``waiting_evidence``, and
        the work item goes ``failed``. ``begin_model_attempt`` then requires
        ``state='queued'``, a matching ``work_id`` and a null
        ``model_attempted_at``, so re-queueing the work item alone would fail
        again immediately as ``candidate_attempt_interrupted``. All three move
        together here or none of them do.

        The real formatter decides: an evaluation that is still oversized keeps
        its failed state and gets a durable marker, so it cannot occupy this
        bounded repair page twice.
        """
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ContractError("INPUT_INVALID", "repair_limit")
        conn = self._tx._check(write=True)
        context, params = self._context("l.")
        rows = conn.execute(
            f"""SELECT e.evaluation_id,e.candidate_ref,e.candidate_revision,e.evidence_refs_json,
                       w.work_id,l.processing_state,l.reason AS lifecycle_reason,l.rule_version AS lifecycle_rule,
                       l.scope_id,l.project_id,l.branch_id,v.state AS fact_state,v.payload_json,
                       c.current_revision,c.read_blocked,c.suppressed
                FROM candidate_evaluations e
                JOIN candidate_lifecycle l USING(candidate_ref,candidate_revision)
                JOIN claims c ON c.claim_id=e.candidate_ref
                JOIN claim_versions v ON v.claim_id=e.candidate_ref AND v.revision=e.candidate_revision
                JOIN work_items w ON w.work_id=e.work_id
                WHERE e.state='failed' AND UPPER(e.failure_code)='INPUT_INVALID'
                  AND w.work_type='evaluate_candidate' AND w.state='failed'
                  AND UPPER(w.last_error_code)='INPUT_INVALID'
                  AND {context}
                ORDER BY e.evaluation_id LIMIT ?""",
            (*params, limit),
        ).fetchall()
        recovered = 0
        for row in rows:
            fits = False
            if not (row["read_blocked"] or row["suppressed"]) \
                    and row["current_revision"] == row["candidate_revision"] \
                    and row["fact_state"] in {"proposed", "disputed"}:
                try:
                    refs = tuple(
                        (text.rsplit("@", 1)[0], int(text.rsplit("@", 1)[1]))
                        for text in json.loads(row["evidence_refs_json"])
                    )
                    sources = tuple(self._tx.source(ref, revision) for ref, revision in refs)
                    if all(source is not None and not source.suppressed for source in sources):
                        # The real stored lifecycle state, not the state it would
                        # have after recovery: this snapshot only measures the
                        # request, and a fabricated field would be the one thing
                        # here that does not describe what is actually on disk.
                        snapshot = CandidateSnapshot(
                            row["candidate_ref"], row["candidate_revision"], row["scope_id"],
                            row["project_id"], row["branch_id"], row["fact_state"],
                            json.loads(row["payload_json"]), row["processing_state"],
                            row["lifecycle_reason"], row["lifecycle_rule"],
                        )
                        formatter(snapshot, sources)
                        fits = True
                except ContractError as exc:
                    if exc.code in {"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED"}:
                        raise
                    fits = False
                except (ValueError, TypeError, AttributeError):
                    fits = False
            moved = self._tx.work.reopen_oversized_candidate_evaluation(
                row["work_id"], now=now, fits=fits,
            )
            if not (fits and moved):
                continue
            conn.execute(
                """UPDATE candidate_evaluations SET state='queued',reason='budget_upgrade',
                   completed_at=NULL,model_attempted_at=NULL,failure_code=NULL
                   WHERE evaluation_id=? AND state='failed'""",
                (row["evaluation_id"],),
            )
            conn.execute(
                """UPDATE candidate_lifecycle SET processing_state='pending_evaluation',
                   reason='budget_upgrade',updated_at=?
                   WHERE (candidate_ref,candidate_revision)=(?,?) AND processing_state<>'blocked'""",
                (now, row["candidate_ref"], row["candidate_revision"]),
            )
            recovered += 1
        return recovered

    def complete(self, evaluation_id: int, work, *, now: str, state: str, reason: str, result_digest: str):
        if state not in {"resolved", "waiting_evidence", "archived"}:
            raise ContractError("INPUT_INVALID", "candidate_completion")
        mutation = self._tx.work.complete(work.work_id, work.lease_token, work.lease_owner, now=now)
        evaluation_state = state
        conn = self._tx._check(write=True)
        conn.execute(
            """UPDATE candidate_evaluations SET state=?,reason=?,completed_at=?,result_digest=?,failure_code=NULL
               WHERE evaluation_id=? AND state='queued'""",
            (evaluation_state, reason, now, result_digest, evaluation_id),
        )
        conn.execute(
            """UPDATE candidate_lifecycle SET processing_state=?,reason=?,last_evaluated_at=?,updated_at=?,
               dormant_at=CASE WHEN ?='archived' THEN ? ELSE dormant_at END
               WHERE (candidate_ref,candidate_revision)=(
                   SELECT candidate_ref,candidate_revision FROM candidate_evaluations WHERE evaluation_id=?)
               AND processing_state<>'blocked'""",
            (state, reason, now, now, state, now, evaluation_id),
        )
        return mutation

    def obsolete(self, evaluation_id: int, work, *, now: str, reason: str):
        mutation = self._tx.work.mark_obsolete(
            work.work_id, work.lease_token, work.lease_owner, now=now,
        )
        conn = self._tx._check(write=True)
        conn.execute(
            """UPDATE candidate_evaluations SET state='obsolete',reason=?,completed_at=?,failure_code=?
               WHERE evaluation_id=? AND state='queued'""",
            (reason, now, reason, evaluation_id),
        )
        conn.execute(
            """UPDATE candidate_lifecycle SET processing_state='waiting_evidence',reason=?,updated_at=?
               WHERE (candidate_ref,candidate_revision)=(
                   SELECT candidate_ref,candidate_revision FROM candidate_evaluations WHERE evaluation_id=?)
               AND processing_state<>'blocked'""",
            (reason, now, evaluation_id),
        )
        return mutation

    def block_objects(self, targets, *, operation_id: str, delete: bool, now: str) -> int:
        """Fence candidate work for deleted or suppressed claims/sources."""
        conn = self._tx._check(write=True)
        pairs: set[tuple[str, int]] = set()
        for target in targets:
            if target.kind == "claim":
                pairs.update((row[0], row[1]) for row in conn.execute(
                    "SELECT candidate_ref,candidate_revision FROM candidate_lifecycle WHERE candidate_ref=?",
                    (target.ref,),
                ))
            if target.kind == "event":
                pairs.update((row[0], row[1]) for row in conn.execute(
                    """SELECT candidate_ref,candidate_revision FROM candidate_evidence
                       WHERE source_ref=?""", (target.ref,),
                ))
        reason = ("deleted" if delete else "suppressed") + ":" + operation_id
        changed = 0
        for ref, revision in sorted(pairs):
            changed += conn.execute(
                """UPDATE candidate_lifecycle SET processing_state='blocked',reason=?,updated_at=?
                   WHERE candidate_ref=? AND candidate_revision=? AND processing_state<>'blocked'""",
                (reason, now, ref, revision),
            ).rowcount
            conn.execute(
                """UPDATE work_items SET state='obsolete',lease_token=lease_token+1,
                   lease_owner=NULL,lease_until=NULL,last_error_code='authority_revoked'
                   WHERE work_id IN (SELECT work_id FROM candidate_evaluations
                       WHERE candidate_ref=? AND candidate_revision=? AND work_id IS NOT NULL)
                   AND state IN ('pending','leased')""",
                (ref, revision),
            )
            conn.execute(
                """UPDATE candidate_evaluations SET state='obsolete',reason='authority_revoked',
                   completed_at=?,failure_code='authority_revoked'
                   WHERE candidate_ref=? AND candidate_revision=? AND state='queued'""",
                (now, ref, revision),
            )
        return changed


__all__ = ["CandidateLifecycle"]
