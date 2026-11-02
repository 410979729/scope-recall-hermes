"""Bounded authority-backed object inspection for host adapters."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import ContractError, TrustedContext
from .injection_guard import notice
from .review_policy import REVIEW_REASONS
from .storage import SQLiteStorage
from .visibility import ObjectRef, release_objects


@dataclass(frozen=True)
class InspectedObject:
    kind: str
    ref: str
    revision: int
    value: object
    memory_epoch: int
    gaps: tuple[str, ...] = ()
    historical: bool = False


def _resolve(tx, ref, revision):
    """Find a visible object using bounded repository lookups, in source-first order."""
    value = tx.source(ref, revision) if revision is not None else tx.sources.source_current(ref)
    if value is not None:
        return "event", value
    claim_revision = revision if revision is not None else tx.claims.current_revision(ref)
    if claim_revision is not None:
        value = tx.claims.version(ref, claim_revision)
        if value is not None:
            return "claim", value
    for kind, repository in (("episode", tx.episodes), ("artifact", tx.artifacts), ("reference", tx.references)):
        value = repository.get(ref, revision)
        if value is not None:
            return kind, value
    raise ContractError("SOURCE_MISSING", "ref")


def inspect_object(
    storage: SQLiteStorage,
    clock,
    context: TrustedContext,
    ref: str,
    revision: int | None = None,
    *,
    limit: int = 24,
    owner_authority=None,
) -> InspectedObject:
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
        held = notice(tx, ref, revision)
        if held is not None:
            return InspectedObject("event", ref, held["revision"], held, status.memory_epoch, (held["state"],))
        kind, value = _resolve(tx, ref, revision)
        actual_revision = int(value.revision)
        if kind == "claim" and getattr(value, "reason", None) in REVIEW_REASONS and owner_authority is None:
            return InspectedObject(kind, ref, actual_revision, None, status.memory_epoch, ("owner_review_required",))
    released = release_objects(
        storage,
        clock,
        context,
        (ObjectRef(kind, ref, actual_revision),),
        expected_epoch=status.memory_epoch,
        automatic=False,
        history=revision is not None,
        owner_authority=owner_authority,
    )
    item = released[0]
    historical = (
        kind == "claim"
        and revision is not None
        and (item.revision != item.current_revision or item.state == "superseded")
    )
    return InspectedObject(kind, ref, actual_revision, item, status.memory_epoch, historical=historical)
