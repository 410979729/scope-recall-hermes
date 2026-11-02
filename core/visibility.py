"""One authority check for all content exits and cached-result release."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..contracts import ContractError
from .claims import select_effective
from .delete_storage import retraction_after
from .injection_guard import blocked_refs

OBJECT_KINDS = ("event", "claim", "episode", "artifact", "reference")
#: Intention states that automatic exits never surface as current.
CLOSED_INTENTION_STATES = frozenset({"completed", "cancelled", "expired"})


@dataclass(frozen=True)
class ObjectRef:
    kind: str
    ref: str
    revision: int

    def __post_init__(self):
        if (
            self.kind not in OBJECT_KINDS
            or type(self.ref) is not str
            or not 1 <= len(self.ref) <= 240
            or type(self.revision) is not int
            or self.revision < 1
        ):
            raise ContractError("INPUT_INVALID", "object_ref")


def _admits(tx, block, *, automatic: bool) -> bool:
    """What an object's block, or its having none, lets the reader of ``tx`` see."""
    return block is None or (
        block["scope_id"] in tx.context.allowed_scope_ids
        and not block["read_blocked"]
        and not (automatic and block["suppressed"])
    )


def allowed(tx, kind: str, ref: str, *, automatic: bool = False) -> bool:
    # Once per read transaction (``Transaction.remembered``).
    remembered = getattr(tx, "remembered", None)
    if remembered is None:
        return _allowed_now(tx, kind, ref, automatic)
    return remembered(("allowed", kind, ref, automatic), lambda: _allowed_now(tx, kind, ref, automatic))


def _allowed_now(tx, kind: str, ref: str, automatic: bool) -> bool:
    return deletion_allows(tx, kind, ref, automatic=automatic) and ref not in blocked_refs(tx, kind, (ref,))


def deletion_allows(tx, kind: str, ref: str, *, automatic: bool = False) -> bool:
    """Check deletion identity fences for capture preflight, without releasing held text."""
    conn = tx._check()
    if conn.execute(
        "SELECT 1 FROM restored_absence_blocks WHERE object_kind=? AND object_ref=?", (kind, ref)
    ).fetchone():
        return False
    row = conn.execute(
        "SELECT read_blocked,suppressed,scope_id FROM object_blocks WHERE object_kind=? AND object_ref=?", (kind, ref)
    ).fetchone()
    return _admits(tx, row, automatic=automatic)


def allowed_refs(tx, kind: str, refs, *, automatic: bool = False) -> frozenset[str]:
    """Resolve one kind of identity in batch queries.

    Checked one by one, an episode's members would repeat queries inside every capture that joins the
    episode, and in a busy Hermes gateway each statement waits for the GIL (``lexical_index.index_terms``).
    """
    wanted = list(dict.fromkeys(refs))
    if not wanted:
        return frozenset()
    row = (
        tx._check()
        .execute(
            """SELECT json_group_array(json_object('ref',j.value,
               'absent',EXISTS(SELECT 1 FROM restored_absence_blocks a WHERE a.object_kind=? AND a.object_ref=j.value),
               'blocked',b.object_ref IS NOT NULL,'scope_id',b.scope_id,'read_blocked',b.read_blocked,
               'suppressed',b.suppressed))
           FROM json_each(?) j LEFT JOIN object_blocks b ON b.object_kind=? AND b.object_ref=j.value""",
            (kind, json.dumps(wanted, ensure_ascii=False), kind),
        )
        .fetchone()
    )
    unsafe = blocked_refs(tx, kind, wanted)
    admitted = frozenset(
        item["ref"]
        for item in json.loads(row[0])
        if not item["absent"]
        and item["ref"] not in unsafe
        and _admits(tx, item if item["blocked"] else None, automatic=automatic)
    )
    remember = getattr(tx, "remember", None)
    if remember is not None:
        for ref in wanted:
            remember(("allowed", kind, ref, automatic), ref in admitted)
    return admitted


def _released_event(tx, clock, ref: ObjectRef, *, automatic: bool, history: bool):
    item = tx.source(ref.ref, ref.revision)
    if not history:
        tx.claims.require_live_source(ref.ref, ref.revision)
    return item


def _released_claim(tx, clock, ref: ObjectRef, *, automatic: bool, history: bool):
    from .summary_validity import is_conversation_summary, summary_live

    versions = tx.claims.versions(ref.ref)
    if history:
        item = next((version for version in versions if version.revision == ref.revision), None)
    else:
        item = select_effective(versions, clock.utc_now())
    if item is not None and item.revision != ref.revision:
        raise ContractError("VERSION_CONFLICT")
    if item is not None and is_conversation_summary(item) and not summary_live(tx, item.ref, item.revision):
        raise ContractError("SOURCE_MISSING", "summary_dependencies")
    if automatic and item is not None and item.payload.get("intention", {}).get("state") in CLOSED_INTENTION_STATES:
        raise ContractError("SOURCE_MISSING")
    return item


def _released_versioned(tx, clock, ref: ObjectRef, *, automatic: bool, history: bool):
    repository = {"episode": tx.episodes, "artifact": tx.artifacts, "reference": tx.references}[ref.kind]
    item = repository.get(ref.ref, ref.revision if history else None)
    if item is not None and item.revision != ref.revision:
        raise ContractError("VERSION_CONFLICT")
    if automatic and ref.kind == "episode" and item is not None and "resume_requires_rebuild" in item.gaps:
        raise ContractError("VERSION_CONFLICT", "episode_sources")
    return item


_RELEASE = {"event": _released_event, "claim": _released_claim}


def epoch_retracted(tx, context, epoch: int) -> bool:
    """Whether what a reader read at ``epoch`` may since have been withdrawn in this context's scopes.

    The epoch moves with every capture: with several entries writing to one store, a read and its
    release are rarely at the same epoch, and refusing on any move would refuse almost every
    release.  Only a deletion or suppression in the reader's scopes withdraws; any other change is
    caught by each object's own fresh load, which follows in the same transaction.
    """
    return tx.status().memory_epoch != epoch and retraction_after(tx._check(), context.allowed_scope_ids, epoch)


def release_objects(
    storage,
    clock,
    context,
    refs: tuple[ObjectRef, ...],
    *,
    expected_epoch: int,
    automatic: bool = True,
    history: bool = False,
) -> tuple:
    """Return freshly loaded SQLite objects; never echo cached/vector text.

    The successful read transaction is the last authority-release boundary.
    Already delivered host text is outside a local transaction's control.
    """
    if (
        type(refs) is not tuple
        or len(refs) > 200
        or any(not isinstance(ref, ObjectRef) for ref in refs)
        or type(expected_epoch) is not int
        or expected_epoch < 0
    ):
        raise ContractError("INPUT_INVALID", "release_request")
    with storage.read(context) as tx:
        if epoch_retracted(tx, context, expected_epoch):
            raise ContractError("VERSION_CONFLICT", "memory_epoch")
        result = []
        for ref in refs:
            if not allowed(tx, ref.kind, ref.ref, automatic=automatic):
                raise ContractError("SOURCE_MISSING")
            load = _RELEASE.get(ref.kind, _released_versioned)
            item = load(tx, clock, ref, automatic=automatic, history=history)
            if item is None or (automatic and item.suppressed):
                raise ContractError("SOURCE_MISSING")
            result.append(item)
        return tuple(result)
