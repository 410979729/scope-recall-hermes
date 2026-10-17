"""The time a question names, read by rule (core/recall_scope.py ``query_time``).

Each form is read as the whole days it spans, up to today, and the question without it is what else it asks.  A
question that names no time has none, and recall ranks it as if the reading did not exist; so do a day still to
come, a placeholder date, a list of more than three times and a log.
"""

from datetime import UTC, date, timedelta, timezone

import pytest
from scope_recall.core.recall_scope import query_time

#: A Wednesday: this week began on Monday 28 September, last week ran from the 21st to the 27th.
NOW = "2026-09-30T09:30:00.000000Z"
NEW_YORK = timezone(timedelta(hours=-4))


def _window(first: str, last: str) -> tuple[str, str]:
    """The UTC bounds of the days ``first`` to ``last`` (ISO dates) in UTC."""
    end = date.fromisoformat(last) + timedelta(days=1)
    return f"{first}T00:00:00.000000Z", f"{end.isoformat()}T00:00:00.000000Z"


@pytest.mark.parametrize(
    ("query", "first", "last"),
    (
        ("2026-09-18 说了什么", "2026-09-18", "2026-09-18"),
        ("9月18日的发布怎么样了", "2026-09-18", "2026-09-18"),
        ("9月18号那天的发布", "2026-09-18", "2026-09-18"),
        ("What did I say about the release on September 18?", "2026-09-18", "2026-09-18"),
        ("Any news from Sept 18th, 2026?", "2026-09-18", "2026-09-18"),
        ("What was decided on the 18th of September?", "2026-09-18", "2026-09-18"),
        ("What did we decide on the 18th?", "2026-09-18", "2026-09-18"),
        ("昨天的发布", "2026-09-29", "2026-09-29"),
        ("What did we fix yesterday?", "2026-09-29", "2026-09-29"),
        ("9月6日到9月8日之间，发布有什么进展？", "2026-09-06", "2026-09-08"),
        ("从2026-09-06到2026-09-08，发布有什么进展？", "2026-09-06", "2026-09-08"),
        ("9月6日和9月8日之间的发布", "2026-09-06", "2026-09-08"),
        ("9月6-8日的发布", "2026-09-06", "2026-09-08"),
        ("What happened between September 6 and September 8?", "2026-09-06", "2026-09-08"),
        ("Catch me up from Sep 6 to Sep 8.", "2026-09-06", "2026-09-08"),
        ("What happened Sep 6-8?", "2026-09-06", "2026-09-08"),
        ("9月28日到10月3日的安排", "2026-09-28", "2026-09-30"),
        ("9月的发布", "2026-09-01", "2026-09-30"),
        ("2026年8月的账单", "2026-08-01", "2026-08-31"),
        ("What shipped in August?", "2026-08-01", "2026-08-31"),
        ("The August 2026 invoices", "2026-08-01", "2026-08-31"),
        ("上周发布有什么进展？", "2026-09-21", "2026-09-27"),
        ("上个星期的发布", "2026-09-21", "2026-09-27"),
        ("What did I say about the release last week?", "2026-09-21", "2026-09-27"),
        ("这周做了什么", "2026-09-28", "2026-09-30"),
        ("本周的发布", "2026-09-28", "2026-09-30"),
        ("What changed this week?", "2026-09-28", "2026-09-30"),
        ("上个月的账单", "2026-08-01", "2026-08-31"),
        ("What did we spend last month?", "2026-08-01", "2026-08-31"),
        ("本月的账单", "2026-09-01", "2026-09-30"),
        ("9月18日之前用的是哪个端口", "1970-01-03", "2026-09-17"),
        ("Which port did we use before September 18?", "1970-01-03", "2026-09-17"),
        ("What changed after September 18?", "2026-09-18", "2026-09-30"),
        ("上周以来有什么变化", "2026-09-21", "2026-09-30"),
        ("What changed since last week?", "2026-09-21", "2026-09-30"),
    ),
)
def test_each_form_is_read_as_the_days_it_spans(query, first, last):
    time = query_time(query, now=NOW, zone=UTC)
    assert time is not None and time.windows == (_window(first, last),), query


@pytest.mark.parametrize(
    ("query", "rest"),
    (
        ("What did I say about the Orchard release on September 18?", "What did I say about the Orchard release on ?"),
        ("从8月1号到8月3号，Vellum 的调价有什么进展？", ",Vellum 的调价有什么进展?"),
        ("9月24号那天，Meridian 的安全评审有什么消息？", ",Meridian 的安全评审有什么消息?"),
        (
            "What happened with the Teasel release between September 6 and September 8?",
            "What happened with the Teasel release ?",
        ),
        ("Which port did we use before September 18?", "Which port did we use ?"),
        ("上周以来 Orchard 有什么变化", "Orchard 有什么变化"),
    ),
)
def test_what_else_a_question_asks_is_it_without_its_time(query, rest):
    """The words that go with a time ("between", "from", "从", "之间", "那天", "before", "以来") go with it."""
    time = query_time(query, now=NOW, zone=UTC)
    assert time is not None and time.rest == rest


@pytest.mark.parametrize(
    ("query", "now", "first", "last"),
    (
        ("9月8日到9月6日的发布", NOW, "2026-09-06", "2026-09-08"),
        ("2026-09-08到2026-09-06的发布", NOW, "2026-09-06", "2026-09-08"),
        ("What happened from Sep 8 to Sep 6?", NOW, "2026-09-06", "2026-09-08"),
        ("9月28日到10月3日的安排", "2026-10-05T09:30:00.000000Z", "2026-09-28", "2026-10-03"),
        ("12月30日到1月2日的安排", "2027-01-05T09:30:00.000000Z", "2026-12-30", "2027-01-02"),
    ),
)
def test_a_range_is_the_days_between_its_ends(query, now, first, last):
    """An end in an earlier month than the first's is in the next year; an end written before its first in the same
    month names the same days end first, not the year up to the next 6 September."""
    time = query_time(query, now=now, zone=UTC)
    assert time is not None and time.windows == (_window(first, last),), query


def test_a_range_whose_end_names_a_day_before_its_first_alone_names_no_time():
    """ "9月28-3日" may end in the next month or be a slip: no window is read for it."""
    assert query_time("9月28-3日的安排", now=NOW, zone=UTC) is None


def test_two_named_days_are_two_windows():
    time = query_time("9月18日和9月20日的发布", now=NOW, zone=UTC)
    assert time is not None
    assert time.windows == (_window("2026-09-18", "2026-09-18"), _window("2026-09-20", "2026-09-20"))


@pytest.mark.parametrize(
    "query",
    (
        "What's the staging port for Orchard?",
        "May I ask what the port is?",
        "The top 10 may change.",
        "Is it on the 2nd floor?",
        "Mar 3.4 release notes",
        "What did we decide last time?",
        "When is the next release?",
        "Did we rotate the keys before or after we split the repo?",
        "3.4.2 修了什么",
        "3个月的计划",
        "上次说的方案",
        "马上周末了",
        "这周边有什么好吃的",
        "本周期的指标",
        "如今天下太平",
        "最近发布怎么样",
        "最近距离是多少",
        "What changed recently?",
        "What did we do the past few days?",
    ),
)
def test_a_question_that_names_no_time_has_none(query):
    """A month's name that is a word ("May I", "may change"), an ordinal that names no day, a version, a span of
    time that is no date ("3个月"), the characters of a time word inside another word (马上周末, 周边, 周期, 如今天下),
    and the last few days ("最近", "recently"), which are no span, name no time."""
    assert query_time(query, now=NOW, zone=UTC) is None


@pytest.mark.parametrize(
    "query",
    (
        "What's in release-2026-09-18?",
        "build_2026-09-18 failed again",
        "Open 2026-09-18.log for me",
        "id2026-09-18 是哪个",
        "v2.2026-09-18 是哪个版本",
        "2026-09-18-hotfix 修了什么",
        "2026-09-18T10:00:00Z 的告警是什么",
        "What should we cook tonight?",
        "What did we discuss this morning?",
        "What did we decide last night?",
        "今晚做什么菜",
        "今早聊了什么",
        "今晨的会",
        "今夜的部署",
        "昨晚说的那个方案",
        "昨夜的告警",
        "What is planned for tomorrow?",
        "明天的发布",
        "后天的会",
        "下周的计划",
        "What is planned next week?",
        "下个月的预算",
        "What is the budget for next month?",
    ),
)
def test_a_date_inside_an_identifier_a_part_of_a_day_or_a_day_to_come_names_no_time(query):
    """The time a question names is the past days a message could have been said on.  A date joined to an identifier
    or run into one is a name; a part of a day is often still to come ("今晚做什么菜"), and the scope alone reads it
    (``query_scope``); tomorrow and next week hold nothing yet."""
    assert query_time(query, now=NOW, zone=UTC) is None


@pytest.mark.parametrize(
    "query", ("We shipped on 2026-09-18.", "那次发布（2026-09-18）", "2026-09-18聊了什么", "2026/09/18 的发布")
)
def test_a_date_between_marks_or_words_is_read(query):
    time = query_time(query, now=NOW, zone=UTC)
    assert time is not None and time.windows == (_window("2026-09-18", "2026-09-18"),), query


@pytest.mark.parametrize(
    "query",
    (
        "10月1日的会",
        "valid_to 的默认值 9999-12-31 是什么意思",
        "0001-01-01 是什么意思",
        "9月1日、9月2日、9月3日和9月4日的发布",
        "今天" * 20 + "的发布",
        "昨天的发布" + "很长的日志" * 120,
    ),
)
def test_a_day_to_come_a_placeholder_a_list_or_a_log_names_no_time(query):
    """A month and day more than half a year back is the one to come, which holds nothing yet; a placeholder date is
    no day a store holds; more than three times is a list; a message naming times more than sixteen times, or longer
    than a question, is a log."""
    assert query_time(query, now=NOW, zone=UTC) is None


def test_the_time_is_read_in_the_zone_the_host_shows_its_model():
    """At 22:00 on 29 September in New York it is already 30 September in UTC: "yesterday" is the 28th there."""
    time = query_time("What did we fix yesterday?", now="2026-09-30T02:00:00.000000Z", zone=NEW_YORK)
    assert time is not None
    assert time.windows == (("2026-09-28T04:00:00.000000Z", "2026-09-29T04:00:00.000000Z"),)
