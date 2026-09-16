"""One recall budget unit and size-aware admission policy.

This is a tokenizer-independent estimate, not an exact model token count.
UTF-8 bytes remain a separate diagnostic; CJK characters must not cost three
budget units merely because their encoding uses three bytes.
"""
from __future__ import annotations

import math
import unicodedata


def estimate_tokens(text: str) -> int:
    """Estimate ASCII runs at four chars/token, CJK at one, symbols by bytes."""
    quarters = 0
    for char in text:
        code = ord(char)
        category = unicodedata.category(char)
        if code < 128 and (char.isalnum() or char.isspace()):
            quarters += 1
        elif (0x2E80 <= code <= 0xA4CF or 0xAC00 <= code <= 0xD7AF
              or 0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x323AF):
            quarters += 4
        elif category[0] in {"L", "N", "M"}:
            quarters += 4
        elif category[0] in {"P", "Z"}:
            quarters += 4
        else:
            quarters += 4 * len(char.encode("utf-8"))
    return max(1, (quarters + 3) // 4)


def event_admission_order(ranked):
    """Rank ordinary event runs by relevance per bounded size cost.

    Use a fixed reference quantum rather than the requested budget, so growing
    a budget cannot promote a previously oversized repeat ahead of a compact
    answer. Exact refs, claims and episode/resume anchors retain their priority.
    No source is deleted or merged: distinct evidence and temporal identity stay
    available; repeated imported text simply cannot win on volume alone.
    """
    ordered, run = [], []

    def flush():
        # Size admission targets oversized raw sources. Re-sorting a run of
        # short statements by raw fusion score loses the ranker's semantic
        # tie-breaks (for example a decisive answer versus an assistant echo).
        if all(estimate_tokens(pair[1].content) <= 256 for pair in run):
            ordered.extend(run)
            run.clear()
            return
        run.sort(key=lambda pair: max(pair[0].fusion_score, 1e-6) /
                 math.sqrt(1 + estimate_tokens(pair[1].content) / 256), reverse=True)
        ordered.extend(run)
        run.clear()

    for pair in ranked:
        candidate, obj = pair
        if obj.kind == "event" and candidate.source not in {"exact_ref", "background"}:
            run.append(pair)
        else:
            flush()
            ordered.append(pair)
    flush()
    return ordered
