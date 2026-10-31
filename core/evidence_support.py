"""Source-bound semantic support receipts; model verdicts never grant authority.

Receipts exist only during publication. They bind the complete proposal and source
contents, so a changed quote, revision or source cannot reuse a prior judgement.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..contracts import positioned_evidence as positioned
from ..contracts import validate_evidence_positions as validate_positions


@dataclass(frozen=True)
class SupportVerdict:
    """One bounded support answer and optional merge target at a specific revision."""

    fingerprint: str
    state: str
    target_ref: str | None = None
    target_revision: int | None = None
    action: str = "new"
    failure_code: str | None = None


_VERDICTS: ContextVar[tuple[SupportVerdict, ...]] = ContextVar("evidence_support", default=())


def support_fingerprint(proposal, contents) -> str:
    """Bind the answer to all cited source contents and the exact proposal."""
    evidence = [
        (span["source_ref"], span["source_revision"], contents.get((span["source_ref"], span["source_revision"])))
        for span in proposal["evidence_spans"]
    ]
    material = json.dumps([proposal, evidence], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


@contextmanager
def using_support(verdicts):
    """Limit prepared verdicts to one publication transaction, restoring nested contexts."""
    token = _VERDICTS.set(tuple(verdicts))
    try:
        yield
    finally:
        _VERDICTS.reset(token)


def support_verdict(proposal, roots) -> SupportVerdict | None:
    """Read only a receipt matching this proposal and these live source versions."""
    contents = {(root.ref, root.revision): root.content for root in roots}
    fingerprint = support_fingerprint(proposal, contents)
    return next((verdict for verdict in _VERDICTS.get() if verdict.fingerprint == fingerprint), None)


__all__ = [
    "SupportVerdict",
    "positioned",
    "validate_positions",
    "support_fingerprint",
    "using_support",
    "support_verdict",
]
