"""A subject may be stated beside its quote, but only if it names something.

Covers ``core/subject_binding.py`` and the second rung it adds to the subject
gate in ``core/claims.qualify``.  The asymmetry is the point: widening must let
through subjects that are genuinely in the evidence and must not let through
anything invented, pointed at, or clause-shaped.
"""
from __future__ import annotations

import pytest

from scope_recall.core.claims import RootEvidence, qualify
from scope_recall.core.subject_binding import (
    is_deictic,
    names_a_thing,
    neighbourhood_binds,
    quote_neighbourhood,
    subject_binding,
)


# --------------------------------------------------------------------------
# The rungs
# --------------------------------------------------------------------------

def test_rung_one_is_todays_rule():
    assert subject_binding("Kimi K3", quote="Kimi K3 上下文 262144", content="…") == "quote"


def test_rung_two_reaches_the_sentence_before_the_quote():
    content = "此测试项目的名称为 SRLIVE-de5a0ba54dbe。请保留这条来源，不要写业务文件。"
    assert subject_binding("此测试项目", quote="不要写业务文件", content=content) == "neighbourhood"


def test_rung_two_does_not_reach_across_a_document():
    """Two sentences of context, not the whole source."""
    content = "the agent is described here。" + "填充句。" * 40 + "DO NOT edit these。"
    assert subject_binding("described here", quote="DO NOT edit these", content=content) is None


def test_a_quote_that_is_not_in_the_content_widens_nothing():
    assert quote_neighbourhood("some stored text", "a quote from elsewhere") == ""
    assert subject_binding("subject", quote="a quote from elsewhere", content="subject is here") is None


@pytest.mark.parametrize("subject", ["you", "You", "the agent", "The User", "用户", "他们", "we"])
def test_a_pointer_is_never_a_subject_at_any_distance(subject):
    content = f"{subject} 在这里被提到。这条引文紧挨着它。"
    assert is_deictic(subject) is True
    assert names_a_thing(subject) is False
    assert subject_binding(subject, quote="这条引文紧挨着它", content=content) is None


@pytest.mark.parametrize("subject", [
    "If the session ran smoothly with no corrections",
    "When the queue drains",
    "unless the worker is running",
    "如果会话顺利结束",
    "除非工作进程还在跑",
])
def test_a_clause_is_never_a_subject(subject):
    content = f"{subject}，就直接停下。"
    assert names_a_thing(subject) is False
    assert subject_binding(subject, quote="就直接停下", content=content) is None


@pytest.mark.parametrize("subject", [
    "Kimi K3", "windows-ocr.ps1", "此测试项目", "跨实例记忆继承（整库灌入）的治理",
    "the user's laptop", "用户手册",
])
def test_something_that_names_a_thing_is_allowed_to_reach_rung_two(subject):
    assert names_a_thing(subject) is True


def test_rung_one_still_wins_for_a_pointer_inside_its_own_quote():
    """Narrowing rung two must not narrow rung one."""
    assert subject_binding("you", quote="you must not edit these", content="") == "quote"


def test_neighbourhood_binds_reports_only_the_second_rung():
    pairs = [("Kimi K3 上下文 262144", "Kimi K3 上下文 262144")]
    assert neighbourhood_binds("Kimi K3", pairs) is False, "rung one is not this function's business"
    pairs = [("不要写业务文件", "此测试项目的名称为 X。不要写业务文件。")]
    assert neighbourhood_binds("此测试项目", pairs) is True


@pytest.mark.parametrize("bad", [None, 17, "", "   "])
def test_a_subject_that_is_not_text_binds_nothing(bad):
    assert subject_binding(bad, quote="anything", content="anything") is None
    assert names_a_thing(bad) is False


# --------------------------------------------------------------------------
# Through the real gate
# --------------------------------------------------------------------------

def _qualify(text, *, subject, quote, value="262144", predicate="上下文窗口", kind="fact"):
    """Same shape the sibling claim-semantics tests use: one root, one span."""
    root = RootEvidence("TEST-root", 1, "human_direct", None, text,
                        "2026-09-06T12:00:00Z", "complete", "TEST-session",
                        source_principal={"kind": "human", "resolution": "verified",
                                          "principal_ref": "principal:TEST-owner"})
    proposal = dict(kind=kind, subject=subject, predicate=predicate, value_text=value,
                    conditions=[], statement_kind="assertion",
                    valid_from=root.occurred_at, valid_to=None,
                    evidence_spans=[dict(source_ref=root.ref, source_revision=1, quote=quote)])
    return qualify(proposal, (root,))


_SOURCE = "TEST-instrument 的型号是 TEST-K3。它的上下文窗口为 262144。"
_QUOTE = "它的上下文窗口为 262144"


def test_a_subject_from_the_neighbouring_sentence_now_qualifies():
    verdict = _qualify(_SOURCE, subject="TEST-instrument", quote=_QUOTE)
    assert verdict.reason != "subject_not_bound"


def test_an_invented_subject_still_does_not_qualify():
    """The 37 live cases whose subject is nowhere in the source must stay refused."""
    verdict = _qualify(_SOURCE, subject="memory configuration", quote=_QUOTE)
    assert verdict.state == "proposed" and verdict.reason == "subject_not_bound"


def test_a_pointer_subject_beside_its_quote_still_does_not_qualify():
    text = "the agent 在这段说明里出现过。上下文窗口为 262144。"
    verdict = _qualify(text, subject="the agent", quote="上下文窗口为 262144")
    assert verdict.state == "proposed" and verdict.reason == "subject_not_bound"


def test_a_clause_subject_beside_its_quote_still_does_not_qualify():
    text = "If the session ran smoothly。上下文窗口为 262144。"
    verdict = _qualify(text, subject="If the session ran smoothly", quote="上下文窗口为 262144")
    assert verdict.state == "proposed" and verdict.reason == "subject_not_bound"


def test_a_subject_two_sentences_away_is_still_refused():
    """One sentence of context, not the whole source."""
    text = "TEST-instrument 是一台仪器。中间这句与它无关。另一句也无关。上下文窗口为 262144。"
    verdict = _qualify(text, subject="TEST-instrument", quote="上下文窗口为 262144")
    assert verdict.state == "proposed" and verdict.reason == "subject_not_bound"


# --------------------------------------------------------------------------
# A quote that already ends a sentence must not read the next one
# --------------------------------------------------------------------------

def test_a_quote_ending_a_sentence_does_not_read_the_next_sentence():
    """Found on alpha: a question in the *following* sentence vetoed a claim.

    The source said, in so many words, that the question was not a
    confirmation -- and the question gate refused the assertion anyway, because
    ``assertion_clause`` searched for the next terminator starting after a
    quote whose own last character already was one.  ``evidence_context`` has
    always had this guard; this asserts the other one does too.
    """
    from scope_recall.core.claims import assertion_clause

    source = ("项目【TEST-青岚】内部技术总结应使用银色，对外版仍是绿色。"
              "我只是问“是不是应该默认红色？”，这不是确认。")
    quote = "项目【TEST-青岚】内部技术总结应使用银色，对外版仍是绿色。"
    assert assertion_clause(source, quote) == quote


def test_a_mid_sentence_quote_still_reaches_its_own_terminator():
    """Narrowing the end must not stop a partial quote seeing its own clause."""
    from scope_recall.core.claims import assertion_clause

    assert assertion_clause("前半句在这里，后半句结束。下一句。", "后半句结束") == "前半句在这里，后半句结束。"


def test_an_escaped_break_still_ends_a_clause():
    """Tool envelopes have no real punctuation; that path must be untouched."""
    from scope_recall.core.claims import assertion_clause

    assert assertion_clause("a line\nVersion: 3.1.0\nanother?", "Version: 3.1.0") == "Version: 3.1.0\n"


def test_the_question_gate_still_refuses_a_question_in_the_quoted_sentence():
    """Narrowing the radius must not let an actual question through."""
    verdict = _qualify("TEST-instrument 的上下文窗口是 262144 吗？",
                       subject="TEST-instrument", quote="TEST-instrument 的上下文窗口是 262144 吗？")
    assert verdict.state == "proposed" and verdict.reason == "question_not_asserted"
