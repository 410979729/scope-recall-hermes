"""How a packet is measured: one canonical serialization and one budget unit.

The unit is a tokenizer-independent estimate, not an exact model token count.
UTF-8 bytes remain a separate diagnostic; CJK characters must not cost three
budget units merely because their encoding uses three bytes.
"""
from __future__ import annotations

import json
import math
import unicodedata


def canonical_render_json(value: object) -> str:
    """Serialize rendered recall data once, compactly and deterministically."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cjk(code: int) -> bool:
    return (0x2E80 <= code <= 0xA4CF or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x323AF)


def estimate_tokens(text: str) -> int:
    """Estimate ASCII runs at four chars/token, CJK at one, symbols by bytes."""
    quarters = 0
    for char in text:
        code = ord(char)
        if code < 128 and (char.isalnum() or char.isspace()):
            quarters += 1
        elif _cjk(code) or unicodedata.category(char)[0] in "LNMPZ":
            quarters += 4
        else:
            quarters += 4 * len(char.encode("utf-8"))
    return max(1, (quarters + 3) // 4)


def _admitted(costs, order, limits, used):
    """Positions the retrieval budget takes in ``order``, and what it has used after.

    The rule of ``RetrievalPipeline._apply_budget``: an item enters while a
    slot is free and its units fit what is left of the budget.
    """
    count, units = used
    taken = set()
    for index in order:
        if count < limits.max_items and units + costs[index] <= limits.budget_tokens:
            count, units = count + 1, units + costs[index]
            taken.add(index)
    return taken, (count, units)


def event_admission_order(ranked, limits):
    """Rank ordinary event runs by relevance per bounded size cost when admission must choose.

    Use a fixed reference quantum rather than the requested budget, so growing
    a budget cannot promote a previously oversized repeat ahead of a compact
    answer. Exact refs, claims and episode/resume anchors retain their priority.
    No source is deleted or merged: distinct evidence and temporal identity stay
    available; repeated imported text simply cannot win on volume alone.

    Size settles only a choice the item cap and budget force.  A run whose
    fusion order admits the same events as its density order keeps fusion
    order, so a newer long report that fits beside an older short note is not
    ranked below it for its length.  Capacity is simulated after everything
    ranked above the run.  Retrieval's budget and the packet compiler both call
    this with the request's effective limits; what retrieval kept always fits
    them, so the compiler keeps retrieval's order rather than re-deciding it.
    """
    ordered, run = [], []
    used = (0, 0)

    def flush():
        nonlocal used
        costs = [estimate_tokens(obj.content) for _candidate, obj in run]
        fusion = range(len(run))
        taken, after = _admitted(costs, fusion, limits, used)
        # Size admission targets oversized raw sources. Re-sorting a run of
        # short statements by raw fusion score loses the ranker's semantic
        # tie-breaks (for example a decisive answer versus an assistant echo).
        if any(cost > 256 for cost in costs):
            density = sorted(fusion, key=lambda index: max(run[index][0].fusion_score, 1e-6) /
                             math.sqrt(1 + costs[index] / 256), reverse=True)
            density_taken, density_after = _admitted(costs, density, limits, used)
            if density_taken != taken:
                run[:] = [run[index] for index in density]
                after = density_after
        used = after
        ordered.extend(run)
        run.clear()

    for pair in ranked:
        candidate, obj = pair
        if obj.kind == "event" and candidate.source not in {"exact_ref", "background"}:
            run.append(pair)
        else:
            flush()
            used = _admitted((estimate_tokens(obj.content),), (0,), limits, used)[1]
            ordered.append(pair)
    flush()
    return ordered
