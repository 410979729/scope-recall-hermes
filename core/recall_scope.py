"""What a question names of when, and of which entry: the time recall ranks by, and a scope it can read directly.

"What did I say about the Orchard release on September 18?" names a subject and a day, and every note on the subject
holds its words: by words alone the newest note comes first, and one of that day holding fewer of them may not come at
all.  The time a question names (``query_time``) is a day, a range of days, a month, last or this week or month, or
the days before or since one of them, each a run of whole days up to today: the past days a message could have been
said on, never a part of a day ("tonight", "今早"), a day still to come or a date inside an identifier
("release-2026-09-18").  It is read for any recall of what holds now (a historical or as-of read is left as it was),
and it only adds: the time is searched for what else the question asks (``RetrievalStorage.dated``), and what was said
in it goes before what was said at other times (``RetrievalPipeline._rank_hydrated``), none of which is left out,
since it often answers.  It is read by rule and only from what a question plainly writes, since a time misread moves
the answer down; a question that names no time has none, and is recalled as if this reading did not exist.

A question that names only an entry and a day supplies metadata rather than the conversation's content words.
A dedicated channel retrieves by entry and calendar day, while the lexical, vector, and other channels continue
to search the question normally.

Only a question about what was said or done on its day is read in it (``asks_what_was_said``): with its days and entries
taken out, what is left must be, whole, one of a few forms of such a question or request ("聊了什么",
"帮我看看做了哪些工作", "总结一下", "有什么进展", "what did we talk about").  Anything else is recalled as if it named
no day: a question about a subject ("9月2日发布的 3.4.2 修了什么", "继续昨天的任务"), which read in its day would lose
the answer said on another day, the current task and the claims that answered it, and a message that only mentions a day
("今天在吗", "我今天在忙呢", "今晚做什么菜"), which would lose the owner's preferences and task.  A bag of words would
let such messages through; a whole form does not, and a question it misses is recalled as if it named no day.  A range
of days, more than three, a day still to come and a placeholder such as 9999-12-31 are no scope either.

A day is a calendar day in the zone the asking host shows its model (``SearchContext.zone``; when the host names
none, the serving machine's, with that day's daylight-saving offset), the zone its recalled times are rendered in,
so "29日" means the same day in the question and in the answer.

A question may ask for what held before instead of naming a time ("What port did Granite use before?", "Granite
以前用的哪个端口？", ``asks_what_held_before``).  Like a question that names a time, it keeps the order recall ranked:
a message whose value was replaced since is not moved behind the value that replaced it
(``RetrievalPipeline._behind_replacements``), since the replaced value may be its answer.
"""

from __future__ import annotations

import calendar
import itertools
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Mapping

from .language_resources import language_rules

#: Days, or spans of days, one question may name.  More is a list, which neither a scope of whole days nor a ranking
#: by time serves.
MAX_SCOPE_DAYS = 3
#: How far back a month and day without a year may reach: "12月25日" asked in January is the last one, "10月1日"
#: asked on 9月30日 is a day with no conversation yet.  A month without a year reaches as far back from its last day:
#: "10月" asked in September is the October to come.
_MONTH_DAY_REACH = timedelta(days=183)
#: The earliest day read, and where a span before a named time begins: an older date is a placeholder or an example
#: ("0001-01-01"), and a day before this one cannot be turned into a time the store holds on every machine.  Windows
#: places no instant before 1970 in the machine's zone, and finding a midnight's offset looks a day earlier: east of
#: UTC, 2 January 1970 already fails.
_EARLIEST = date(1970, 1, 3)
#: Longest message read for its days and entries: a day's question is its days, its entries and a rest of at most
#: ``_QUESTION_CHARS``.  Every entry's name was looked for in the whole message, and a log naming instances hundreds
#: of times took a tenth of a second before the rest was found too long.
_MESSAGE_CHARS = 512
#: Places a message may name entries in before it is a list or a log.
_MAX_ENTRY_MENTIONS = 16

#: The forms a day's question may take once its days and entries are out, whole: who asks or requests ("帮我看看",
#: "我们"), a time of day, an adverb ("都", "主要"), then what was said or done, completed ("聊了什么", "做了哪些工作",
#: "改了什么", "聊什么了", "有什么进展", "进展如何", "总结一下", "的聊天记录").  Curated and strict: a message with
#: anything more in it ("做什么饭", "继续做的", "忙吗", "在忙呢", "怎么办"), one asking what to do rather than what was
#: done ("今天做什么", "今天聊点什么", "今天可以做什么"), or where the work stopped ("昨天聊到哪了", "where did we leave
#: off"), which the current task answers and the whole day's spread would not, is no question about its day.
_REQUEST = (
    r"(?:帮我|帮忙|告诉我|给我|跟我|和我|说说|讲讲|列一下|列出|问一下|查查|查一下|看看|看一下|总结|回顾|汇总|复盘|"
    r"梳理|盘点)"
)
#: "我" before a request says what the person will do ("我今天总结一下"), not what they ask for.
_ASKER = (
    rf"(?:{_REQUEST}|(?:能不能|可不可以|可以)(?={_REQUEST})|请问|请|麻烦|想知道|你们|你|我们|咱们|大家|我(?!{_REQUEST})|"
    r"和你|跟你|在|的|对话|聊天|会话|工作)"
)
#: A clock time has one reading: "5:00", "3点", "3点15分".  Read two ways ("5:00:00" as "5:00" and "0:00"), a list
#: of twenty times took seconds to reject and doubled with each more.
_CLOCK = r"\d{1,2}(?::\d{2}|点(?:\d{1,2}分?)?)(?!\d)"
#: A part of the day with its time, or alone and followed by no digit: read both as one time and as "上午" then
#: "1点", "上午1点" would double the work with each more.
_TIME_OF_DAY = (
    rf"(?:(?:上午|下午|中午|晚上|早上|凌晨|夜里|傍晚|半夜)(?:{_CLOCK}|(?!\d))|{_CLOCK})"
    r"(?:左右|前后|之前|之后|以后|以前)?"
)
_ADVERB = r"(?:都|主要|一共|具体|大概|总共|分别|又|还|一起|到底|究竟)"
_WHAT = r"(?:什么|啥|哪些|哪儿|哪里|哪)"
_THINGS = r"(?:事|事情|工作|问题|内容|话题|东西|方面|活儿|活)"
_DONE = r"(?:聊|说|讲|谈|讨论|做|干|忙|弄|搞|处理|完成|发生|交流|沟通|改|修|修复|提交|写|实现|部署|上线|测试|更新|发布)"
_DAY_QUESTION = re.compile(
    rf"(?:{_ASKER}|{_TIME_OF_DAY})*{_ADVERB}*(?:"
    rf"{_DONE}(?:了|过|的)(?:些|点)?{_WHAT}{_THINGS}?"
    rf"|{_DONE}{_WHAT}{_THINGS}?了"
    rf"|干嘛了"
    rf"|有{_WHAT}新?(?:进展|进度|动静|变化|收获|结果)"
    rf"|的?(?:进展|进度)(?:如何|怎么样|怎样)?"
    rf"|(?:总结|回顾|汇总|复盘|梳理|盘点)(?:一下|下)?(?:的?(?:对话|聊天|工作|进展|内容|事情))?"
    rf"|的?(?:聊天|对话|会话)(?:记录|内容)?"
    rf")(?:呢|吗)?"
)
#: English asks what was done with an auxiliary ("what did we do", "what have you been working on") or in the past
#: ("what we did"): "what we do today" and "What I do today" ask what to do.
_DAY_QUESTION_EN = re.compile(
    r"(?:(?:please|can you|could you|tell me|show me|let me know|give me)\s+)*(?:"
    r"what\s+(?:did|have|has|were|was)\s+(?:(?:we|you|i|they)\s+)?(?:been\s+)?"
    r"(?:talk(?:ed)?\s+about|discuss(?:ed)?|do|done|doing|work(?:ed|ing)?\s+on|decided?|say|said|chat(?:ted)?\s+about|"
    r"get\s+done|changed?|fix(?:ed)?)"
    r"|what\s+(?:(?:we|you|i|they)\s+)?(?:talked\s+about|discussed|did|worked\s+on|decided|said|chatted\s+about|"
    r"got\s+done|changed|fixed)"
    r"|what\s+happened|what\s+was\s+(?:said|done|discussed)"
    r"|(?:summarize|summarise|recap|summary)(?:\s+(?:of\s+)?(?:the\s+)?(?:day|conversation|chat|discussion|work))?"
    r"|(?:any\s+)?(?:updates|progress)"
    r")(?:\s+(?:on|from|of|in|at|so\s+far))?"
)
#: Longest rest a day's question has: a longer one is a message that also says something, and a long run of a
#: character the trailing strip scans would make that strip quadratic.
_QUESTION_CHARS = 64
_TRAILING = re.compile(r"[\s?？。.!！~～…,，、]+$")
#: Acknowledgements, as whole words: "好的，继续吧", "OK 继续执行", "按你说的做", "确认", "下一步", "可以，就按这个来",
#: "知道了", "搞定"; with only the fillers around them.  What is left after them says something: "错了", "我没说",
#: "是你说的", "我到了", "中午吃了面".  Latin words: acknowledgements ("lgtm", "sounds good", "do it"), a single letter
#: or a number of one or two digits (an option chosen); a longer number ("8080", "3.4.6"), any other word, and any
#: letter of another script say something.
_ACK_PHRASES = tuple(
    sorted(
        (
            "按你说的做",
            "就按这个来",
            "就这样",
            "没问题",
            "没错",
            "知道了",
            "收到",
            "明白",
            "了解",
            "感谢",
            "多谢",
            "谢谢",
            "搞定",
            "不错",
            "很好",
            "太好了",
            "好的",
            "好嘞",
            "好滴",
            "好哒",
            "可以",
            "继续",
            "接着",
            "开始",
            "下一步",
            "确认",
            "同意",
            "批准",
            "允许",
            "辛苦了",
            "辛苦",
            "推进",
            "执行",
            "照做",
            "行",
            "好",
            "嗯",
            "哦",
            "噢",
            "喔",
            "赞",
            "对",
        ),
        key=len,
        reverse=True,
    )
)
_ACK_FILLER = frozenset("吧呀啊嘞滴哒啦嘛哟呢了的你我这那个就们哈")
_ACK_WORDS = frozenset(
    {
        "ok",
        "okay",
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "go",
        "ahead",
        "on",
        "thanks",
        "thank",
        "you",
        "thx",
        "ty",
        "np",
        "continue",
        "proceed",
        "fine",
        "good",
        "great",
        "cool",
        "nice",
        "perfect",
        "right",
        "got",
        "it",
        "lgtm",
        "sounds",
        "looks",
        "keep",
        "going",
        "done",
        "next",
        "alright",
        "all",
        "noted",
        "please",
        "do",
        "understood",
        "agreed",
        "makes",
        "sense",
        "that",
        "works",
    }
)
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f]")

_TIME_RULES = language_rules("query_time")
_ISO_DATE = _TIME_RULES["_ISO_DATE"]
_CN_DATE = _TIME_RULES["_CN_DATE"]
_MONTH_DAY = _TIME_RULES["_MONTH_DAY"]
_EN_MONTH_NAMES = _TIME_RULES["_EN_MONTH_NAMES"]
_EN_MONTH = _TIME_RULES["_EN_MONTH"]
_EN_MONTHS = _TIME_RULES["_EN_MONTHS"]
_EN_MONTH_DAY = _TIME_RULES["_EN_MONTH_DAY"]
_EN_DAY_MONTH = _TIME_RULES["_EN_DAY_MONTH"]
_EN_ORDINAL_DAY = _TIME_RULES["_EN_ORDINAL_DAY"]
_RANGE_TAIL = _TIME_RULES["_RANGE_TAIL"]
_EN_RANGE_TAIL = _TIME_RULES["_EN_RANGE_TAIL"]
_DATE_FORMS = _TIME_RULES["_DATE_FORMS"]
_MONTH = _TIME_RULES["_MONTH"]
_EN_MONTH_ALONE = _TIME_RULES["_EN_MONTH_ALONE"]
_RANGE = _TIME_RULES["_RANGE"]
_JOINER = _TIME_RULES["_JOINER"]
_LEADS = _TIME_RULES["_LEADS"]
_TRAILS = _TIME_RULES["_TRAILS"]
_BEFORE = _TIME_RULES["_BEFORE"]
_SINCE = _TIME_RULES["_SINCE"]
_BEFORE_ZH = _TIME_RULES["_BEFORE_ZH"]
_SINCE_ZH = _TIME_RULES["_SINCE_ZH"]
_HELD_BEFORE_EN = _TIME_RULES["_HELD_BEFORE_EN"]
_BEFORE_WORD_ZH = _TIME_RULES["_BEFORE_WORD_ZH"]
_HELD_BEFORE_ZH = _TIME_RULES["_HELD_BEFORE_ZH"]
_RELATIVE = _TIME_RULES["_RELATIVE"]
_NOT_A_DAY_AFTER = _TIME_RULES["_NOT_A_DAY_AFTER"]
_MAX_DAY_MENTIONS = _TIME_RULES["_MAX_DAY_MENTIONS"]
_RELATIVE_EN = _TIME_RULES["_RELATIVE_EN"]
_EN_PARTS_OF_DAY = _TIME_RULES["_EN_PARTS_OF_DAY"]
_PERIODS = _TIME_RULES["_PERIODS"]
#: Characters an entry's name may be written with or without between its own: spaces, and "的" ("工作机的 Codex").
_NAME_GAP = r"[\s的]*"


@dataclass(frozen=True)
class QueryScope:
    """Named days as UTC ``[start, end)`` bounds in the store's time format, the named entries (none: any entry),
    and what the question asks once they are taken out of it."""

    windows: tuple[tuple[str, str], ...]
    entry_ids: tuple[str, ...]
    rest: str


@dataclass(frozen=True)
class QueryTime:
    """The time a question names as UTC ``[start, end)`` bounds in the store's time format, and what the question
    asks once that time is taken out of it."""

    windows: tuple[tuple[str, str], ...]
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


@dataclass(frozen=True)
class _Named:
    """Where a text names a time, and the whole days it spans, ``first`` to ``last``: None where no calendar holds
    the day or none is read for it (a month and day more than half a year back).  ``day`` when it names one day."""

    span: tuple[int, int]
    first: date | None
    last: date | None
    day: bool


def _calendar_day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _latest(month: int, day: int, today: date) -> date | None:
    """A month and day without a year: the latest one not in the future, within ``_MONTH_DAY_REACH``."""
    named = _calendar_day(today.year, month, day)
    if named is None or named <= today:
        return named
    named = _calendar_day(today.year - 1, month, day)
    return named if named is not None and today - named <= _MONTH_DAY_REACH else None


def _month_number(name: str) -> int:
    return int(name) if name.isdigit() else _EN_MONTHS.index(name[:3].casefold()) + 1


def _day_of(match: re.Match[str], today: date) -> date | None:
    """The day a date names: in the year it names, else the latest one (``_latest``).  A day of the month alone ("on
    the 18th") is this month's once it has come, else that of the latest month that holds it."""
    named = match.groupdict()
    day = int(named["day"])
    if named.get("month") is None:
        year, month = today.year, today.month
        for _month_back in range(3):
            found = _calendar_day(year, month, day)
            if found is not None and found <= today:
                return found
            year, month = (year, month - 1) if month > 1 else (year - 1, 12)
        return None
    month = _month_number(named["month"])
    return _calendar_day(int(named["year"]), month, day) if named.get("year") else _latest(month, day, today)


def _range_of(first: date | None, tail: re.Match[str]) -> tuple[date | None, date | None]:
    """The days of a range written as its first day and the end after it ("9月28-30日", "9月28日到10月3日"), the end in
    the first's year and month unless it names its own.  An end naming a month before the first's is in the next year
    ("12月30日到1月2日"); one before its first in the same month is the same days written end first ("9月8日到9月6日"),
    since both are named, unless it names its day alone ("9月28-3日"), which may mean the next month and reads as none.
    An end no calendar holds is unread, and the range then ends today."""
    named = tail.groupdict()
    if first is None:
        return None, None
    month = int(named.get("month") or first.month)
    year = int(named.get("year") or first.year) + (0 if named.get("year") or month >= first.month else 1)
    end = _calendar_day(year, month, int(named["day"]))
    if end is None or end >= first:
        return first, end
    return (end, first) if named.get("month") or named.get("year") else (None, None)


def _month_span(match: re.Match[str], today: date) -> tuple[date | None, date | None]:
    """The first and last days of a month named without a day: of the year it names, else the latest one begun, as
    long as it ended within ``_MONTH_DAY_REACH``."""
    named = match.groupdict()
    month = _month_number(named["month"])
    if not 1 <= month <= 12:
        return None, None
    year = int(named["year"]) if named.get("year") else today.year if month <= today.month else today.year - 1
    first = _calendar_day(year, month, 1)
    if first is None:
        return None, None
    last = first.replace(day=calendar.monthrange(year, month)[1])
    if not named.get("year") and month > today.month and today - last > _MONTH_DAY_REACH:
        return None, None
    return first, last


def _period(name: str, today: date) -> tuple[date, date]:
    """The days a word of ``_PERIODS`` names, back from today."""
    monday = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    return {
        "last_week": (monday - timedelta(days=7), monday - timedelta(days=1)),
        "this_week": (monday, today),
        "last_month": ((month - timedelta(days=1)).replace(day=1), month - timedelta(days=1)),
        "this_month": (month, today),
    }[name]


def _dates(text: str, today: date, *, scope: bool) -> Iterator[_Named]:
    """The days the text writes as dates (``_DATE_FORMS``), each with the end of a range written after it; with
    ``scope``, also a date a Latin word runs into (``_ISO_DATE``)."""
    for pattern, tail in _DATE_FORMS:
        for match in pattern.finditer(text):
            before = text[match.start() - 1 : match.start()]
            if pattern is _ISO_DATE and before.isascii() and before.isalpha() and not scope:
                continue
            first = _day_of(match, today)
            end = tail.match(text, match.end()) if tail is not None else None
            if end is None:
                yield _Named(match.span(), first, first, True)
            else:
                yield _Named((match.start(), end.end()), *_range_of(first, end), False)


def _words(text: str, today: date, *, scope: bool) -> Iterator[_Named]:
    """The days and spans of days the text names in words: today, yesterday and the days before ("前天"), a month
    ("9月", "in September"), and the words of ``_PERIODS``; with ``scope``, also a part of a day (``_RELATIVE``)."""
    for word, back, part in _RELATIVE:
        start = text.find(word) if scope or not part else -1
        while start >= 0:
            if not (start and text[start - 1] in _NOT_A_DAY_AFTER):
                day = today - timedelta(days=back)
                yield _Named((start, start + len(word)), day, day, True)
            start = text.find(word, start + len(word))
    for match in _RELATIVE_EN.finditer(text):
        word = match.group(1).lower()
        if scope or word not in _EN_PARTS_OF_DAY:
            day = today - timedelta(days=1 if word in {"yesterday", "last night"} else 0)
            yield _Named(match.span(), day, day, True)
    for pattern in (_MONTH, _EN_MONTH_ALONE):
        for match in pattern.finditer(text):
            yield _Named(match.span(), *_month_span(match, today), False)
    for name, pattern in _PERIODS:
        for match in pattern.finditer(text):
            yield _Named(match.span(), *_period(name, today), False)


def _mentions(text: str, today: date, *, scope: bool = False) -> list[_Named]:
    """Every time the text names, in its order: what ``_dates`` reads, then what ``_words`` reads of the rest, with
    ``scope`` what only the scope reads as well.  More than ``_MAX_DAY_MENTIONS`` is a log (``_NoScope``)."""
    found: list[_Named] = []
    for named in itertools.chain(_dates(text, today, scope=scope), _words(text, today, scope=scope)):
        if any(named.span[0] < other.span[1] and other.span[0] < named.span[1] for other in found):
            continue
        found.append(named)
        if len(found) > _MAX_DAY_MENTIONS:
            # A log that repeats "今天" thousands of times would cost a quadratic scan of its mentions.
            raise _NoScope
    return sorted(found, key=lambda named: named.span)


def _joined(text: str, named: list[_Named]) -> list[_Named]:
    """``named`` with each two a range joins made one (``_RANGE``, ``_JOINER``): "9月28日到9月30日", "from Sep 28 to
    Oct 2", "between Sep 6 and Sep 8", "9月6日和9月8日之间".  Two written end first ("from Sep 8 to Sep 6") are the same
    days."""
    joined: list[_Named] = []
    for item in named:
        left = joined[-1] if joined else None
        if left is not None:
            gap = text[left.span[1] : item.span[0]]
            listed = _JOINER.fullmatch(gap) and (
                _LEADS.search(text, 0, left.span[0]) or _TRAILS.match(text, item.span[1])
            )
            if _RANGE.fullmatch(gap) or listed:
                backwards = left.first is not None and item.last is not None and item.last < left.first
                first, last = (item.first, left.last) if backwards else (left.first, item.last)
                joined[-1] = _Named((left.span[0], item.span[1]), first, last, False)
                continue
        joined.append(item)
    return joined


def _bounded(text: str, named: _Named, today: date) -> _Named:
    """A named time with the words that go with it (``_LEADS``, ``_TRAILS``) and, after one that makes it one end of
    a span, the days before it or those from it to today (``_BEFORE``, ``_SINCE``)."""
    lead, trail = _LEADS.search(text, 0, named.span[0]), _TRAILS.match(text, named.span[1])
    start, end = lead.start() if lead else named.span[0], trail.end() if trail else named.span[1]
    before = _BEFORE.search(text, 0, start) or _BEFORE_ZH.match(text, end)
    since = None if before else _SINCE.search(text, 0, start) or _SINCE_ZH.match(text, end)
    word = before or since
    if word is None:
        return _Named((start, end), named.first, named.last, named.day)
    span = (min(start, word.start()), max(end, word.end()))
    if since is not None:
        return _Named(span, named.first, today, False)
    if named.first is None or named.first <= _EARLIEST:
        return _Named(span, None, None, False)
    return _Named(span, _EARLIEST, named.first - timedelta(days=1), False)


def _named_days(text: str, today: date) -> tuple[list[date], list[tuple[int, int]]] | None:
    """The days the text names that can hold a conversation, and where it names them, parts of days and dates an
    entry's name runs into included (``_mentions``); None when it names a range or another span of days, or more than
    three days, counting those that hold none."""
    named = _mentions(text, today, scope=True)
    if not all(item.day for item in named) or len(_joined(text, named)) < len(named):
        return None
    if len({item.first or item.span for item in named}) > MAX_SCOPE_DAYS:
        return None
    days = [day for item in named if (day := item.first) is not None and _EARLIEST <= day <= today]
    return list(dict.fromkeys(days)), [item.span for item in named]


def _name_pattern(letters: str) -> re.Pattern[str]:
    """A Latin name is a word of its own: "desk" is no entry in "Claude Desktop", nor "codex" in "codexbar" or
    "codex2".  Digits that begin a date may follow it: "Claude Code9月17日做了哪些工作" names the entry."""
    head = r"(?<![A-Za-z0-9])" if letters[0].isascii() and letters[0].isalnum() else ""
    # One digit and a look at what follows the run, not every length of it: a name before 8,000 digits took a
    # fifth of a second.
    tail = r"(?![A-Za-z])(?!\d(?![\d\s]*[年月日号/.\-]))" if letters[-1].isascii() and letters[-1].isalnum() else ""
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
            if len(found) > _MAX_ENTRY_MENTIONS:
                raise _NoScope
    chosen: list[tuple[int, int, str]] = []
    for start, end, entry_id in sorted(found, key=lambda item: (item[0] - item[1], item[0])):
        if all(end <= other_start or start >= other_end for other_start, other_end, _ in chosen):
            chosen.append((start, end, entry_id))
    ids = list(dict.fromkeys(entry_id for _, _, entry_id in sorted(chosen)))
    return ids, [(start, end) for start, end, _ in chosen]


class _NoScope(Exception):
    """A message that cannot be read as naming a few days."""


def asks_what_was_said(rest: str) -> bool:
    """Whether the rest of a day's question, its days and entries taken out, is whole one of the forms of a question or
    request about what was said or done then (``_DAY_QUESTION``, ``_DAY_QUESTION_EN``)."""
    if len(rest) > _QUESTION_CHARS:
        return False
    text = _TRAILING.sub("", unicodedata.normalize("NFKC", rest).casefold())
    if _DAY_QUESTION.fullmatch("".join(text.split())):
        return True
    return _DAY_QUESTION_EN.fullmatch(" ".join(text.split())) is not None


def asks_what_held_before(query: str) -> bool:
    """Whether ``query`` asks for what held before rather than what holds now (``_HELD_BEFORE_EN``,
    ``_HELD_BEFORE_ZH``), read in a message no longer than ``_MESSAGE_CHARS``: a value replaced since may be its
    answer."""
    text = unicodedata.normalize("NFKC", query)
    if len(text) > _MESSAGE_CHARS:
        return False
    return _HELD_BEFORE_EN.search(text) is not None or _HELD_BEFORE_ZH.search(text) is not None


def says_something(text: str) -> bool:
    """Whether a short message says anything of its day (``_ACK_PHRASES``, ``_ACK_WORDS``)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    if any(character.isalpha() and not character.isascii() and not _CJK.match(character) for character in folded):
        return True
    for word in re.findall(r"[a-z0-9.]+", folded):
        word = word.strip(".")
        if not word or (word.isdigit() and len(word) <= 2) or (word.isalpha() and len(word) == 1):
            continue
        if not word.isalpha() or word not in _ACK_WORDS:
            return True
    han = "".join(character for character in folded if _CJK.match(character))
    for phrase in _ACK_PHRASES:
        han = han.replace(phrase, "")
    return any(character not in _ACK_FILLER for character in han)


def _today(now: str, zone: tzinfo | None) -> date:
    """The day it is at ``now``, the recall's clock (UTC ISO), in ``zone``; with none, on this machine."""
    moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (moment.astimezone(zone) if zone is not None else moment.astimezone()).date()


def query_time(query: str, *, now: str, zone: tzinfo | None) -> QueryTime | None:
    """The time ``query`` names: runs of whole days in ``zone``, read as ``query_scope`` reads its days, that end
    today at the latest; a range or span ends today when its end cannot be read.

    None when it names no time, names more than ``MAX_SCOPE_DAYS`` or only days still to come, or is longer than
    ``_MESSAGE_CHARS``.
    """
    text = unicodedata.normalize("NFKC", query)
    if len(text) > _MESSAGE_CHARS:
        return None
    today = _today(now, zone)
    try:
        named = [_bounded(text, item, today) for item in _joined(text, _mentions(text, today))]
    except _NoScope:
        return None
    if len(named) > MAX_SCOPE_DAYS:
        return None
    days: list[tuple[date, date]] = []
    for item in named:
        if item.first is None:
            continue
        first, last = max(item.first, _EARLIEST), min(item.last or today, today)
        if first <= last and (first, last) not in days:
            days.append((first, last))
    if not days:
        return None
    rest = text
    for item in reversed(named):
        rest = rest[: item.span[0]] + " " + rest[item.span[1] :]
    windows = tuple((_day_window(first, zone)[0], _day_window(last, zone)[1]) for first, last in days)
    return QueryTime(windows, " ".join(rest.split()))


def query_scope(query: str, *, now: str, zone: tzinfo | None, entries: Mapping[str, str]) -> QueryScope | None:
    """The days and entries ``query`` names, or None when it names no day that can be read as one.

    ``now`` is the recall's clock (UTC ISO); ``entries`` maps a shared store's entry ids to their display names
    (empty in a store of one host).  An entry named without a day is no scope.  Whether the rest asks what was said
    is the caller's to judge (``asks_what_was_said``).  A message longer than ``_MESSAGE_CHARS`` names no scope.
    """
    text = unicodedata.normalize("NFKC", query)
    if len(text) > _MESSAGE_CHARS:
        return None
    today = _today(now, zone)
    try:
        named = _named_days(text, today)
        if named is None or not named[0]:
            return None
        days, day_spans = named
        entry_ids, entry_spans = _named_entries(text, entries)
    except _NoScope:
        return None
    # What joins the named days and entries goes with them, between entries as between days: "9月28日和9月29日聊了什么"
    # asks what was said on both, and "昨天pc1和pc2聊了什么" what those two entries said.
    ordered = sorted([*day_spans, *entry_spans])
    joiners = [
        (end, start)
        for (_, end), (start, _) in zip(ordered, ordered[1:])
        if end < start and _JOINER.fullmatch(text[end:start])
    ]
    rest = text
    for start, end in sorted([*day_spans, *entry_spans, *joiners], reverse=True):
        rest = rest[:start] + " " + rest[end:]
    return QueryScope(tuple(_day_window(day, zone) for day in days), tuple(entry_ids), " ".join(rest.split()))
