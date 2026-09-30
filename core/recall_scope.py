"""What a question names of when, and of which entry: a scope the recall can read directly.

"9月29日工作机 Claude Code 聊了什么" asks for one entry's conversation on one day.  Searched by its words, the
date and the entry's name matched nothing useful ("聊了", "工作", "29") and the recall answered from other days and
other entries: 4 of 106 such questions over two weeks of the shared store found a message of the named entry from
the named day (yuheng's audit of 3.4.2 and its baseline).  The scope is read here and nowhere else; the lexical,
vector and other channels are unchanged, and a question that names no day has no scope.

A day is a calendar day in the zone the asking host shows its model (``SearchContext.zone``; this machine's when the
host names none), the zone its recalled times are rendered in, so "29日" means the same day in the question and in
the answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import re
import unicodedata
from typing import Mapping

#: A scoped candidate's ``lexical_score``: what the question asks (it shares the rest of the question's words, or the
#: question asks only what was said that day), or what only fills the day in.  Read for its weight when candidates
#: are fused (``RetrievalPipeline._fuse_candidates``); a scoped candidate is admitted without a lexical test.
SCOPED_ANSWER = 2.0
SCOPED_FILL = 1.0
#: Days one question may name.  More is a range or a list, which a scope of whole days does not serve.
MAX_SCOPE_DAYS = 3

_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*[日号]?(?!\d)")
_MONTH_DAY = re.compile(r"(?<![\d.])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?(?![\d.])")
#: Days before today each relative word names.
_RELATIVE = (("大前天", 3), ("前天", 2), ("昨天", 1), ("昨日", 1), ("今天", 0), ("今日", 0))
_RELATIVE_EN = re.compile(r"\b(today|yesterday)\b", re.IGNORECASE)
#: Characters an entry's name may be written with or without between its own: spaces, and "的" ("工作机的 Codex").
_NAME_GAP = r"[\s的]*"


@dataclass(frozen=True)
class QueryScope:
    """Named days as UTC ``[start, end)`` bounds in the store's time format, the named entries (none: any entry),
    and what the question asks once they are taken out of it."""

    windows: tuple[tuple[str, str], ...]
    entry_ids: tuple[str, ...]
    rest: str


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _day_window(day: date, zone: tzinfo) -> tuple[str, str]:
    start = datetime.combine(day, time(0), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone)
    return _stamp(start), _stamp(end)


def _named_days(text: str, today: date) -> tuple[list[date], list[tuple[int, int]]]:
    days: list[date] = []
    spans: list[tuple[int, int]] = []

    def add(day: date | None, span: tuple[int, int]) -> None:
        if day is not None and day not in days:
            days.append(day)
        spans.append(span)

    for match in _FULL_DATE.finditer(text):
        try:
            add(date(int(match.group(1)), int(match.group(2)), int(match.group(3))), match.span())
        except ValueError:
            continue
    for match in _MONTH_DAY.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        try:
            day = date(today.year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            continue
        # A month and day without a year is the latest one not in the future.
        if day > today:
            try:
                day = day.replace(year=today.year - 1)
            except ValueError:
                continue
        add(day, match.span())
    for word, back in _RELATIVE:
        start = text.find(word)
        while start >= 0:
            if not any(s <= start < e for s, e in spans):
                add(today - timedelta(days=back), (start, start + len(word)))
            start = text.find(word, start + len(word))
    for match in _RELATIVE_EN.finditer(text):
        add(today - timedelta(days=1 if match.group(1).lower() == "yesterday" else 0), match.span())
    return days, spans


def _named_entries(text: str, entries: Mapping[str, str]) -> tuple[list[str], list[tuple[int, int]]]:
    """The entries whose display name or id the text holds, longest first and never two over one stretch: in
    "工作机 Claude Code" only the work computer's entry, not also "Claude Code"."""
    found: list[tuple[int, int, str]] = []
    for entry_id, name in entries.items():
        for label in {name, entry_id}:
            letters = "".join(unicodedata.normalize("NFKC", label or "").split())
            if len(letters) < 2:
                continue
            pattern = re.compile(_NAME_GAP.join(map(re.escape, letters)), re.IGNORECASE)
            found.extend((match.start(), match.end(), entry_id) for match in pattern.finditer(text))
    chosen: list[tuple[int, int, str]] = []
    for start, end, entry_id in sorted(found, key=lambda item: (item[0] - item[1], item[0])):
        if all(end <= other_start or start >= other_end for other_start, other_end, _ in chosen):
            chosen.append((start, end, entry_id))
    ids = list(dict.fromkeys(entry_id for _, _, entry_id in sorted(chosen)))
    return ids, [(start, end) for start, end, _ in chosen]


def query_scope(query: str, *, now: str, zone: tzinfo | None, entries: Mapping[str, str]) -> QueryScope | None:
    """The days and entries ``query`` names, or None when it names no day.

    ``now`` is the recall's clock (UTC ISO); ``entries`` maps a shared store's entry ids to their display names
    (empty in a store of one host).  An entry named without a day is no scope: "天姬的模型换成什么了" asks about a
    subject, and reading that entry's messages would crowd out the answer.
    """
    text = unicodedata.normalize("NFKC", query)
    local_zone = zone or datetime.now().astimezone().tzinfo or timezone.utc
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(local_zone).date()
    days, day_spans = _named_days(text, today)
    if not days:
        return None
    entry_ids, entry_spans = _named_entries(text, entries)
    rest = text
    for start, end in sorted([*day_spans, *entry_spans], reverse=True):
        rest = rest[:start] + " " + rest[end:]
    return QueryScope(tuple(_day_window(day, local_zone) for day in days[:MAX_SCOPE_DAYS]), tuple(entry_ids),
                      " ".join(rest.split()))
