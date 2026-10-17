"""Reading Claude Code's session record: what counts as said on screen, and where a read resumes.

Rows are synthetic and shaped like the record's; nothing here is a person's conversation.
"""

from __future__ import annotations

import json

from scope_recall.adapters.clients import transcript
from v11_support import slowdown

AT = "2026-09-25T11:16:54.627Z"


def _row(kind, **fields):
    return {"type": kind, "uuid": "TEST-uuid", "timestamp": AT, **fields}


def _person(content, **fields):
    return _row("user", origin={"kind": "human"}, message={"role": "user", "content": content}, **fields)


def _model(*blocks, **fields):
    return _row("assistant", message={"role": "assistant", "model": "TEST-model", "content": list(blocks)}, **fields)


def test_the_person_s_messages_are_said():
    said = transcript.said(_person("TEST 你好"))
    assert said == transcript.Said("TEST-uuid", "user", "TEST 你好", "2026-09-25T11:16:54.627000Z")
    pasted = transcript.said(_person([{"type": "image", "source": {}}, {"type": "text", "text": "TEST 看这张图"}]))
    assert pasted is not None and pasted.text == "TEST 看这张图"
    queued = transcript.said(
        _row(
            "attachment",
            attachment={
                "type": "queued_command",
                "commandMode": "prompt",
                "origin": {"kind": "human"},
                "prompt": "TEST 顺便",
            },
        )
    )
    assert queued is not None and (queued.role, queued.text) == ("user", "TEST 顺便")


def test_only_entries_marked_as_the_person_s_are_the_person_s():
    assert transcript.said(_row("user", message={"role": "user", "content": "TEST no origin"})) is None
    assert (
        transcript.said(
            _row(
                "user",
                origin={"kind": "task-notification"},
                message={"role": "user", "content": "<task-notification>TEST</task-notification>"},
            )
        )
        is None
    )
    assert transcript.said(_person([{"type": "tool_result", "tool_use_id": "T", "content": "TEST"}])) is None
    assert transcript.said(_person("TEST summary", isCompactSummary=True)) is None
    assert transcript.said(_person("TEST meta", isMeta=True)) is None
    assert transcript.said(_person("TEST side", isSidechain=True)) is None
    assert (
        transcript.said(
            _row(
                "attachment",
                attachment={
                    "type": "queued_command",
                    "commandMode": "task-notification",
                    "origin": {"kind": "human"},
                    "prompt": "TEST",
                },
            )
        )
        is None
    )


#: What Claude's desktop app puts in the person's first message when a session starts without a project folder,
#: shortened.  Synthetic.
APP_REMINDER = (
    "<system-reminder>\nTEST The user started this session without choosing a project folder, so your working "
    "directory is a scratch workspace the app created for it.\n</system-reminder>"
)


def test_the_reminders_the_desktop_app_puts_in_a_message_are_not_the_person_s():
    """The reminder comes ahead of the typed text in one string, or as a text block of its own, which the record's
    reader joins with a line break; either way it is not stored as the person's."""
    from scope_recall.adapters.clients.boundary import claude_code_person_text

    typed = "TEST 帮我看看这个仓库。"
    assert claude_code_person_text(f"{APP_REMINDER}\n\n{typed}") == typed
    assert claude_code_person_text(f"{APP_REMINDER}\n{typed}") == typed, "two text blocks, joined"
    assert claude_code_person_text(f"{APP_REMINDER}{typed}") == typed, "no break after it"
    assert claude_code_person_text(f"{typed}\n{APP_REMINDER}") == typed, "one after the typed text"
    assert claude_code_person_text(f"  {APP_REMINDER}\n{APP_REMINDER}\n\n{typed}\n") == typed
    assert claude_code_person_text(f"{typed}\n\n{APP_REMINDER}\n\nTEST 第二段。") == f"{typed}\n\nTEST 第二段。"
    assert claude_code_person_text(APP_REMINDER) == "" and claude_code_person_text(f"\n{APP_REMINDER}\n ") == ""
    # The person's words come back exactly as they came, unless a reminder was taken out of them.
    assert claude_code_person_text(f"  {typed}\n") == f"  {typed}\n"
    # Their own words about such a block, inside a line, stay theirs; so does a block that never closes.
    mention = "TEST 为什么 <system-reminder>…</system-reminder> 会被存下来？"
    assert claude_code_person_text(mention) == mention
    assert claude_code_person_text(f"TEST 看这个：`{APP_REMINDER}`") == f"TEST 看这个：`{APP_REMINDER}`"
    unclosed = "<system-reminder>\nTEST 没有结尾"
    assert claude_code_person_text(unclosed) == unclosed
    assert claude_code_person_text(f"{unclosed}\n{APP_REMINDER}\n{typed}") == typed, "the first closing ends it"


def test_many_unclosed_reminders_cost_one_pass():
    """A text of many unclosed openings is read in one pass, not by a lazy match retried from every opening, which is
    quadratic: four times the openings take about four times as long (``slowdown``)."""
    from scope_recall.adapters.clients.boundary import claude_code_person_text

    def read(text):
        return claude_code_person_text(text), claude_code_person_text(text + "TEST</system-reminder>\nTEST 后面")

    text = "<system-reminder>\n" * 40_000
    assert read(text) == (text, "TEST 后面")
    assert slowdown(read, "<system-reminder>\n" * 10_000, text) < 8
    lone = "<system-reminder" * 40_000
    assert claude_code_person_text(lone) == lone
    assert slowdown(claude_code_person_text, "<system-reminder" * 10_000, lone) < 8


def test_the_model_s_visible_text_is_said_and_nothing_else_of_it():
    said = transcript.said(_model({"type": "text", "text": "TEST 我先看一下。"}))
    assert said is not None and (said.role, said.text) == ("assistant", "TEST 我先看一下。")
    assert transcript.said(_model({"type": "thinking", "thinking": "TEST"})) is None
    assert transcript.said(_model({"type": "tool_use", "id": "T", "name": "Bash", "input": {}})) is None
    assert transcript.said(_model({"type": "text", "text": "TEST"}, isApiErrorMessage=True)) is None
    synthetic = _model({"type": "text", "text": "TEST"})
    synthetic["message"]["model"] = "<synthetic>"
    assert transcript.said(synthetic) is None


def test_an_entry_without_an_id_a_time_or_words_is_skipped():
    assert transcript.said(_person("TEST", uuid=None)) is None
    assert transcript.said({**_person("TEST"), "timestamp": "not a time"}) is None
    assert transcript.said({**_person("TEST"), "timestamp": "2026-09-25T11:16:54"}) is None, "a time without a zone"
    assert transcript.said(_person("   ")) is None
    assert transcript.said(["TEST"]) is None
    assert transcript.said(_row("system", content="TEST")) is None


def test_a_prompt_id_the_store_cannot_bind_is_dropped_and_the_message_kept():
    """A lone surrogate from a malformed escape, in the id only: binding it raised in the store, and every later
    Stop of the session stopped at that line.  From another machine such a line is refused whole."""
    said = transcript.said(_person("TEST 你好", promptId="TEST-\ud800"))
    assert said is not None and said.text == "TEST 你好" and said.prompt_id is None
    wire = transcript.said_to_wire(
        transcript.Said("TEST-uuid", "user", "TEST 你好", "2026-09-25T11:16:54.627000Z", "TEST-p1")
    )
    assert transcript.said_from_wire(wire) is not None
    assert transcript.said_from_wire(dict(wire, prompt_id="TEST-\ud800")) is None


def test_a_read_takes_complete_lines_and_says_where_it_stopped(tmp_path):
    record = tmp_path / "TEST-session.jsonl"
    first = json.dumps(_person("TEST 一"), ensure_ascii=False).encode("utf-8") + b"\n"
    broken = b"{not json\n"
    partial = json.dumps(_person("TEST 二"), ensure_ascii=False).encode("utf-8")
    record.write_bytes(first + broken + partial)
    lines = transcript.read(record, 0)
    assert [end for end, _said in lines] == [len(first), len(first) + len(broken)]
    assert lines[0][1] is not None and lines[0][1].text == "TEST 一" and lines[1][1] is None
    assert transcript.read(record, len(first) + len(broken)) == [], "a line still being written waits"
    assert len(transcript.read(record, 0, limit=1)) == 1, "a read stops once it has gone through its limit"


def _compaction_rows(stamp, **fields):
    """A compaction as Claude Code records it: its boundary, then the summary that replaces what came before."""
    return [
        {"type": "system", "subtype": "compact_boundary", "timestamp": stamp, "compactMetadata": {"trigger": "auto"}},
        {**_person("TEST summary", isCompactSummary=True), "timestamp": stamp, **fields},
    ]


def _write(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return path


def test_the_model_holds_the_session_since_its_last_compaction(tmp_path, monkeypatch):
    """What was said since the last compaction is still in the model's context, word for word; with none, the
    whole session is.  A subagent's compaction is its own, and a record too long to read to its start without finding
    one says nothing."""
    record = tmp_path / "TEST-session.jsonl"
    first = {**_person("TEST 一"), "timestamp": "2026-09-25T10:00:00.000Z"}
    _write(record, [{"type": "summary", "summary": "TEST"}, first, _model({"type": "text", "text": "TEST"})])
    assert transcript.context_since(record) == "2026-09-25T10:00:00Z", "no compaction: since the first line"
    rows = [first, *_compaction_rows("2026-09-25T11:00:00.000Z"), _person("TEST 二")]
    rows += [*_compaction_rows("2026-09-25T12:30:00.500Z"), _person("TEST 三")]
    _write(record, rows)
    assert transcript.context_since(record) == "2026-09-25T12:30:00.500000Z", "the last compaction"
    _write(record, [*rows, {**_compaction_rows("2026-09-25T13:00:00Z")[1], "isSidechain": True}])
    assert transcript.context_since(record) == "2026-09-25T12:30:00.500000Z", "a subagent's is not the session's"
    # The person's own words naming a compaction are not one.
    _write(record, [*rows, _person('TEST "isCompactSummary":true "compact_boundary"')])
    assert transcript.context_since(record) == "2026-09-25T12:30:00.500000Z"
    # A summary alone, written with spaces, as another writer may.
    spaced = (
        json.dumps(first)
        + "\n"
        + json.dumps(
            {**_person("TEST summary"), "timestamp": "2026-09-25T11:00:00Z", "isCompactSummary": True},
            separators=(", ", ": "),
        )
        + "\n"
    )
    record.write_text(spaced, encoding="utf-8")
    assert transcript.context_since(record) == "2026-09-25T11:00:00Z"
    _write(record, rows)
    monkeypatch.setattr(transcript, "_CONTEXT_TAIL_BYTES", 600)
    _write(record, [*rows, *[_person("TEST " + "长" * 100) for _ in range(20)]])
    assert transcript.context_since(record) is None, "no compaction in what was read, and not read to its start"
    long = [_person("TEST " + "长" * 100) for _ in range(20)]
    _write(record, [*rows, *long, *_compaction_rows("2026-09-26T08:00:00Z")])
    assert transcript.context_since(record) == "2026-09-26T08:00:00Z", "the end of a long record has one"
    assert transcript.context_since(tmp_path / "TEST-missing.jsonl") is None


def test_only_the_session_s_own_record_is_read(tmp_path):
    record = tmp_path / "TEST-session.jsonl"
    record.write_text("", encoding="utf-8")
    assert transcript.record_path(str(record), "TEST-session") == record
    assert transcript.record_path(str(record), "TEST-other") is None
    assert transcript.record_path("TEST-session.jsonl", "TEST-session") is None, "a relative path"
    assert transcript.record_path(str(tmp_path / "missing" / "TEST-session.jsonl"), "TEST-session") is None
    assert transcript.record_path(None, "TEST-session") is None


def test_a_read_resumes_where_it_stopped_unless_the_record_is_another(tmp_path):
    record = tmp_path / "TEST-session.jsonl"
    record.write_bytes(b"x" * 100)
    cursor = transcript.Cursor(tmp_path / "TEST-home", "TEST-session", record)
    assert cursor.load() == 0, "no saved position"
    cursor.save(60)
    assert transcript.Cursor(tmp_path / "TEST-home", "TEST-session", record).load() == 60
    assert transcript.Cursor(tmp_path / "TEST-home", "TEST-other", record).load() == 0
    record.write_bytes(b"y" * 100)
    assert cursor.load() == 0, "another record under the same name"
    cursor.save(100)
    record.write_bytes(b"y" * 50)
    assert cursor.load() == 0, "a record shorter than the saved position"
