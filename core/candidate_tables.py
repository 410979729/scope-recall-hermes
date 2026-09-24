"""The candidate tables, borrowed from the owning transaction.

Candidate processing is metadata beside a claim version: ``candidate_lifecycle``
says where a candidate stands, ``candidate_evidence`` what it has heard,
``candidate_evaluations`` which questions were asked and ``work_items`` who is
asking.  This module holds the SQL every part of the lifecycle shares -- the
audience filter, the joined "is this candidate still judgeable" row, the bounded
evidence selection and the single-row moves -- so ``candidate_intake``,
``candidate_evaluations`` and ``candidate_sweeps`` each read as one job.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from ..contracts import ContractError
from .candidate_debounce import settle_reason
from .candidate_lifecycle import CandidateSnapshot
from .evidence_question import DERIVATION_ROOT_ORIGINS, question_digest, restates

#: Characters of evidence content one candidate evaluation may carry.
#:
#: CANDIDATE_EVALUATION_INPUT_BUDGET is 64,000 bytes and roughly 12 KB of every
#: request is fixed prompt overhead, so this leaves room even when every
#: character is three UTF-8 bytes.  The bound is a size, not a source count: a
#: count let candidates accumulate sixteen sources of any length until their
#: prompt exceeded every budget and died terminal.
CANDIDATE_EVIDENCE_BUDGET_CHARS = 14000

#: Recalled memory delivered back into a conversation.  It is this system's
#: own output, so admitting it as candidate evidence is a feedback loop: it
#: adds no information the store did not already hold and keeps a candidate
#: from ever going quiet.
ECHO_ORIGINS = frozenset({"memory_reinjection"})

#: Fact states a candidate may still be judged in.
JUDGEABLE_STATES = frozenset({"proposed", "disputed"})

#: Fingerprint of a candidate that has not been asked anything yet.
EMPTY_FINGERPRINT = hashlib.sha256(b"[]").hexdigest()

#: One lifecycle row joined to its claim head and current version.  Every
#: reader that must decide whether a candidate is still judgeable selects these
#: from ``candidate_lifecycle l`` through ``HEAD_JOINS``.
HEAD_COLUMNS = """l.candidate_ref,l.candidate_revision,l.scope_id,l.project_id,l.branch_id,
       l.processing_state,l.reason AS lifecycle_reason,l.rule_version AS lifecycle_rule,
       v.state AS fact_state,v.payload_json,c.current_revision,c.read_blocked,c.suppressed"""
HEAD_JOINS = """JOIN claims c ON c.claim_id=l.candidate_ref
       JOIN claim_versions v ON v.claim_id=l.candidate_ref AND v.revision=l.candidate_revision"""


def parse_time(value: str) -> datetime:
    """A caller-supplied timestamp; it must carry a zone."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ContractError("INPUT_INVALID", "candidate_timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError("INPUT_INVALID", "candidate_timestamp")
    return parsed.astimezone(timezone.utc)


def utc(value: str) -> str:
    """Normalise a caller-supplied timestamp to the stored ``Z`` form."""
    return stamp(parse_time(value))


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def rule(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 64 or not value.strip() or "\x00" in value:
        raise ContractError("INPUT_INVALID", "candidate_rule_version")
    return value


def encode_refs(refs) -> str:
    return json.dumps([f"{ref}@{revision}" for ref, revision in refs], ensure_ascii=False, separators=(",", ":"))


def parse_refs(text: str) -> tuple[tuple[str, int], ...]:
    """Inverse of ``encode_refs``; raises the plain JSON/shape errors for the caller to classify."""
    return tuple((item.rsplit("@", 1)[0], int(item.rsplit("@", 1)[1])) for item in json.loads(text))


def reachable_sql(prefix: str = "") -> str:
    """Rows new evidence can still reach: live, or asleep only for lack of evidence."""
    return (f"({prefix}processing_state IN ('pending_evaluation','waiting_evidence') OR "
            f"({prefix}processing_state='archived' AND {prefix}reason='dormant_no_evidence'))")


def is_reachable(processing_state: str, reason: str) -> bool:
    return processing_state in {"pending_evaluation", "waiting_evidence"} or (
        processing_state == "archived" and reason == "dormant_no_evidence"
    )


def is_live_head(row) -> bool:
    """The joined row still describes a current, readable, judgeable candidate."""
    return (not row["read_blocked"] and not row["suppressed"]
            and row["current_revision"] == row["candidate_revision"]
            and row["fact_state"] in JUDGEABLE_STATES)


def snapshot(row, *, processing_state: str | None = None) -> CandidateSnapshot:
    """Build the model-facing snapshot from a ``HEAD_COLUMNS`` row."""
    return CandidateSnapshot(
        row["candidate_ref"], row["candidate_revision"], row["scope_id"], row["project_id"], row["branch_id"],
        row["fact_state"], json.loads(row["payload_json"]), processing_state or row["processing_state"],
        row["lifecycle_reason"], row["lifecycle_rule"],
    )


def settled_reason(row, now: str, *, has_queued_evaluation: bool = False) -> str | None:
    """Apply the debounce rule to a lifecycle row's own timestamps."""
    return settle_reason(
        now=now, last_evidence_at=row["last_evidence_at"], last_evaluated_at=row["last_evaluated_at"],
        created_at=row["created_at"], has_queued_evaluation=has_queued_evaluation,
    )


class CandidateTables:
    """SQL shared by every part of the candidate lifecycle."""

    def __init__(self, transaction) -> None:
        self._tx = transaction

    def _read(self):
        return self._tx._check()

    def _write(self):
        return self._tx._check(write=True)

    def _context(self, prefix: str = "", *, all_projects: bool = False) -> tuple[str, tuple]:
        """Audience filter for any table carrying scope/project/branch columns."""
        scopes = sorted(self._tx.context.allowed_scope_ids)
        clause = f"{prefix}scope_id IN ({','.join('?' for _ in scopes)})"
        if all_projects:
            return clause, tuple(scopes)
        clause += (f" AND ({prefix}project_id IS NULL OR {prefix}project_id=?)"
                   f" AND ({prefix}branch_id IS NULL OR {prefix}branch_id=?)")
        return clause, (*scopes, self._tx.context.project_id, self._tx.context.branch_id)

    def _lifecycle_row(self, ref: str, revision: int):
        return self._read().execute(
            """SELECT processing_state,reason,rule_version,updated_at,last_evidence_at,last_evaluated_at,created_at
               FROM candidate_lifecycle WHERE candidate_ref=? AND candidate_revision=?""",
            (ref, revision),
        ).fetchone()

    def _cited_origins(self, ref: str, revision: int) -> frozenset[str]:
        """Effective origins of the sources this claim version cites, imports resolved."""
        origins = set()
        for row in self._read().execute(
            """SELECT s.origin,s.source_original_origin,s.import_provenance_sha256
               FROM evidence_links l JOIN source_events s
                 ON s.event_id=l.source_ref AND s.source_revision=l.source_revision
               WHERE l.object_kind='claim' AND l.object_ref=? AND l.object_revision=?""",
            (ref, revision),
        ).fetchall():
            origin = row["origin"]
            if origin == "imported":
                verified = row["import_provenance_sha256"] is not None
                origin = (row["source_original_origin"] or "origin_unknown") if verified else "origin_unknown"
            origins.add(origin)
        return frozenset(origins)

    def restated_by_a_root(self, ref: str, revision: int, payload) -> bool:
        """Whether a live source a claim may be derived from, attached to this candidate version, restates it.

        A person who says a proposal again is heard here and not in the claim's own evidence, because
        ``apply_claim`` takes the restatement for a duplicate; such a proposal is left to its
        evaluation (``requalify.retire_rootless_proposals``).  A source that only shares a word with
        it -- on the pilot, 1,525 of 2,785 tool-derived proposals had one -- restates nothing.
        """
        origins = sorted(DERIVATION_ROOT_ORIGINS)
        contents = [row[0] for row in self._read().execute(
            f"""SELECT s.content FROM candidate_evidence e JOIN source_events s
                  ON s.event_id=e.source_ref AND s.source_revision=e.source_revision
                WHERE e.candidate_ref=? AND e.candidate_revision=? AND s.read_blocked=0 AND s.suppressed=0
                  AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                      AND b.object_ref=e.source_ref AND (b.read_blocked=1 OR b.suppressed=1))
                  AND (CASE WHEN s.origin<>'imported' THEN s.origin
                            WHEN s.import_provenance_sha256 IS NOT NULL
                            THEN COALESCE(s.source_original_origin,'origin_unknown')
                            ELSE 'origin_unknown' END) IN ({','.join('?' for _ in origins)})""",
            (ref, revision, *origins),
        )]
        return bool(contents) and restates(payload, contents)

    def _model_verdicts(self, ref: str, revision: int, *,
                        excluding: int | None = None) -> tuple[int, frozenset[tuple[str, int]]]:
        """Model verdicts this candidate already had, and every source they judged.

        An attempt handed back without a verdict (a refused account, a capacity
        refusal) clears ``model_attempted_at`` and is not counted.
        """
        rows = self._read().execute(
            """SELECT evidence_refs_json FROM candidate_evaluations
               WHERE candidate_ref=? AND candidate_revision=? AND model_attempted_at IS NOT NULL
                 AND evaluation_id<>?""",
            (ref, revision, -1 if excluding is None else excluding),
        ).fetchall()
        judged: set[tuple[str, int]] = set()
        for row in rows:
            try:
                judged.update(parse_refs(row[0]))
            except (ValueError, TypeError, AttributeError):
                continue
        return len(rows), frozenset(judged)

    def _restated_in_unjudged(self, payload, refs, judged: frozenset[tuple[str, int]]) -> bool:
        """Whether a source no earlier verdict saw restates the candidate."""
        unjudged = []
        for ref, revision in refs:
            if (ref, revision) in judged:
                continue
            row = self._read().execute(
                "SELECT content FROM source_events WHERE event_id=? AND source_revision=?", (ref, revision),
            ).fetchone()
            if row is not None:
                unjudged.append(row["content"])
        return bool(unjudged) and restates(payload, unjudged)

    def _candidate_of(self, evaluation_id: int) -> tuple[str, int] | None:
        row = self._read().execute(
            "SELECT candidate_ref,candidate_revision FROM candidate_evaluations WHERE evaluation_id=?",
            (evaluation_id,),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def _move(self, ref: str, revision: int, state: str, reason: str, *,
              now: str | None = None, evaluated_at: str | None = None, dormant_at: str | None = None) -> int:
        """Move one candidate to ``state``/``reason`` unless it is fenced ``blocked``.

        A column left ``None`` keeps its stored value, so a caller names only
        what its transition touches.
        """
        return self._write().execute(
            """UPDATE candidate_lifecycle SET processing_state=?,reason=?,
               updated_at=COALESCE(?,updated_at),last_evaluated_at=COALESCE(?,last_evaluated_at),
               dormant_at=COALESCE(?,dormant_at)
               WHERE candidate_ref=? AND candidate_revision=? AND processing_state<>'blocked'""",
            (state, reason, now, evaluated_at, dormant_at, ref, revision),
        ).rowcount

    def _move_for(self, evaluation_id: int, state: str, reason: str, *,
                  now: str | None = None, evaluated_at: str | None = None, dormant_at: str | None = None) -> int:
        """``_move`` addressed by the evaluation the worker is holding."""
        key = self._candidate_of(evaluation_id)
        if key is None:
            return 0
        return self._move(*key, state, reason, now=now, evaluated_at=evaluated_at, dormant_at=dormant_at)

    def _close_evaluation(self, evaluation_id: int, state: str, reason: str, now: str, *,
                          failure_code: str | None = None, result_digest: str | None = None) -> None:
        """Finish a queued evaluation as ``failed``, ``obsolete`` or a verdict state."""
        self._write().execute(
            """UPDATE candidate_evaluations SET state=?,reason=?,completed_at=?,result_digest=?,failure_code=?
               WHERE evaluation_id=? AND state='queued'""",
            (state, reason, now, result_digest, failure_code, evaluation_id),
        )

    def _release_attempt(self, evaluation_id: int, reason: str, code: str) -> None:
        """Hand a queued evaluation back for another attempt without a verdict."""
        self._write().execute(
            """UPDATE candidate_evaluations SET model_attempted_at=NULL,reason=?,failure_code=?
               WHERE evaluation_id=? AND state='queued'""",
            (reason, code, evaluation_id),
        )

    def _requeue_failed(self, evaluation_id: int, reason: str) -> int:
        """Put a failed evaluation back where ``begin_model_attempt`` will accept it."""
        return self._write().execute(
            """UPDATE candidate_evaluations SET state='queued',reason=?,
               completed_at=NULL,model_attempted_at=NULL,failure_code=NULL
               WHERE evaluation_id=? AND state='failed'""",
            (reason, evaluation_id),
        ).rowcount

    def _retire_evaluations(self, ref: str, revision: int, code: str, now: str, *, others: bool = False) -> None:
        """Obsolete a candidate's queued evaluations and fence their unfinished work.

        ``others`` retires every revision *except* ``revision`` instead, for a
        claim that has just moved to a new head.
        """
        match = "candidate_revision<>?" if others else "candidate_revision=?"
        conn = self._write()
        conn.execute(
            f"""UPDATE work_items SET state='obsolete',lease_token=lease_token+1,lease_owner=NULL,lease_until=NULL,
                last_error_code=? WHERE state IN ('pending','leased') AND work_id IN (
                    SELECT work_id FROM candidate_evaluations
                    WHERE candidate_ref=? AND {match} AND work_id IS NOT NULL)""",
            (code, ref, revision),
        )
        conn.execute(
            f"""UPDATE candidate_evaluations SET state='obsolete',reason=?,completed_at=?,failure_code=?
                WHERE candidate_ref=? AND {match} AND state='queued'""",
            (code, now, code, ref, revision),
        )

    def _evaluation_evidence(self, ref: str, revision: int) -> list:
        """The live, bounded evidence set an evaluation of this candidate would carry."""
        evidence = self._read().execute(
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
            (ref, revision),
        ).fetchall()
        # Bound the bytes here, where the set is chosen, so what the evaluation
        # declares is what the prompt can carry; trimming later would leave the
        # model returning fewer source_refs than the evaluation committed to.
        # Newest first, but only among sources that fit: one oversized tool
        # transcript at the head must not doom a candidate that also holds a
        # short first-hand line right behind it.
        selected, budget_left = [], CANDIDATE_EVIDENCE_BUDGET_CHARS
        for item in evidence:
            length = int(item["content_length"] or 0)
            if length <= budget_left:
                selected.append(item)
                budget_left -= length
        if not selected and evidence:
            # Every source is oversized on its own.  Judge the smallest rather
            # than nothing: the alternative is a candidate no evidence can reach.
            selected = [min(evidence, key=lambda item: int(item["content_length"] or 0))]
        return selected

    def _question(self, ref: str, revision: int) -> tuple[tuple[tuple[str, int], ...], str]:
        """The evidence refs a fresh evaluation would carry, and the question they pose.

        The digest keys on the *question*, not the byte-exact selection: one
        more tool observation displacing an older one is not a new question,
        and keying on that is what let the fingerprint guard never collide.
        """
        selected = self._evaluation_evidence(ref, revision)
        refs = tuple(sorted((str(item["source_ref"]), int(item["source_revision"])) for item in selected))
        digest = question_digest(
            (str(item["source_ref"]), int(item["source_revision"]), item["origin"]) for item in selected
        )
        return refs, digest


__all__ = [
    "CANDIDATE_EVIDENCE_BUDGET_CHARS", "ECHO_ORIGINS", "EMPTY_FINGERPRINT", "HEAD_COLUMNS", "HEAD_JOINS",
    "JUDGEABLE_STATES", "CandidateTables", "encode_refs", "is_live_head", "is_reachable",
    "parse_refs", "parse_time", "reachable_sql", "rule", "settled_reason", "snapshot", "stamp", "utc",
]
