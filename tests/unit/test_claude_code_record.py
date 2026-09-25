"""Reading Claude Code's session record: what counts as said on screen, and where a read resumes.

Rows are synthetic and shaped like the record's; nothing here is a person's conversation.
"""
from __future__ import annotations

import json

from scope_recall.adapters.codex import transcript

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
    queued = transcript.said(_row("attachment", attachment={
        "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"}, "prompt": "TEST 顺便"}))
    assert queued is not None and (queued.role, queued.text) == ("user", "TEST 顺便")


def test_only_entries_marked_as_the_person_s_are_the_person_s():
    assert transcript.said(_row("user", message={"role": "user", "content": "TEST no origin"})) is None
    assert transcript.said(_row("user", origin={"kind": "task-notification"},
                                message={"role": "user", "content": "<task-notification>TEST</task-notification>"})) is None
    assert transcript.said(_person([{"type": "tool_result", "tool_use_id": "T", "content": "TEST"}])) is None
    assert transcript.said(_person("TEST summary", isCompactSummary=True)) is None
    assert transcript.said(_person("TEST meta", isMeta=True)) is None
    assert transcript.said(_person("TEST side", isSidechain=True)) is None
    assert transcript.said(_row("attachment", attachment={
        "type": "queued_command", "commandMode": "task-notification", "origin": {"kind": "human"}, "prompt": "TEST"})) is None


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
