"""Who a store's owner is called, and a question that names them, asked as they would ask it themselves.

First-person statements can omit the owner's name, while questions about that owner can include it.  Recall
therefore searches a first-person form alongside the original question and also applies lexical admission without
the name.  The original query remains available for text that names the owner.

The names are the installation's ``owner_names`` and the name the host attests for the person asking
(``TrustedSourcePrincipal.display_name``), never a name a model or a message supplies.  No name is built in: every
installation reads its own owner's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .events import CJK_RUN

#: How many names an installation may configure, and how long each may be (``RuntimeInstanceConfig.owner_names``).
OWNER_NAMES_LIMIT = 8
OWNER_NAME_LENGTH = 64

_CJK_CLASS = CJK_RUN.pattern.removesuffix("+")
#: Space beside Chinese text, which a name taken out or turned into 我 leaves behind; a line break stays.
_SPACE_BY_CJK = re.compile(rf"[^\S\n]+(?={_CJK_CLASS})|(?<={_CJK_CLASS})[^\S\n]+")
#: A host's account id, not a name: one run of letters and digits with a digit in it, at least 12 long (a WeCom
#: external user id arrives where a gateway puts the user's name).
_ACCOUNT_ID = re.compile(r"(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{12,}")
#: What a name may be wrapped in, taken off before it is read.
_WRAPPING = " \"'“”‘’()[]{}<>.,;:!?|*~_-"


def name_forms(display_name: object) -> tuple[str, ...]:
    """What a person is called, from the name a host attests: the whole name and, for a name of several words, the
    first, the one people are called by (``NameOne SurnameOne`` gives ``NameOne SurnameOne`` and ``NameOne``).
    The others are left out: a middle name or a surname (``Ann``, ``Lee`` of ``Mary Ann Lee``) is someone else's as
    often.  A host's account id and a name without a letter give nothing; a first word of one character gives the
    whole name alone."""
    if type(display_name) is not str:
        return ()
    name = " ".join(display_name.split()).strip(_WRAPPING)
    if not 2 <= len(name) <= OWNER_NAME_LENGTH or _ACCOUNT_ID.fullmatch(name):
        return ()
    if not any(char.isalpha() for char in name):
        return ()
    first = name.split(" ")[0]
    called = (first,) if " " in name and len(first) >= 2 and any(char.isalpha() for char in first) else ()
    return tuple(dict.fromkeys((name, *called)))


def owner_names(configured: tuple[str, ...], principal: object) -> tuple[str, ...]:
    """The names recall reads as the owner's: the installation's own, and those of the name the host attests for the
    person asking, when it verified them as a human."""
    attested: tuple[str, ...] = ()
    if getattr(principal, "kind", None) == "human" and getattr(principal, "resolution", None) == "verified":
        attested = name_forms(getattr(principal, "display_name", None))
    return tuple(dict.fromkeys((*configured, *attested)))


def _chinese(text: str) -> bool:
    return CJK_RUN.search(text) is not None


def _glued(text: str, index: int) -> bool:
    """Whether the character at ``index`` runs a Latin name on into a longer word, name or identifier: a letter or a
    digit of any script but Chinese, which is written straight against a name (``NameOne对辣``), or ``_./-\\`` of a
    path or an identifier."""
    if not 0 <= index < len(text):
        return False
    char = text[index]
    return char in "_./-\\" or (char.isalnum() and not _chinese(char))


#: An ASCII word beginning with an uppercase letter after a Latin name is treated as a possible continuation of that
#: name (``NameOne SurnameTwo``, ``NameOne SURNAMETWO``); the pronoun ``I`` is excluded.
_NEXT_WORD = re.compile(r"[^\S\n]+(?!I\b)([A-Z][A-Za-z]+)")
#: What may follow a Chinese name in a question about the person: a particle, a preposition, a common verb or adverb,
#: a time.  Another character runs the name on into someone's longer one (``王芳芳``, ``王芳林``), which is no name
#: of the owner's.  Not every word is here: a name missed is a question recalled as asked.
_AFTER_A_CHINESE_NAME = frozenset(
    "的对是在有和跟与及同说讲提问喜爱想要会能可该应得当了吗呢吧啊呀也都还就又再才被把给让叫去来做用最更不没别很太挺"
    "一怎什为从到向往比像如觉认希期平每经曾正刚已总常今昨明前上下之们自本那这家住生买吃喝穿看听写读玩选定计打准需所"
)


#: The English possessive after a name, and the Chinese one, which may stand a space apart from a Latin name.
_OWNS = ("'s", "’s")
_OWNED_BY_DE = re.compile(r"[^\S\n]*的")


def _spans(query: str, name: str, names: tuple[str, ...]) -> list[tuple[int, int, bool]]:
    """Where ``name`` stands in ``query``, as (start, end, possessive), the end past a possessive after it (``的`` in
    a Chinese question, ``'s`` in another).

    Latin names are matched case-sensitively at word boundaries so ordinary lowercase words are not mistaken for
    names (``May`` does not match ``may``).  A following capitalized word extends the name unless that word is also
    configured in ``names``.  A Chinese name
    runs into the words around it, as Chinese is written (``王芳喜欢什么``), and is followed by what a question about
    the person goes on with (``_AFTER_A_CHINESE_NAME``), not by more of a longer name (``王芳芳``).  A name of one
    character is too short to tell from a word."""
    if len(name) < 2:
        return []
    chinese = _chinese(name)
    spans: list[tuple[int, int, bool]] = []
    for match in re.finditer(re.escape(name), query):
        start, end = match.span()
        if not chinese and (_glued(query, start - 1) or _glued(query, end)):
            continue
        if chinese and end < len(query) and _chinese(query[end]):
            # 王明明: the name's last character again is more of a name, whatever else it may begin.
            if query[end] not in _AFTER_A_CHINESE_NAME or query[end] == name[-1]:
                continue
        following = None if chinese else _NEXT_WORD.match(query, end)
        if following is not None and following.group(1) not in names:
            continue
        owned = _OWNED_BY_DE.match(query, end)
        if owned:
            spans.append((start, owned.end(), True))
        elif query[end : end + 2] in _OWNS and not _glued(query, end + 2):
            spans.append((start, end + 2, True))
        else:
            spans.append((start, end, False))
    return spans


def _joined(spans: list[tuple[int, int, bool]], query: str) -> list[tuple[int, int, bool]]:
    """Names that stand next to each other with only space between them, one person (``Mary Ann's``)."""
    joined: list[tuple[int, int, bool]] = []
    for start, end, possessive in sorted(spans):
        if joined and not joined[-1][2] and not query[joined[-1][1] : start].strip():
            joined[-1] = (joined[-1][0], end, possessive)
        else:
            joined.append((start, end, possessive))
    return joined


@dataclass(frozen=True)
class OwnerView:
    """A question that names the owner, as they would ask it: ``first`` in the first person, for the vector channel;
    ``bare`` without their name, for lexical admission."""

    first: str
    bare: str


def _tidy(text: str, chinese: bool) -> str:
    """The question without the spaces a replacement left doubled or at a line's ends; its lines stay lines."""
    text = "\n".join(" ".join(line.split()) for line in text.split("\n"))
    return _SPACE_BY_CJK.sub("", text) if chinese else text


@lru_cache(maxsize=256)
def owner_view(query: str, names: tuple[str, ...]) -> OwnerView | None:
    """The question asked as the owner would ask it, or None when it names none of ``names``.  The longest name is
    read first (``NameOne SurnameOne`` before ``NameOne``), and a question in Chinese takes 我 where an English one
    takes I."""
    chosen: list[tuple[int, int, bool]] = []
    for name in sorted(set(names), key=lambda name: (-len(name), name)):
        for span in _spans(query, name, names):
            if all(span[1] <= start or span[0] >= end for start, end, _possessive in chosen):
                chosen.append(span)
    if not chosen:
        return None
    chinese = _chinese(query)
    first = bare = query
    for start, end, possessive in reversed(_joined(chosen, query)):
        person = ("我的" if possessive else "我") if chinese else ("my" if possessive else "I")
        first = f"{first[:start]} {person} {first[end:]}"
        bare = f"{bare[:start]} {bare[end:]}"
    return OwnerView(first=_tidy(first, chinese), bare=_tidy(bare, chinese))
