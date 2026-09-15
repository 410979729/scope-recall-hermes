"""Bounded views of existing source records; no new source identity or raw copy."""
from __future__ import annotations

from dataclasses import dataclass
import re

from ..contracts import ContractError
from .storage import StoredSource


@dataclass(frozen=True)
class ConsolidationChunk:
    start: int
    end: int
    total: int

    @property
    def final(self):
        return self.end == self.total


@dataclass(frozen=True)
class ChunkedSource(StoredSource):
    consolidation_window: ConsolidationChunk | None = None
    consolidation_seed: tuple = ()


def source_chunk(source, offset, *, formatter, episode_ref=None, resume_seed=()):
    """Choose an exact consecutive window using the real serialized byte limit.

    Prefer sentence boundaries. Oversized individual sentences still make
    bounded progress; acceptance qualifies every quote against the complete
    original source, including omitted negations and repeated occurrences.
    Coverage is committed only after every consecutive window is accepted.
    """
    content = source.event["content"]
    total = len(content)
    if type(offset) is not int or not 0 <= offset < total:
        raise ContractError("DERIVATION_INVALID", "consolidation_offset")

    def view(end):
        chunk = ConsolidationChunk(offset, end, total)
        return ChunkedSource(**dict(source.__dict__, event=dict(source.event, content=content[offset:end])),
                             consolidation_window=chunk, consolidation_seed=resume_seed)

    lower, upper = offset + 1, total
    best = None
    while lower <= upper:
        middle = (lower + upper) // 2
        candidate = view(middle)
        try:
            formatter((candidate,), episode_ref=episode_ref)
        except ContractError as exc:
            if exc.code != "INPUT_INVALID" or exc.field != "consolidation_input_budget":
                raise
            upper = middle - 1
        else:
            best = candidate
            lower = middle + 1
    if best is None:
        raise ContractError("INPUT_INVALID", "consolidation_metadata_budget")
    if best.consolidation_window.end < total:
        # Do not reduce a useful page to one leading delimiter. This is only a
        # boundary preference; complete-root qualification remains mandatory.
        text = best.event["content"]
        boundaries = list(re.finditer(r"[。！？!?;；\n]", text))
        if boundaries and boundaries[-1].end() >= max(1, len(text) // 2):
            best = view(offset + boundaries[-1].end())
    return best, best.consolidation_window
