"""Normalize public datasets without mixing gold answers into stored messages."""

from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# The upstream evaluation separates multi-hop (1), temporal (2), open-domain
# (3), single-hop (4), and adversarial (5). Category 5 has no supported answer;
# its adversarial_answer is a distractor and must never become the gold answer.
LOCOMO_CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


@dataclass(frozen=True)
class Turn:
    """One source turn with its stable corpus identity and observation time."""

    key: str
    session: str
    role: str
    content: str
    at: str
    answer: bool = False


@dataclass(frozen=True)
class Case:
    """A question and its independent history; gold stays on the scoring side."""

    id: str
    category: str
    question: str
    at: str
    gold: str
    sessions: tuple[str, ...]
    turns: tuple[Turn, ...]


def stamp(value: str) -> str:
    """Interpret published naive calendar times as UTC for a repeatable clock."""
    return datetime.strptime(value, "%Y/%m/%d (%a) %H:%M").replace(tzinfo=UTC).isoformat()


def longmemeval(path: Path) -> list[Case]:
    """Load the cleaned file, retaining assistant roles and answer-turn labels."""
    cases = []
    for row in json.loads(path.read_text(encoding="utf-8")):
        turns = []
        sessions = zip(row["haystack_session_ids"], row["haystack_dates"], row["haystack_sessions"], strict=True)
        # Stable sorting retains file order for simultaneous sessions and never
        # changes the turn order within a session.
        for sid, date, messages in sorted(sessions, key=lambda session: datetime.fromisoformat(stamp(session[1]))):
            for index, message in enumerate(messages):
                turns.append(
                    Turn(
                        f"{sid}:{index}",
                        sid,
                        message["role"],
                        message["content"],
                        stamp(date),
                        bool(message.get("has_answer", False)),
                    )
                )
        category = "abstention" if row["question_id"].endswith("_abs") else row["question_type"]
        cases.append(
            Case(
                row["question_id"],
                category,
                row["question"],
                stamp(row["question_date"]),
                str(row["answer"]),
                tuple(row["answer_session_ids"]),
                tuple(turns),
            )
        )
    return cases


def fixed_splits(cases: list[Case]) -> dict:
    """Freeze a disjoint 50/150/300 partition by seeded hash, independent of input order."""
    ids = [case.id for case in cases]
    if len(ids) != 500 or len(set(ids)) != 500:
        raise ValueError("expected exactly 500 unique question ids")
    ordered = sorted(ids, key=lambda qid: hashlib.sha256(f"memory-evaluation-split/1:42:{qid}".encode()).hexdigest())
    return {
        "seed": 42,
        "method": "sha256(memory-evaluation-split/1:42:question_id)",
        "dev": ordered[:50],
        "regression": ordered[50:200],
        "sealed": ordered[200:],
    }


def locomo(path: Path) -> list[Case]:
    """Adapt two-person dialogues as attributed external documents, without inventing an owner."""
    cases = []
    for sample in json.loads(path.read_text(encoding="utf-8")):
        conversation, turns = sample["conversation"], []
        keys = sorted(
            (key for key in conversation if key.startswith("session_") and not key.endswith("date_time")),
            key=lambda key: int(key.split("_")[1]),
        )
        for key in keys:
            date = datetime.strptime(conversation[f"{key}_date_time"], "%I:%M %p on %d %B, %Y").replace(tzinfo=UTC)
            for index, message in enumerate(conversation[key]):
                text = f"{message['speaker']}: {message['text']}"
                turns.append(
                    Turn(message["dia_id"], key, "document", text, (date + timedelta(seconds=index)).isoformat())
                )
        now = (datetime.fromisoformat(turns[-1].at) + timedelta(days=1)).isoformat()
        for index, qa in enumerate(sample["qa"]):
            category = LOCOMO_CATEGORIES[qa["category"]]
            gold = "no information available" if qa["category"] == 5 else str(qa["answer"])
            evidence = tuple(qa.get("evidence", ()))
            sessions = tuple(sorted({turn.session for turn in turns if turn.key in evidence}))
            marked = tuple(Turn(t.key, t.session, t.role, t.content, t.at, t.key in evidence) for t in turns)
            cases.append(Case(f"{sample['sample_id']}:{index}", category, qa["question"], now, gold, sessions, marked))
    return cases


def factconsolidation(path: Path) -> list[Case]:
    """Adapt numbered facts in order as external documents; later numbers arrive later.

    The file stores questions, answers and metadata as Python-literal strings.
    literal_eval reads those values without executing dataset content. Each row
    is a distinct history; question ids retain the upstream SH/MH and size tags.
    """
    import pyarrow.parquet as parquet

    cases = []
    for row_index, row in enumerate(parquet.read_table(path).to_pylist()):
        meta = ast.literal_eval(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        questions = ast.literal_eval(row["questions"]) if isinstance(row["questions"], str) else row["questions"]
        answers = ast.literal_eval(row["answers"]) if isinstance(row["answers"], str) else row["answers"]
        ids = meta["qa_pair_ids"]
        turns = []
        start = datetime(2020, 1, 1, tzinfo=UTC)
        for index, line in enumerate(row["context"].splitlines()):
            if line.partition(". ")[0].isdigit():
                turns.append(
                    Turn(
                        f"fact:{index}",
                        f"row:{row_index}",
                        "document",
                        line,
                        (start + timedelta(seconds=index)).isoformat(),
                    )
                )
        if not turns:
            raise ValueError("no numbered facts in FactConsolidation history")
        now = (datetime.fromisoformat(turns[-1].at) + timedelta(days=1)).isoformat()
        for qid, question, gold in zip(ids, questions, answers, strict=True):
            category = "multi-hop" if "_mh_" in qid else "single-hop"
            cases.append(Case(qid, category, question, now, json.dumps(gold), (), tuple(turns)))
    return cases


def category_counts(cases: list[Case]) -> dict[str, int]:
    """Count every category, including abstention instead of folding it into another class."""
    return dict(sorted(Counter(case.category for case in cases).items()))
