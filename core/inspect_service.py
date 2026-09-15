"""Bounded authority-backed object inspection for host adapters."""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts import ContractError, TrustedContext
from .storage import SQLiteStorage
from .visibility import ObjectRef, release_objects


@dataclass(frozen=True)
class InspectedObject:
    kind: str
    ref: str
    revision: int
    value: object
    memory_epoch: int


def inspect_object(storage: SQLiteStorage, clock, context: TrustedContext, ref: str, revision: int | None = None, *, limit: int = 24) -> InspectedObject:
    """Resolve one object by direct repository lookups, then release it again.

    No history/list scan is used: current revisions come from their owning head
    row, while an explicit revision addresses exactly one version.  The final
    ``release_objects`` read is the authority boundary after the initial read.
    """
    if type(ref) is not str or not 1 <= len(ref) <= 240:
        raise ContractError("INPUT_INVALID", "ref")
    if revision is not None and (type(revision) is not int or revision < 1):
        raise ContractError("INPUT_INVALID", "revision")
    if type(limit) is not int or not 1 <= limit <= 24:
        raise ContractError("INPUT_INVALID", "limit")
    with storage.read(context) as tx:
        status = tx.status()
        kind: str | None = None
        value = tx.source(ref, revision) if revision is not None else tx.source_current(ref)
        if value is not None:
            kind = "event"
        if value is None:
            claim_revision = revision
            if claim_revision is None:
                claim_revision = tx.claims.current_revision(ref)
            if claim_revision is not None:
                value = tx.claims.version(ref, claim_revision)
                if value is not None:
                    kind = "claim"
        if value is None:
            value = tx.episodes.get(ref, revision)
            if value is not None:
                kind = "episode"
        if value is None:
            value = tx.artifacts.get(ref, revision)
            if value is not None:
                kind = "artifact"
        if value is None:
            value = tx.references.get(ref, revision)
            if value is not None:
                kind = "reference"
        if value is None or kind is None:
            raise ContractError("SOURCE_MISSING", "ref")
        actual_revision = int(value.revision)
    released = release_objects(
        storage,
        clock,
        context,
        (ObjectRef(kind, ref, actual_revision),),
        expected_epoch=status.memory_epoch,
        automatic=False,
        history=revision is not None,
    )
    return InspectedObject(kind, ref, actual_revision, released[0], status.memory_epoch)
