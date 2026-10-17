"""Bounded source-grounded claim rules; synthetic text and no model calls."""

import itertools
from dataclasses import replace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.claims import RootEvidence, qualify
from scope_recall.core.source_qualification import conditions_match, self_report_bound

from tests.contract.test_claims import Clock, capture, initial, revise_request
from tests.v11_support import context


def qualification(text, *, subject="user", value="蓝色", conditions=(), principal=None, quote=None):
    if principal is None:
        principal = {
            "kind": "human",
            "resolution": "verified",
            "principal_ref": "principal:TEST-owner",
        }
    root = RootEvidence(
        "TEST-root",
        1,
        "human_direct",
        None,
        text,
        "2026-09-06T12:00:00Z",
        "complete",
        "TEST-session",
        source_principal=principal,
    )
    proposal = dict(
        kind="preference",
        subject=subject,
        predicate="颜色偏好",
        value_text=value,
        conditions=list(conditions),
        statement_kind="assertion",
        valid_from=root.occurred_at,
        valid_to=None,
        evidence_spans=[dict(source_ref=root.ref, source_revision=1, quote=text if quote is None else quote)],
    )
    return qualify(proposal, (root,))


def test_condition_wrappers_preserve_assertion_and_identity():
    for condition, query in (
        ("写小说时", "现在写小说"),
        ("当写小说时", "现在写小说"),
        ("when writing fiction", "I am writing fiction"),
        ("when writing  fiction", "I am writing fiction"),
        ("café", "cafe\u0301"),
        ("TEST-sandbox", "TEST-sandbox."),
        ("写小说时", "现在写小说，能帮我润色吗？"),
    ):
        assert conditions_match([condition], query), (condition, query)
    for condition, query in (
        ("写小说时", "现在不写小说"),
        ("TEST-sandbox", "TEST-sandbox-other"),
        ("blue", "blue.txt"),
        ("TEST-sandbox", "test-sandbox"),
        ("写小说", "如果写小说"),
        ("写小说", "客户说写小说"),
        ("写小说", "写小说吗？"),
        ("写小说时", "是不是写小说"),
        ("今天", "今天"),
        ("本次", "本次"),
        ("暂时", "暂时"),
    ):
        assert not conditions_match([condition], query), (condition, query)


def test_self_report_does_not_promote_third_party_or_team_to_user():
    for text, value in (
        ("我喜欢蓝色。", "蓝色"),
        ("我的偏好是蓝色。", "蓝色"),
        ("I prefer blue.", "blue"),
        ("My preference is blue.", "blue"),
        ("我不喜欢蓝色。", "不喜欢蓝色"),
        ("I do not like blue.", "do not like blue"),
    ):
        assert qualification(text, value=value).state == "active", text
    for text, value in (
        ("我的同事喜欢蓝色。", "蓝色"),
        ("我们喜欢蓝色。", "蓝色"),
        ("我朋友喜欢蓝色。", "蓝色"),
        ("我同事喜欢蓝色。", "蓝色"),
        ("我有个同事喜欢蓝色。", "蓝色"),
        ("I have a colleague who likes blue.", "blue"),
        ("My colleague likes blue.", "blue"),
        ("We prefer blue.", "blue"),
        ("她说：“我喜欢蓝色。”", "蓝色"),
        ('He said "I prefer blue."', "blue"),
    ):
        assert qualification(text, value=value).state == "proposed", text
    assert qualification("我的同事喜欢蓝色。", subject="我").state == "proposed"


def test_an_everyday_self_report_is_the_speaker_s():
    """我不吃辣 is the speaker's own as 我喜欢蓝色 is, after their own framing too (老实说，That said, a list
    marker).  Said of someone else, quoted, reported, or with the value away from the verb, it is not."""
    said = "我登陆了，我不吃辣，预算40以下都可以，尽量20左右，但是超出也没事，不超过40最好，除非那菜特别好"
    assert self_report_bound(said, "不吃辣", kind="preference")
    assert not self_report_bound("客户说" + said, "不吃辣", kind="preference")
    for text, value in (
        ("我不吃辣。", "不吃辣"),
        ("我不喝酒。", "不喝酒"),
        ("我平时不太吃辣。", "不太吃辣"),
        ("我从来不抽烟。", "从来不抽烟"),
        ("我从不抽烟。", "从不抽烟"),
        ("我爱吃辣。", "辣"),
        ("我对花生过敏。", "对花生过敏"),
        ("我讨厌香菜。", "讨厌香菜"),
        ("I do not eat spicy food.", "do not eat spicy food"),
        ("I never drink coffee.", "never drink coffee"),
        ("I'm allergic to peanuts.", "allergic to peanuts"),
        ("I don't eat spicy food.", "don't eat spicy food"),
        ("我说：我不吃辣。", "不吃辣"),
        ("我不能吃辣。", "不能吃辣"),
        ("我只喝美式。", "只喝美式"),
        ("Honestly, I never drink coffee.", "never drink coffee"),
        ("I don't really eat pork.", "don't really eat pork"),
        ("老实说，我不吃辣。", "不吃辣"),
        ("一般来说，我不喝酒。", "不喝酒"),
        ("跟你说，我不吃辣。", "不吃辣"),
        ("That said, I never drink coffee.", "never drink coffee"),
        ("- I don't eat spicy food.", "don't eat spicy food"),
        ("- I prefer dark mode.", "dark mode"),
        ("In general I never drink coffee.", "never drink coffee"),
        ("嗯，老实说，我不吃辣。", "不吃辣"),
        ("- That said, I never drink coffee.", "never drink coffee"),
        ("In practice I never drink coffee.", "never drink coffee"),
        ("In practice I prefer blue.", "blue"),
        ("- My preference is blue.", "blue"),
        ("I never drink coffee.\nUpdate: the rest is done.", "never drink coffee"),
        ("https://example.com/menu\nI never drink coffee.", "never drink coffee"),
    ):
        verdict = qualification(text, value=value)
        assert verdict.state == "active", (text, verdict.reason)
    for text, value in (
        ("客户说我不吃辣。", "不吃辣"),
        ("她说：“我不吃辣。”", "不吃辣"),
        ("我们不吃辣。", "不吃辣"),
        ("我妈不吃辣。", "不吃辣"),
        ("张三说：我不吃辣。", "不吃辣"),
        ("张三说：'我不吃辣'", "不吃辣"),
        ("Alice said: 'I eat spicy food.'", "eat spicy food"),
        ("Alice said I do not eat spicy food.", "do not eat spicy food"),
        ("我对象对花生过敏。", "对花生过敏"),
        ("我对象过敏。", "对象过敏"),
        ("我看不吃辣更健康。", "不吃辣"),
        ("我对面的同事对花生过敏。", "对花生过敏"),
        ("我讨厌吃香菜的人。", "讨厌吃香菜"),
        ("儿子：我不喝牛奶。", "不喝牛奶"),
        ("比如：我不喝酒。", "不喝酒"),
        ("我不打算戒烟。", "戒烟"),
        ("Everyone thinks I drink coffee.", "drink coffee"),
        ("For example, I never smoke.", "never smoke"),
        ("The rumor that I eat spicy food is false.", "eat spicy food"),
        ("The rumor that I prefer blue is false.", "blue"),
        ("张三说，我不吃辣。", "不吃辣"),
        ("张三表示，我不喝酒。", "不喝酒"),
        ("张三跟你说，我不吃辣。", "不吃辣"),
        ("Alice sent a message that said, I never drink coffee.", "never drink coffee"),
        ("Alice:\n- I eat spicy food.", "eat spicy food"),
        ("儿子：\n- 我不喝牛奶。", "不喝牛奶"),
        ("Alice:\nIn general I never drink coffee.", "never drink coffee"),
        ("儿子：老实说，我不喝牛奶。", "不喝牛奶"),
        ("Alice:\nMy preference is blue.", "blue"),
        ("- Alice:\nMy preference is blue.", "blue"),
        ("* Alice:\nMy preference is blue.", "blue"),
        ("1. Alice:\nMy preference is blue.", "blue"),
        ("- [10:32] Alice:\nMy preference is blue.", "blue"),
        ("- 儿子：\n我不喝牛奶。", "不喝牛奶"),
        ("Alice:\nWell,\nin general I never drink coffee.", "never drink coffee"),
        ("[10:32] Alice: I never drink coffee.", "never drink coffee"),
        ("[10:32] Alice:\nI never drink coffee.", "never drink coffee"),
        ("<alice> I never drink coffee.", "never drink coffee"),
        ("<alice>\nI never drink coffee.", "never drink coffee"),
        ("- The rumor that I eat spicy food is false.", "eat spicy food"),
        ("Son: I don't drink milk.", "don't drink milk"),
        ("他和我都不吃辣。", "不吃辣"),
        ("张三爱吃辣 我不吃。", "爱吃辣"),
        ("我看他不吃辣。", "不吃辣"),
        ("我看不吃辣的人更健康。", "不吃辣"),
        ("我觉得他不喝酒。", "不喝酒"),
        ("我用他的电脑。", "电脑"),
        ("He does not eat spicy food.", "does not eat spicy food"),
        ("My wife never drinks coffee.", "never drinks coffee"),
    ):
        assert not self_report_bound(text, value, kind="preference"), text
        assert qualification(text, value=value).state == "proposed", text


def test_a_transcript_s_label_outside_the_quote_s_sentence_still_names_someone_else():
    """A claim's evidence is read around its quote.  A pasted transcript's label lines above it are someone else's
    words all the same, and so is a label in Markdown emphasis or on a quoted line; the owner's own words before a
    label are theirs."""
    preference = ("My preference is blue.", "blue")
    for text, (quote, value) in (
        ("Alice:\nHello.\nMy preference is blue.", preference),
        ("**Alice:**\nMy preference is blue.", preference),
        ("**Alice**: hi\nMy preference is blue.", preference),
        ("> Alice: hi\nMy preference is blue.", preference),
        ("Alice (10:32):\nHello.\nMy preference is blue.", preference),
        ("Alice sent this.\n```\nMy preference is blue.\n```", preference),
        ("Alice sent this.\n~~~\nHi.\nMy preference is blue.\n~~~", preference),
        ("张三发来这段。\n> 你好。\n> 我不吃辣。", ("我不吃辣。", "不吃辣")),
        ("Alice sent this.\n```\nMy preference is blue.\n```", ("```\nMy preference is blue.\n```", "blue")),
        ("Alice sent this.\n~~~markdown\n```\nMy preference is blue.\n```\n~~~", preference),
        ("Alice sent this.\n“Hello.\nMy preference is blue.\n”", preference),
        ('Alice sent this.\n"Hello.\nMy preference is blue."', preference),
        ("张三发来这段。\n「你好。\n我不吃辣。」", ("我不吃辣。", "不吃辣")),
        ("Alice sent this.\n‘Hello.\nMy preference is blue.\n’", preference),
        ("Alice sent this.\n‘I don’t mind.\nMy preference is blue.\n’", preference),
        ("Alice sent this.\n‘The others’ choices are fine.\nMy preference is blue.\n’", preference),
        ("Alice sent this.\n'Hello.\nMy preference is blue.\n'", preference),
        ("Alice wrote ‘Hello.\nMy preference is blue.\n’", preference),
        ("Alice wrote 'Hello.\nMy preference is blue.\n'", preference),
        ("Alice sent this.\n```\n```python\nMy preference is blue.\n```", preference),
        ("[2026-10-10 Saturday] Alice Wonderland-Smith:\nMy preference is blue.", preference),
    ):
        assert qualification(text, value=value, quote=quote).state == "proposed", text
    for owner_text in (
        "My preference is blue.\nUpdate: done.",
        "My preference is blue.\n```\nTEST code\n```",
        "```text\n~~~\n```\nMy preference is blue.",
        "````\n```\n````\nMy preference is blue.",
        'He called it "fine".\nMy preference is blue.',
        "我说“好”。\nMy preference is blue.",
        "```python\ndelimiter = '\"'\n```\nMy preference is blue.",
        "```python\nname: str = 'x'\n```\nMy preference is blue.",
        "It’s settled.\nMy preference is blue.",
        "‘Hello.’\nMy preference is blue.",
        "The others’ choices are fine.\nMy preference is blue.",
        "'Hi.\nBye.'\nMy preference is blue.",
        "Alice said ‘hi’.\nMy preference is blue.",
        "It's done, and the students' books are back.\nMy preference is blue.",
        "```inline``` is code.\nMy preference is blue.",
        "Here is a short summary of where things stand this week:\nMy preference is blue.",
    ):
        owner = qualification(owner_text, value="blue", quote="My preference is blue.")
        assert owner.state == "active", owner_text


def test_self_report_requires_a_verified_c1_principal():
    missing = qualification("我喜欢蓝色。", principal={})
    assert missing.state == "proposed"
    assert missing.reason == "source_identity_unresolved"
    unresolved = qualification(
        "我喜欢蓝色。",
        principal={"kind": "human", "resolution": "unresolved"},
    )
    assert unresolved.state == "proposed"
    assert unresolved.reason == "source_identity_unresolved"


def test_unrelated_condition_cannot_erase_relative_scope():
    text = "我本次在TEST沙箱喜欢蓝色。"
    assert qualification(text, conditions=["TEST沙箱"]).reason == "relative_scope_not_preserved"
    assert qualification(text, conditions=["本次", "TEST沙箱"]).state == "active"


def observed_version(text, value):
    root = RootEvidence(
        "TEST-root", 1, "tool_observation", None, text, "2026-09-16T10:00:00Z", "complete", "TEST-session"
    )
    proposal = dict(
        kind="fact",
        subject="阿乙",
        predicate="当前版本",
        value_text=value,
        conditions=[],
        statement_kind="assertion",
        valid_from=None,
        valid_to=None,
        evidence_spans=[dict(source_ref=root.ref, source_revision=1, quote=text)],
    )
    verdict = qualify(proposal, (root,))
    return verdict.state, verdict.basis, verdict.reason


@pytest.mark.parametrize(
    "text,value",
    [
        ("阿乙 当前版本: rc28", "rc28"),
        ("阿乙 当前版本: 3.1.0rc28", "3.1.0rc28"),
        ("阿乙 当前版本: v3.1.0-rc28", "v3.1.0-rc28"),
        ("阿乙 当前版本: 3.1.0.dev2+alpha.2", "3.1.0.dev2+alpha.2"),
        ("阿乙 当前版本: rc28。升级没有开始", "rc28"),
        ("阿乙 当前版本: 3.1.0rc28. 升级没有开始", "3.1.0rc28"),
    ],
)
def test_a_dotted_value_is_one_token_of_one_observed_clause(text, value):
    assert observed_version(text, value) == ("active", "observed", "observation_at_source_time")


@pytest.mark.parametrize(
    "text,value",
    [
        ("阿乙 当前版本: rc28，升级没有开始", "rc28"),
        ("阿乙 当前版本: 3.1.0rc28，升级没有开始", "3.1.0rc28"),
    ],
)
def test_negation_in_the_same_clause_still_refuses_an_observed_version(text, value):
    assert observed_version(text, value) == ("proposed", "inferred_suggestion", "fact_entailment_unproved")


def test_sentence_periods_dates_and_month_abbreviations_keep_their_clauses():
    from scope_recall.core.fact_evidence import _value_clauses

    assert _value_clauses("Moved to Berlin on Sep. 5. Left Paris.", "Berlin") == ["moved to berlin on sep 5"]
    assert _value_clauses("Moved to Berlin on 2026.9.16. Left Paris.", "Berlin") == ["moved to berlin on 2026-9-16"]
    assert _value_clauses("我 9.16 搬到柏林。之后去巴黎。", "柏林") == ["我 9-16 搬到柏林"]
    assert _value_clauses("Version: 3.1.0rc28. Rollout 3.5 hours.", "3.1.0rc28") == ["version: 3.1.0rc28"]


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-sprint-claims"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def test_attribute_correction_cannot_skip_negative_value_qualification(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="H100", kind="fact")
    source = capture(core, ctx, "更正TEST-project，配色不是H200。", when="2026-09-06T12:00:00Z")
    with pytest.raises(ContractError, match="value_polarity_not_preserved"):
        core.revise(ctx, revise_request(item, source, "H200"))
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "H100"
    assert len(core.claim_history(ctx, item.ref)) == 1


def test_explicit_replacement_forms_keep_exact_old_value(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="H100", kind="fact")
    capture(core, ctx, "TEST-project 配色替换为H200。", when="2026-09-06T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "H200"
    capture(core, ctx, "TEST-project 配色 replace h200 with H300.", when="2026-09-06T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "H200"
    capture(core, ctx, "TEST-project 配色 replace H200 with H300.", when="2026-09-06T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "H300"
    assert len(core.claim_history(ctx, item.ref)) == 3


def test_temporary_retraction_does_not_erase_regular_rule(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="H100", kind="fact")
    source = capture(core, ctx, "本次撤回TEST-project 配色H100。", when="2026-09-06T12:00:00Z")
    with pytest.raises(ContractError, match="conditional_retraction_not_authorized"):
        core.revise(ctx, revise_request(item, source, None))
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "H100"
    assert core.source(ctx, source.ref, 1).event["content"] == source.event["content"]
