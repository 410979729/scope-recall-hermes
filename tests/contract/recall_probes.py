"""The judged recall probe set, and the runner that scores it.

Recall quality is the one property synthetic fixtures cannot demonstrate, so
this is deliberately *not* a unit test: it runs against a real instance's
corpus.  What lives here is the part that has to be reproducible -- the
questions, and what counts as a correct answer -- because a score nobody else
can re-derive is an anecdote.

Each gold predicate was read against the matching documents and confirmed by
hand.  They are regexes over full source content rather than a copied list of
event ids so the set survives a re-import and can be audited by rerunning it.

A gold document *answers* the question; it does not merely mention its words.
That distinction is why the predicates are narrow: an audit dump that quotes
other events would otherwise satisfy almost any of them, which is exactly the
false positive that sent an early measurement of this set in the wrong
direction.

Run it against a copy, never the live store::

    python tests/eval/recall_probes.py --instance-root <hermes home>

Not responsible for: building the shadow copy or supplying credentials -- the
runner takes an instance root and works on a copy of it.
"""
from __future__ import annotations

import re

#: Questions a corpus can answer, with a predicate identifying a correct answer.
#: The shape is the contract; the corpus is whoever runs it.  These read as an
#: operator's own notes because that is what the set is for -- the earlier
#: version quoted one instance's real conversations, an installation id and a
#: project id, in a repository anyone can read.
ANSWERABLE: tuple[tuple[str, str], ...] = (
    ("What installation id does this instance report?",
     r"install:[0-9a-f]{8,}"),
    ("Which API protocol does the configured chat model speak, and how large is its context?",
     r"(?is)(protocol|格式|协议).{0,400}(context|上下文)"),
    ("What has to be watched when one instance inherits another's memory?",
     r"(?is)(inherit|继承).{0,40}(workflow|工作流|bulk|整库)"),
    ("What does the screen-reading helper script do?",
     r"(?is)ocr.{0,600}(recognis|识别|screen|截图)"),
    ("Which identifier does the acceptance project use?",
     r"[A-Z]{3,}-[0-9a-f]{6,}"),
    ("What happens if only the suppressed flag is cleared during a recovery?",
     r"(?is)(suppressed).{0,200}(not enough|still|仍|不够)"),
    ("How far behind its upstream is the host application?",
     r"(?is)(behind|落后).{0,20}\d+"),
    ("What went wrong with capture admission on the other host?",
     r"(?is)(capture|捕获).{0,40}(admission|准入)"),
)

#: Questions the corpus cannot answer. The packet must say so rather than
#: assemble something plausible: no query evidence means ``answerability`` is
#: not ``supported``. A question whose *identifier* genuinely appears in the
#: store is answerable -- "there is no such project" is an answer.
UNANSWERABLE: tuple[str, ...] = (
    "What identifier does project UNRECORDED-7a047b41a2c607 use?",
    "What is my favourite colour?",
    "What was the quarterly revenue in 2019?",
)

GOLD = tuple((query, re.compile(pattern)) for query, pattern in ANSWERABLE)


def score(items, pattern) -> int | None:
    """1-based position of the first item that answers, or ``None``."""
    for position, item in enumerate(items, 1):
        if pattern.search(getattr(item, "content", "") or ""):
            return position
    return None


def mean_reciprocal_rank(positions) -> float:
    found = [1 / position for position in positions if position]
    return sum(found) / len(positions) if positions else 0.0


__all__ = ["ANSWERABLE", "GOLD", "UNANSWERABLE", "mean_reciprocal_rank", "score"]
