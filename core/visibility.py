"""One authority check for all content exits and cached-result release."""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts import ContractError
from .claims import select_effective

OBJECT_KINDS = ("event", "claim", "episode", "artifact", "reference")
#: Intention states that automatic exits never surface as current.
CLOSED_INTENTION_STATES = frozenset({"completed", "cancelled", "expired"})


@dataclass(frozen=True)
class ObjectRef:
    kind: str
    ref: str
    revision: int

    def __post_init__(self):
        if (self.kind not in OBJECT_KINDS or type(self.ref) is not str or not 1 <= len(self.ref) <= 240
                or type(self.revision) is not int or self.revision < 1):
            raise ContractError("INPUT_INVALID", "object_ref")


def allowed(tx, kind: str, ref: str, *, automatic: bool = False) -> bool:
    conn = tx._check()
    if conn.execute("SELECT 1 FROM restored_absence_blocks WHERE object_kind=? AND object_ref=?", (kind, ref)).fetchone():
        return False
    row = conn.execute("SELECT read_blocked,suppressed,scope_id FROM object_blocks WHERE object_kind=? AND object_ref=?",
                       (kind, ref)).fetchone()
    return row is None or (row["scope_id"] in tx.context.allowed_scope_ids and not row["read_blocked"]
                           and not (automatic and row["suppressed"]))


def _released_event(tx, clock, ref: ObjectRef, *, automatic: bool, history: bool):
    item = tx.source(ref.ref, ref.revision)
    if not history:
        tx.claims.require_live_source(ref.ref, ref.revision)
    return item


def _released_claim(tx, clock, ref: ObjectRef, *, automatic: bool, history: bool):
    versions = tx.claims.versions(ref.ref)
    if history:
        item = next((version for version in versions if version.revision == ref.revision), None)
    else:
        item = select_effective(versions, clock.utc_now())
    if item is not None and item.revision != ref.revision:
        raise ContractError("VERSION_CONFLICT")
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


def release_objects(storage, clock, context, refs: tuple[ObjectRef, ...], *, expected_epoch: int,
                    automatic: bool = True, history: bool = False) -> tuple:
    """Return freshly loaded SQLite objects; never echo cached/vector text.

    The successful read transaction is the last authority-release boundary.
    Already delivered host text is outside a local transaction's control.
    """
    if (type(refs) is not tuple or len(refs) > 200 or any(not isinstance(ref, ObjectRef) for ref in refs)
            or type(expected_epoch) is not int or expected_epoch < 0):
        raise ContractError("INPUT_INVALID", "release_request")
    with storage.read(context) as tx:
        if tx.status().memory_epoch != expected_epoch:
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
