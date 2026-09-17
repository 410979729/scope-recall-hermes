"""SQLite work_items lease and lifecycle inside the owning transaction."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re

from ..contracts import ContractError
from .schema import SCHEMA_VERSION

MAX_RECOVERABLE_ATTEMPTS = 3
MAX_OPERATOR_RETRIES = 2
DERIVATION_RETRY_MARKER = "derivation_retry:1"
OPERATOR_ORIGINS = frozenset({"human_direct", "host_generated"})
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_OPERATOR_TOKEN = re.compile(r"(?:^|\|prior:)operator_retry:([A-Za-z0-9][A-Za-z0-9_.-]{0,63})(?=\||$)")
_AUTO_TOKEN = re.compile(r"(?:^|\|(?:prior:)?)auto_retry:([0-9]+)(?=\||$)")
# Only infrastructure failures may be recovered without a new source revision.
# Invalid derivations and rejected authority remain terminal and inspectable.
AUTO_RECOVERABLE_ERRORS = frozenset({
    "model_unavailable", "model_timeout", "timeout", "network_error", "http_429",
    "http_500", "http_502", "http_503", "http_504", "rate_limited",
    "storage_unavailable", "STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED",
    "memory_epoch_changed", "lease_exhausted", "embedding_unavailable",
})

#: Failures that are never about the work item.  A provider declining to serve
#: anyone says nothing about this payload -- unlike a timeout, which a large
#: item can genuinely cause -- so the lease never got an attempt at all and the
#: attempt is refunded.  Without the refund one four-hour provider outage pushed
#: 195 items into ``failed`` at ``attempt=3`` apiece, each needing an operator.
CAPACITY_REFUSALS = frozenset({"http_429", "rate_limited", "http_502", "http_503", "http_504"})

#: The provider declining the account rather than this request: payment
#: required, key rejected, access forbidden.  No payload changes that answer, so
#: the worker parks the item without an attempt (``BUDGET_PAUSE_ERRORS``) until
#: someone fixes the account.  On alpha a DeepSeek balance that ran out
#: answered 402 for fifteen minutes and failed 100 candidate evaluations
#: outright, none of which an operator command could reopen afterwards.
ACCOUNT_REFUSALS = frozenset({"http_401", "http_402", "http_403"})

#: Token recording how many times in a row a provider refused for capacity.
#: Kept in the error code beside ``auto_retry:`` rather than a new column, so
#: no migration is needed and an operator reading the row sees the history.
_CAPACITY_TOKEN = re.compile(r"(?:^|\|)capacity:([0-9]+)(?=\||$)")

#: First wait after a provider refuses for capacity, and the longest one.
#: Sized against a measured outage: the median interval between one item's
#: retries was 115 minutes, so the floor sits above that cycle rather than
#: below it (a 60 s floor would have made the instance ask *more* often).
#: There is no limit on how many times an item may check: it is never given
#: up on, only asked less often.
CAPACITY_BACKOFF_FLOOR_SECONDS = 1800.0
CAPACITY_BACKOFF_CEILING_SECONDS = 4 * 3600.0

#: Purge rows are claimed before anything else: a delete must land before
#: derived work can rebuild what it removes.
_PURGE_FIRST = "CASE WHEN work_type='purge' THEN 0 ELSE 1 END"
_LEASED_ROW = "work_id=? AND state='leased' AND lease_token=? AND lease_owner=?"

# --- the fresh conversation lane ---------------------------------------------
#
# Claiming is otherwise strictly FIFO, so a message typed now waited behind the
# whole backlog -- 1,443 items, the oldest 19 hours, on one live instance --
# before it was consolidated into claims or embedded for semantic recall.  A
# lane claim prefers ready consolidate/embed work whose subject source is fresh
# conversation, oldest first, so one conversation is still consolidated in
# order and its episode batch still forms.  Purge keeps absolute priority and
# the drain gives the lane at most every other claim, so the backlog keeps
# moving however busy the chat is.

#: A source is fresh for two hours after it was persisted: about 48 default
#: worker cycles (120 s pass + 30 s interval).  Work the lane has not reached by
#: then means the lane is itself behind, and it ages in FIFO like any other.
#: The window also outlasts the first capacity backoff (30 min) of a provider
#: refusal, so one refused call does not cost an item its place.
FRESH_LANE_SECONDS = 2 * 3600.0
#: The conversation roots a person or a tool produced in the conversation.
#: The other roots are ingested content: external_document and imported record
#: when content was stored, not when anyone said it, and one bulk import would
#: otherwise fill the lane.
FRESH_CONVERSATION_ORIGINS = frozenset({"human_direct", "tool_observation"})
#: assistant_visible is not a consolidation root, but recall searches it
#: semantically, so its embedding is fresh work too.
FRESH_LANE_ORIGINS = {
    "consolidate": FRESH_CONVERSATION_ORIGINS,
    "embed": FRESH_CONVERSATION_ORIGINS | {"assistant_visible"},
}
#: Recently available rows one lane claim examines, newest first.  The bound
#: keeps the query off the backlog: a reverse range scan of ``work_ready``, one
#: primary-key probe into ``source_events`` per row and a sort of at most this
#: many rows, a few milliseconds with tens of thousands pending.  It covers 16
#: passes of the largest refill (16 sources x 2 types), which is more than a
#: realistic window holds.  Fresh work beyond it is still claimed in FIFO order.
FRESH_LANE_SCAN_ROWS = 512


def fresh_since(now: str) -> str:
    """The oldest ``persisted_at`` that still counts as fresh conversation."""
    return _after(now, -FRESH_LANE_SECONDS)


def _marks(values) -> str:
    return ",".join("?" for _ in values)


def _auto_count(error: str) -> int:
    return max((int(value) for value in _AUTO_TOKEN.findall(error)), default=0)


def _failure_kind(error: str) -> str:
    return error.rsplit("|", 1)[-1]


def _preserve_retry_history(error) -> bool:
    return bool(error and (str(error).startswith(("operator_retry:", "chunk_upgrade:1106|"))
                           or _auto_count(str(error)) or "derivation_retry:1" in str(error)))


def _auto_retry_stamp(error: str) -> str:
    """The error code of a row granted one automatic recovery."""
    stamp = f"auto_retry:{_auto_count(error) + 1}|{_failure_kind(error)}"
    if "derivation_retry:1|" in error:
        stamp = "derivation_retry:1|" + stamp
    if error.startswith("operator_retry:"):
        stamp = f"{error}|{stamp}"[-1024:]
    return stamp


def _capacity_count(error: object) -> int:
    return max((int(value) for value in _CAPACITY_TOKEN.findall(str(error or ""))), default=0)


def _capacity_backoff_seconds(refusals: int) -> float:
    """Wait before asking a provider that just refused for capacity again."""
    if type(refusals) is not int or refusals <= 1:
        return CAPACITY_BACKOFF_FLOOR_SECONDS
    return min(CAPACITY_BACKOFF_CEILING_SECONDS,
               CAPACITY_BACKOFF_FLOOR_SECONDS * (2.0 ** min(refusals - 1, 16)))


def _operator_retry_ids(value: str) -> tuple[str, ...]:
    return tuple(_OPERATOR_TOKEN.findall(value))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ContractError("INPUT_INVALID", "timestamp")
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _after(now: str, seconds: float) -> str:
    return _format_time(_parse_time(now) + timedelta(seconds=seconds))


def _backoff_seconds(attempt: int) -> float:
    return min(60.0, 2.0 ** max(0, attempt))


@dataclass(frozen=True)
class LeasedWork:
    work_id: int
    work_type: str
    subject_ref: str
    subject_revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None
    lease_token: int
    attempt: int
    lease_owner: str

    @property
    def lease(self) -> tuple[int, int, str]:
        """``(work_id, lease_token, lease_owner)``: the fence every mutation takes."""
        return self.work_id, self.lease_token, self.lease_owner


@dataclass(frozen=True)
class WorkMutation:
    work_id: int
    disposition: str
    state: str
    lease_token: int | None = None


@dataclass(frozen=True)
class OperatorRetryMutation:
    """Auditable result of one explicit, operator-authorized retry request.

    This is intentionally separate from ``WorkMutation``: an operator request
    may be rejected without owning a lease, and must never impersonate a
    worker completion/failure transition.
    """

    work_id: int
    disposition: str
    state: str
    reason: str | None = None
    operation_id: str | None = None


# --- where each work type's subject lives ----------------------------------
#
# Enqueue resolves the subject to the scope, project and branch a new row
# inherits; the retry paths ask whether that subject may still be worked on.


def _source_context(tx, ref: str, revision: int) -> tuple[str, str | None, str | None]:
    source = tx.source(ref, revision)
    if source is None:
        raise ContractError("SOURCE_MISSING")
    return source.scope_id, source.project_id, source.branch_id


def _claim_context(tx, ref: str, revision: int) -> tuple[str, str | None, str | None]:
    version = tx.claims.version(ref, revision)
    if version is None:
        raise ContractError("SOURCE_MISSING")
    tx.claims.require_target(version)
    return version.scope_id, version.project_id, version.branch_id


def _source_or_claim_context(tx, ref: str, revision: int) -> tuple[str, str | None, str | None]:
    # A claim is embeddable and projectable too; without this fallback derived
    # objects never reach the vector index and enter recall only by relation
    # expansion out of some event retrieved first.
    source = tx.source(ref, revision)
    if source is not None:
        return source.scope_id, source.project_id, source.branch_id
    return _claim_context(tx, ref, revision)


def _purge_context(tx, ref: str, revision: int) -> tuple[str, str | None, str | None]:
    scope_id = ref.split(":", 1)[-1]
    tx._scope(scope_id)
    return scope_id, tx.context.project_id, tx.context.branch_id


def _candidate_context(tx, ref: str, revision: int) -> tuple[str, str | None, str | None]:
    row = tx._check().execute(
        """SELECT candidate_ref,scope_id,project_id,branch_id,state FROM candidate_evaluations e
           JOIN candidate_lifecycle l USING(candidate_ref,candidate_revision)
           WHERE evaluation_id=?""",
        (revision,),
    ).fetchone()
    if row is None or "candidate:" + row["candidate_ref"] != ref or row["state"] != "queued":
        raise ContractError("SOURCE_MISSING", "candidate_evaluation")
    tx._scope(row["scope_id"])
    if (row["project_id"], row["branch_id"]) != (tx.context.project_id, tx.context.branch_id):
        raise ContractError("ACCESS_DENIED", "candidate_context")
    return row["scope_id"], row["project_id"], row["branch_id"]


_SUBJECT_CONTEXT = {
    "consolidate": _source_context,
    "embed": _source_or_claim_context,
    "rebuild_projection": _source_or_claim_context,
    "purge": _purge_context,
    "evaluate_candidate": _candidate_context,
}
ALLOWED_WORK_TYPES = frozenset(_SUBJECT_CONTEXT)


def _current_source_reason(tx, source) -> str | None:
    current = tx.source_current(source.ref)
    if current is None or current.revision != source.revision:
        return "source_revision_stale"
    try:
        tx.claims.require_live_source(source.ref, source.revision)
    except ContractError:
        return "source_revision_stale"
    return None


def _source_retry_reason(tx, ref: str, revision: int, *, current_epoch: int | None = None) -> str | None:
    source = tx.source(ref, revision)
    if source is None:
        return "source_deleted_or_inaccessible"
    return _current_source_reason(tx, source)


def _projection_retry_reason(tx, ref: str, revision: int, *, current_epoch: int | None = None) -> str | None:
    source = tx.source(ref, revision)
    if source is not None:
        return _current_source_reason(tx, source)
    version = tx.claims.version(ref, revision)
    if version is None or tx.claims.current_revision(ref) != revision:
        return "claim_revision_stale_or_deleted"
    try:
        tx.claims.require_target(version)
    except ContractError:
        return "claim_denied"
    return None


def _purge_retry_reason(tx, ref: str, revision: int, *, current_epoch: int | None = None) -> str | None:
    try:
        receipt = tx.deletions.receipt(ref.rsplit(":", 1)[0])
    except ContractError:
        receipt = None
    if receipt is None or receipt.get("mode") != "delete":
        return "delete_fence_missing"
    if current_epoch is not None and int(receipt.get("memory_epoch", current_epoch)) > current_epoch:
        return "memory_epoch_stale"
    return None


#: Why a failed row's subject may no longer be worked on.  Candidate
#: evaluations are absent on purpose: an evidence combination is evaluated at
#: most once, and new evidence makes a new row rather than a retry.
_RETRY_SUBJECT_REASON = {
    "consolidate": _source_retry_reason,
    "embed": _source_retry_reason,
    "rebuild_projection": _projection_retry_reason,
    "purge": _purge_retry_reason,
}


class WorkItems:
    def __init__(self, transaction) -> None:
        self._tx = transaction

    def derivation_feedback(self, work_id: int) -> dict[str, str] | None:
        """Read repair metadata only for a work item granted the bounded retry."""
        from .failure_retry import validation_feedback  # imports this module

        conn = self._tx._check()
        row = conn.execute("SELECT last_error_code FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if row is None or f"{DERIVATION_RETRY_MARKER}|" not in str(row[0] or ""):
            return None
        # A successful fragment consumes this feedback, not the durable retry
        # allowance. Keep the marker/history, but never replay an old page's
        # validation error after its checkpoint (including later timeouts).
        outstanding = str(row[0]).lower().rsplit("consolidation_chunk_pending", 1)[-1]
        if not {"derivation_invalid", "input_invalid"}.intersection(outstanding.split("|")):
            return None
        detail = conn.execute(
            """SELECT error_code,error_field FROM work_error_details WHERE work_id=?
               AND lower(error_code) IN ('derivation_invalid','input_invalid')
               ORDER BY detail_id DESC LIMIT 1""", (work_id,),
        ).fetchone()
        return validation_feedback(*(detail if detail is not None else (None, None)))

    def _visible_filter(self) -> tuple[str, tuple]:
        """SQL restricting work_items to the context's scopes, project and branch."""
        context = self._tx.context
        scopes = sorted(context.allowed_scope_ids)
        return (
            f"scope_id IN ({_marks(scopes)}) AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)",
            (*scopes, context.project_id, context.branch_id),
        )

    def _context_denial(self, row) -> str | None:
        """Why this context may not act on the row, or None when it may."""
        context = self._tx.context
        if row["scope_id"] not in context.allowed_scope_ids:
            return "scope_denied"
        if row["project_id"] is not None and row["project_id"] != context.project_id:
            return "project_denied"
        if row["branch_id"] is not None and row["branch_id"] != context.branch_id:
            return "branch_denied"
        return None

    def _retry_subject_reason(self, row, *, current_epoch: int | None = None) -> str | None:
        return _RETRY_SUBJECT_REASON[row["work_type"]](
            self._tx, row["subject_ref"], row["subject_revision"], current_epoch=current_epoch)

    def enqueue(self, work_type: str, subject_ref: str, subject_revision: int, *, available_at: str) -> bool:
        conn = self._tx._check(write=True)
        if work_type not in ALLOWED_WORK_TYPES:
            raise ContractError("INPUT_INVALID", "work_type")
        if type(subject_ref) is not str or not subject_ref or type(subject_revision) is not int or subject_revision < 1:
            raise ContractError("INPUT_INVALID", "work_subject")
        scope_id, project_id, branch_id = _SUBJECT_CONTEXT[work_type](self._tx, subject_ref, subject_revision)
        before = conn.total_changes
        conn.execute(
            """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(work_type,subject_ref,subject_revision) DO NOTHING""",
            (work_type, subject_ref, subject_revision, scope_id, project_id, branch_id, available_at),
        )
        return conn.total_changes > before

    def release_stale(self, now: str) -> int:
        conn = self._tx._check(write=True)
        visible, params = self._visible_filter()
        base = f"state='leased' AND {visible} AND lease_until IS NOT NULL AND lease_until < ?"
        params = (*params, now)
        failed = conn.execute(
            f"""UPDATE work_items SET state='failed',lease_owner=NULL,lease_until=NULL,last_error_code=CASE
                    WHEN last_error_code LIKE 'operator_retry:%' OR last_error_code LIKE 'auto_retry:%' OR last_error_code LIKE '%derivation_retry:1|%' THEN last_error_code || '|lease_exhausted'
                    ELSE 'lease_exhausted' END, available_at=?
                WHERE {base} AND attempt >= ?""",
            (now, *params, MAX_RECOVERABLE_ATTEMPTS),
        ).rowcount
        released = conn.execute(
            f"""UPDATE work_items SET state='pending',lease_owner=NULL,lease_until=NULL
                WHERE {base} AND attempt < ?""",
            (*params, MAX_RECOVERABLE_ATTEMPTS),
        ).rowcount
        return failed + released

    def claim_next(self, owner: str, now: str, *, lease_seconds: float, limit: int = 1,
                   allowed_work_types: frozenset[str] = ALLOWED_WORK_TYPES,
                   fresh_lane: bool = False) -> tuple[LeasedWork, ...]:
        """Lease ready work: purge first, then FIFO by availability.

        ``fresh_lane`` places ready fresh conversation work (see
        ``FRESH_LANE_ORIGINS``) after purge and before the rest of the FIFO
        page.  Without fresh work the result is exactly the FIFO claim.
        """
        conn = self._tx._check(write=True)
        if type(owner) is not str or not owner or type(limit) is not int or not 1 <= limit <= 32:
            raise ContractError("INPUT_INVALID", "work_claim")
        if type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ContractError("INPUT_INVALID", "lease_seconds")
        if type(allowed_work_types) is not frozenset or not allowed_work_types <= ALLOWED_WORK_TYPES:
            raise ContractError("INPUT_INVALID", "work_types")
        if type(fresh_lane) is not bool:
            raise ContractError("INPUT_INVALID", "work_claim")
        if not allowed_work_types:
            return ()
        visible, params = self._visible_filter()
        kinds = sorted(allowed_work_types)
        self.release_stale(now)
        rows = conn.execute(
            f"""SELECT work_id,work_type FROM work_items
                WHERE state='pending' AND available_at<=? AND {visible} AND work_type IN ({_marks(kinds)})
                ORDER BY {_PURGE_FIRST}, available_at, work_id LIMIT ?""",
            (now, *params, *kinds, limit),
        ).fetchall()
        if fresh_lane:
            rows = self._fresh_first(rows, now=now, limit=limit, kinds=allowed_work_types)
        until = _after(now, float(lease_seconds))
        claimed = []
        for row in rows:
            # The write transaction is IMMEDIATE, so the pending rows selected
            # above cannot change under this loop; the new token is the fence
            # every later mutation must present.
            conn.execute(
                """UPDATE work_items SET state='leased',lease_token=lease_token+1,lease_owner=?,lease_until=?,attempt=attempt+1
                   WHERE work_id=?""",
                (owner, until, row["work_id"]),
            )
            item = conn.execute("SELECT * FROM work_items WHERE work_id=?", (row["work_id"],)).fetchone()
            claimed.append(LeasedWork(
                item["work_id"], item["work_type"], item["subject_ref"], item["subject_revision"],
                item["scope_id"], item["project_id"], item["branch_id"], item["lease_token"], item["attempt"], owner,
            ))
        return tuple(claimed)

    def _fresh_first(self, fifo, *, now: str, limit: int, kinds: frozenset[str]) -> list:
        """Purge rows, then ready fresh conversation work, then the rest of the FIFO page."""
        purges = [row for row in fifo if row["work_type"] == "purge"]
        lane = sorted(kinds & FRESH_LANE_ORIGINS.keys())
        if len(purges) == limit or not lane:
            return fifo
        since = fresh_since(now)
        visible, params = self._visible_filter()
        origins = " OR ".join(f"(w.work_type=? AND e.origin IN ({_marks(FRESH_LANE_ORIGINS[kind])}))" for kind in lane)
        origin_params = tuple(value for kind in lane for value in (kind, *sorted(FRESH_LANE_ORIGINS[kind])))
        # Work never becomes available before its source was persisted, so the
        # window bounds the index range as well as the source it joins.
        fresh = self._tx._check().execute(
            f"""SELECT w.work_id,w.work_type FROM (
                    SELECT work_id,work_type,subject_ref,subject_revision,available_at FROM work_items
                    WHERE state='pending' AND available_at>=? AND available_at<=? AND {visible}
                      AND work_type IN ({_marks(lane)})
                    ORDER BY available_at DESC,work_id DESC LIMIT ?) w
                JOIN source_events e ON e.event_id=w.subject_ref AND e.source_revision=w.subject_revision
                WHERE e.persisted_at>=? AND ({origins})
                ORDER BY w.available_at,w.work_id LIMIT ?""",
            (since, now, *params, *lane, FRESH_LANE_SCAN_ROWS, since, *origin_params, limit - len(purges)),
        ).fetchall()
        chosen = {row["work_id"] for row in (*purges, *fresh)}
        return [*purges, *fresh, *(row for row in fifo if row["work_id"] not in chosen)][:limit]

    def recover_transient_failures(self, *, now: str, allowed_work_types: frozenset[str],
                                   cooldown_seconds: float = 3600, max_recoveries: int = 2,
                                   limit: int = 32) -> int:
        """Grant bounded, cooled-down attempts without resetting lifetime attempts.

        Scope/project/branch and source/delete authority are rechecked in this
        transaction; a later worker still owns its lease and epoch checks.
        The existing error field records the finite automatic recovery budget.
        """
        conn = self._tx._check(write=True)
        if not allowed_work_types <= ALLOWED_WORK_TYPES or not 0 <= max_recoveries <= 4:
            raise ContractError("INPUT_INVALID", "auto_recovery")
        if not math.isfinite(cooldown_seconds) or not 60 <= cooldown_seconds <= 86400 or not 1 <= limit <= 200:
            raise ContractError("INPUT_INVALID", "auto_recovery_budget")
        cutoff = _after(now, -cooldown_seconds)
        kinds = sorted(allowed_work_types & frozenset(_RETRY_SUBJECT_REASON))
        if not kinds or max_recoveries == 0:
            return 0
        visible, params = self._visible_filter()
        error_filter = " OR ".join("(last_error_code=? OR last_error_code LIKE ?)" for _ in AUTO_RECOVERABLE_ERRORS)
        error_params = tuple(value for code in sorted(AUTO_RECOVERABLE_ERRORS) for value in (code, f"%|{code}"))
        # Rows that already spent their automatic budget are excluded in SQL so
        # they cannot fill the page; ``_auto_count`` below remains the authority.
        exhausted_filter = " AND ".join("last_error_code NOT LIKE ?" for _ in range(max_recoveries, 5))
        exhausted_params = tuple(f"%auto_retry:{count}|%" for count in range(max_recoveries, 5))
        rows = conn.execute(
            f"""SELECT * FROM work_items WHERE state='failed' AND available_at<=?
                AND {visible} AND ({error_filter}) AND {exhausted_filter}
                AND work_type IN ({_marks(kinds)})
                ORDER BY {_PURGE_FIRST},available_at,work_id LIMIT ?""",
            (cutoff, *params, *error_params, *exhausted_params, *kinds, limit),
        ).fetchall()
        recovered = 0
        for row in rows:
            error = str(row["last_error_code"] or "")
            if _auto_count(error) >= max_recoveries or _failure_kind(error) not in AUTO_RECOVERABLE_ERRORS:
                continue
            try:
                revoked = self._retry_subject_reason(row) is not None
            except ContractError:
                revoked = True
            if revoked:
                conn.execute("UPDATE work_items SET state='obsolete',last_error_code='authority_revoked' WHERE work_id=? AND state='failed'", (row["work_id"],))
                continue
            recovered += conn.execute(
                """UPDATE work_items SET state='pending',available_at=?,last_error_code=?,
                   lease_owner=NULL,lease_until=NULL WHERE work_id=? AND state='failed'""",
                (now, _auto_retry_stamp(error), row["work_id"]),
            ).rowcount
        return recovered

    def recover_oversized_consolidations(self, *, now: str, formatter, limit: int = 8) -> int:
        """One repair of old input-budget failures, never a general model retry.

        The old worker retained INPUT_INVALID but lost its field. Therefore
        re-open the live original source and reproduce the exact formatter
        failure before changing its state. Retain the old attempt count and a
        durable repair marker so an invalid model result cannot loop here.
        """
        conn = self._tx._check(write=True)
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ContractError("INPUT_INVALID", "repair_limit")
        visible, params = self._visible_filter()
        rows = conn.execute(
            f"""SELECT * FROM work_items
                WHERE work_type='consolidate' AND state='failed'
                  AND last_error_code='INPUT_INVALID' AND consolidation_offset=0 AND {visible}
                ORDER BY work_id LIMIT ?""",
            (*params, limit),
        ).fetchall()
        recovered = 0
        for row in rows:
            oversized = False
            try:
                self._tx.claims.require_live_source(row["subject_ref"], row["subject_revision"])
                source = self._tx.source(row["subject_ref"], row["subject_revision"])
                if source is not None and not source.capture_gaps and source.event["capture_state"] == "complete":
                    episode = self._tx.episodes.source_episode(source.ref, source.revision)
                    formatter((source,), episode_ref=episode.ref if episode else None)
            except ContractError as exc:
                if exc.code in {"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED"}:
                    raise
                oversized = exc.code == "INPUT_INVALID" and exc.field == "consolidation_input_budget"
            if not oversized:
                # Keep the failure and its attempt count, but do not let a
                # non-budget failure repeatedly occupy this bounded repair
                # page. No original content or claim is changed.
                conn.execute("""UPDATE work_items SET last_error_code='chunk_checked:1106|INPUT_INVALID'
                    WHERE work_id=? AND state='failed' AND last_error_code='INPUT_INVALID'
                    AND consolidation_offset=0""", (row["work_id"],))
                continue
            recovered += conn.execute(
                """UPDATE work_items SET state='pending',available_at=?,lease_owner=NULL,lease_until=NULL,
                   last_error_code='chunk_upgrade:1106|INPUT_INVALID'
                   WHERE work_id=? AND state='failed' AND last_error_code='INPUT_INVALID'
                   AND consolidation_offset=0""", (now, row["work_id"]),
            ).rowcount
        return recovered

    def reopen_oversized_candidate_evaluation(self, work_id: int, *, now: str, fits: bool) -> int:
        """Re-queue (or durably mark) one work item whose evaluation was oversized.

        ``recover_oversized_consolidations`` cannot reach these rows: it filters
        on ``work_type='consolidate'`` and reproduces the failure by re-formatting
        a single source, while a candidate evaluation formats a candidate against
        its whole evidence set. The candidate worker also lowercases its error
        codes, so that sibling's exact-case ``INPUT_INVALID`` filter would miss
        them even without the work-type gate -- matching here is case-insensitive.

        The caller owns deciding ``fits`` and moving the candidate tables with it;
        this only touches ``work_items``. Either way the row gets a durable
        marker, so a still-oversized evaluation cannot loop through the bounded
        repair page.
        """
        conn = self._tx._check(write=True)
        marker = "budget_upgrade" if fits else "budget_checked"
        return conn.execute(
            """UPDATE work_items SET state=?,available_at=COALESCE(?,available_at),
               lease_owner=NULL,lease_until=NULL,last_error_code=?
               WHERE work_id=? AND state='failed' AND UPPER(last_error_code)='INPUT_INVALID'""",
            ("pending" if fits else "failed", now if fits else None,
             f"{marker}:{SCHEMA_VERSION}|input_invalid", work_id),
        ).rowcount

    def recover_invalid_derivations(self, *, now: str, allowed_work_types: frozenset[str], limit: int = 32) -> int:
        """Grant legacy invalid results the same single extra attempt as new work.

        Reuse the operator retry's three-table transition for candidates. The
        next worker revalidates source/epoch authority before any model call.
        The durable marker is independent of schema generation and attempts.
        """
        conn = self._tx._check(write=True)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError("INPUT_INVALID", "retry_limit")
        kinds = sorted(allowed_work_types & {"consolidate", "evaluate_candidate"})
        if not kinds:
            return 0
        visible, params = self._visible_filter()
        rows = conn.execute(
            f"""SELECT work_id,work_type,last_error_code FROM work_items
                WHERE state='failed' AND {visible} AND work_type IN ({_marks(kinds)})
                AND (lower(last_error_code)='derivation_invalid'
                     OR lower(last_error_code) LIKE '%|derivation_invalid')
                AND last_error_code NOT LIKE ? ORDER BY work_id LIMIT ?""",
            (*params, *kinds, f"%{DERIVATION_RETRY_MARKER}|%", limit),
        ).fetchall()
        return sum(self._reopen_failed(row["work_id"], row["work_type"], row["last_error_code"],
                                       now=now, automatic=True) for row in rows)

    def retry_failed(self, *, now: str, include_terminal: bool = False,
                     limit: int = 64, dry_run: bool = True) -> dict:
        """Grant one bounded re-look to failures a shipped fix may have cured.

        Distinct from ``recover_transient_failures``, which is automatic and
        governed by a per-item budget that these rows have already spent, and
        from the two oversized-evaluation repairs, which reproduce a specific
        failure before deciding.  This one reproduces nothing: it records that
        an operator granted the attempt and lets the next run decide.

        A candidate evaluation needs three tables to move together -- evaluation,
        lifecycle and work item -- or ``begin_model_attempt`` refuses the next
        attempt as ``candidate_attempt_interrupted``.  Other work types need
        only the work item.

        Idempotent: every row is stamped with the schema generation that granted
        it, and a row already carrying this generation's stamp is skipped.
        """
        from .failure_retry import selects, validate_page  # imports this module

        validate_page(limit)
        if type(include_terminal) is not bool or type(dry_run) is not bool:
            raise ContractError("INPUT_INVALID", "retry_flags")
        # A preview must be able to run inside a read transaction, which is what
        # makes "look before you touch production" the cheap option.
        conn = self._tx._check(write=not dry_run)
        visible, params = self._visible_filter()
        rows = conn.execute(
            f"""SELECT work_id, work_type, last_error_code FROM work_items
                WHERE state='failed' AND {visible} ORDER BY work_id LIMIT ?""",
            (*params, max(limit * 8, limit)),
        ).fetchall()

        report = {"examined": 0, "retried": 0, "by_kind": {}, "applied": not dry_run}
        for row in rows:
            if report["retried"] >= limit:
                break
            report["examined"] += 1
            code = row["last_error_code"]
            if not selects(code, include_terminal=include_terminal, generation=SCHEMA_VERSION):
                continue
            if not dry_run and not self._reopen_failed(row["work_id"], row["work_type"], code, now=now):
                continue
            kind = str(code or "").rsplit("|", 1)[-1]
            report["by_kind"][kind] = report["by_kind"].get(kind, 0) + 1
            report["retried"] += 1
        return report

    def _reopen_failed(self, work_id: int, work_type: str, code: object, *, now: str, automatic: bool = False) -> bool:
        """Move one failed row, and its sibling tables when it has any."""
        from .failure_retry import marked  # imports this module

        conn = self._tx._check(write=True)
        reason = "derivation_retry" if automatic else "operator_retry"
        if work_type == "evaluate_candidate" and not self._tx.candidates.reopen_evaluation(work_id, now=now, reason=reason):
            return False
        return conn.execute(
            """UPDATE work_items SET state='pending',available_at=?,lease_owner=NULL,
               lease_until=NULL,last_error_code=? WHERE work_id=? AND state='failed'""",
            (now, f"{DERIVATION_RETRY_MARKER}|{code}" if automatic else marked(code, generation=SCHEMA_VERSION), work_id),
        ).rowcount == 1

    def defer_without_attempt(self, work_id: int, lease_token: int, owner: str, *,
                              now: str, error_code: str, seconds: float = 3600) -> WorkMutation:
        """A pre-network capability/budget refusal is not a model attempt."""
        conn = self._tx._check(write=True)
        if not self._verify_lease(work_id, lease_token, owner, now=now):
            return WorkMutation(work_id, "stale", self.read_state(work_id) or "stale", lease_token)
        prior = conn.execute("SELECT last_error_code FROM work_items WHERE work_id=?", (work_id,)).fetchone()[0]
        if _preserve_retry_history(prior):
            error_code = f"{prior}|{error_code}"[:1024]
        conn.execute(f"""UPDATE work_items SET state='pending',attempt=MAX(0,attempt-1),
                       lease_owner=NULL,lease_until=NULL,last_error_code=?,available_at=?
                       WHERE {_LEASED_ROW}""",
                     (error_code, _after(now, seconds), work_id, lease_token, owner))
        return WorkMutation(work_id, "deferred", "pending", lease_token)

    def read_state(self, work_id: int) -> str | None:
        row = self._tx._check().execute("SELECT state FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        return None if row is None else row["state"]

    def operator_retry_failed(
        self,
        work_ids,
        *,
        now: str,
        operation_id: str,
        expected_memory_epoch: int | None = None,
        max_items: int = 8,
    ) -> tuple[OperatorRetryMutation, ...]:
        """Requeue explicitly named failed work under the current fence.

        This is the sole storage mutation for the maintenance retry entry
        point.  It never resets the automatic attempt counter (the normal
        three-attempt ceiling therefore remains intact), and it does not
        lease or execute work.  A subsequent bounded worker drain performs the
        actual operation.  Calling the same operation again while its item is
        pending is idempotent.
        """
        conn = self._tx._check(write=True)
        self._check_operator_request(work_ids, operation_id, expected_memory_epoch, max_items)
        # Validate the timestamp through the strict UTC parser before any row
        # is changed.  The worker's clock remains the authority.
        _parse_time(now)
        current_epoch = self._tx.status().memory_epoch
        results = []
        for work_id in work_ids:
            row = conn.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            verdict = self._operator_retry_rejection(row, operation_id, current_epoch)
            if verdict is None:
                verdict = self._operator_requeue(conn, row, operation_id, now)
            results.append(OperatorRetryMutation(work_id, *verdict, operation_id))
        # Re-read the epoch in the same write transaction.  A concurrent
        # deletion/update cannot commit between the eligibility check and the
        # requeue commit; a later worker fence still handles a post-commit
        # epoch change conservatively.
        if expected_memory_epoch is not None and self._tx.status().memory_epoch != expected_memory_epoch:
            raise ContractError("VERSION_CONFLICT", "memory_epoch")
        return tuple(results)

    def _check_operator_request(self, work_ids, operation_id, expected_memory_epoch, max_items) -> None:
        if self._tx.context.actor_origin not in OPERATOR_ORIGINS:
            raise ContractError("ACCESS_DENIED", "operator_origin")
        if not isinstance(work_ids, (tuple, list)) or not work_ids:
            raise ContractError("INPUT_INVALID", "retry_work_ids")
        if type(max_items) is not int or not 1 <= max_items <= 8 or len(work_ids) > max_items:
            raise ContractError("INPUT_INVALID", "retry_budget")
        if any(type(value) is not int or value < 1 for value in work_ids) or len(set(work_ids)) != len(work_ids):
            raise ContractError("INPUT_INVALID", "retry_work_ids")
        if type(operation_id) is not str or not _OPERATION_ID.fullmatch(operation_id):
            raise ContractError("INPUT_INVALID", "retry_operation")
        if expected_memory_epoch is not None:
            if type(expected_memory_epoch) is not int or expected_memory_epoch < 0:
                raise ContractError("INPUT_INVALID", "memory_epoch")
            if self._tx.status().memory_epoch != expected_memory_epoch:
                raise ContractError("VERSION_CONFLICT", "memory_epoch")

    def _operator_retry_rejection(self, row, operation_id: str, current_epoch: int) -> tuple[str, str, str] | None:
        """``(disposition, state, reason)`` refusing the retry, or None to requeue."""
        if row is None:
            return "rejected", "missing", "work_missing"
        denied = self._context_denial(row)
        if denied is not None:
            return "rejected", "unavailable", denied
        state = row["state"]
        if row["work_type"] == "evaluate_candidate":
            return "rejected", state, "new_evidence_required"
        if state == "pending":
            return "already_pending", state, "idempotent"
        if state == "leased":
            return "rejected", state, "leased"
        if state != "failed":
            return "rejected", state, "state_not_failed"
        prior_operations = _operator_retry_ids(row["last_error_code"] or "")
        if operation_id in prior_operations:
            return "already_retried", state, "idempotent"
        if len(prior_operations) >= MAX_OPERATOR_RETRIES:
            return "rejected", state, "operator_retry_budget"
        reason = self._retry_subject_reason(row, current_epoch=current_epoch)
        if reason is not None:
            return "rejected", state, reason
        return None

    @staticmethod
    def _operator_requeue(conn, row, operation_id: str, now: str) -> tuple[str, str, str]:
        # Keep attempt as-is: explicit maintenance retry grants exactly one
        # new worker execution and cannot silently restart the automatic
        # three-attempt budget.  The operation marker is durable in the
        # existing bounded error field for audit/idempotence.
        marker = f"operator_retry:{operation_id}"
        prior_error = row["last_error_code"] or ""
        stamped = marker if not prior_error else f"{marker}|prior:{prior_error}"[:1024]
        conn.execute(
            """UPDATE work_items SET state='pending',available_at=?,lease_owner=NULL,
               lease_until=NULL,last_error_code=?
               WHERE work_id=? AND state='failed'""",
            (now, stamped, row["work_id"]),
        )
        return "requeued", "pending", "operator_retry_requested"

    def _verify_lease(self, work_id: int, lease_token: int, owner: str, *, now: str) -> bool:
        # Verification is a read operation.  Mutations call it from a write
        # transaction, while staged vector publication uses the same fence
        # from a read-only guard immediately before external commit.
        row = self._tx._check().execute(
            """SELECT state,lease_token,lease_owner,lease_until,scope_id,project_id,branch_id
               FROM work_items WHERE work_id=?""",
            (work_id,),
        ).fetchone()
        if row is None or row["state"] != "leased" or row["lease_token"] != lease_token or row["lease_owner"] != owner:
            return False
        if self._context_denial(row) is not None:
            return False
        return row["lease_until"] is None or _parse_time(row["lease_until"]) > _parse_time(now)

    def _stale_mutation(self, conn, work_id: int) -> WorkMutation:
        row = conn.execute("SELECT state,lease_token FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise ContractError("SOURCE_MISSING", "work_item")
        return WorkMutation(work_id, "stale", row["state"], row["lease_token"])

    def complete(self, work_id: int, lease_token: int, owner: str, *, now: str) -> WorkMutation:
        conn = self._tx._check(write=True)
        if not self._verify_lease(work_id, lease_token, owner, now=now):
            return self._stale_mutation(conn, work_id)
        conn.execute(
            f"UPDATE work_items SET state='done',lease_owner=NULL,lease_until=NULL,last_error_code=NULL WHERE {_LEASED_ROW}",
            (work_id, lease_token, owner),
        )
        return WorkMutation(work_id, "completed", "done", lease_token)

    def complete_consolidation(
        self, work_id: int, lease_token: int, owner: str, *, now: str,
        covered_source_refs: frozenset[str], pending_sources: tuple[tuple[str, int, int], ...] = (),
    ) -> WorkMutation:
        conn = self._tx._check(write=True)
        if not self._verify_lease(work_id, lease_token, owner, now=now):
            return self.complete(work_id, lease_token, owner, now=now)
        current = conn.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if current["work_type"] != "consolidate" or f"{current['subject_ref']}@{current['subject_revision']}" not in covered_source_refs:
            raise ContractError("DERIVATION_INVALID", "consolidation_subject_uncovered")
        result = self.complete(work_id, lease_token, owner, now=now)
        for ref, revision, token in pending_sources:
            if f"{ref}@{revision}" not in covered_source_refs:
                continue
            conn.execute(
                """UPDATE work_items SET state='done',lease_owner=NULL,lease_until=NULL,last_error_code=NULL
                   WHERE work_type='consolidate' AND subject_ref=? AND subject_revision=?
                   AND state='pending' AND lease_token=? AND scope_id=?
                   AND project_id IS ? AND branch_id IS ?""",
                (ref, revision, token, current["scope_id"], current["project_id"], current["branch_id"]),
            )
        return result

    def consolidation_offset(self, work_id: int, lease_token: int, owner: str, *, now: str) -> int:
        if not self._verify_lease(work_id, lease_token, owner, now=now):
            raise ContractError("ACCESS_DENIED", "lease_stale")
        row = self._tx._check().execute(
            "SELECT work_type,consolidation_offset FROM work_items WHERE work_id=?", (work_id,),
        ).fetchone()
        if row["work_type"] != "consolidate":
            raise ContractError("INPUT_INVALID", "work_type")
        return row["consolidation_offset"]

    def advance_consolidation(self, work_id: int, lease_token: int, owner: str, *,
                              now: str, expected_offset: int, next_offset: int,
                              final: bool) -> WorkMutation:
        """Commit accepted claims and their exact window checkpoint together.

        This method is called in the same transaction as claim application.
        Successful pages do not spend the finite failed-attempt allowance.
        """
        conn = self._tx._check(write=True)
        current = self.consolidation_offset(work_id, lease_token, owner, now=now)
        if type(next_offset) is not int or next_offset <= expected_offset or current != expected_offset:
            raise ContractError("DERIVATION_INVALID", "consolidation_offset")
        conn.execute("UPDATE work_items SET consolidation_offset=? WHERE work_id=?", (next_offset, work_id))
        if final:
            return self.complete(work_id, lease_token, owner, now=now)
        return self.defer_without_attempt(work_id, lease_token, owner, now=now,
                                          error_code="consolidation_chunk_pending", seconds=0)

    def fail(self, work_id: int, lease_token: int, owner: str, *, error_code: str, now: str, recoverable: bool) -> WorkMutation:
        conn = self._tx._check(write=True)
        if not self._verify_lease(work_id, lease_token, owner, now=now):
            return self._stale_mutation(conn, work_id)
        row = conn.execute("SELECT attempt,last_error_code,work_type FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        attempt, prior = row["attempt"], row["last_error_code"]
        # Invalid output gets exactly one extra execution, even when earlier
        # infrastructure failures have consumed the ordinary attempt counter.
        invalid = (_failure_kind(error_code).lower() == "derivation_invalid"
                   and row["work_type"] in {"consolidate", "evaluate_candidate"})
        if invalid:
            recoverable = f"{DERIVATION_RETRY_MARKER}|" not in str(prior or "")
            if recoverable:
                prior = f"{DERIVATION_RETRY_MARKER}|{prior or ''}".rstrip("|")
        if recoverable and _failure_kind(error_code) in CAPACITY_REFUSALS:
            # The provider refused everyone; this lease never got an attempt, so
            # the attempt is refunded and the item is never given up on.  What
            # *is* bounded is how often it asks again: refunding alone left the
            # ordinary 60s ceiling in place and 197 items retried without pause
            # for an entire provider outage, which is the silent loop this
            # whole design exists to prevent.
            refusals = _capacity_count(prior) + 1
            retry_error = f"capacity:{refusals}|{error_code}"[:1024]
            if "derivation_retry:1|" in str(prior or ""):
                retry_error = "derivation_retry:1|" + retry_error
            conn.execute(
                f"""UPDATE work_items SET state='pending',lease_owner=NULL,lease_until=NULL,
                   last_error_code=?,available_at=?,attempt=MAX(attempt-1,0) WHERE {_LEASED_ROW}""",
                (retry_error, _after(now, _capacity_backoff_seconds(refusals)), work_id, lease_token, owner),
            )
            return WorkMutation(work_id, "retry", "pending", lease_token)
        history = f"{prior}|{error_code}"[:1024] if _preserve_retry_history(prior) else error_code
        if recoverable and (invalid or attempt < MAX_RECOVERABLE_ATTEMPTS):
            conn.execute(
                f"""UPDATE work_items SET state='pending',lease_owner=NULL,lease_until=NULL,last_error_code=?,available_at=?
                   WHERE {_LEASED_ROW}""",
                (history, _after(now, _backoff_seconds(attempt)), work_id, lease_token, owner),
            )
            return WorkMutation(work_id, "retry", "pending", lease_token)
        conn.execute(
            f"""UPDATE work_items SET state='failed',lease_owner=NULL,lease_until=NULL,last_error_code=?,available_at=?
               WHERE {_LEASED_ROW}""",
            (history, now, work_id, lease_token, owner),
        )
        return WorkMutation(work_id, "failed", "failed", lease_token)

    def mark_obsolete(self, work_id: int, lease_token: int, owner: str, *, now: str) -> WorkMutation:
        conn = self._tx._check(write=True)
        if not self._verify_lease(work_id, lease_token, owner, now=now):
            return self._stale_mutation(conn, work_id)
        conn.execute(
            f"""UPDATE work_items SET state='obsolete',lease_owner=NULL,lease_until=NULL,last_error_code='authority_revoked'
               WHERE {_LEASED_ROW}""",
            (work_id, lease_token, owner),
        )
        return WorkMutation(work_id, "obsolete", "obsolete", lease_token)
