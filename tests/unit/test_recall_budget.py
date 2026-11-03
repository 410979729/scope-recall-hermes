"""The packet's size estimate, counted without a Python step per character.

A per-character loop would cost a visible share of an automatic recall: the packet weighs every candidate it
considers, millions of characters in a recall on a large store.  The estimate itself must not move, so the count
is checked against the per-character loop.
"""

import random
import unicodedata

from scope_recall.core.recall_budget import estimate_tokens
from v11_support import slowdown


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
    """Each distinct symbol is counted once, not over the whole text again, which takes time quadratic in how many
    different ones there are: a glyph table holds tens of thousands, and every candidate is weighed at least twice.
    Four times the symbols take about four times as long (``slowdown``)."""

    def symbols(count):
        return "".join(chr(0xF0000 + index) for index in range(count))

    text = symbols(64_000)
    assert slowdown(estimate_tokens, symbols(16_000), text) < 8
    assert estimate_tokens(text) == _reference(text)


def saturated_legacy_host_texts():
    """Yield saturated brief/question components for complete host assembly checks."""
    from datetime import UTC, timedelta, timezone

    from scope_recall.adapters.runtime_wiring import render_host_recall_context
    from scope_recall.core.recall_packet import _DIAGNOSTIC_REF_PLACEHOLDER, RecallPacketRenderer, envelope_tokens
    from v11_support import recall_item, recall_packet

    for count in range(1, 7):
        for glyph in ("a", "中", "🧪"):
            items = [
                recall_item(
                    ref=f"TEST-claim-{n}",
                    content="",
                    occurred_at="2026-09-01T12:00:00Z",
                    entries=[{"id": "test-other", "name": "中" * 32}],
                )
                for n in range(count)
            ]
            packet = recall_packet(items=items, diagnostic_ref=_DIAGNOSTIC_REF_PLACEHOLDER)
            low, high = 0, 12000
            while low < high:
                middle = (low + high + 1) // 2
                items[0]["content"] = glyph * middle
                if envelope_tokens(packet) <= 4096:
                    low = middle
                else:
                    high = middle - 1
            items[0]["content"] = glyph * low
            rendered = RecallPacketRenderer().prepare(
                packet, installation_id="TEST-install", session_id="TEST-s", request_id="TEST-r"
            )
            for zone in (UTC, timezone(timedelta(hours=-12)), timezone(timedelta(hours=14))):
                question = render_host_recall_context(
                    rendered.canonical_text, context=rendered.context, entry=("t" + "-" * 31, "🧪" * 32), zone=zone
                )
                for bound in (1024, 8192):
                    prefix = "Session background:\n"
                    brief = prefix + "a" * ((bound - estimate_tokens(prefix)) * 4)
                    while estimate_tokens(brief + "a") <= bound:
                        brief += "a"
                    yield brief, question, bound


def test_saturated_legacy_host_text_sets_default_injection_cap(record_property, tmp_path):
    """Include the allowed private hint as well as the complete memory envelope."""
    from scope_recall.core import CoreConfig
    from scope_recall.core.private import notice_for_mode
    from scope_recall.core.recall_budget import canonical_render_json
    from v11_support import context

    measurements = {}
    for brief, question, bound in saturated_legacy_host_texts():
        text = "\n\n".join((brief, question, notice_for_mode("read-only")))
        cost = estimate_tokens(text)
        measurements[str(bound)] = max(measurements.get(str(bound), 0), cost)
    record_property("legacy_host_maxima", canonical_render_json(measurements))
    assert CoreConfig(context(tmp_path / "TEST-unused-measurement").binding).injection_token_cap >= max(
        measurements.values()
    )


def test_repeated_long_source_estimate_counts_the_body_once(monkeypatch):
    from scope_recall.core import recall_budget

    pattern = recall_budget._CJK
    calls = []

    class Observed:
        def sub(self, replacement, text):
            calls.append(len(text))
            return pattern.sub(replacement, text)

    monkeypatch.setattr(recall_budget, "_CJK", Observed())
    text = "TEST long budget source 项目🧪 " * 1200
    first = recall_budget.estimate_tokens(text)
    assert recall_budget.estimate_tokens(text) == first
    assert calls == [len(text)]


def test_token_estimate_cache_is_bounded_and_larger_renderings_are_not_retained(monkeypatch):
    from scope_recall.core import recall_budget

    recall_budget._cached_estimate_tokens.cache_clear()
    for index in range(300):
        assert recall_budget.estimate_tokens(f"TEST cached source {index}") > 0
    info = recall_budget._cached_estimate_tokens.cache_info()
    assert info.currsize == info.maxsize == 256
    text = "TEST oversized rendering 项目🧪 " * 3000
    assert len(text) > 65536
    calls = []
    original = recall_budget._estimate_tokens

    def counted(body):
        calls.append(body)
        return original(body)

    monkeypatch.setattr(recall_budget, "_estimate_tokens", counted)
    assert recall_budget.estimate_tokens(text) == recall_budget.estimate_tokens(text)
    assert calls == [text, text]
    assert recall_budget._cached_estimate_tokens.cache_info() == info
    first = recall_budget.estimate_tokens("TEST changed source α")
    second = recall_budget.estimate_tokens("TEST changed source 项目🧪")
    assert first != second
