"""Lease-fenced work outcomes, derivation fences and sanitized failure persistence.

Owned by the worker drain; model calls stay outside SQLite transactions.
Every processor returns ``(disposition, error_code, state)`` and records its
verdict only through the helpers here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from ..contracts import ContractError
from .failure_retry import validation_feedback
from .work_storage import ACCOUNT_REFUSALS

_NON_RETRYABLE_MODEL_ERRORS = frozenset({
    "budget_exhausted",
    "meter_breach",
    "input_invalid",
    "budget_unavailable",
    # The request guard refused secret-like text; the payload will not change.
    "sensitive_request",
})

#: Refusals that say nothing about the item: raised before any network attempt,
#: or the provider declining the account (``ACCOUNT_REFUSALS``).  The item is
#: parked for an hour without spending an attempt, and its work type stands down
#: for the pass.  ``provider_hold`` is the adapter declining to ask a provider
#: that refused the calls just before (runtime/model_budget.py).
BUDGET_PAUSE_ERRORS = frozenset({
    "budget_exhausted",
    "budget_unavailable",
    "credential_missing",
    "credential_shape_invalid",
    "provider_hold",
}) | ACCOUNT_REFUSALS

#: Port rejections that describe this payload rather than the provider;
#: sending the same input again would fail the same way.
_INVALID_INPUT_CODES = frozenset({"DERIVATION_INVALID", "INPUT_INVALID"})


class _Outcome(tuple):
    """A (disposition, error_code, state) result that can carry the contract field.

    Acceptance raises ContractError with both a code and a field, but only the
    code reaches ``work_items.last_error_code`` -- and that column is pinned to
    an exact value by contract tests, so the field cannot be appended to it.
    Subclassing tuple lets the field ride along to the receipt while every
    existing three-way unpack keeps working untouched.
    """

    def __new__(cls, disposition, error_code, state, detail=None):
        value = super().__new__(cls, (disposition, error_code, state))
        value.detail = detail
        return value


def _remaining(started: float, clock, budget: float) -> float:
    return max(0.0, budget - (clock.monotonic() - started))


def _work_result(mutation, *, error_code: str | None = None) -> tuple[str, str | None, str]:
    return mutation.disposition, error_code, mutation.state


def _stale(tx, item, error_code: str | None = None) -> tuple[str, str | None, str]:
    """The lease is gone; report the row's current state without touching it."""
    return "stale", error_code, tx.work.read_state(item.work_id) or "stale"


def _deadline_result(storage, context, item) -> tuple[str, str, str]:
    with storage.read(context) as tx:
        return "skipped", "DEADLINE_EXCEEDED", tx.work.read_state(item.work_id) or "stale"


def _mark_obsolete(tx, item, now: str) -> tuple[str, str | None, str]:
    return _work_result(tx.work.mark_obsolete(*item.lease, now=now), error_code="authority_revoked")


def _epoch_changed(tx, item, now: str) -> tuple[str, str | None, str]:
    """A dependency of this item's derivation changed under it; retry it.

    The code keeps its historical name: it is pinned by contract tests, and
    operators and recovery (``AUTO_RECOVERABLE_ERRORS``) already know it.
    """
    return _work_result(
        tx.work.fail(*item.lease, error_code="memory_epoch_changed", now=now, recoverable=True),
        error_code="memory_epoch_changed",
    )


# --- derivation dependency fence ---------------------------------------------
#
# ``memory_epoch`` moves on every source insert and every claim, resume,
# reference, artifact and deletion write anywhere in the instance.  It stays
# the read-side release fence.  Worker derivations used to compare it across
# their paid model or vector call, so every chat message or tool call captured
# meanwhile discarded results that never depended on it.  A derivation now
# records what it depends on before the call and re-reads exactly that in the
# transaction that would record its result.  Clocks play no part: deletion
# operations are dated by the epoch they create, and claim versions by the
# rowid order of an append-only table, both assigned under SQLite's single
# writer lock and therefore comparable across processes.


@dataclass(frozen=True)
class DerivationFence:
    """What one worker derivation read before its model or vector call.

    ``sources`` and ``claims`` hold each subject row's identity fingerprint and
    authority state: its read block and suppression flags and the object and
    source-group blocks deletion writes beside it.  ``episode`` is the resume
    a resume proposal would replace and ``references`` the bindings a
    reference proposal would revise.  ``memory_epoch`` dates the read, because
    a deletion operation stores the epoch it created.  ``source_mark`` and
    ``claim_mark`` are the last rows of the append-only ``source_events`` and
    ``claim_versions`` tables at the read, as ``(rowid, identity...)``.
    """

    scope_id: str
    memory_epoch: int
    source_mark: tuple
    claim_mark: tuple
    sources: tuple[tuple[str, int, tuple | None], ...] = ()
    claims: tuple[tuple[str, int, tuple | None], ...] = ()
    episode_ref: str | None = None
    episode: tuple | None = None
    references: tuple[tuple, ...] = ()

    def only_sources(self, sources) -> DerivationFence:
        """The same fence restricted to the sources the derivation finally used."""
        keep = {(source.ref, source.revision) for source in sources}
        return replace(self, sources=tuple(entry for entry in self.sources if entry[:2] in keep))


def _digest(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_state(tx, ref: str, revision: int) -> tuple | None:
    from .delete_storage import group_digest

    conn = tx._check()
    row = conn.execute(
        """SELECT e.event_sha256,e.read_blocked,e.suppressed,e.scope_id,e.project_id,e.branch_id,
                  e.source_group_key,b.read_blocked,b.suppressed,b.operation_id
           FROM source_events e LEFT JOIN object_blocks b ON b.object_kind='event' AND b.object_ref=e.event_id
           WHERE e.event_id=? AND e.source_revision=?""",
        (ref, revision),
    ).fetchone()
    if row is None:
        return None
    group = conn.execute(
        "SELECT read_blocked,suppressed,operation_id FROM source_group_blocks WHERE group_sha256=?",
        (group_digest(tx.context.binding, row[3], row[4], row[5], row[6]),),
    ).fetchone()
    return (*tuple(row), None if group is None else tuple(group))


def _claim_state(tx, ref: str, revision: int) -> tuple | None:
    row = tx._check().execute(
        """SELECT c.read_blocked,c.suppressed,v.state,v.payload_json,b.read_blocked,b.suppressed,b.operation_id,
                  EXISTS(SELECT 1 FROM restored_absence_blocks a WHERE a.object_kind='claim' AND a.object_ref=c.claim_id)
           FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=?
           LEFT JOIN object_blocks b ON b.object_kind='claim' AND b.object_ref=c.claim_id
           WHERE c.claim_id=?""",
        (revision, ref),
    ).fetchone()
    return None if row is None else (*tuple(row[:3]), _digest(row[3]), *tuple(row[4:]))


def _episode_state(tx, ref: str) -> tuple | None:
    """The resume a proposal would replace.

    Attaching a newly captured source adds an episode revision but copies this
    resume, watermark and processed sequence forward unchanged, so a capture
    in the same session is not a change; applying a resume or revising a
    reference binding that the resume depends on is.
    """
    row = tx._check().execute(
        """SELECT e.read_blocked,v.resume_json,v.source_watermark,v.processed_sequence,b.read_blocked,b.suppressed
           FROM episodes e JOIN episode_versions v ON v.episode_id=e.episode_id AND v.revision=e.current_revision
           LEFT JOIN object_blocks b ON b.object_kind='episode' AND b.object_ref=e.episode_id
           WHERE e.episode_id=?""",
        (ref,),
    ).fetchone()
    return None if row is None else (row[0], _digest(row[1]), *tuple(row[2:]))


def _reference_state(tx, episode_ref: str) -> tuple[tuple, ...]:
    return tuple(tuple(row) for row in tx._check().execute(
        """SELECT r.reference_id,r.current_revision,r.read_blocked,b.read_blocked,b.suppressed
           FROM reference_bindings r
           LEFT JOIN object_blocks b ON b.object_kind='reference' AND b.object_ref=r.reference_id
           WHERE r.episode_id=? ORDER BY r.reference_id""",
        (episode_ref,),
    ))


def _last_row(tx, table: str, identity: str) -> tuple:
    row = tx._check().execute(f"SELECT rowid,{identity} FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
    return (0,) if row is None else tuple(row)


_SOURCE_IDENTITY = "event_id,source_revision,event_sha256"
_CLAIM_IDENTITY = "claim_id,revision,recorded_from"


def _mark_moved(tx, table: str, identity: str, mark: tuple) -> bool:
    """Whether the row the mark names is gone or is no longer the same row.

    Neither table deletes rows or rewrites these identity columns in normal
    operation, so this only fires when the database under the fence was
    replaced, as a restore from an older backup does.
    """
    if not mark[0]:
        return False
    row = tx._check().execute(f"SELECT {identity} FROM {table} WHERE rowid=?", (mark[0],)).fetchone()
    return row is None or tuple(row) != mark[1:]


def read_derivation_fence(tx, *, scope_id: str, sources=(), claims=(), episode_ref: str | None = None) -> DerivationFence:
    """Record, in the transaction the derivation's inputs were read from, what its result depends on."""
    source_keys = tuple(dict.fromkeys((source.ref, source.revision) for source in sources))
    return DerivationFence(
        scope_id,
        tx.memory_epoch(),
        _last_row(tx, "source_events", _SOURCE_IDENTITY),
        _last_row(tx, "claim_versions", _CLAIM_IDENTITY),
        tuple((ref, revision, _source_state(tx, ref, revision)) for ref, revision in source_keys),
        tuple((ref, revision, _claim_state(tx, ref, revision)) for ref, revision in dict.fromkeys(claims)),
        episode_ref,
        None if episode_ref is None else _episode_state(tx, episode_ref),
        () if episode_ref is None else _reference_state(tx, episode_ref),
    )


def derivation_changed(tx, fence: DerivationFence, *, episode: bool = False, references: bool = False) -> str | None:
    """Name the first dependency that changed since ``fence`` was read, or None.

    A deletion or suppression operation in the fence's scope, a replaced
    database (restore), a changed state of any recorded source or claim, and --
    when asked -- a changed episode resume or reference binding all count.
    Captures and writes to anything else do not.  Callers keep their lease,
    visibility and newest-revision checks ahead of this one.
    """
    if tx._check().execute(
        """SELECT 1 FROM deletion_operations o WHERE o.memory_epoch>?
           AND EXISTS(SELECT 1 FROM json_each(o.scope_ids_json) s WHERE s.value=?) LIMIT 1""",
        (fence.memory_epoch, fence.scope_id),
    ).fetchone() is not None:
        return "deletion_operation"
    if (_mark_moved(tx, "source_events", _SOURCE_IDENTITY, fence.source_mark)
            or _mark_moved(tx, "claim_versions", _CLAIM_IDENTITY, fence.claim_mark)):
        return "database_replaced"
    if any(_source_state(tx, ref, revision) != state for ref, revision, state in fence.sources):
        return "source"
    if any(_claim_state(tx, ref, revision) != state for ref, revision, state in fence.claims):
        return "claim"
    if episode and fence.episode_ref is not None and _episode_state(tx, fence.episode_ref) != fence.episode:
        return "episode"
    if references and fence.episode_ref is not None and _reference_state(tx, fence.episode_ref) != fence.references:
        return "reference"
    return None


def claim_versions_mark(tx) -> int:
    """The last ``claim_versions`` rowid this transaction sees."""
    return _last_row(tx, "claim_versions", _CLAIM_IDENTITY)[0]


def claims_changed(tx, fence: DerivationFence, *, until: int, claim_refs=None) -> bool:
    """Whether another writer recorded a claim version this result collides with.

    ``claim_versions`` is append-only, so its rowids order its inserts.  The
    accepting transaction holds the write lock and read ``until`` before it
    wrote any version, so rows in ``(claim_mark, until]`` were written by
    others after the pre-call read -- a correction, confirmation, revision or
    another derivation.  ``claim_refs`` are the claims the result resolved its
    slots to; ``None`` widens the check to the fence's audience for a result
    that failed before it could say.  A moved mark means the rowids no longer
    order anything and counts as a change.
    """
    mark = fence.claim_mark
    if _mark_moved(tx, "claim_versions", _CLAIM_IDENTITY, mark) or until < mark[0]:
        return True
    conn = tx._check()
    if claim_refs is None:
        return conn.execute(
            """SELECT 1 FROM claim_versions v JOIN claims c ON c.claim_id=v.claim_id
               WHERE v.rowid>? AND v.rowid<=? AND c.scope_id=? AND c.project_id IS ? AND c.branch_id IS ? LIMIT 1""",
            (mark[0], until, fence.scope_id, tx.context.project_id, tx.context.branch_id),
        ).fetchone() is not None
    refs = sorted(set(claim_refs))
    return any(
        conn.execute(
            f"SELECT 1 FROM claim_versions WHERE rowid>? AND rowid<=? AND claim_id IN ({','.join('?' for _ in part)}) LIMIT 1",
            (mark[0], until, *part),
        ).fetchone() is not None
        for part in (refs[index:index + 200] for index in range(0, len(refs), 200))
    )


def _model_exception_outcome(exc: BaseException) -> tuple[str, str] | None:
    """Keep auxiliary error types; do not relabel budget failures as missing model."""
    error_type = getattr(exc, "error_type", None)
    if type(error_type) is not str or not error_type or len(error_type) > 80:
        return None
    if any(character.isspace() for character in error_type):
        return None
    if error_type == "http_status":
        status = getattr(exc, "detail", "")
        if type(status) is str and status.isdigit() and len(status) == 3:
            code = int(status)
            return ("retry" if code == 429 or 500 <= code <= 599 else "failed", f"http_{code}")
    disposition = "failed" if error_type in _NON_RETRYABLE_MODEL_ERRORS else "retry"
    return disposition, error_type


def _model_failure(exc: BaseException) -> tuple[str, str]:
    """Disposition and code for an arbitrary exception out of a model call."""
    return _model_exception_outcome(exc) or ("retry", "model_unavailable")


def _port_failure(exc: BaseException) -> tuple[str, str]:
    """Disposition and code for an exception out of an embedding port."""
    if isinstance(exc, ContractError):
        disposition = "failed" if exc.code in _INVALID_INPUT_CODES else "retry"
        return disposition, exc.code or "model_unavailable"
    return _model_failure(exc)


def _record_error_detail(conn, item, *, now: str, stage: str, code: str, detail: str | None) -> None:
    # Store metadata only, never raw model output or exception bodies.
    field = validation_feedback(code, detail)["field"] if detail else None
    conn.execute(
        "INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at) VALUES (?,?,?,?,?,?)",
        (item.work_id, item.lease_token, stage, code, field, now),
    )
    conn.execute(
        "DELETE FROM work_error_details WHERE work_id=? AND detail_id NOT IN "
        "(SELECT detail_id FROM work_error_details WHERE work_id=? ORDER BY detail_id DESC LIMIT 16)",
        (item.work_id, item.work_id),
    )


def _finalize_work(storage, clock, context, item, disposition: str, error_code: str | None, *, started: float, budget: float,
                   error_detail: str | None = None, stage: str = "process",
                   validation_code: str | None = None) -> tuple[str, str | None, str]:
    """Record a retry, failed or obsolete verdict under the lease.

    A spent deadline or a lost lease reports the row instead of mutating it.
    """
    now = clock.utc_now()
    if _remaining(started, clock, budget) <= 0:
        return _deadline_result(storage, context, item)
    with storage.write(context, remaining_seconds=_remaining(started, clock, budget)) as tx:
        conn = tx._check(write=True)
        if not tx.work._verify_lease(*item.lease, now=now):
            return _stale(tx, item, error_code)
        if error_code:
            _record_error_detail(conn, item, now=now, stage=stage, code=validation_code or error_code, detail=error_detail)
        if error_code in BUDGET_PAUSE_ERRORS:
            return _work_result(
                tx.work.defer_without_attempt(*item.lease, now=now, error_code=error_code, seconds=3600),
                error_code=error_code,
            )
        if disposition == "obsolete":
            return _work_result(tx.work.mark_obsolete(*item.lease, now=now), error_code=error_code)
        recoverable = disposition != "failed"
        return _work_result(
            tx.work.fail(
                *item.lease,
                error_code=error_code or ("model_unavailable" if recoverable else "derivation_invalid"),
                now=now,
                recoverable=recoverable,
            ),
            error_code=error_code,
        )
