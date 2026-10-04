"""Reading the messages dsh's plugin sends with a Stop: what counts as a message, and how lines are numbered.

Rows are synthetic, shaped like the plugin's (``distribution/dsh/scope-recall/index.mjs``): ``{id, role, text, time}``
with ``time`` in milliseconds.  Nothing here is a person's conversation.
"""
from __future__ import annotations

from scope_recall.adapters.codex import transcript

AT_MS = 1759320000123
AT = "2025-10-01T12:00:00.123000Z"


def _row(role="user", text="TEST 一句话", **fields):
    return {"id": "TEST-id", "role": role, "text": text, "time": AT_MS, **fields}


def test_each_message_is_a_line_numbered_from_one():
    lines = transcript.dsh_lines([_row(), _row("assistant", "TEST 回答", id="TEST-reply")])
    assert lines == [(1, transcript.Said("TEST-id", "user", "TEST 一句话", AT)),
                     (2, transcript.Said("TEST-reply", "assistant", "TEST 回答", AT))]


def test_a_row_that_is_no_message_is_counted_and_shows_nothing():
    rows = [None, "TEST", _row(role="system"), _row(text="  "), _row(id=""), _row(id="x" * 101), _row(time="TEST"),
            _row(time=-5), _row(text=3), _row()]
    lines = transcript.dsh_lines(rows)
    assert [index for index, _said in lines] == list(range(1, len(rows) + 1)), "every row is a line, so all can be dropped"
    assert [said for _index, said in lines if said is not None] == [transcript.Said("TEST-id", "user", "TEST 一句话", AT)]


def test_only_a_list_has_lines_and_one_stop_takes_at_most_five_hundred():
    assert transcript.dsh_lines(None) == [] and transcript.dsh_lines({"id": "TEST"}) == []
    lines = transcript.dsh_lines([_row(id=f"TEST-{n}") for n in range(600)])
    assert len(lines) == 500 and lines[-1][0] == 500, "the plugin keeps the rest and sends them with a later Stop"


def test_an_iso_time_is_taken_and_half_an_emoji_is_kept_as_a_replacement():
    said = transcript.dsh_lines([_row(time="2025-10-01T12:00:00.123Z", text="TEST \ud83d 半个")])[0][1]
    assert said is not None and said.occurred_at == AT and said.text == "TEST � 半个"
