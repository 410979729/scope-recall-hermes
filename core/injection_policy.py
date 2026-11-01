"""Bounded, data-driven source policy with escaped literal matching.

Token slots describe ordered literal alternatives within a 256-character span.
Only bounded windows are inspected between deadline checks. Policy data cannot
introduce regex quantifiers, callbacks or unbounded backtracking.
"""

from __future__ import annotations

import json
import re
import shlex
import time
import unicodedata
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
from functools import lru_cache

from .injection_schema import DEFAULT_RULES

STRIDE = 2048
MARGIN = 512
SCAN_SECONDS = 0.1
_FLAGS = tuple(
    "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in code) + "\U000e007f" for code in ("gbeng", "gbsct", "gbwls")
)
_CONTROLS = re.compile(
    r"[\u00ad\u034f\u061c\u180e\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]"
)
_JOIN = re.compile(r"[A-Za-z\u4e00-\u9fff][\u200c\u200d]|[\u200c\u200d][A-Za-z\u4e00-\u9fff]")
_QUOTE_TOKEN = re.compile('["“”‘’`＂｀]')
_QUOTE_FOLD = {"＂": '"', "｀": "`"}
_QUOTE_CLOSE = {'"': '"', "“": "”", "‘": "’", "`": "`"}
_DISCUSSION = (
    "prompt injection",
    "do not follow",
    "attack example",
    "security discussion",
    "提示注入",
    "不要执行",
    "攻击示例",
    "安全讨论",
)
_BODY = ("--data", "--data-raw", "--data-binary", "--data-urlencode", "--post-data", "--post-file", "-d")


@dataclass(frozen=True)
class ScanResult:
    """A content-free verdict whose reasons may be shown outside owner review."""

    state: str
    rules: tuple[str, ...] = ()


@lru_cache(maxsize=256)
def _slots(pattern):
    value = json.loads(pattern)
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise ValueError("slots")
    for slot in value:
        if not isinstance(slot, list) or not 1 <= len(slot) <= 32:
            raise ValueError("alternatives")
        if any(type(token) is not str or not 1 <= len(token) <= 96 for token in slot):
            raise ValueError("literal")
    return tuple(tuple(token.casefold() for token in slot) for slot in value)


def load_policy(conn, deadline=None):
    """Validate required rules, categories and every enabled rule before scanning."""
    rows = conn.execute("SELECT * FROM injection_rules ORDER BY rule_id LIMIT 65").fetchall()
    required = {rule[0]: (rule[1], rule[2], rule[3] if rule[2] == "unicode" else None) for rule in DEFAULT_RULES}
    found = {}
    active = []
    for row in rows:
        if deadline is not None and time.perf_counter() >= deadline:
            raise TimeoutError
        if not row["enabled"]:
            continue
        matcher, pattern = row["matcher"], row["pattern"]
        if len(pattern) > 4096 or row["disposition"] not in {"quarantined", "pending_review"}:
            raise ValueError("rule")
        if matcher in {"tokens", "command"}:
            compiled = _slots(pattern)
            if matcher == "command" and len(compiled) != 2:
                raise ValueError("command slots")
        elif matcher == "unicode" and pattern in {"controls", "join"}:
            compiled = pattern
        else:
            raise ValueError("matcher")
        found[row["rule_id"]] = (row["category"], matcher, pattern if matcher == "unicode" else None)
        active.append((row["rule_id"], matcher, compiled, row["disposition"]))
    if len(rows) > 64 or any(found.get(key) != category for key, category in required.items()):
        raise ValueError("incomplete policy")
    return active


@lru_cache(maxsize=256)
def _literal_regex(alternatives):
    """Compile escaped alternatives with fixed-width word boundaries only."""
    forms = []
    for token in alternatives:
        literal = re.escape(token)
        if token.isascii() and token.isalpha():
            literal = r"(?<![A-Za-z])" + literal + r"(?![A-Za-z])"
        forms.append(literal)
    return re.compile("|".join(forms))


def _literal_positions(text, alternatives):
    return [(match.start(), match.end()) for match in _literal_regex(alternatives).finditer(text)]


def _token_spans(text, slots):
    positions = []
    for slot in reversed(slots):
        if not any(token in text for token in slot):
            return
    for slot in reversed(slots):
        found = _literal_positions(text, slot)
        if not found:
            return
        positions.append(found)
    positions.reverse()
    for start, end in positions[0]:
        tail = end
        for slot in positions[1:]:
            index = bisect_left(slot, (tail,))
            match = slot[index] if index < len(slot) and slot[index][1] - start <= 256 else None
            if match is None:
                break
            tail = match[1]
        else:
            yield start, tail


class _QuotedSpans:
    """Track quote orientation once across raw input, retaining only a bounded window.

    A closing quote at a window's left edge must not become the opening quote
    of every subsequent example. Escaped delimiters cannot authorize an exemption.
    """

    def __init__(self, value):
        self.value = value
        self.cursor = 0
        self.opening: tuple[int, str] | None = None
        self.spans: deque[tuple[int, int]] = deque()

    def _advance(self, limit):
        for match in _QUOTE_TOKEN.finditer(self.value, self.cursor, limit):
            position = match.start()
            if position and self.value[position - 1] in "\\＼":
                continue
            token = _QUOTE_FOLD.get(match[0], match[0])
            if self.opening is None:
                if token in _QUOTE_CLOSE:
                    self.opening = (position, _QUOTE_CLOSE[token])
            elif token == self.opening[1]:
                start = self.opening[0]
                if 1 < position - start <= 513:
                    self.spans.append((start, position))
                self.opening = None
        self.cursor = limit

    def window(self, raw, folded, offset):
        """Map known complete quote pairs to the folded window in linear bounded work."""
        start = max(0, offset - MARGIN)
        while self.spans and self.spans[0][1] < start:
            self.spans.popleft()
        self._advance(start + len(raw))
        original = list(_QUOTE_TOKEN.finditer(raw))
        normalized = list(_QUOTE_TOKEN.finditer(folded))
        if len(original) != len(normalized):
            return ()
        positions = {}
        for left, right in zip(original, normalized, strict=True):
            if _QUOTE_FOLD.get(left[0], left[0]) != right[0]:
                return ()
            positions[start + left.start()] = right.start()
        return tuple((positions[a], positions[b] + 1) for a, b in self.spans if a in positions and b in positions)


def _quoted_discussion(text, start, end, quotes):
    """Exempt a complete quoted example only with nearby explicit safety discussion."""
    if not any(cue in text[max(0, start - 160) : end + 160] for cue in _DISCUSSION):
        return False
    index = bisect_right(quotes, (start, len(text) + 1)) - 1
    return index >= 0 and quotes[index][0] < start and end < quotes[index][1]


def _command_spans(text, slots):
    if not any(token in text for token in slots[0]):
        return
    for start, _end in _literal_positions(text, slots[0]):
        command = text[start : start + 256].split("\n", 1)[0]
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        for index, word in enumerate(words):
            flag, separator, value = word.partition("=")
            if flag in _BODY:
                value = value if separator else words[index + 1] if index + 1 < len(words) else ""
            elif word.startswith("-d") and len(word) > 2:
                value = word[2:]
            else:
                continue
            if ("$" in value or "@" in value) and any(token in value for token in slots[1]):
                yield start, start + len(command)
                break


def _hits(rule, raw, text, discussion, owned, folded_owned, quotes):
    _name, matcher, pattern, _state = rule
    if matcher == "unicode":
        if pattern == "join":
            if "\u200c" not in text and "\u200d" not in text:
                return False
            return any(folded_owned[0] <= m.start() < folded_owned[1] for m in _JOIN.finditer(text))
        if raw.isascii():
            return False
        for flag in _FLAGS:
            raw = raw.replace(flag, " " * len(flag))
        return any(owned[0] <= m.start() < owned[1] for m in _CONTROLS.finditer(raw))
    spans = _command_spans(text, pattern) if matcher == "command" else _token_spans(text, pattern)
    return any(
        folded_owned[0] <= start < folded_owned[1]
        and (not discussion or not _quoted_discussion(text, start, end, quotes))
        for start, end in spans
    )


def _window(value, offset):
    """Keep raw controls and folded text in their respective coordinate spaces.

    Extra context contains a complete quoted example even when its opening
    delimiter belongs to a previous window. Each match belongs to one core.
    """
    start = max(0, offset - MARGIN)
    raw = value[start : offset + STRIDE + MARGIN]
    owned = (offset - start, min(len(raw), offset - start + STRIDE))
    text = unicodedata.normalize("NFKC", raw).casefold()
    folded_owned = (
        owned if raw.isascii() else tuple(len(unicodedata.normalize("NFKC", raw[:end]).casefold()) for end in owned)
    )
    return raw, text, owned, folded_owned


def scan(tx, event, *, remaining_seconds=SCAN_SECONDS):
    """Scan under a wall-clock deadline; unfinished or invalid policy stays withheld."""
    deadline = time.perf_counter() + max(0, remaining_seconds)
    try:
        policy = load_policy(tx._check(), deadline)
    except TimeoutError:
        return ScanResult("pending_review", ("scan_time_budget",))
    except (ValueError, TypeError, KeyError, RecursionError):
        return ScanResult("pending_review", ("invalid_scan_policy",))
    content = event["content"]
    metadata = json.dumps({k: v for k, v in event.items() if k != "content"}, ensure_ascii=False)
    if len(content) + len(metadata) > 2097152:
        return ScanResult("pending_review", ("scan_input_budget",))
    matches = {}
    for value, discussion in ((content, True), (metadata, False)):
        tracker = _QuotedSpans(value) if discussion else None
        for offset in range(0, len(value), STRIDE):
            if time.perf_counter() >= deadline:
                return ScanResult(
                    "quarantined" if "quarantined" in matches.values() else "pending_review",
                    tuple((*matches, "scan_time_budget")),
                )
            raw, text, owned, folded_owned = _window(value, offset)
            if len(text) > 8192:
                return ScanResult("pending_review", ("scan_normalization_budget",))
            quotes = tracker.window(raw, text, offset) if tracker is not None else ()
            for rule in policy:
                if time.perf_counter() >= deadline:
                    return ScanResult(
                        "quarantined" if "quarantined" in matches.values() else "pending_review",
                        tuple((*matches, "scan_time_budget")),
                    )
                if rule[0] not in matches and _hits(rule, raw, text, discussion, owned, folded_owned, quotes):
                    matches[rule[0]] = rule[3]
    state = "quarantined" if "quarantined" in matches.values() else "pending_review" if matches else "clear"
    return ScanResult(state, tuple(matches))
