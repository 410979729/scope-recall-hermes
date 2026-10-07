"""Reading WorkBuddy's session record and prompt: what counts as the person's words, and where the record is.

Rows are synthetic and shaped like the record's (``message`` lines with ``input_text`` / ``output_text`` blocks,
millisecond timestamps); nothing here is a person's conversation.
"""

from __future__ import annotations

import json

from scope_recall.adapters.clients import transcript
from scope_recall.adapters.clients.boundary import is_workbuddy_agent_run, workbuddy_person_text

AT_MS = 1759320000123
AT = "2025-10-01T12:00:00.123000Z"


def _message(role, text, **fields):
    kind = "input_text" if role == "user" else "output_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": kind, "text": text}],
        "id": "TEST-id",
        "parentId": "TEST-parent",
        "sessionId": "TEST-session",
        "timestamp": AT_MS,
        "status": "completed",
        **fields,
    }


def test_the_person_s_words_are_the_last_query_without_reminders():
    joined = (
        "<system-reminder>TEST 环境说明</system-reminder>\n<user_query>TEST 上一条</user_query>\n"
        '<system-reminder data-role="tool-hint">TEST 提示</system-reminder><user_query>\nTEST 这一条\n</user_query>'
    )
    assert workbuddy_person_text(joined) == "TEST 这一条"
    assert workbuddy_person_text("<system-reminder>TEST</system-reminder>\nTEST 没有块的话") == "TEST 没有块的话"
    assert workbuddy_person_text("TEST 已经去掉了外壳") == "TEST 已经去掉了外壳", "WorkBuddy 5.3.14 strips them itself"
    assert workbuddy_person_text("<system-reminder>TEST only</system-reminder>  ") == ""


def test_only_a_subagent_s_hook_is_an_agent_run():
    assert is_workbuddy_agent_run({"agent_id": "agent-TEST1", "agent_type": "TEST-explorer"})
    assert is_workbuddy_agent_run({"transcript_path": "C:/TEST/projects/c--w/S/subagents/agent-TEST1.jsonl"})
    assert is_workbuddy_agent_run({"transcript_path": "C:\\TEST\\projects\\c--w\\S\\subagents\\agent-TEST1.jsonl"})
    # The agent that runs the person's own session is named on every turn after the first.
    assert not is_workbuddy_agent_run({"agent_type": "craft"})
    # A session loaded from a record named otherwise reports that record's id.
    assert not is_workbuddy_agent_run({"agent_id": "0f3c2d1e-TEST", "agent_type": "craft"})
    assert not is_workbuddy_agent_run({})


def test_the_person_s_message_and_the_model_s_text_are_said():
    raw = "<system-reminder>TEST 提醒</system-reminder><user_query>TEST 第一行\nTEST 第二行</user_query>"
    said = transcript.workbuddy_said(_message("user", raw))
    assert said == transcript.Said("TEST-id", "user", "TEST 第一行\nTEST 第二行", AT)
    model = _message("assistant", "TEST 我先看一下")
    model["content"].append({"type": "output_text", "text": "，再回答。"})
    said = transcript.workbuddy_said(model)
    assert said is not None and (said.role, said.text) == ("assistant", "TEST 我先看一下，再回答。"), (
        "joined as the Stop hook's last_assistant_message joins them"
    )
    plain = transcript.workbuddy_said({**_message("user", ""), "content": "<user_query>TEST 字符串内容</user_query>"})
    assert plain is not None and plain.text == "TEST 字符串内容"
    stamped = transcript.workbuddy_said(
        {**_message("user", "<user_query>TEST</user_query>"), "timestamp": "2025-10-01T12:00:00.123Z"}
    )
    assert stamped is not None and stamped.occurred_at == AT


def test_nothing_else_of_the_record_is_said():
    query = "<user_query>TEST</user_query>"
    assert transcript.workbuddy_said(_message("user", query)) is not None
    for kind in ("reasoning", "function_call", "function_call_result", "ai-title", "file-history-snapshot"):
        assert transcript.workbuddy_said({**_message("assistant", "TEST"), "type": kind}) is None, kind
    assert transcript.workbuddy_said(_message("user", query, providerData={"isMeta": True})) is None
    assert transcript.workbuddy_said(_message("user", query, providerData={"isCompactInternal": True})) is None
    notice = "<task-notification>\n<task-id>TEST</task-id>\n<status>completed</status>\n</task-notification>"
    assert transcript.workbuddy_said(_message("user", notice)) is None
    assert transcript.workbuddy_said(_message("assistant", "TEST")["content"][0]) is None
    wrong_block = _message("user", query)
    wrong_block["content"][0]["type"] = "output_text"
    assert transcript.workbuddy_said(wrong_block) is None
    assert transcript.workbuddy_said(_message("system", query)) is None
    assert transcript.workbuddy_said(_message("user", "<system-reminder>TEST</system-reminder>")) is None


def test_an_error_shown_in_place_of_a_reply_is_not_said(tmp_path):
    """WorkBuddy's agent records the error it showed when the model could not answer as a model message whose words
    are the error's (``providerData.error``, status ``incomplete``).  A reply that broke off carries the error too and
    keeps its words; the Stop's reply is the notice only when the record's last model message is that notice."""
    notice = "TEST Authentication required. Please use /login command to sign in to your account"
    failed = _message(
        "assistant",
        notice,
        status="incomplete",
        providerData={"error": {"message": notice, "isNetworkError": False, "isRetryable": False}},
    )
    assert transcript.workbuddy_said(failed) is None
    padded = _message("assistant", f"{notice}\n", providerData={"error": {"message": f" {notice}\n"}})
    assert transcript.workbuddy_said(padded) is None, "whitespace around either side is not words"
    cut = _message(
        "assistant",
        "TEST 写到一半",
        status="incomplete",
        providerData={"error": {"message": "TEST stream timed out", "isStreamTimeout": True}},
    )
    assert transcript.workbuddy_said(cut).text == "TEST 写到一半"
    assert transcript.workbuddy_said(_message("assistant", notice, providerData={"error": {"message": 3}})) is not None
    record = tmp_path / "TEST-session.jsonl"
    lines = [_message("user", "<user_query>TEST 问</user_query>"), failed, {"type": "ai-title", "title": "TEST"}]
    record.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    assert transcript.workbuddy_error_reply(record, notice)
    assert transcript.workbuddy_error_reply(record, f"  {notice}\n")
    assert not transcript.workbuddy_error_reply(record, "TEST 别的回答")
    assert not transcript.workbuddy_error_reply(record, "  ")
    with record.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_message("assistant", notice), ensure_ascii=False) + "\n")
    assert not transcript.workbuddy_error_reply(record, notice), "the last model message carries no error"
    assert not transcript.workbuddy_error_reply(tmp_path / "TEST-missing.jsonl", notice)


def _failed_line(notice, entry_id="a-err"):
    return {
        **_message("assistant", notice, status="incomplete", providerData={"error": {"message": notice}}),
        "id": entry_id,
    }


def _write(path, rows, *, newline="\n", prefix=b"", suffix=b""):
    path.write_bytes(
        prefix + "".join(json.dumps(row, ensure_ascii=False) + newline for row in rows).encode("utf-8") + suffix
    )
    return path


def test_the_error_check_reads_the_record_s_last_model_message_whatever_follows_it(tmp_path):
    """The Stop's check reads the record's end: other lines after the error (reasoning, tool calls, a title) are passed
    over, a line still being written or bytes that are not UTF-8 are skipped, and CRLF or a BOM change nothing."""
    notice = "TEST Authentication required. Please use /login command to sign in to your account"
    tail = [
        {
            "type": "reasoning",
            "role": "assistant",
            "id": "r1",
            "providerData": {"error": {"message": "TEST 别的"}},
            "summary": [{"type": "summary_text", "text": notice}],
        },
        {"type": "function_call", "id": "f1", "callId": "c1", "name": "TEST", "arguments": "{}"},
        {"type": "ai-title", "title": "TEST"},
    ]
    assert transcript.workbuddy_error_reply(_write(tmp_path / "a.jsonl", [_failed_line(notice), *tail]), notice)
    partial = b'{"type": "message", "role": "assistant", "content": [{"ty'
    assert transcript.workbuddy_error_reply(
        _write(tmp_path / "b.jsonl", [_failed_line(notice)], newline="\r\n", suffix=partial), notice
    )
    assert transcript.workbuddy_error_reply(
        _write(tmp_path / "c.jsonl", [_failed_line(notice)], suffix=b"\xff\xfe TEST \xc3\n"), notice
    )
    assert transcript.workbuddy_error_reply(
        _write(tmp_path / "d.jsonl", [_failed_line(notice)], prefix=b"\xef\xbb\xbf"), notice
    )


def test_the_error_check_takes_only_the_record_s_end_and_fails_open(tmp_path):
    """A record far past the part read still has its last model message there.  When the end holds no whole model
    message (one line longer than the part read), the check says no and the reply is stored as before: an older error
    is never taken for the new reply."""
    notice = "TEST Request failed."
    early = [_message("assistant", "TEST " + "x" * 4000) for _ in range(100)]
    record = _write(tmp_path / "big.jsonl", [*early, _failed_line(notice)])
    assert record.stat().st_size > transcript._TAIL_BYTES
    assert transcript.workbuddy_error_reply(record, notice)
    long_reply = _message("assistant", "TEST " + "z" * (300 * 1024))
    assert not transcript.workbuddy_error_reply(
        _write(tmp_path / "cut.jsonl", [_failed_line(notice), long_reply]), notice
    )


def test_an_error_check_finds_no_error_where_the_record_names_none(tmp_path):
    notice = "TEST Authentication required."
    for provider in (
        {"error": notice},
        {"error": {"message": 3}},
        {"error": {"message": "  "}},
        {"error": None},
        "TEST-not-a-dict",
    ):
        row = _message("assistant", notice, providerData=provider)
        assert not transcript.workbuddy_error_reply(_write(tmp_path / "r.jsonl", [row]), notice), provider
        assert transcript.workbuddy_said(row) is not None, provider


def test_an_error_handed_without_its_line_breaks_is_still_the_error(tmp_path):
    """WorkBuddy takes the line breaks out of what it hands its hooks; the comparison ignores whitespace, as the rest of
    its matching does."""
    notice = "TEST Request failed.\nTEST Check your network and try again."
    record = _write(tmp_path / "r.jsonl", [_failed_line(notice)])
    assert transcript.workbuddy_error_reply(record, notice.replace("\n", ""))
    assert transcript.workbuddy_said(_failed_line(notice)) is None


def test_a_user_message_without_a_user_query_block_is_not_the_person_s():
    """WorkBuddy saves what the person sent inside ``<user_query>``.  The user messages it adds itself carry none: a
    local command and its output, a shell command run in bash mode and its output (``CommandMessageUtils``, saved
    with ``skipRun``), a teammate's report, and a slash command's expansion, which the command interceptor writes over
    the typed command before the message is saved (the prompt hook is handed the typed command)."""
    rows = [
        _message("user", "<bash-input>dir</bash-input>", providerData={"skipRun": True}),
        _message(
            "user",
            "<bash-stdout>TEST a.py\nTEST b.py</bash-stdout><bash-stderr></bash-stderr>",
            providerData={"skipRun": True},
        ),
        _message(
            "user",
            "<command-name>/model</command-name><command-args>TEST</command-args>",
            providerData={"skipRun": True},
        ),
        _message("user", "<local-command-stdout>TEST switched</local-command-stdout>", providerData={"skipRun": True}),
        _message(
            "user",
            '<teammate-message teammate_id="TEST" summary="TEST">\nTEST done\n</teammate-message>',
            providerData={"teammateMessage": {"from": "TEST"}},
        ),
        _message(
            "user",
            "<command-message>review</command-message> <command-name>/review</command-name>\n"
            "Base directory for this skill: C:/TEST\nTEST the skill's own instructions",
        ),
        _message("user", "TEST plain text that WorkBuddy did not wrap"),
    ]
    for row in rows:
        assert transcript.workbuddy_said(row) is None, row["content"][0]["text"]


def test_every_query_of_a_merged_message_is_the_person_s():
    """Messages sent while a turn ran are merged into one, a block each (``mergeConsecutiveUserMessages``); the prompt
    hook is handed only the last, so the record is where the others are."""
    merged = _message("user", "<system-reminder>TEST 提醒</system-reminder>\n<user_query>TEST 第一件事</user_query>")
    merged["content"].append({"type": "input_text", "text": "<user_query>TEST 第二件事</user_query>"})
    said = transcript.workbuddy_said(merged)
    assert said is not None and said.text == "TEST 第一件事\nTEST 第二件事"


def test_a_message_without_an_id_or_a_time_is_skipped():
    query = "<user_query>TEST</user_query>"
    assert transcript.workbuddy_said({**_message("user", query), "id": None}) is None
    assert transcript.workbuddy_said({**_message("user", query), "id": "x" * 101}) is None
    for stamp in (None, 0, -5, float("nan"), "not a time", "2025-10-01T12:00:00", True, 10**20):
        assert transcript.workbuddy_said({**_message("user", query), "timestamp": stamp}) is None, stamp


def test_a_read_takes_workbuddy_lines_with_its_reader(tmp_path):
    record = tmp_path / "TEST-session.jsonl"
    lines = [
        _message("user", "<user_query>TEST 问</user_query>"),
        {"type": "reasoning", "id": "r", "timestamp": AT_MS},
        _message("assistant", "TEST 答", id="TEST-id-2"),
    ]
    record.write_bytes(b"".join(json.dumps(line, ensure_ascii=False).encode("utf-8") + b"\n" for line in lines))
    read = transcript.read(record, 0, rows=transcript.workbuddy_said)
    assert [said.text if said else None for _end, said in read] == ["TEST 问", None, "TEST 答"]
    assert read[-1][0] == record.stat().st_size


def test_the_record_is_the_hook_s_path_or_is_found_by_its_name(tmp_path):
    projects = tmp_path / "TEST-projects"
    record = projects / "c--TEST-work" / "TEST-session.jsonl"
    record.parent.mkdir(parents=True)
    record.write_text("", encoding="utf-8")
    (projects / "c--TEST-other").mkdir()
    find = lambda value, **kwargs: transcript.workbuddy_record_path(
        value,
        "TEST-session",
        projects=projects,  # noqa: E731
        **kwargs,
    )
    assert find(str(record)) == record
    # Reported wrong: ".json" for ".jsonl", or the path cut two characters short.
    assert find(str(record)[:-1]) == record
    assert find(str(record)[:-2]) == record
    assert find(None) == record and find("") == record
    assert find("TEST-session.jsonl") == record, "a relative path is never opened, the record is looked for"
    # Another session's existing record is not read in this one's place.
    other = projects / "c--TEST-other" / "TEST-other.jsonl"
    other.write_text("", encoding="utf-8")
    assert transcript.workbuddy_record_path(str(other), "TEST-session", projects=tmp_path / "TEST-empty") is None
    assert transcript.workbuddy_record_path(str(record)[:-1], "TEST-missing", projects=projects) is None
    assert transcript.workbuddy_record_path(None, "TEST-session", projects=tmp_path / "TEST-none") is None


def test_the_records_are_where_workbuddy_s_configuration_folder_is(tmp_path, monkeypatch):
    """WorkBuddy's CLI keeps its records under the folder ``CODEBUDDY_CONFIG_DIR`` names, which its hooks inherit."""
    monkeypatch.setenv("CODEBUDDY_CONFIG_DIR", str(tmp_path / "TEST-config"))
    assert transcript.workbuddy_projects() == tmp_path / "TEST-config" / "projects"
    monkeypatch.setenv("CODEBUDDY_CONFIG_DIR", "TEST-relative")
    assert transcript.workbuddy_projects().parent.name == ".workbuddy"
    monkeypatch.delenv("CODEBUDDY_CONFIG_DIR")
    assert transcript.workbuddy_projects().parent.name == ".workbuddy"


def test_a_session_with_a_record_of_another_name_is_read_under_that_name(tmp_path):
    """WorkBuddy names the record by the session's store id when it has one, and reports that id as ``agent_id``."""
    projects = tmp_path / "TEST-projects"
    record = projects / "c--TEST-work" / "TEST-store-id.jsonl"
    record.parent.mkdir(parents=True)
    record.write_text("", encoding="utf-8")
    assert (
        transcript.workbuddy_record_path(str(record), "TEST-session", record_id="TEST-store-id", projects=projects)
        == record
    )
    assert (
        transcript.workbuddy_record_path(str(record)[:-1], "TEST-session", record_id="TEST-store-id", projects=projects)
        == record
    )
    assert (
        transcript.workbuddy_record_path(None, "TEST-session", record_id="../TEST-store-id", projects=projects) is None
    ), "an id that is a path names nothing"
    # A record under the session's own id beside it is not the one WorkBuddy writes on in.
    (record.parent / "TEST-session.jsonl").write_text("", encoding="utf-8")
    assert (
        transcript.workbuddy_record_path(None, "TEST-session", record_id="TEST-store-id", projects=projects) == record
    )


def test_an_id_that_is_a_path_reaches_no_file_outside_the_workspace_folders(tmp_path):
    """The record is looked for by name in the workspace folders only.  An id with a separator, a drive or a dot
    segment names nothing, even where such a path would lead to a file that exists."""
    projects = tmp_path / "TEST-projects"
    (projects / "c--TEST-work").mkdir(parents=True)
    (projects / "TEST-escaped.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "TEST-outside.jsonl").write_text("", encoding="utf-8")
    for name in ("../TEST-escaped", "..\\TEST-escaped", str(tmp_path / "TEST-outside")):
        assert transcript.workbuddy_record_path(None, name, projects=projects) is None, name
        assert transcript.workbuddy_record_path(None, "TEST-session", record_id=name, projects=projects) is None, name
