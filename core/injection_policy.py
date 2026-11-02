"""Bounded, data-driven source policy with escaped literal matching.

Token slots describe ordered literal alternatives within each rule's bounded span.
Only bounded windows are inspected between deadline checks. Policy data cannot
introduce regex quantifiers, callbacks or unbounded backtracking.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
from functools import lru_cache

from .injection_commands import command_candidate, command_spans
from .injection_schema import DEFAULT_RULES

STRIDE = 2048
MARGIN = 2048
COMPILER_VERSION = "bounded-structured-payload-3"
SCAN_SECONDS = 0.1
_SIMPLE_FOLD = re.compile(r"[\x00-\x7f\u4e00-\u9fff\uff01-\uff5e]*")
_WIDE_ASCII = str.maketrans({cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)})
_FLAGS = tuple(
    "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in code) + "\U000e007f" for code in ("gbeng", "gbsct", "gbwls")
)
_CONTROLS = re.compile(
    r"[\u00ad\u034f\u061c\u180e\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]"
)
_PROSE_BOUNDARY = re.compile(
    r"[.!?。！？][ \t]*\r?\n|\r?\n[ \t]*\r?\n|"
    r"</?(?:address|article|aside|blockquote|dd|div|dl|dt|fieldset|figcaption|figure|footer|form|"
    r"h[1-6]|header|hr|li|main|nav|ol|p|pre|section|table|tbody|td|tfoot|th|thead|tr|ul)\b"
)
_JOIN = re.compile(r"[A-Za-z\u4e00-\u9fff][\u200c\u200d]|[\u200c\u200d][A-Za-z\u4e00-\u9fff]")
_QUOTE_TOKEN = re.compile("[\"'“”‘’`＂＇｀]")
_QUOTE_FOLD = {"＂": '"', "＇": "'", "｀": "`"}
_QUOTE_CLOSE = {'"': '"', "'": "'", "“": "”", "‘": "’", "`": "`"}


@dataclass(frozen=True)
class ScanResult:
    """A content-free verdict whose reasons may be shown outside owner review."""

    state: str
    rules: tuple[str, ...] = ()
    progress: dict | None = None


@dataclass(frozen=True)
class Policy:
    """One transaction's compiled rules and exact configuration fingerprint."""

    rules: tuple
    fingerprint: str


@lru_cache(maxsize=256)
def _slots(pattern):
    value = json.loads(pattern)
    span = value.get("span", 256) if isinstance(value, dict) else 256
    if type(span) is not int or not 16 <= span <= MARGIN:
        raise ValueError("span")
    value = value.get("slots") if isinstance(value, dict) else value
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise ValueError("slots")
    for slot in value:
        if not isinstance(slot, list) or not 1 <= len(slot) <= 32:
            raise ValueError("alternatives")
        if any(type(token) is not str or not 1 <= len(token) <= 96 for token in slot):
            raise ValueError("literal")
    return tuple(tuple(token.casefold() for token in slot) for slot in value), span


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
            if matcher == "command" and len(compiled[0]) != 2:
                raise ValueError("command slots")
        elif matcher == "unicode" and pattern in {"controls", "join"}:
            compiled = pattern
        else:
            raise ValueError("matcher")
        found[row["rule_id"]] = (row["category"], matcher, pattern if matcher == "unicode" else None)
        active.append((row["rule_id"], matcher, compiled, row["disposition"]))
    if len(rows) > 64 or any(found.get(key) != category for key, category in required.items()):
        raise ValueError("incomplete policy")
    return Policy(tuple(active), _policy_digest(rows))


def _policy_digest(rows):
    return hashlib.sha256((COMPILER_VERSION + json.dumps([tuple(row) for row in rows])).encode()).hexdigest()


def policy_fingerprint(conn):
    """Recheck exact policy bytes at publication without compiling a second policy."""
    return _policy_digest(conn.execute("SELECT * FROM injection_rules ORDER BY rule_id LIMIT 65").fetchall())


@lru_cache(maxsize=256)
def _literal_regex(alternatives, ignore_case=False):
    """Compile bounded alternatives with required literal anchors for cheap rejection."""
    forms = []
    anchors = []
    for token in alternatives:
        token = re.sub(r"\s*([\[\]<>:])\s*", r"\1", token)
        chunks = [chunk for chunk in re.split(r"[\s\[\]<>:]+", token) if chunk]
        anchors.append(max(chunks, key=len) if chunks else token[0])
        parts = re.split(r"(\s+)", token)
        literal = r"\s{1,32}+".join(re.escape(part) for part in parts if part and not part.isspace())
        for symbol in ("[", "<", ">"):
            literal = literal.replace(re.escape(symbol), re.escape(symbol) + r"\s{0,32}+")
        literal = literal.replace(":", r"\s{0,32}+:\s{0,32}+")
        literal = literal.replace(r"\]", r"\s{0,32}+\]")
        if token.isascii() and token.isalpha():
            literal = r"(?<![A-Za-z])" + literal + r"(?![A-Za-z])"
        forms.append(literal)
    return re.compile("|".join(forms), re.I if ignore_case else 0), tuple(anchors)


def _literal_positions(text, alternatives, *, ignore_case=False):
    pattern, anchors = _literal_regex(alternatives, ignore_case)
    if not ignore_case and not any(anchor in text for anchor in anchors):
        return []
    return [(match.start(), match.end()) for match in pattern.finditer(text)]


def _token_spans(text, slots, span):
    positions = []
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
            match = slot[index] if index < len(slot) and slot[index][1] - start <= span else None
            if match is None:
                break
            tail = match[1]
        else:
            yield start, tail


class _QuotedSpans:
    """Track quote orientation once across raw input, retaining only a bounded window.

    A closing quote at a window's left edge must not become the opening quote
    of every subsequent example. Escaped delimiters do not establish a complete quotation.
    """

    def __init__(self, value, state=None):
        self.value = value
        state = state or {}
        self.cursor = state.get("cursor", 0)
        self.opening = tuple(state["opening"]) if state.get("opening") else None
        self.spans = deque(tuple(pair) for pair in state.get("spans", ()))

    def checkpoint(self):
        """Content-free quote orientation and bounded pairs at the next window."""
        return dict(cursor=self.cursor, opening=self.opening, spans=list(self.spans))

    def _advance(self, limit):
        for match in _QUOTE_TOKEN.finditer(self.value, self.cursor, limit):
            position = match.start()
            if position and self.value[position - 1] in "\\＼":
                continue
            token = _QUOTE_FOLD.get(match[0], match[0])
            neighbors = self.value[max(0, position - 1) : position + 2]
            if (
                token == "'"
                and len(neighbors) == 3
                and neighbors.isascii()
                and neighbors[0].isalpha()
                and neighbors[2].isalpha()
            ):
                continue
            if self.opening is None:
                if token in _QUOTE_CLOSE:
                    self.opening = (position, _QUOTE_CLOSE[token])
            elif token == self.opening[1]:
                start = self.opening[0]
                if position - start > 1:
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
        pairs = list(self.spans)
        if self.opening is not None:
            pairs.append((self.opening[0], start + len(raw)))
        return tuple(
            (positions.get(a, -1), positions.get(b, len(folded)) + 1)
            for a, b in pairs
            if a < start + len(raw) and b >= start
        )


def _quoted_discussion(text, start, end, quotes):
    """Identify ambiguity without trusting any safety claim made by the body itself."""
    index = bisect_right(quotes, (start, len(text) + 1)) - 1
    return index >= 0 and quotes[index][0] < start and end < quotes[index][1]


def _structured_span(name, text, start, end):
    """Keep attributes inside their tag, comments inside their comment and prose within one block.

    The policy's span still caps every match. HTML block delimiters and paragraph
    boundaries cannot join unrelated prose into a transfer instruction.
    """
    if name == "hidden_instruction":
        terminator = "-->" if text.startswith("<!--", start) else ">"
        boundary = text.find(terminator, start + (4 if terminator == "-->" else 1))
        return boundary < 0 or end <= boundary + len(terminator)
    if name.startswith("exfiltration_link_"):
        return _PROSE_BOUNDARY.search(text, start, end) is None
    return True


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
    slots, span = pattern
    if matcher == "command":
        if not command_candidate(text, slots[0]):
            return False
        command_text = unicodedata.normalize("NFKC", raw)
        spans = (
            (len(command_text[:a].casefold()), len(command_text[:b].casefold()))
            for a, b in command_spans(command_text, slots, span)
        )
    else:
        spans = _token_spans(text, slots, span)
    quoted = False
    for start, end in spans:
        if not folded_owned[0] <= start < folded_owned[1]:
            continue
        if not _structured_span(_name, text, start, end):
            continue
        if discussion and _quoted_discussion(text, start, end, quotes):
            quoted = True
        else:
            return "quarantined"
    return "pending_review" if quoted else False


def _window(value, offset):
    """Keep raw controls and folded text in their respective coordinate spaces.

    Extra context contains a complete quoted example even when its opening
    delimiter belongs to a previous window. Each match belongs to one core.
    """
    start = max(0, offset - MARGIN)
    raw = value[start : offset + STRIDE + MARGIN]
    owned = (offset - start, min(len(raw), offset - start + STRIDE))
    # These characters neither compose across boundaries nor expand when folded.
    # Their one-to-one mapping keeps raw ownership coordinates exact.
    if not raw.isascii() and _SIMPLE_FOLD.fullmatch(raw) is not None:
        return raw, raw.translate(_WIDE_ASCII).casefold(), owned, owned
    text = unicodedata.normalize("NFKC", raw).casefold()
    folded_owned = (
        owned if raw.isascii() else tuple(len(unicodedata.normalize("NFKC", raw[:end]).casefold()) for end in owned)
    )
    return raw, text, owned, folded_owned


def scan(tx, event, *, remaining_seconds=SCAN_SECONDS, policy=None, progress=None, max_windows=None):
    """Scan one body and its metadata; a checkpoint never authorizes publication."""
    return scan_events(
        tx, [event], remaining_seconds=remaining_seconds, policy=policy, progress=progress, max_windows=max_windows
    )


def scan_events(tx, events, *, remaining_seconds=SCAN_SECONDS, policy=None, progress=None, max_windows=None):
    """Resume a complete group's body and every part's metadata within a bounded batch.

    The caller binds checkpoints to exact group bytes and policy before reusing them.
    Rules and quote orientation survive windows, never identity changes.
    """
    deadline = time.perf_counter() + max(0, remaining_seconds)
    try:
        policy = policy if policy is not None else load_policy(tx._check())
    except (ValueError, TypeError, KeyError, RecursionError):
        return ScanResult("pending_review", ("invalid_scan_policy",))
    values = [("".join(event["content"] for event in events), True)]
    values.extend(
        (json.dumps({k: v for k, v in event.items() if k != "content"}, ensure_ascii=False), False) for event in events
    )
    cursor = dict(progress or {})
    matches = dict(cursor.get("matches", {}))
    windows = 0
    for index in range(cursor.get("value", 0), len(values)):
        value, discussion = values[index]
        first_offset = cursor.get("offset", 0) if index == cursor.get("value", 0) else 0
        tracker = _QuotedSpans(value, cursor.get("quotes")) if discussion else None
        for offset in range(first_offset, len(value), STRIDE):
            if time.perf_counter() >= deadline or (max_windows is not None and windows >= max_windows):
                return _verdict(matches, _checkpoint(index, offset, matches, tracker))
            raw, text, owned, folded_owned = _window(value, offset)
            if len(text) > 32768:
                return ScanResult("pending_review", ("scan_normalization_budget",))
            quotes = tracker.window(raw, text, offset) if tracker is not None else ()
            for rule in policy.rules:
                if time.perf_counter() >= deadline:
                    return _verdict(matches, _checkpoint(index, offset, matches, tracker))
                hit = _hits(rule, raw, text, discussion, owned, folded_owned, quotes)
                if hit:
                    state = rule[3] if hit != "pending_review" else "pending_review"
                    if matches.get(rule[0]) != "quarantined":
                        matches[rule[0]] = state
                    if hit == "pending_review":
                        matches["quoted_instruction"] = "pending_review"
            windows += 1
        cursor = {}
    return _verdict(matches)


def _checkpoint(index, offset, matches, tracker):
    return dict(value=index, offset=offset, matches=matches, quotes=tracker.checkpoint() if tracker else None)


def _verdict(matches, progress=None):
    state = "quarantined" if "quarantined" in matches.values() else "pending_review" if matches or progress else "clear"
    rules = tuple(matches) + (("scan_time_budget",) if progress is not None else ())
    return ScanResult(state, rules, progress)
