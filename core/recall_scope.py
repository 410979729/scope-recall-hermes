"""What a question names of when, and of which entry: a scope the recall can read directly.

"9月29日工作机 Claude Code 聊了什么" asks for one entry's conversation on one day.  Searched by its words, the
date and the entry's name matched nothing useful ("聊了", "工作", "29") and the recall answered from other days and
other entries: 4 of 106 such questions over two weeks of the shared store found a message of the named entry from
the named day (yuheng's audit of 3.4.2 and its baseline).  The scope is read here and nowhere else; the lexical,
vector and other channels are unchanged.

Only a question about what was said or done on its day is read in it (``asks_what_was_said``): with its days and
entries taken out, what is left must be, whole, one of a few forms of such a question or request ("聊了什么",
"帮我看看做了哪些工作", "总结一下", "有什么进展", "what did we talk about").  Anything else is recalled as if it
named no day: a question about a subject ("9月2日发布的 3.4.2 修了什么", "继续昨天的任务"), which read in its day
lost the answer said on another day, the current task and the claims that answered it, and a message that only
mentions a day ("今天在吗", "我今天在忙呢", "今晚做什么菜"), which lost the owner's preferences and task (reviews of
3.4.6).  A bag of words let such messages through round after round; a whole form does not, and a question it
misses is only recalled as before.  A range of days, more than three, a day still to come and a placeholder such as
9999-12-31 are no scope either.

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
#: Longest rest read for its words: a search refuses a longer text, and a prompt cut to the hosts' 8,192 characters
#: can grow past it when its "…" is normalised to "...".
_REST_CHARS = 8192

#: The forms a day's question may take once its days and entries are out, whole: who asks or requests ("帮我看看",
#: "我们"), a time of day, an adverb ("都", "主要"), then what was said, done or reached ("聊了什么", "做了哪些工作",
#: "聊到哪了", "有什么进展", "进展如何", "总结一下", "的聊天记录").  Curated and strict: a message with anything
#: more in it ("做什么饭", "继续做的", "忙吗", "在忙呢", "怎么办") is no question about its day.
_ASKER = (r"(?:帮我|帮忙|请问|请|麻烦|能不能|可不可以|可以|告诉我|给我|跟我|和我|看看|看一下|查查|查一下|说说|讲讲|"
          r"列一下|列出|问一下|想知道|我想知道|你们|你|我们|咱们|大家|在|的|对话|聊天|会话)")
_TIME_OF_DAY = (r"(?:(?:上午|下午|中午|晚上|早上|凌晨|夜里|傍晚|半夜)(?:\d{1,2}(?:点|:)\d{0,2}分?)?"
                r"|\d{1,2}(?:点|:)\d{0,2}分?)(?:左右|前后|之前|之后|以后|以前)?")
_ADVERB = r"(?:都|主要|一共|具体|大概|总共|分别|又|还|一起|到底|究竟)"
_WHAT = r"(?:什么|啥|哪些|哪儿|哪里|哪)"
_THINGS = r"(?:事|事情|工作|问题|内容|话题|东西|方面|活儿|活)"
_DAY_QUESTION = re.compile(
    rf"(?:{_ASKER}|{_TIME_OF_DAY})*{_ADVERB}*(?:"
    rf"(?:聊|说|讲|谈|讨论|做|干|忙|弄|搞|处理|完成|发生|交流|沟通)(?:了|过)?(?:些|点)?{_WHAT}{_THINGS}?"
    rf"|干嘛了?"
    rf"|(?:聊|说|做|进行)到{_WHAT}(?:一步|地方)?"
    rf"|有{_WHAT}新?(?:进展|进度|动静|变化|收获|结果)"
    rf"|的?(?:进展|进度)(?:如何|怎么样|怎样)?"
    rf"|(?:总结|回顾|汇总|复盘|梳理|盘点)(?:一下|下)?(?:的?(?:对话|聊天|工作|进展|内容|事情))?"
    rf"|的?(?:聊天|对话|会话)(?:记录|内容)?"
    rf")(?:了|呢|吗)?")
_DAY_QUESTION_EN = re.compile(
    r"(?:(?:please|can you|could you|tell me|show me|let me know|give me)\s+)*(?:"
    r"what\s+(?:did|have|were|was|are|do)\s+(?:we|you|i|they)\s+(?:talk(?:ed)?\s+about|discuss(?:ed)?|do|done|doing|"
    r"work(?:ed|ing)?\s+on|decided?|say|said|chat(?:ted)?\s+about|get\s+done)"
    r"|what\s+happened|what\s+was\s+(?:said|done|discussed)"
    r"|(?:summarize|summarise|recap)(?:\s+(?:the\s+)?(?:day|conversation|chat|discussion|work))?"
    r"|(?:any\s+)?(?:updates|progress)"
    r")(?:\s+(?:on|from|of|in|at|so\s+far))?")
_TRAILING = re.compile(r"[\s?？。.!！~～…,，、]+$")
#: What an acknowledgement is made of: "好的，继续吧", "OK 继续执行", "按你说的做", "确认", "下一步", "可以，就按这个来";
#: and in Latin letters "lgtm", "sounds good", a single letter or a number.  Any other character says something:
#: "错了", "别做了", "中午吃了面".
_ACK_CHARACTERS = frozenset("好行嗯哦噢喔哈嘞滴哒啦嘛吧呀啊哟呢可以收到明白了解懂谢按照执开始继续接着下一步确认同意批准允许"
                            "辛苦推进就这样那个来去的你我说做对是没问题稍等")
_ACK_WORDS = frozenset({"ok", "okay", "yes", "yeah", "yep", "yup", "sure", "go", "ahead", "on", "thanks", "thank", "you",
                        "thx", "ty", "np", "continue", "proceed", "fine", "good", "great", "cool", "nice", "perfect",
                        "right", "got", "it", "lgtm", "sounds", "looks", "keep", "going", "done", "next", "alright",
                        "all", "noted"})
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*[日号]?(?!\d)")
_MONTH_DAY = re.compile(r"(?<![\d.])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?(?![\d.])")
#: What joins two days into a range ("9月28日到9月30日", "2026-09-28 至 2026-09-30", "昨天到今天"), and a date
#: followed by the end of its range ("9月28-30日", "9月28日到30日").
_RANGE = re.compile(r"\s*(?:到|至|~|～|-|—|–)\s*")
_RANGE_TAIL = re.compile(r"\s*(?:到|至|~|～|-|—|–)\s*(?:\d{4}\s*[-/年]\s*)?(?:\d{1,2}\s*[-/月]\s*)?\d{1,2}(?![\d.])")
#: Days before today each relative word names.
_RELATIVE = (("大前天", 3), ("前天", 2), ("昨天", 1), ("昨日", 1), ("昨晚", 1), ("昨夜", 1), ("今天", 0),
             ("今日", 0), ("今早", 0), ("今晨", 0), ("今晚", 0), ("今夜", 0))
#: A character before which a relative word is part of another word: 如今 ("如今天下"), 往前 ("往前天数"), 之前,
#: 以前, 目前 ("目前天天都在做什么"), 至今.
_NOT_A_DAY_AFTER = frozenset("如往向提之以目至")
#: Places a message may name days in before it is a list or a log, not a question about a day.
_MAX_DAY_MENTIONS = 16
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
    """The days the text names that can hold a conversation, and where it names days; None for a range, or for
    more than three named days, counting those that hold none."""
    days: list[date] = []
    named: set[object] = set()
    spans: list[tuple[int, int]] = []

    def add(day: date | None, span: tuple[int, int]) -> None:
        spans.append(span)
        named.add(day if day is not None else span)
        if day is not None and _EARLIEST <= day <= today and day not in days:
            days.append(day)
        if len(spans) > _MAX_DAY_MENTIONS:
            # A log that repeats "今天" thousands of times cost a quadratic scan of its mentions (review of 3.4.6).
            raise _NoScope

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
            if not any(s <= start < e for s, e in spans) and not (start and text[start - 1] in _NOT_A_DAY_AFTER):
                add(today - timedelta(days=back), (start, start + len(word)))
            start = text.find(word, start + len(word))
    for match in _RELATIVE_EN.finditer(text):
        add(today - timedelta(days=1 if match.group(1).lower() == "yesterday" else 0), match.span())
    ordered = sorted(spans)
    if any(_RANGE.fullmatch(text[end:start]) for (_, end), (start, _) in zip(ordered, ordered[1:])):
        return None
    if len(named) > MAX_SCOPE_DAYS:
        return None
    return days, spans


def _name_pattern(letters: str) -> re.Pattern[str]:
    """A Latin name is a word of its own: "desk" is no entry in "Claude Desktop", nor "codex" in "codexbar" or
    "codex2".  Digits that begin a date may follow it: "Claude Code9月17日做了哪些工作" names the entry."""
    head = r"(?<![A-Za-z0-9])" if letters[0].isascii() and letters[0].isalnum() else ""
    tail = (r"(?![A-Za-z])(?!\d+(?![\d\s]*[年月日号/.\-]))" if letters[-1].isascii() and letters[-1].isalnum()
            else "")
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


class _NoScope(Exception):
    """A message that cannot be read as naming a few days."""


def asks_what_was_said(rest: str) -> bool:
    """Whether the rest of a day's question, its days and entries taken out, is whole one of the forms of a question or
    request about what was said or done then (``_DAY_QUESTION``, ``_DAY_QUESTION_EN``)."""
    text = _TRAILING.sub("", unicodedata.normalize("NFKC", rest).casefold())
    if _DAY_QUESTION.fullmatch("".join(text.split())):
        return True
    return _DAY_QUESTION_EN.fullmatch(" ".join(text.split())) is not None


def says_something(text: str) -> bool:
    """Whether a short message says anything of its day (``_ACK_CHARACTERS``, ``_ACK_WORDS``)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    if any(_CJK.match(character) and character not in _ACK_CHARACTERS for character in folded):
        return True
    return any(not word.isdigit() and len(word) > 1 and word not in _ACK_WORDS
               for word in re.findall(r"[a-z0-9]+", folded))


def query_scope(query: str, *, now: str, zone: tzinfo | None, entries: Mapping[str, str]) -> QueryScope | None:
    """The days and entries ``query`` names, or None when it names no day that can be read as one.

    ``now`` is the recall's clock (UTC ISO); ``entries`` maps a shared store's entry ids to their display names
    (empty in a store of one host).  An entry named without a day is no scope.  Whether the rest asks what was said
    is the caller's to judge (``asks_what_was_said``); ``rest`` is at most 8,192 characters.
    """
    text = unicodedata.normalize("NFKC", query)
    moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    today = (moment.astimezone(zone) if zone is not None else moment.astimezone()).date()
    try:
        named = _named_days(text, today)
    except _NoScope:
        return None
    if named is None:
        return None
    days, day_spans = named
    if not days:
        return None
    entry_ids, entry_spans = _named_entries(text, entries)
    rest = text
    for start, end in sorted([*day_spans, *entry_spans], reverse=True):
        rest = rest[:start] + " " + rest[end:]
    return QueryScope(tuple(_day_window(day, zone) for day in days), tuple(entry_ids),
                      " ".join(rest.split())[:_REST_CHARS])
