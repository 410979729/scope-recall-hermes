"""The packet's size estimate, counted without a Python step per character.

The per-character loop cost 180 ms of a 2.5 s automatic recall on the shared store: the packet weighs every
candidate it considers, 1.9 million characters in that recall.  The estimate itself must not move, so the
rewrite is checked against the loop it replaced.
"""

import random
import unicodedata

from scope_recall.core.recall_budget import estimate_tokens


def _reference(text: str) -> int:
    """The estimate as it was counted before, one character at a time."""

    def cjk(code: int) -> bool:
        return (
            0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF
            or 0x20000 <= code <= 0x323AF
        )

    quarters = 0
    for char in text:
        code = ord(char)
        if code < 128 and (char.isalnum() or char.isspace()):
            quarters += 1
        elif cjk(code) or unicodedata.category(char)[0] in "LNMPZ":
            quarters += 4
        else:
            quarters += 4 * len(char.encode("utf-8"))
    return max(1, (quarters + 3) // 4)


SAMPLES = [
    "",
    " ",
    "a",
    "hello world",
    "TEST 项目偏好白色。",
    "继续",
    "def f(x): return x+1  # ok",
    "emoji 🧪🚀 and ✓ marks ≤ ≥ ∑",
    "\x00\x1c\x1f\x7f control",
    "ｆｕｌｌ ｗｉｄｔｈ，标点！",
    "한국어 텍스트",
    "𠀀𠀁 extension B",
    "tabs\tand\nlines\r\n",
    "€ £ ¥ © ® ™",
    "a·b—c…d",
]


def test_the_estimate_is_unchanged_for_every_kind_of_character():
    for text in SAMPLES:
        assert estimate_tokens(text) == _reference(text), repr(text)


def test_the_estimate_is_unchanged_for_random_mixed_text():
    alphabet = (
        [chr(code) for code in range(0, 128)]
        + list("项目偏好白色中文记忆，。！？「」")
        + list("🧪🚀✓≤∑€©™·—…ｆ한𠀀")
        + [chr(0x0301), chr(0x200B), chr(0x3000)]
    )
    rng = random.Random(20260928)
    for _ in range(400):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 300)))
        assert estimate_tokens(text) == _reference(text), repr(text)


def test_a_text_of_many_different_symbols_is_counted_in_linear_time():
    """Counting each distinct symbol over the whole text again was quadratic in the number of different ones: a
    glyph table of 65,536 private-use characters took 1.2 s, and every candidate is weighed at least twice."""
    import time

    text = "".join(chr(0xF0000 + index) for index in range(65000))
    started = time.monotonic()
    counted = estimate_tokens(text)
    assert time.monotonic() - started < 0.3
    assert counted == _reference(text)
