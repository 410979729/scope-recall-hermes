"""Pure Claim qualification and temporal selection; no adapters, SQL or models."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime

from ..contracts import (
    VALUE_FREE_KINDS,
    Basis,
    ClaimProposal,
    ContractError,
    Origin,
    SourcePrincipal,
    utc_timestamp,
    verified_human_principal_ref,
)
from .candidate_lifecycle import SELF_SUBJECTS
from .fact_actions import ClaimDraft, EvidenceReference
from .fact_evidence import evidence_supports_claim, evidence_supports_relation, stem
from .fact_temporal_semantics import classify_durable_state_clause
from .source_qualification import (
    AUTHORITY_QUESTION,
    CONDITIONAL,
    RELATIVE_SCOPE,
    UNASSERTED_UNCERTAINTY,
    asserted_marker,
    attributed_to_another,
    bound_literal,
    condition_supports_value,
    in_pasted_text,
    is_transient_request,
    preserves_qualifiers,
    self_report_bound,
)
from .subject_binding import neighbourhood_binds


@dataclass(frozen=True)
class RootEvidence:
    ref: str
    revision: int
    origin: Origin
    original_origin: str | None
    content: str
    occurred_at: str | None
    capture_state: str
    session_id: str
    import_verified: bool = False
    capture_gaps: tuple[str, ...] = ()
    source_principal: SourcePrincipal | None = None


@dataclass(frozen=True)
class Qualification:
    state: str
    basis: Basis
    reason: str


@dataclass(frozen=True)
class ClaimVersion:
    ref: str
    revision: int
    current_revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None
    payload: ClaimProposal
    state: str
    basis: Basis
    reason: str
    valid_from: str | None
    valid_to: str | None
    recorded_from: str
    recorded_to: str | None
    replaces_revision: int | None
    conflict_revisions: tuple[int, ...]
    suppressed: bool = False


def canonical_time(value: str | None) -> str | None:
    return None if value is None else utc_timestamp(value)


def claim_slot(scope_id: str, project_id: str | None, branch_id: str | None, proposal: ClaimProposal) -> str:
    """Only identical explicit slots merge; aliases/semantic similarity confer no identity."""
    identity = [
        scope_id,
        project_id,
        branch_id,
        proposal["kind"],
        proposal["subject"],
        proposal["predicate"],
        sorted(set(proposal["conditions"])),
    ]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def same_assertion(left: ClaimProposal, right: ClaimProposal) -> bool:
    fields = (
        "kind",
        "subject",
        "predicate",
        "value_text",
        "conditions",
        "statement_kind",
        "valid_from",
        "valid_to",
        "procedure",
        "intention",
        "alias",
    )
    return all(
        (sorted(set(left[k])) == sorted(set(right[k]))) if k == "conditions" else left.get(k) == right.get(k)
        for k in fields
    )


_HYPOTHETICAL = re.compile(r"假设|假如|设想|虚构|假定|\b(?:suppose|hypothetic|fictional|what if)\b", re.I)
_UNDECIDED = re.compile(r"先不要定|还[没未]决定|尚未确定|没有决定|待定|\b(?:not decided|undecided)\b", re.I)
_QUOTED_OTHER = re.compile(r"不是我的|不是我本人|客户原文|\b(?:not my preference|customer said)\b", re.I)
_LIMIT = re.compile(r"只限|仅限|本次|这次|今天|未授权|除非|\b(?:only this|this time|unless|without permission)\b", re.I)
_ACCEPTANCE = re.compile(
    r"认可|验收通过|按这个方法|就按这样|这个方法[很好对可]|\b(?:accept|approved|use this method)\b", re.I
)
_COMPLETION = re.compile(r"已经完成|已完成|检查完成|完成了|\b(?:completed|finished|done)\b", re.I)
_CANCELLATION = re.compile(r"取消|撤销|作废|\b(?:cancel|revoke|withdraw)\b", re.I)
_NEGATED_COMPLETION = re.compile(r"未完成|没[有]?完成|尚未完成|提醒|\b(?:not done|not completed|remind)\b", re.I)
_NEGATION = re.compile(r"不是|并非|没有|未曾|不喜欢|讨厌|\b(?:not|never|dislike)\b", re.I)
#: The verbs of being, having, liking and doing a Chinese negation opens a value with ("不是 X", "没有车", "不吃辣",
#: "不喜欢用 X", "不在线"); before any other word a negator is part of that word (不错, 不过, 没问题, 不客气, 不同意,
#: 不知道, 不用谢, 不在乎).
_DENIED_VERB = (
    r"是|有(?!问题|关系|事)|喜欢|爱|想(?!到)|要(?!紧)|需要|用(?!谢)|使用|吃|喝|抽|吸|穿|戴|碰|玩|开|住|睡|养|信|怕"
    r"|在(?!乎|意)|去|做|坐|骑|买|选|看|听"
)
#: A negation at the opening of a value, after the adverbs that may lead it ("一点也不喜欢", "really don't"): a
#: Chinese negator with the degree or the time it carries (不是很, 不太, 不再, 别再) before a verb of being, having,
#: liking or doing; an outright rejection ("并非 X", "讨厌 X", "受不了 X", "can't stand X", "dislikes X"); or an
#: English negator ("not X", "isn't X", "don't have a car", "never drink coffee", "no longer uses X").  不一定 doubts
#: rather than negates, and an English negator before a word of knowing, minding, applying or contrast negates nothing
#: ("not bad", "not only X but Y", "don't know", "not sure", "not applicable", "doesn't apply", "isn't it X").  Joined
#: to the word after it, a negator is part of that word, as in NOT_STARTED, no-op and never-ending.
_NEGATOR = re.compile(
    r"^\s*(?:(?:一点[也都]|根本|完全|从来|从|向来|也|都|还|并|已经|再也|很|真|实在|一直|平时|现在)\s*)*"
    r"(?:(?P<outright>并非|绝非|讨厌|厌恶|反感|受不了)(?=\s*[^\s。，！？,.!?])"
    r"|(?P<negator>不|没|别|未曾|尚未|未)(?:是(?=很|太|那么|特别))?(?P<modal>(?:太|大|很|那么|特别|怎么|常|再|会|能)*)"
    rf"(?=(?P<verb>{_DENIED_VERB})\s*[^\s。，！？,.!?]))"
    r"|^\s*(?:(?:really|just|still|simply|definitely|absolutely|is|are|was|were|do|does|did)\s+)*"
    r"(?:(?:not|never|no\s+longer|cannot|can['’]?t|\w+n['’]t)\s+"
    r"(?!(?:bad|only|just|merely|sure|certain|know|knows|knew|think|mind|care|matter|remember|applicable|apply"
    r"|applies|it|that|this|there|a\s+problem)\b)"
    r"|(?P<dislike>dislikes?|hates?)\s+)(?=[^\s.,!?])",
    re.I,
)
#: The Chinese verbs a 不 or a 别 before them negates as a state of the subject rather than a choice: being somewhere
#: (不在线, 不在家), having, and opening or enabling (不开放).
_ZH_STATE_VERBS = frozenset({"在", "有", "开"})
#: The degree words a 不是 may carry: before a word of state they make a degree of it (不是很稳定), and only a verb of
#: liking after them a rejection (不是很喜欢 X).
_ZH_DEGREE = re.compile(r"很|太|那么|特别")
#: English words that may stand between a negator and the word it negates and name nothing themselves ("not
#: currently", "isn't being", "not a").  With nothing after them, the negation is a polar answer ("not really").
_EN_UNSTRESSED = frozenset(
    "a an the be been being currently fully always very too so quite really entirely completely totally".split()
)
#: English words of state that no ending marks: adjectives and polar answers (not null, not ready, not online, not
#: empty, not enough) and irregular past participles (not found, not set, not done, not built, not run).
_EN_STATE_WORDS = frozenset(
    """null nil none ready online offline empty free open live valid invalid up down due busy idle full safe secure
    true false ok okay fine good bad great well enough much many here away healthy dirty flaky noisy steady easy early
    fast slow main
    likely read-only up-to-date done set found known shown seen given taken broken chosen written built run made sent
    paid held kept left lost met read said sold told won begun gone got gotten hidden frozen driven eaten proven spoken
    stolen worn torn grown thrown drawn""".split()
)
#: Endings an English adjective or participle carries strongly enough to describe the subject (deployed, running,
#: available, active, continuous, useful, stateless, optional, public, persistent, temporary); -ment ends a noun
#: (deployment).  ible and ant also end names, so they leave the reading open (``_EN_OPEN_ENDING``).
_EN_STATE_ENDING = re.compile(r"(?:ed|ing|able|ive|ous|ful|less|al|ic|(?<!m)ent|ary)$")
#: What case cannot settle: history may identify it, and a capital may not name it.  ible and ant end a
#: description or a name (compatible, Ansible, vagrant).  default is a status a slot may hold: listed with the state
#: words, "not default" would stay a value after a held default, and a capital would deny "not Default" with none.
_EN_OPEN_WORDS = frozenset({"default"})
_EN_OPEN_ENDING = re.compile(r"(?:ible|ant)$")
#: English verbs of having, holding and doing what a thing is for: negated, they say whether the subject has or does
#: it, a state ("doesn't have a GPU", "doesn't support IPv6", "doesn't work", "won't start").
_EN_HAVING = frozenset(
    "have has had own support include contain allow accept work run exist respond load start connect build compile pass"
    " fit scale".split()
)
#: English verbs of liking, wanting, choosing, using and taking in: negated, they reject what they name ("don't like
#: PowerShell", "no longer uses Jenkins", "never drink coffee", "can't stand X").
_EN_REJECTING = frozenset(
    "like love enjoy prefer want need use choose pick eat drink smoke wear play watch listen drive ride buy take go"
    " stand bear".split()
)
#: How an English negator reads the verb after it: one a subject cannot do (can't connect), one that did not happen
#: (didn't pass), and one a subject does not, will not or no longer does (doesn't support, won't start, never drink).
_EN_UNABLE = re.compile(r"\bcan(?:not|['’]t|\s+not)\b|\bcould(?:n['’]t|\s+not)\b", re.I)
_EN_DID_NOT = re.compile(r"\bdid(?:n['’]t|\s+not)\b", re.I)
_EN_VERBAL = re.compile(
    r"\b(?:do|does|will|wo|would|should|has|have|had)(?:n['’]t|\s+not)\b|\bnever\b|\bno\s+longer\b", re.I
)
#: A complete number-word quantity names a scalar alternative, as a digit does; an age or degree description
#: ("not one week old", 不是一周的) does not match. Missing quantity forms remain values.
_NAMED_QUANTITY = re.compile(
    r"(?:(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen"
    r"|sixteen|seventeen|eighteen|nineteen|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?)\s+"
    r"(?:milliseconds?|seconds?|minutes?|hours?|days?|weeks?|months?|years?|bytes?|kilobytes?|megabytes?|gigabytes?"
    r"|terabytes?|requests?)|[零〇一二两三四五六七八九十百千万亿]+(?:毫秒|秒|分钟|小时|天|周|个月|月|年|字节|次))",
    re.I,
)
#: Where a denial goes on to say what holds instead: "not X but Y", 不是 X 而是 Y, 不是 X，是 Y, 不再用 X 改用 Y.
_CONTRAST = re.compile(
    r"\s*(?:[，,;；]\s*(?:而是|是|改用|改为|改成|换成|换用|换为|用)|而是|改用|改为|改成|换成|换用|换为"
    r"|,?\s*\b(?:but|instead)\b)",
    re.I,
)
#: A particle or a word of time a denial may end its target with, which neither names nor narrows what it denies (了,
#: "any more", "again").
_SINCE = re.compile(
    r"(?:\s*(?:了|啦|呀|啊|以后|now|already|anymore|any\s+(?:more|longer)|no\s+more|at\s+all|(?:ever\s+)?again"
    r"|from\s+now\s+on))+[\s。.!！]*$",
    re.I,
)
#: Kinds whose slot holds a value, so a denial can only say what it is not.  A constraint's negation is the rule
#: itself ("never push to main"), and a decision not to do something is the decision.
_VALUE_SLOT_KINDS = frozenset({"fact", "preference"})
#: Why a denial's own version is a proposal: it denies another value than the one its slot holds; or it is no value
#: of its slot (``mutate.denial_verdict``).
DENIAL_REASON = "denial_of_another_value"
NOT_A_VALUE_REASON = "denial_not_a_value"
#: Why a value ended: a source denies it.
DENIED_REASON = "value_denied"
#: A word of a value as values are compared: one CJK character, or an identifier whole with the marks it carries
#: (v3.1, my_var, ops-pager, a/b, #ops-pager, @owner).
_VALUE_WORD = re.compile(r"[\u3400-\u9fff]|[#@]?[^\W\u3400-\u9fff](?:[./-]?[^\W\u3400-\u9fff])*")
_ARTICLES = frozenset({"a", "an", "the"})


def _value_words(text: str) -> tuple[str, ...]:
    words = _VALUE_WORD.findall(unicodedata.normalize("NFKC", text).casefold())
    return tuple(stem(word) for word in words if word not in _ARTICLES)


#: The copula a denial puts before what it denies ("不是 X", "is not X"), and the verbs it may name it under ("不喜欢用
#: X", "don't like X", "can't stand X"), as ``_value_words`` gives them.
_COPULA = frozenset(_value_words("是 is are was were am be"))
_DENYING_VERBS = _COPULA | frozenset(
    _value_words(
        "有 喜欢 爱 想 要 需要 用 使用 吃 喝 抽 吸 穿 戴 碰 玩 开 住 睡 养 信 怕 在 去 做 坐 骑 买 选 看 听"
        " have has having like liking love want need use using eat drink smoke wear drive play own live work go take"
        " prefer enjoy stand"
    )
)


@dataclass(frozen=True)
class Denial:
    """A denial read from a claim's value (``read_denial``): the words of the target it denies, as ``_value_words``
    gives them and without the time words after it, and whether it goes on to say what holds instead."""

    target: tuple[str, ...]
    contrast: bool

    @property
    def names_a_value(self) -> bool:
        """Whether the target holds more than the verbs of its construction: 不吃了 and "never again" name nothing."""
        return any(word not in _DENYING_VERBS for word in self.target)


def _set_in_capitals(value: str, negator: str) -> bool:
    """Whether a value opening with an English negator is set in capitals, every letter of it: a term as a keyword, a
    status or a flag is written (NOT NULL, NOT FOUND), not a sentence's denial.  Capitals on the negator alone, before
    words in their own case, stress a denial ("NOT Jenkins any more"); a Chinese negator has no case."""
    cased = [char for char in value if char.lower() != char.upper()]
    return any(char.lower() != char.upper() for char in negator) and all(char.isupper() for char in cased)


def _negation(payload) -> re.Match | None:
    """The negation a fact's or a preference's value opens with, unless the value asks ("isn't it X?")."""
    value = str(payload.get("value_text") or "")
    if payload.get("kind") not in _VALUE_SLOT_KINDS or AUTHORITY_QUESTION.search(value):
        return None
    return _NEGATOR.match(value)


def _lemmas(word: str) -> set[str]:
    """The forms an English verb may have before its ending: likes, liking and liked give like, uses and using use,
    running run."""
    forms = {word}
    for ending, restored in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e")):
        if word.endswith(ending) and len(word) > len(ending) + 1:
            base = word[: -len(ending)] + restored
            forms.add(base)
            if not restored and len(base) > 2 and base[-1] == base[-2]:
                forms.add(base[:-1])
    return forms


def _written_as_a_name(word: str) -> bool:
    """Digits, identifier marks and camelCase positively identify a target; capitals alone also spell states."""
    return any(char.isdigit() or char in "#@./_" for char in word) or re.search(r"[a-z][A-Z]", word) is not None


def _english_evidence(negator: str, target: str) -> bool | None:
    """``_target_evidence`` for an English negation, read from the first word it negates that names something.  A
    negation of a state not yet reached (\"not yet\") or of unstressed words alone (\"not really\") describes."""
    words = [word for word in (part.strip(",;:!?\"'()[]").rstrip(".") for part in target.split()) if word]
    if not words or words[0].casefold() == "yet":
        return False
    while words and words[0].casefold() in _EN_UNSTRESSED:
        words = words[1:]
    return _english_word_evidence(negator, words[0], bool(words[1:])) if words else False


def _english_word_evidence(negator: str, head: str, followed: bool) -> bool | None:
    """How ``head``, the first word an English ``negator`` negates that names something, reads; ``followed`` says
    whether more words come after it.  True: a name (identifier marks, camelCase, a capital on a word no clear
    ending or list makes a description and no open reading leaves unresolved) or a verb of liking, using or doing
    aimed at an object.  False: a description, what the subject cannot do, a past that did not happen, a verb of
    having or of what a thing does, a listed adjective, or a clear adjective or participle ending; capitals do not
    make such a word a name.  None: neither.  An open reading (ible, ant, or default) is unresolved in either
    case, so a capital does not deny it and a value the slot holds or held still can."""
    word = head.casefold()
    lemmas = _lemmas(word)
    if _EN_UNABLE.search(negator):
        return followed and bool(lemmas & {"stand", "bear"})
    if _EN_DID_NOT.search(negator) or lemmas & _EN_HAVING:
        return False
    if _written_as_a_name(head):
        return True
    if followed and lemmas & _EN_REJECTING and not word.endswith("ed"):
        return True
    stem = word.rsplit("-", 1)[-1]
    if word in _EN_STATE_WORDS or _EN_STATE_ENDING.search(stem):
        return False
    open_reading = word in _EN_OPEN_WORDS or _EN_OPEN_ENDING.search(stem) is not None
    if (head[:1].isupper() and not open_reading) or (
        followed and bool(lemmas & _EN_REJECTING or _EN_VERBAL.search(negator))
    ):
        return True
    return None


def _target_evidence(negation: re.Match, target: str) -> bool | None:
    """What a negation's own text says of the ``target`` it negates, slot history aside.

    True: it names an alternative or directs liking, using or doing at one: a complete number-word quantity, a
    Chinese verb of choice, Latin text, an identifier or a quotation after 不是 or 并非, an aversion, an English name
    or action (``_english_word_evidence``).  False: it describes its subject: 没, 未, 尚未 and 未曾, 不 before a
    verb of being somewhere, having or opening, 能 or a 会 without 再, a phrase after 不是 or 并非 that ends in 的 or
    opens with a degree word, an English description.  None: the text decides neither (不是主节点, \"not bash\", an
    open reading such as \"not Default\" or \"not Ansible\").
    """
    chinese = negation.group("negator") is not None or negation.group("outright") is not None
    verb = negation.group("verb")
    if _NAMED_QUANTITY.fullmatch(target.removeprefix("是").strip()):
        return True
    if chinese:
        if negation.group("negator") not in (None, "不", "别"):
            return False
        modal = negation.group("modal") or ""
        if verb in _ZH_STATE_VERBS or "能" in modal or re.search("会(?!再)", modal):
            return False
        if verb == "是" or negation.group("outright") in {"并非", "绝非"}:
            description = target.removeprefix("是").strip()
            if description.endswith("的") or _ZH_DEGREE.match(description):
                return False
            return True if re.search(r"[A-Za-z0-9#@./_]|[\"'“‘「『]", description) else None
        return True
    if negation.group("dislike") or re.match(r"\s*[\"'“‘「『].+?[\"'”’」』]", target):
        return True
    return _english_evidence(negation.group(), target)


def read_denial(payload, held_values=()) -> Denial | None:
    """The denial a fact's or a preference's value is, or None: read from its own text first (``_target_evidence``),
    then from the values its slot holds or held (``held_values``) where the text decides nothing.

    A value whose text describes its subject is the slot's value even when it negates the value held: 不在线 after
    在线, \"not online\" after online, \"not Configured\" after configured, 不是默认的 after 默认的 each say the state
    changed.  History settles only what the text leaves open: \"not bash\" or 不是主节点 denies bash or 主节点 where
    the slot holds or held it, and is a value where it never did.  An open reading stays unresolved in either case, so
    \"not Ansible\", \"not vagrant\" and \"not Default\" are not names by their capitals, and a held Ansible, vagrant
    or default is denied.  Descriptions form an open set, so an unrecognized
    one stays a value rather than withdraw stored data; that ambiguity is the accepted cost.  A value that goes on to
    say what holds instead (\"not X but Y\") is a denial whatever its target.  Fixed expressions and polar answers
    remain values; capitals throughout supply no evidence without matching history.  Target comparison uses the
    same normalization as ``negates``; a contrast and trailing time words belong to the construction, not the target.
    """
    negation = _negation(payload)
    if negation is None:
        return None
    value = str(payload.get("value_text") or "")
    rest = value[negation.end() :]
    contrast = _CONTRAST.search(rest)
    target = _SINCE.sub("", rest if contrast is None else rest[: contrast.start()])
    read = Denial(_value_words(target), contrast is not None)
    if not read.names_a_value:
        return None
    evidence = _target_evidence(negation, target)
    if evidence is False and contrast is None:
        return None
    if any(_target_negates(read, held) for held in held_values):
        return read
    if _set_in_capitals(value, negation.group()):
        return None
    return read if contrast is not None or evidence else None


def is_denial(payload, held_values=()) -> bool:
    """Whether a claim's value says what its slot is not (``read_denial``): "not CircleCI", "不吃辣", "don't like
    PowerShell"; not 不错, 没问题, 不一定喜欢 X, "not bad", "isn't it X?", NOT NULL, NOT_STARTED, 未开始, 不在线 or
    "not started", which say what state their subject is in."""
    return read_denial(payload, held_values) is not None


def negates(denial, held) -> bool:
    """Whether the denial ``denial`` says that the claim ``held`` (a payload) does not hold.

    The target the denial denies (``read_denial``) and ``held``'s value are compared as the store compares values
    (NFKC, case-folded), word by word: a CJK character is a word, an identifier is one word with its marks (v3.1,
    #ops-pager), a Latin word is taken by the stem the fact gate gives a predicate's words, and an article is no word.
    The denial negates the held value when the held value holds all the target after a copula (不是周三 negates
    周三下午, 不吃辣 negates 爱吃辣, "don't drink coffee" negates "drinks coffee"), or when the target is the held
    value itself under the verbs of the construction ("don't like PowerShell" negates PowerShell, 不喜欢喝红茶 negates
    红茶).  A longer target does not negate a shorter value: its other words narrow what it denies, so 不是周三下午
    leaves 周三 standing, as "not v3" leaves v3.1, "not 4 GB" 14 GB and 不喜欢喝红茶 茶.  A target of verbs alone
    (不吃了) negates nothing.
    """
    read = read_denial(denial, (held,))
    return read is not None and _target_negates(read, held)


def _target_negates(read: Denial, held) -> bool:
    """Compare a target with a held assertion, never with another negation of it: "not fast" cannot deny itself."""
    value = str(held.get("value_text") or "")
    if _NEGATOR.match(value):
        return False
    holds = _value_words(value)
    if not read.names_a_value or not holds:
        return False
    denied = read.target
    named = denied[1:] if denied[0] in _COPULA and len(denied) > 1 else denied
    lead = len(denied) - len(holds)
    return _runs_within(holds, named) or (
        lead > 0 and denied[lead:] == holds and all(word in _DENYING_VERBS for word in denied[:lead])
    )


def _runs_within(words: tuple[str, ...], run: tuple[str, ...]) -> bool:
    """Whether ``run`` stands in ``words`` as consecutive words."""
    return any(words[at : at + len(run)] == run for at in range(len(words) - len(run) + 1))


def evidence_refs(proposal: ClaimProposal) -> tuple[str, ...]:
    refs = [f"{s['source_ref']}@{s['source_revision']}" for s in proposal["evidence_spans"]]
    intention = proposal.get("intention")
    if intention is not None:
        refs.extend(intention["state_evidence_refs"])
    procedure = proposal.get("procedure")
    if procedure is not None:
        refs.extend(procedure["counterexample_refs"])
    return tuple(dict.fromkeys(refs))


def verified_human_ref(root: RootEvidence) -> str | None:
    return verified_human_principal_ref(root.source_principal)


def _unresolved_subject(proposal: ClaimProposal, roots: tuple[RootEvidence, ...]) -> str:
    refs = sorted(
        f"{root.ref}@{root.revision}"
        for root in roots
        if any(
            span["source_ref"] == root.ref and span["source_revision"] == root.revision
            for span in proposal["evidence_spans"]
        )
    )
    digest = hashlib.sha256(json.dumps(refs, separators=(",", ":")).encode()).hexdigest()
    return f"unresolved-source:{digest}"


def bind_claim_subject(
    proposal: ClaimProposal,
    roots: tuple[RootEvidence, ...],
    *,
    stored: bool = False,
) -> tuple[ClaimProposal, bool, str | None]:
    """Bind a first-person model label to C1 evidence, or isolate it safely.

    The model may repeat ``我`` or a legacy self label, but it cannot mint an
    authority key. Unresolved sources receive a source-specific non-authority
    slot so different speakers in one shared scope cannot collapse together.
    A claim already in the store (``stored``) carries the principal its self
    label was bound to when it was written, and is bound from that label again,
    so re-judging reads the person's own claims as writing did.
    """
    relevant = tuple(
        root
        for root in roots
        if effective_origin(root) == "human_direct"
        and any(
            span["source_ref"] == root.ref and span["source_revision"] == root.revision
            for span in proposal["evidence_spans"]
        )
    )
    written = stored and {verified_human_ref(root) for root in relevant} == {proposal["subject"]}
    if proposal["subject"].casefold() not in SELF_SUBJECTS and not written:
        return proposal, False, None
    bound = deepcopy(proposal)

    def spans_of(root):
        return [
            span
            for span in proposal["evidence_spans"]
            if span["source_ref"] == root.ref and span["source_revision"] == root.revision
        ]

    # The context around a quote can leave out what marks pasted text, a speaker's label above it or the fence around
    # it: the whole message is asked.
    if not relevant or not any(
        self_report_bound(
            "\n".join(evidence_context(root.content, span["quote"]) for span in spans_of(root)),
            proposal["value_text"],
            kind=proposal["kind"],
        )
        and not any(in_pasted_text(root.content, span["quote"]) for span in spans_of(root))
        for root in relevant
    ):
        bound["subject"] = _unresolved_subject(proposal, roots)
        return bound, False, "subject_not_bound"
    principals = tuple(verified_human_ref(root) for root in relevant)
    if any(principal is None for principal in principals):
        bound["subject"] = _unresolved_subject(proposal, roots)
        return bound, False, "source_identity_unresolved"
    unique = set(principals)
    if len(unique) != 1:
        bound["subject"] = _unresolved_subject(proposal, roots)
        return bound, False, "source_identity_conflict"
    bound["subject"] = next(iter(unique))
    return bound, True, None


def effective_origin(root: RootEvidence) -> str:
    return (
        (root.original_origin or "origin_unknown")
        if root.origin == "imported" and root.import_verified
        else ("origin_unknown" if root.origin == "imported" else root.origin)
    )


def grounded_time(value: str | None, roots: tuple[RootEvidence, ...]) -> bool:
    if value is None:
        return True
    canonical_value = canonical_time(value)
    if canonical_value is None:
        return False
    stamp = datetime.fromisoformat(canonical_value)
    for root in roots:
        if root.occurred_at is not None and canonical_time(root.occurred_at) == canonical_time(value):
            return True
        text = root.content
        dates = (stamp.strftime("%Y-%m-%d"), f"{stamp.year}年{stamp.month}月{stamp.day}日")
        if any(date in text for date in dates):
            return True
        if root.occurred_at is not None:
            canonical_source_time = canonical_time(root.occurred_at)
            if canonical_source_time is None:
                continue
            source_time = datetime.fromisoformat(canonical_source_time)
            if source_time.year == stamp.year and f"{stamp.month}月{stamp.day}日" in text:
                return True
            if (
                "今天" in text
                and source_time.date() == stamp.date()
                and stamp.hour == stamp.minute == stamp.second == stamp.microsecond == 0
            ):
                return True
            # End of the explicitly stated one-day exception is the following
            # UTC boundary only when the source itself supplies a UTC timestamp.
            if (
                "只限今天" in text
                and (stamp.date() - source_time.date()).days == 1
                and stamp.hour == stamp.minute == stamp.second == stamp.microsecond == 0
            ):
                return True
    return False


def evidence_context(content: str, quote: str) -> str:
    """Include the quote's sentence so trimming cannot discard its negation.

    Unrelated sentences are not evidence for this assertion. A preceding label
    ending in a colon belongs to the quote (for example a fictional scenario).
    Repeated indistinguishable quotations retain all their possible contexts.
    """
    contexts = []
    start = 0
    for _ in range(128):
        at = content.find(quote, start)
        if at < 0:
            break
        before = list(re.finditer(r"[。！？!?;；\n]", content[:at]))
        left = before[-1].end() if before else 0
        quote_end = at + len(quote)
        if quote.rstrip(" \t\r\"“”’'").endswith(tuple("。！？!?;；\n")):
            right = quote_end
        else:
            following = re.search(r"[。！？!?;；\n]", content[quote_end:])
            right = quote_end + following.end() if following else len(content)
        prefix = content[:left].rstrip()
        if prefix.endswith((":", "：")):
            header = re.split(r"[。！？!?;；\n]", prefix)[-1]
            left = max(0, left - len(header) - 1)
        contexts.append(content[left:right])
        start = at + max(1, len(quote))
    if content.find(quote, start) >= 0:
        raise ContractError("DERIVATION_INVALID", "ambiguous_quote_locations")
    return "\n".join(dict.fromkeys(contexts))


#: Real sentence punctuation *plus* the two-character escapes that stand in for
#: it when a tool envelope is stored as a JSON string body.
_ASSERTION_BREAK = re.compile(r"[。！？!?;；\n]|\\[nrt]")


def _ends_clause(quote: str) -> bool:
    """Whether the quote's own final character already closes its clause."""
    trimmed = quote.rstrip(" \t\r\"\u201c\u201d\u2019'")
    if not trimmed:
        return False
    breaks = list(_ASSERTION_BREAK.finditer(trimmed))
    return bool(breaks) and breaks[-1].end() == len(trimmed)


def assertion_clause(content: str, quote: str) -> str:
    """Return the clause the quote sits in, treating escaped breaks as breaks.

    ``evidence_context`` cuts on real punctuation only, because its job is to
    keep a quote's negation attached to it.  A tool envelope stored as an
    escaped JSON body has no real punctuation to cut on: measured in one store,
    **all 16** claims rejected as ``question_not_asserted`` came from sources
    with zero real newlines and 14 to 46 escaped ones, so the context ran 155 to
    1,581 characters and one ``?`` anywhere inside vetoed the claim.  Twelve of
    the sixteen had no question marker in the quoted text at all -- the rejected
    statements were version lines such as ``Version: 3.1.0.dev2``.

    This is for tests that must judge *this* assertion rather than everything
    the source happens to mention.  Splitting on ``\\n`` also splits a literal
    Windows path such as ``C:\\new``, which can only ever narrow what such a
    test reads -- acceptable here, and the reason this is not used for the
    negation, polarity or condition checks, which keep the wider context.
    """
    clauses = []
    start = 0
    closes = _ends_clause(quote)
    for _ in range(128):
        at = content.find(quote, start)
        if at < 0:
            break
        before = list(_ASSERTION_BREAK.finditer(content, 0, at))
        left = before[-1].end() if before else 0
        end = at + len(quote)
        # A quote whose own last character is the break already ends its
        # clause.  Searching onwards from there would find the *next*
        # sentence's terminator and pull that whole sentence in, so a question
        # standing in the sentence after a correct assertion would veto it.
        # ``evidence_context`` has the same guard.
        if closes:
            right = end
        else:
            after = _ASSERTION_BREAK.search(content, end)
            right = after.end() if after else len(content)
        clauses.append(content[left:right])
        start = at + max(1, len(quote))
    return "\n".join(dict.fromkeys(clauses)) if clauses else quote


_UNASSERTED_STATEMENTS = frozenset({"request", "proposal", "hypothetical", "quotation", "fictional", "unknown"})
HUMAN_ONLY_KINDS = frozenset({"preference", "constraint", "decision", "intention", "alias"})
#: Origins a cited root may lend authority from; ``_authority`` needs one of them.
AUTHORITY_ORIGINS = ("human_direct", "tool_observation", "external_document")
_POLARITY_KINDS = frozenset({"fact", "preference", "constraint", "decision"})


@dataclass(frozen=True)
class _Cited:
    """One proposal against the complete root spans it actually cites.

    ``roots`` are narrowed to the sentence radius around each quote; ``quoted``
    is the quotes themselves; ``asserted`` is the clause each quote was lifted
    from, taken before narrowing because the negation and polarity rules keep
    the wider radius.  ``human``/``observed``/``documents`` partition the roots
    by effective origin.
    """

    proposal: ClaimProposal
    roots: tuple[RootEvidence, ...]
    quoted: str
    asserted: str
    cited_spans: tuple[tuple[str, str], ...]
    source_text: str
    human: tuple[RootEvidence, ...]
    observed: tuple[RootEvidence, ...]
    documents: tuple[RootEvidence, ...]
    subject_bound: bool
    explicit_attribute_correction: bool

    @property
    def kind(self) -> str:
        return self.proposal["kind"]

    @property
    def subject(self) -> str:
        return self.proposal["subject"]


def _cite(
    proposal: ClaimProposal,
    roots: tuple[RootEvidence, ...],
    *,
    subject_bound: bool,
    explicit_attribute_correction: bool,
) -> _Cited | None:
    """Pair every cited span with its complete root; None when no root qualifies."""
    spans = proposal["evidence_spans"]

    def cited(root: RootEvidence):
        return [span for span in spans if span["source_ref"] == root.ref and span["source_revision"] == root.revision]

    # A summary may link to a root for lineage, but cannot lend its own wording
    # that root's authority.  Promotion requires a span on the actual root.
    roots = tuple(r for r in roots if cited(r) and r.capture_state == "complete" and not r.capture_gaps)
    if not roots:
        return None
    narrowed = tuple(
        replace(r, content="\n".join(evidence_context(r.content, span["quote"]) for span in cited(r))) for r in roots
    )
    by_origin = {origin: tuple(r for r in narrowed if effective_origin(r) == origin) for origin in AUTHORITY_ORIGINS}
    return _Cited(
        proposal,
        narrowed,
        quoted="\n".join(span["quote"] for span in spans),
        asserted="\n".join(assertion_clause(r.content, span["quote"]) for r in roots for span in cited(r)),
        cited_spans=tuple((span["quote"], r.content) for r in roots for span in cited(r)),
        source_text="\n".join(r.content for r in narrowed),
        human=by_origin["human_direct"],
        observed=by_origin["tool_observation"],
        documents=by_origin["external_document"],
        subject_bound=subject_bound,
        explicit_attribute_correction=explicit_attribute_correction,
    )


# Each rule answers with the refusal reason, or None.  Order matters: the first
# refusal wins, and later rules assume the earlier ones passed.
def _not_asserted(c: _Cited) -> str | None:
    if c.proposal["statement_kind"] in _UNASSERTED_STATEMENTS:
        return "statement_not_asserted"
    if AUTHORITY_QUESTION.search(c.asserted or c.source_text):
        return "question_not_asserted"
    if (
        _HYPOTHETICAL.search(c.source_text)
        or _UNDECIDED.search(c.source_text)
        or UNASSERTED_UNCERTAINTY.search(c.source_text)
    ):
        return "hypothetical_or_undecided"
    if is_transient_request(c.source_text):
        return "transient_request_not_durable"
    if (
        c.kind in {"preference", "constraint", "decision"} and _QUOTED_OTHER.search(c.source_text)
    ) or attributed_to_another(c.source_text):
        return "other_speaker"
    return None


def _scope_preserved(c: _Cited) -> str | None:
    conditions, valid_to = c.proposal["conditions"], c.proposal["valid_to"]
    if any(
        not condition_supports_value(c.source_text, condition, c.proposal["value_text"]) for condition in conditions
    ):
        return "condition_not_supported"
    # "要是值班表调整，告警频道是 X" holds only under its condition: a claim that drops it would say X holds now.
    if CONDITIONAL.search(c.source_text) and not conditions:
        return "condition_not_preserved"
    if _LIMIT.search(c.source_text) and not conditions and valid_to is None:
        return "missing_limitation"
    for marker in RELATIVE_SCOPE.finditer(c.source_text):
        if valid_to is None and not any(bound_literal(condition, marker.group()) for condition in conditions):
            return "relative_scope_not_preserved"
    return None


def _authority(c: _Cited) -> str | None:
    if not (c.human or c.observed or c.documents):
        return "no_independent_authority"
    if c.kind in HUMAN_ONLY_KINDS and not c.human:
        return "requires_human_source"
    if any(not grounded_time(c.proposal[field], c.roots) for field in ("valid_from", "valid_to")):
        return "time_not_grounded"
    return None


def _subject_binds(c: _Cited) -> str | None:
    # Aliases have a separate, exact old-name -> target proof in apply_claim;
    # that proof may name the sourced old label instead of its internal subject.
    if c.subject_bound or c.kind == "alias":
        return None
    subject, kind = c.subject, c.kind
    self_bound = subject.casefold() in SELF_SUBJECTS and self_report_bound(
        c.source_text, c.proposal["value_text"], kind=kind
    )
    if subject in c.quoted and not bound_literal(c.quoted, subject):
        return "subject_not_bound"
    if subject == "我" and not self_bound:
        return "subject_not_bound"
    # The subject may also be stated verbatim in the sentence next to the quote,
    # in the same source; see core/subject_binding.py.  Nothing is inferred: only
    # literal text from the cited source counts, and a subject that merely
    # points ("you", "the agent") is refused at any distance.
    if not bound_literal(c.quoted, subject) and not neighbourhood_binds(subject, c.cited_spans) and not self_bound:
        return "subject_not_bound"
    return None


def _intention_proved(c: _Cited) -> str | None:
    if c.kind != "intention":
        return None
    intention = c.proposal.get("intention")
    if intention is None:
        return "intention_missing"
    if intention["target"] not in c.source_text and c.proposal["predicate"] not in c.source_text:
        return "intention_target_unproved"
    state = intention["state"]
    if state == "pending" and intention["cue"] not in c.source_text:
        return "intention_cue_unproved"
    state_sources = set(intention["state_evidence_refs"])
    authorized = "\n".join(
        r.content
        for r in c.roots
        if f"{r.ref}@{r.revision}" in state_sources and effective_origin(r) in {"human_direct", "tool_observation"}
    )
    if state == "completed" and (
        not asserted_marker(_COMPLETION, authorized) or _NEGATED_COMPLETION.search(authorized)
    ):
        return "completion_unproved"
    if state == "cancelled" and not asserted_marker(_CANCELLATION, authorized):
        return "cancellation_unproved"
    if state == "expired" and c.proposal["valid_to"] is None:
        return "expiry_unproved"
    return None


def _procedure_proved(c: _Cited) -> str | None:
    if c.kind != "procedure":
        return None
    procedure = c.proposal.get("procedure")
    if procedure is None:
        return "procedure_missing"
    if any(step not in c.source_text for step in procedure["method"]):
        return "method_steps_unproved"
    if any(condition not in c.source_text for condition in procedure["non_applicable"]):
        return "method_exception_unproved"
    verification = procedure["verification_basis"]
    if verification == "user_accepted" and not any(asserted_marker(_ACCEPTANCE, r.content) for r in c.human):
        return "method_acceptance_unproved"
    if verification == "observed_once" and not c.observed:
        return "method_observation_unproved"
    if verification in {"inferred_suggestion", "unknown"}:
        return "method_is_suggestion"
    return None


def _fact_entailed(c: _Cited) -> str | None:
    """Ordinary durable facts keep the claim-frame support check.  A person's negation of themselves, a denial or a
    state ("I don't have a car", 我不吃辣, 我没有车), holds its verb in its value, so no frame orders subject, relation
    and value in it; bound to their own clause (``bind_claim_subject``), it stands on that as their preference does."""
    if c.kind != "fact" or c.explicit_attribute_correction or (c.subject_bound and _negation(c.proposal) is not None):
        return None
    try:
        draft = ClaimDraft.from_parts(
            subject=c.subject,
            predicate=c.proposal["predicate"],
            value=c.proposal["value_text"],
            scope_id="qualification-only",
        )
        evidence = tuple(
            EvidenceReference(
                "direct_user" if effective_origin(root) == "human_direct" else "external_record",
                root.ref,
                evidence_context(root.content, span["quote"]),
                c.subject if c.subject_bound and verified_human_ref(root) == c.subject else "",
            )
            for root in (*c.human, *c.observed, *c.documents)
            for span in c.proposal["evidence_spans"]
            if span["source_ref"] == root.ref and span["source_revision"] == root.revision
        )
    except ValueError:
        return "fact_entailment_unproved"
    if any(evidence_supports_claim(item, draft) for item in evidence) or any(
        evidence_supports_relation(item, draft) for item in evidence
    ):
        return None
    return "fact_entailment_unproved"


def _value_preserved(c: _Cited) -> str | None:
    value, conditions = c.proposal["value_text"], c.proposal["conditions"]
    if c.kind not in VALUE_FREE_KINDS and value not in c.quoted:
        return "value_not_supported_by_quote"
    if c.kind in _POLARITY_KINDS and not preserves_qualifiers(
        c.source_text, value, conditions=conditions, polarity_only=True
    ):
        return "value_polarity_not_preserved"
    if c.kind in {"preference", "decision"} and _NEGATION.search(c.source_text) and not _NEGATION.search(value):
        if not rejects_other_value(c.source_text, c.proposal):
            return "negation_not_preserved"
    return None


def rejects_other_value(content, proposal):
    """An explicit 'X, not Y' does not negate the positive X clause.

    A general denial of the statement remains a denial. Every negative clause
    must be an isolated rejected alternative; questions/uncertainty are still
    checked by the owning qualifier.
    """
    clauses = re.split(r"[，,;；。!?！？\n]", content)
    subject, predicate, value = (proposal[k] for k in ("subject", "predicate", "value_text"))
    frame = re.compile(
        re.escape(subject) + r"\s*(?:应当|应该|应|仍然|仍)?" + re.escape(predicate) + r"\s*" + re.escape(value)
    )
    if not any(frame.search(c) and not _NEGATION.search(c) for c in clauses):
        return False
    negatives = [c.strip() for c in clauses if _NEGATION.search(c)]
    if not negatives:
        return False
    for clause in negatives:
        match = re.fullmatch(r"(?:不是|并非)([^\s，,。;；!?！？]{1,120})", clause)
        if (
            not match
            or value in match.group(1)
            or re.search(r"事实|确认|真的|真实|认真|意思|偏好|断言|承诺|决定|成立|正确", match.group(1))
        ):
            return False
    return True


def _time_scope_known(c: _Cited) -> str | None:
    temporal = classify_durable_state_clause(c.source_text)
    valid_from, valid_to = c.proposal["valid_from"], c.proposal["valid_to"]
    if temporal == "past" and valid_from is None:
        return "historical_start_unknown"
    if temporal == "future" and valid_from is None and c.kind != "intention":
        return "future_start_unknown"
    if temporal == "temporary" and valid_to is None and not c.proposal["conditions"]:
        return "temporary_scope_unknown"
    return None


_REFUSALS = (
    _not_asserted,
    _scope_preserved,
    _authority,
    _subject_binds,
    _intention_proved,
    _procedure_proved,
    _fact_entailed,
    _value_preserved,
    _time_scope_known,
)


def qualify(
    proposal: ClaimProposal,
    roots: tuple[RootEvidence, ...],
    *,
    project_id: str | None = None,
    explicit_attribute_correction: bool = False,
    _subject_bound: bool = False,
) -> Qualification:
    """Source roles are necessary, never sufficient, to promote a proposal.

    P01 has already checked exact evidence spans against these source versions.
    This rule retains unsupported interpretation as proposed. It neither invents
    dates nor converts the model's requested state into governance authority.
    """
    if not _subject_bound:
        bound_proposal, subject_bound, binding_issue = bind_claim_subject(proposal, roots)
        if binding_issue is not None:
            return Qualification("proposed", "inferred_suggestion", binding_issue)
        if subject_bound:
            return qualify(
                bound_proposal,
                roots,
                project_id=project_id,
                explicit_attribute_correction=explicit_attribute_correction,
                _subject_bound=True,
            )
    cited = _cite(
        proposal, roots, subject_bound=_subject_bound, explicit_attribute_correction=explicit_attribute_correction
    )
    if cited is None:
        return Qualification("proposed", "inferred_suggestion", "no_complete_root_span")
    for rule in _REFUSALS:
        reason = rule(cited)
        if reason is not None:
            return Qualification("proposed", "inferred_suggestion", reason)
    if cited.human:
        return Qualification("active", "direct_report", "explicit_scoped_source")
    return Qualification("active", "observed", "observation_at_source_time")


def valid_at(version: ClaimVersion, instant: str, *, historical: bool) -> bool:
    when = canonical_time(instant)
    start, end = canonical_time(version.valid_from), canonical_time(version.valid_to)
    if historical and start is None:
        return False
    return (start is None or (when is not None and start <= when)) and (
        end is None or (when is not None and when < end)
    )


def select_proposal(versions: tuple[ClaimVersion, ...], instant: str) -> ClaimVersion | None:
    """The head of a claim that qualification has never promoted.

    ``select_effective`` deliberately returns None for these, and it is right to:
    a proposal is not an effective version and must never be answered as if it
    were settled. But that alone removes them from recall entirely, and in a real
    store almost the whole derived layer can sit in this state (231 proposed
    against 11 active in one), so most of what consolidation produces would be
    invisible to every caller, with no error and no gap to notice it by.

    Returning the head here lets retrieval admit it *labelled*, so the reader can
    weigh it, instead of the system silently having nothing to show.

    Not for historical queries: ``as_of`` asks what was true at a past instant,
    and an unpromoted proposal cannot answer that.
    """
    if not versions:
        return None
    heads = [v for v in versions if v.revision == 1 or v.replaces_revision is not None]
    if not heads:
        return None
    head = max(heads, key=lambda v: v.revision)
    if head.state != "proposed" or not valid_at(head, instant, historical=False):
        return None
    return head


def select_effective(
    versions: tuple[ClaimVersion, ...], instant: str, *, as_of: bool = False, known_at: str | None = None
) -> ClaimVersion | None:
    if known_at is not None:
        known_time = canonical_time(known_at)
        if known_time is None:
            return None
        versions = tuple(
            v for v in versions if (recorded := canonical_time(v.recorded_from)) is not None and recorded <= known_time
        )
    if not versions:
        return None
    heads = [v for v in versions if v.revision == 1 or v.replaces_revision is not None]
    head = max(heads, key=lambda v: v.revision)
    # A replacement with an unknown effective date can establish an explicitly
    # current assertion, but supplies no answer about a past date.
    if as_of and head.valid_from is None and head.state != "proposed":
        return None
    if head.state != "proposed" and valid_at(head, instant, historical=as_of):
        return None if head.state == "retracted" else head
    candidates = [
        v
        for v in versions
        if v.state in {"active", "superseded", "disputed", "retracted"}
        and valid_at(v, instant, historical=as_of or head.state == "disputed")
    ]
    if not candidates:
        return None
    chosen = max(candidates, key=lambda v: (canonical_time(v.valid_from) or "", v.revision))
    return None if chosen.state == "retracted" else chosen
