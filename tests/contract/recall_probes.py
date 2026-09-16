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

#: Questions the corpus can answer, with a predicate identifying a correct
#: answer. Measured on TianShu 2026-09-14: 8/8 answered, MRR 0.833.
ANSWERABLE: tuple[tuple[str, str], ...] = (
    ("天枢这个实例的 installation_id 是什么？",
     r"hermes-install:797f237b4e3fd14d3202efa36bf6bbf0"),
    ("Kimi K3 用的是什么 API 协议？上下文多大？",
     r"(?is)kimi[ -]?k3.{0,400}(provider|协议|上下文|context)"),
    ("跨实例继承记忆库要注意什么？",
     r"跨实例记忆(继承|治理).{0,40}(工作流|整库灌入)"),
    ("windows-ocr.ps1 是做什么的？",
     r"(?is)windows-ocr\.ps1.{0,600}(Windows\.Media\.Ocr|built-in OCR)"),
    ("受控记忆验收项目 SRLIVE-de5a0ba54dbe 的唯一标识是什么？",
     r"SRLIVE-de5a0ba54dbe"),
    ("只清掉 source_events.suppressed 会有什么后果？",
     r"(?is)第一轮恢复只清了|只清.{0,12}suppressed.{0,200}(还|仍|不够|没有)"),
    ("天枢的 hermes 版本落后官方多少个提交？",
     r"落后官方 99 个提交"),
    ("clawlore 的记忆捕获准入是怎么回事？",
     r"clawlore 记忆捕获的内容准入失守"),
)

#: Questions the corpus cannot answer. The packet must say so rather than
#: assemble something plausible: no query evidence means ``answerability`` is
#: not ``supported``. A question whose *identifier* genuinely appears in the
#: store is answerable -- "there is no such project" is an answer.
UNANSWERABLE: tuple[str, ...] = (
    "UNRECORDED-7a047b41a2c607 这个项目的唯一标识是什么？",
    "我最喜欢的颜色是什么？",
    "2019 年的季度营收是多少？",
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
