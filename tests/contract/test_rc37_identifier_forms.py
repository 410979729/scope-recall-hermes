"""One product written two ways is one product.

A question asked about DLSS5 and glm5.3-flash; the replies wrote DLSS 5 and glm-5.3-flash.
Compared as written, the hard identifier of the query was absent from the reply and the
reply was refused as naming something else.  What is offered is read both ways; the query
is not, or asking for "the top 5 options" would demand an answer containing "top5".
"""
from __future__ import annotations

from scope_recall.core.recall_policy import identifiers_compatible
from tests.contract.test_rc33_recall_accuracy import _packet, _say  # noqa: F401  (helpers)
from tests.contract.test_v11_claims import app  # noqa: F401  (fixture)


def test_a_name_spelled_out_in_the_answer_is_the_name_that_was_asked_for():
    for query, content in (("DLSS5 到底是什么", "DLSS 5 是英伟达的超分技术"),
                           ("glm5.3-flash 多少钱", "glm-5.3-flash 输入每百万 0.6 元"),
                           ("H100 的价格", "H_100 现在的价格"),
                           ("rc36 修了什么", "rc-36 修了三件事"),
                           ("rc-36 修了什么", "rc36 修了三件事")):
        assert identifiers_compatible(query, content), (query, content)


def test_names_that_differ_in_their_digits_stay_apart():
    for query, content in (("rc28 修了什么", "rc29 修了三件事"),
                           ("H100 的价格", "H200 现在的价格"),
                           ("DLSS5 到底是什么", "DLSS 4 是英伟达的超分技术"),
                           ("glm5.3-flash 多少钱", "glm-4.6 输入每百万 0.6 元")):
        assert not identifiers_compatible(query, content), (query, content)


def test_a_query_that_merely_counts_demands_no_spelling():
    """Reading the query both ways made "me 3" a hard identifier of its own."""
    assert identifiers_compatible("give me 3 options", "这里有三个方案：A、B、C")
    assert identifiers_compatible("H100 exact identifier", "P08 keeps H100 as an exact identifier.")


def test_a_comparison_still_admits_one_side_at_a_time():
    assert identifiers_compatible("H100 和 H200 差多少", "H200 的显存带宽更高")


def test_a_reply_that_spells_the_name_out_is_recalled(app):
    """The alpha case: the answer to "DLSS5到底是什么" wrote "DLSS 5" and was refused."""
    core, ctx = app
    question = _say(core, ctx, "DLSS5到底是什么", origin="human_direct", role="user",
                    when="2026-09-02T09:00:00Z", key="TEST-ident/ask")
    answer = _say(core, ctx, "DLSS 5 是英伟达的帧生成技术，靠 transformer 模型做超分。", origin="assistant_visible",
                  role="assistant", when="2026-09-02T09:00:20Z", key="TEST-ident/answer")
    refs = [item["ref"] for item in _packet(core, ctx, "DLSS5到底是什么")["items"]]
    assert question.ref in refs and answer.ref in refs
