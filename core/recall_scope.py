"""What a question names of when, and of which entry: a scope the recall can read directly.

"9月29日工作机 Claude Code 聊了什么" asks for one entry's conversation on one day.  Searched by its words, the
date and the entry's name matched nothing useful ("聊了", "工作", "29") and the recall answered from other days and
other entries: 4 of 106 such questions over two weeks of the shared store found a message of the named entry from
the named day (yuheng's audit of 3.4.2 and its baseline).  The scope is read here and nowhere else; the lexical,
vector and other channels are unchanged.

Only a question that asks what was said or done is read in its day: once its days and entries are taken out, none
of its words names a subject (``names_a_subject``: "聊了什么", "做了哪些事", "有什么进展").  One that names a
subject beside a day ("9月2日发布的 3.4.2 修了什么", "继续昨天的任务", "昨天说的偏好是什么") is recalled as if it
named none: read in its day, the day's messages pushed out an answer said on another day, the current task and the
claims that answered it (review of 3.4.6).  A range of days, more than three of them, and a day that holds no
conversation (a future one, or a placeholder such as 9999-12-31) are no scope either.

A day is a calendar day in the zone the asking host shows its model (``SearchContext.zone``; when the host names
none, the serving machine's, with that day's daylight-saving offset), the zone its recalled times are rendered in,
so "29日" means the same day in the question and in the answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import re
import unicodedata
from typing import Mapping

#: Days one question may name.  More is a list, which a scope of whole days does not serve.
MAX_SCOPE_DAYS = 3
#: How far back a month and day without a year may reach: "12月25日" asked in January is the last one, "10月1日"
#: asked on 9月30日 is a day with no conversation yet.
_MONTH_DAY_REACH = timedelta(days=183)
#: The earliest day read: an older date is a placeholder or an example ("0001-01-01"), and a day before this one
#: cannot be turned into a time the store holds on every machine.
_EARLIEST = date(1970, 1, 2)
#: What the rest of a day's question may say without naming a subject: talking and doing, the time of day, and
#: the particles, pronouns and measure words around them ("聊了什么", "都说了些什么", "做了哪些事", "有什么进展",
#: "下午的对话").  A term of the rest made only of these characters, or one of these English words, names
#: nothing; a term with any other character names a subject ("bug", "修好", "金丝雀", "3.4.2").  Curated, like
#: ``recall_policy.SYNONYM_GROUPS``.
_SAID_OR_DONE = frozenset("聊说讲谈问答做干忙弄搞办进展度讨论事情况话对天记录内容发生上下午中晚早"
                          "了的地得着过吗呢吧啊呀么啥哪什怎样如何些都也又还就在有是和跟与个件次一我你他她它们咱这那")
_SAID_OR_DONE_WORDS = frozenset({"talk", "talked", "say", "said", "discuss", "discussed", "chat", "chatted",
                                 "do", "did", "done", "work", "worked", "progress", "happen", "happened",
                                 "we", "you", "i", "they", "us", "about"})

_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*[日号]?(?!\d)")
_MONTH_DAY = re.compile(r"(?<![\d.])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?(?![\d.])")
#: What joins two days into a range ("9月28日到9月30日", "2026-09-28 至 2026-09-30", "昨天到今天"), and a date
#: followed by the end of its range ("9月28-30日", "9月28日到30日").
_RANGE = re.compile(r"\s*(?:到|至|~|～|-|—|–)\s*")
_RANGE_TAIL = re.compile(r"\s*(?:到|至|~|～|-|—|–)\s*(?:\d{4}\s*[-/年]\s*)?(?:\d{1,2}\s*[-/月]\s*)?\d{1,2}(?![\d.])")
#: Days before today each relative word names.
_RELATIVE = (("大前天", 3), ("前天", 2), ("昨天", 1), ("昨日", 1), ("今天", 0), ("今日", 0))
_RELATIVE_EN = re.compile(r"(?<![A-Za-z])(today|yesterday)(?![A-Za-z])", re.IGNORECASE)
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


def _day_window(day: date, zone: tzinfo | None) -> tuple[str, str]:
    start, end = datetime.combine(day, time(0)), datetime.combine(day + timedelta(days=1), time(0))
    if zone is None:
        # The machine's rules for that day, as its recalled times are rendered: today's offset would move every
        # day on the other side of a daylight-saving change by an hour.
        return _stamp(start.astimezone()), _stamp(end.astimezone())
    return _stamp(start.replace(tzinfo=zone)), _stamp(end.replace(tzinfo=zone))


def _named_days(text: str, today: date) -> tuple[list[date], list[tuple[int, int]]] | None:
    """The days the text names that can hold a conversation, and where it names days; None for a range."""
    days: list[date] = []
    spans: list[tuple[int, int]] = []

    def add(day: date | None, span: tuple[int, int]) -> None:
        spans.append(span)
        if day is not None and _EARLIEST <= day <= today and day not in days:
            days.append(day)

    for match in _FULL_DATE.finditer(text):
        if _RANGE_TAIL.match(text, match.end()):
            return None
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            day = None
        add(day, match.span())
    for match in _MONTH_DAY.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        if _RANGE_TAIL.match(text, match.end()):
            return None
        try:
            day = date(today.year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            day = None
        # A month and day without a year is the latest one not in the future, within half a year.
        if day is not None and day > today:
            try:
                day = day.replace(year=today.year - 1)
            except ValueError:
                day = None
            if day is not None and today - day > _MONTH_DAY_REACH:
                day = None
        add(day, match.span())
    for word, back in _RELATIVE:
        start = text.find(word)
        while start >= 0:
            if not any(s <= start < e for s, e in spans):
                add(today - timedelta(days=back), (start, start + len(word)))
            start = text.find(word, start + len(word))
    for match in _RELATIVE_EN.finditer(text):
        add(today - timedelta(days=1 if match.group(1).lower() == "yesterday" else 0), match.span())
    ordered = sorted(spans)
    if any(_RANGE.fullmatch(text[end:start]) for (_, end), (start, _) in zip(ordered, ordered[1:])):
        return None
    return days, spans


def _name_pattern(letters: str) -> re.Pattern[str]:
    """A Latin name is a word of its own: "desk" is no entry in "Claude Desktop", nor "codex" in "codexbar"."""
    head = r"(?<![A-Za-z0-9])" if letters[0].isascii() and letters[0].isalnum() else ""
    tail = r"(?![A-Za-z0-9])" if letters[-1].isascii() and letters[-1].isalnum() else ""
    return re.compile(head + _NAME_GAP.join(map(re.escape, letters)) + tail, re.IGNORECASE)


def _named_entries(text: str, entries: Mapping[str, str]) -> tuple[list[str], list[tuple[int, int]]]:
    """The entries whose display name or id the text holds, longest first and never two over one stretch: in
    "工作机 Claude Code" only the work computer's entry, not also "Claude Code"."""
    found: list[tuple[int, int, str]] = []
    for entry_id, name in entries.items():
        for label in {name, entry_id}:
            letters = "".join(unicodedata.normalize("NFKC", label or "").split())
            if len(letters) < 2:
                continue
            found.extend((match.start(), match.end(), entry_id) for match in _name_pattern(letters).finditer(text))
    chosen: list[tuple[int, int, str]] = []
    for start, end, entry_id in sorted(found, key=lambda item: (item[0] - item[1], item[0])):
        if all(end <= other_start or start >= other_end for other_start, other_end, _ in chosen):
            chosen.append((start, end, entry_id))
    ids = list(dict.fromkeys(entry_id for _, _, entry_id in sorted(chosen)))
    return ids, [(start, end) for start, end, _ in chosen]


def names_a_subject(term: str) -> bool:
    """Whether a search term of the rest of a day's question, its days and entries taken out, names what it asks
    about.  A rest none of whose terms does asks only what was said or done, and the day's messages answer it."""
    return not (set(term) <= _SAID_OR_DONE or term.lower() in _SAID_OR_DONE_WORDS)


def query_scope(query: str, *, now: str, zone: tzinfo | None, entries: Mapping[str, str]) -> QueryScope | None:
    """The days and entries ``query`` names, or None when it names no day that can be read as one.

    ``now`` is the recall's clock (UTC ISO); ``entries`` maps a shared store's entry ids to their display names
    (empty in a store of one host).  An entry named without a day is no scope.  Whether the rest names a subject is
    the caller's to judge (``names_a_subject`` over its search terms).
    """
    text = unicodedata.normalize("NFKC", query)
    moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    today = (moment.astimezone(zone) if zone is not None else moment.astimezone()).date()
    named = _named_days(text, today)
    if named is None:
        return None
    days, day_spans = named
    if not days or len(days) > MAX_SCOPE_DAYS:
        return None
    entry_ids, entry_spans = _named_entries(text, entries)
    rest = text
    for start, end in sorted([*day_spans, *entry_spans], reverse=True):
        rest = rest[:start] + " " + rest[end:]
    return QueryScope(tuple(_day_window(day, zone) for day in days), tuple(entry_ids), " ".join(rest.split()))
