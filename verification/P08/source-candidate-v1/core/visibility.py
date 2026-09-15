"""One authority check for all content exits and cached-result release."""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts import ContractError
from .claims import select_effective


@dataclass(frozen=True)
class ObjectRef:
    kind: str
    ref: str
    revision: int

    def __post_init__(self):
        if self.kind not in {"event","claim","episode","artifact","reference"} or type(self.ref) is not str or not 1 <= len(self.ref) <= 240 or type(self.revision) is not int or self.revision < 1:
            raise ContractError("INPUT_INVALID","object_ref")


def allowed(tx, kind: str, ref: str, *, automatic: bool = False) -> bool:
    if tx._check().execute("SELECT 1 FROM restored_absence_blocks WHERE object_kind=? AND object_ref=?",(kind,ref)).fetchone():
        return False
    row = tx._check().execute("SELECT read_blocked,suppressed,scope_id FROM object_blocks WHERE object_kind=? AND object_ref=?",(kind,ref)).fetchone()
    return row is None or (row["scope_id"] in tx.context.allowed_scope_ids and not row["read_blocked"] and not (automatic and row["suppressed"]))


def release_objects(storage, clock, context, refs: tuple[ObjectRef, ...], *, expected_epoch: int,
                    automatic: bool = True, history: bool = False) -> tuple:
    """Return freshly loaded SQLite objects; never echo cached/vector text.

    The successful read transaction is the last authority-release boundary.
    Already delivered host text is outside a local transaction's control.
    """
    if type(refs) is not tuple or not len(refs) <= 200 or any(not isinstance(r,ObjectRef) for r in refs) or type(expected_epoch) is not int or expected_epoch < 0:
        raise ContractError("INPUT_INVALID","release_request")
    with storage.read(context) as tx:
        if tx.status().memory_epoch != expected_epoch:
            raise ContractError("VERSION_CONFLICT","memory_epoch")
        result = []
        for ref in refs:
            if not allowed(tx,ref.kind,ref.ref,automatic=automatic):
                raise ContractError("SOURCE_MISSING")
            if ref.kind == "event":
                item = tx.source(ref.ref,ref.revision)
                if not history:
                    tx.claims.require_live_source(ref.ref,ref.revision)
            elif ref.kind == "claim":
                versions = tx.claims.versions(ref.ref)
                item = next((v for v in versions if v.revision == ref.revision),None) if history else select_effective(versions,clock.utc_now())
                if item is not None and item.revision != ref.revision:
                    raise ContractError("VERSION_CONFLICT")
                if automatic and item is not None and item.payload.get("intention",{}).get("state") in {"cancelled","completed","expired"}:
                    raise ContractError("SOURCE_MISSING")
            elif ref.kind in {'episode','artifact','reference'}:
                repo={'episode':tx.episodes,'artifact':tx.artifacts,'reference':tx.references}[ref.kind]
                item=repo.get(ref.ref,ref.revision if history else None)
                if item is not None and item.revision!=ref.revision:raise ContractError('VERSION_CONFLICT')
                if automatic and ref.kind=='episode' and item is not None and 'resume_requires_rebuild' in item.gaps:
                    raise ContractError('VERSION_CONFLICT','episode_sources')
            if item is None or (automatic and item.suppressed):
                raise ContractError("SOURCE_MISSING")
            result.append(item)
        return tuple(result)
