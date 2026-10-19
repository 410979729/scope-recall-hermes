"""A question that names the store's owner, recalled as they would ask it themselves (``core/owner_view.py``).

No name is built in: the names are an installation's own (``owner_names``) and the one its host attests for the
person asking.  Synthetic text only.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from scope_recall.contracts import ContractError, TrustedSourcePrincipal
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.owner_view import name_forms, owner_names, owner_view
from scope_recall.core.recall_policy import SPACE_ID, RecallPolicy
from scope_recall.core.retrieval import CandidateRef

from tests.contract.test_claims import Clock, capture
from tests.v11_support import context, recall_request

ASKER = TrustedSourcePrincipal("human", "verified", "principal:TEST-owner", display_name="NameOne SurnameOne")


def test_a_host_s_name_for_a_person_gives_what_they_are_called():
    assert name_forms("NameOne SurnameOne") == ("NameOne SurnameOne", "NameOne")
    # The first word is what people are called by; a middle name or a surname is someone else's as often.
    assert name_forms("  “Mary Ann Lee” ") == ("Mary Ann Lee", "Mary")
    assert name_forms("J Smith") == ("J Smith",)
    assert name_forms("王芳") == ("王芳",)
    assert name_forms("nameone3158") == ("nameone3158",)
    # A WeCom external user id arrives where a gateway puts the user's name; a letter or a number alone is no name.
    for nothing in (None, 7, "", "A", "12345", "wotD8aOQAAQqarAq4kO-ta84yxVucGYQ", "x" * 65, "--- ---"):
        assert name_forms(nothing) == (), nothing


def test_only_the_name_attested_for_a_verified_human_counts_beside_the_configured_ones():
    assert owner_names(("名字一",), ASKER) == ("名字一", "NameOne SurnameOne", "NameOne")
    assert owner_names(("NameOne",), ASKER) == ("NameOne", "NameOne SurnameOne")
    assert owner_names((), TrustedSourcePrincipal("human", "unresolved", display_name="NameOne")) == ()
    agent = TrustedSourcePrincipal("assistant", "verified", "principal:TEST-agent", display_name="NameOne")
    assert owner_names((), agent) == ()
    assert owner_names(("NameOne",), None) == ("NameOne",)


@pytest.mark.parametrize(
    ("query", "names", "first", "bare"),
    [
        ("NameOne 对辣的饮食偏好是什么？", ("NameOne",), "我对辣的饮食偏好是什么？", "对辣的饮食偏好是什么？"),
        ("NameOne对辣的饮食偏好是什么？", ("NameOne",), "我对辣的饮食偏好是什么？", "对辣的饮食偏好是什么？"),
        ("NameOne的猫叫什么名字", ("NameOne",), "我的猫叫什么名字", "猫叫什么名字"),
        ("NameOne 的猫叫什么名字", ("NameOne",), "我的猫叫什么名字", "猫叫什么名字"),
        ("王芳喜欢什么颜色", ("王芳",), "我喜欢什么颜色", "喜欢什么颜色"),
        ("王芳对动画有什么要求", ("王芳",), "我对动画有什么要求", "对动画有什么要求"),
        ("问问王芳", ("王芳",), "问问我", "问问"),
        ("王芳的生日是哪天", ("王芳",), "我的生日是哪天", "生日是哪天"),
        # The longest name is read first: 王芳 here is the start of another name.
        ("王芳芳喜欢什么", ("王芳", "王芳芳"), "我喜欢什么", "喜欢什么"),
        ("Where is NameOne's laptop?", ("NameOne",), "Where is my laptop?", "Where is laptop?"),
        ("What does NameOne prefer?", ("NameOne",), "What does I prefer?", "What does prefer?"),
        ("Tell me Mary Ann's plans", ("Mary Ann Lee", "Mary", "Ann", "Lee"), "Tell me my plans", "Tell me plans"),
        (
            "NameOne SurnameOne 和 NameOne 都说了什么",
            ("NameOne SurnameOne", "NameOne"),
            "我和我都说了什么",
            "和都说了什么",
        ),
        ("NameOne 的偏好\n第二行 NameOne 说的", ("NameOne",), "我的偏好\n第二行我说的", "偏好\n第二行说的"),
        ("NameOne's plan\n  and NameOne's budget", ("NameOne",), "my plan\nand my budget", "plan\nand budget"),
    ],
)
def test_a_question_that_names_the_owner_is_read_as_their_own(query, names, first, bare):
    view = owner_view(query, names)
    assert (view.first, view.bare) == (first, bare)


@pytest.mark.parametrize(
    ("query", "names"),
    [
        ("Is nameone-hub running?", ("NameOne",)),
        ("/opt/NameOne/bin 在哪", ("NameOne",)),
        ("NameOnea said what?", ("NameOne",)),
        ("what may I prefer these days", ("May",)),
        ("Année ok", ("Ann",)),
        ("名字一一喜欢什么", ("名字一一基",)),
        ("芳芳去哪了", ("芳",)),
        ("TEST 没有提到名字", ("NameOne",)),
        ("Find C:\\NameOne\\notes", ("NameOne",)),
        ("NameOne SurnameTwo's preferences", ("NameOne",)),
        ("NameOne SURNAMETWO's preferences", ("NameOne",)),
        ("NameOne  SURNAMETWO's preferences", ("NameOne",)),
        ("王明明的手机号", ("王明",)),
        ("Ann Smith's preferences", ("Mary Ann Lee", "Mary")),
        ("王芳芳喜欢什么颜色", ("王芳",)),
        ("王芳林的手机号", ("王芳",)),
    ],
)
def test_a_name_inside_a_word_or_an_identifier_or_of_one_letter_is_no_name(query, names):
    assert owner_view(query, names) is None


def test_recall_policy_refuses_names_it_could_not_read():
    assert RecallPolicy(vector_threshold=None, owner_names=("NameOne", "名字一")).owner_names == ("NameOne", "名字一")
    for invalid in (
        "NameOne",
        ("",),
        ("J",),
        (" NameOne",),
        ("x" * 65,),
        tuple(f"n{i}" for i in range(9)),
        ("Al\nice",),
    ):
        with pytest.raises(ContractError):
            RecallPolicy(vector_threshold=None, owner_names=invalid)


@pytest.fixture
def app(tmp_path):
    ctx = context(tmp_path / "TEST-owner")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    return core, ctx


class Vector:
    """Answers each question from ``answers``; records what it was asked early, with what else, and what it searched,
    with what limit and whether with an embedding asked for early."""

    def __init__(self, answers):
        self.answers, self.searched, self.early = answers, [], []

    def prefetch_query(self, context, *, also=()):
        self.early.append((context.query, also))
        return SimpleNamespace(query=context.query, queries=(context.query, *also))

    def search(self, context, *, limit, remaining_seconds, prefetched=None):
        self.searched.append((context.query, limit, prefetched is not None and context.query in prefetched.queries))
        return tuple(
            CandidateRef(
                "event",
                ref,
                1,
                "vector",
                rank=rank,
                vector_id=f"TEST-v{rank}",
                embedding_space=SPACE_ID,
                vector_score=0.9,
            )
            for rank, ref in enumerate(self.answers.get(context.query, ()), 1)
        )


class OneAtATime(Vector):
    """A port that asks for one question's embedding early."""

    def prefetch_query(self, context):
        return super().prefetch_query(context)


def _recall(core, ctx, query, vector, names=()):
    core.recall_pipeline.vector_port = vector
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.8, owner_names=names)
    return {
        item.ref for item in core.recall(ctx, recall_request(query=query, mode="current"), deadline_seconds=5).items
    }


ASKED, FIRST = "NameOne 对辣的饮食偏好是什么？", "我对辣的饮食偏好是什么？"


def test_a_question_naming_the_owner_is_searched_as_asked_and_as_they_would_ask_it(app):
    """The owner's own words never carry their name, and a greeting addressed to them carries little else: the
    question in their first person finds the first, the question as asked still finds what names them.  Both
    embeddings are asked for early in one request, and each search has the channel's slots."""
    core, ctx = app
    said = capture(core, ctx, "我不吃辣，预算四十以内。", key="TEST-owner/said")
    named = capture(core, ctx, "NameOne 说过她不吃辣。", key="TEST-owner/named")
    vector = Vector({ASKED: [named.ref], FIRST: [said.ref]})
    assert {said.ref, named.ref} <= _recall(core, ctx, ASKED, vector, names=("NameOne",))
    assert vector.early == [(ASKED, (FIRST,))]
    (asked, asked_limit, asked_early), (owned, owned_limit, owned_early) = vector.searched
    assert (asked, owned) == (ASKED, FIRST) and asked_early and owned_early
    assert owned_limit == asked_limit, "the first-person search has slots of its own"


def test_a_port_that_takes_one_question_early_embeds_the_other_in_its_search(app):
    core, ctx = app
    vector = OneAtATime({})
    _recall(core, ctx, ASKED, vector, names=("NameOne",))
    assert vector.early == [(ASKED, ())]
    assert [(query, early) for query, _limit, early in vector.searched] == [(ASKED, True), (FIRST, False)]


def test_the_name_the_host_attests_for_the_asker_is_read_without_any_configured(app):
    core, ctx = app
    said = capture(core, ctx, "我不吃辣。", key="TEST-owner/said")
    vector = Vector({FIRST: [said.ref]})
    assert said.ref in _recall(core, replace(ctx, source_principal=ASKER), "NameOne对辣的饮食偏好是什么？", vector)
    assert [query for query, _limit, _early in vector.searched] == ["NameOne对辣的饮食偏好是什么？", FIRST]


def test_a_question_that_names_no_one_known_or_nothing_besides_is_searched_as_asked(app):
    core, ctx = app
    for query, names in ((ASKED, ()), ("对辣的饮食偏好是什么？", ("NameOne",)), ("NameOne 是谁？", ("NameOne",))):
        vector = Vector({})
        _recall(core, ctx, query, vector, names=names)
        assert vector.early == [(query, ())], query
        assert [(asked, early) for asked, _limit, early in vector.searched] == [(query, True)], query


def test_an_answer_in_the_owner_s_words_is_admitted_without_their_name_and_a_greeting_is_not(app):
    """Lexically the owner's own words fell short of the name a question asks them by, while a greeting addressed
    to them matched it; read as their own question, the name asks nothing of an answer and proves nothing."""
    core, ctx = app
    answer = capture(core, ctx, "我不吃辣，预算四十以内。", key="TEST-owner/answer")
    for index, text in enumerate(("在的，NameOne。", "在，NameOne。你说。", "在呀，NameOne。")):
        capture(core, ctx, text, key=f"TEST-owner/greeting-{index}")
    core.recall_pipeline.vector_port = None
    request = recall_request(query="NameOne 吃辣吗？", mode="current")
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=None)
    unnamed = [item.ref for item in core.recall(ctx, request, deadline_seconds=5).items]
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=None, owner_names=("NameOne",))
    named = [item.ref for item in core.recall(ctx, request, deadline_seconds=5).items]
    assert answer.ref not in unnamed
    assert named == [answer.ref]


def test_a_reply_that_names_the_owner_beside_the_question_s_other_words_is_still_admitted(app):
    """Without the name, the two words an agent's note shares with the question are too few for it; beside them the
    name still counts.  Alone it admits nothing."""
    core, ctx = app
    note = capture(core, ctx, "NameOne 讲过运镜的要求。", key="TEST-owner/note")
    greeting = capture(core, ctx, "在的，NameOne。", key="TEST-owner/greeting")
    core.recall_pipeline.vector_port = None
    core.recall_pipeline.policy = RecallPolicy(vector_threshold=None, owner_names=("NameOne",))
    request = recall_request(query="NameOne 对动画运镜有什么要求？", mode="current")
    found = [item.ref for item in core.recall(ctx, request, deadline_seconds=5).items]
    assert note.ref in found
    assert greeting.ref not in found
