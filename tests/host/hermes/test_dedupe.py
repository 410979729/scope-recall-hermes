"""Cross-hook source dedupe and outcome gap contracts."""
from __future__ import annotations

import sqlite3

from scope_recall.adapters.hermes import ScopeRecallHermesAdapter
from scope_recall.adapters.hermes.boundary import SourceObservationLedger, pre_llm_source_event, sync_turn_source_events
from scope_recall.core import capture_inbox
from tests.v11_support import context


def test_same_event_identity_is_idempotent_across_hooks(tmp_path):
    ledger = SourceObservationLedger()
    ctx = context(tmp_path / "db")
    first, _, first_identity = pre_llm_source_event(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-1",
        user_message="same text",
        recorded_at="2026-09-06T12:00:00Z",
    )
    second, _, _ = pre_llm_source_event(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-1",
        user_message="same text",
        recorded_at="2026-09-06T12:00:01Z",
    )
    assert first is not None
    assert first_identity is not None
    assert second is None


def test_equal_text_without_shared_identity_stays_distinct(tmp_path):
    ledger = SourceObservationLedger()
    ctx = context(tmp_path / "db")
    pre, _, _ = pre_llm_source_event(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-1",
        user_message="same text",
        recorded_at="2026-09-06T12:00:00Z",
    )
    sync_events, gaps = sync_turn_source_events(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-2",
        user_content="same text",
        assistant_content="ok",
        recorded_at="2026-09-06T12:00:01Z",
        outcome="success",
    )
    assert pre is not None
    assert len(sync_events) == 2
    assert gaps == ()


def test_pre_llm_and_sync_turn_replay_same_stable_turn_once(tmp_path):
    ledger = SourceObservationLedger()
    ctx = context(tmp_path / "db")
    pre, _, _ = pre_llm_source_event(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-shared",
        user_message="same text",
        recorded_at="2026-09-06T12:00:00Z",
    )
    sync_events, gaps = sync_turn_source_events(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-shared",
        user_content="same text",
        assistant_content="ok",
        recorded_at="2026-09-06T12:00:01Z",
        outcome="success",
    )
    assert pre is not None
    assert [event[0]["role"] for event in sync_events] == ["assistant"]
    assert gaps == ()


def test_failure_and_truncated_outcomes_record_gaps(adapter):
    provider, _clock = adapter
    provider.on_turn_start(2, "fail", turn_id="turn-2")
    provider.observe_api_request_error(session_id="TEST-session-1", turn_id="turn-2", status="400")
    provider.sync_turn("question", "", session_id="TEST-session-1")
    gaps = provider.diagnostics.pending_outcome_gaps
    assert any("failure" in gap for gap in gaps)
    assert any("truncated" in gap or "missing_assistant" in gap for gap in gaps)


def test_success_sync_persists_with_trusted_context(adapter, hermes_home):
    provider, _clock = adapter
    provider.on_turn_start(3, "ok", turn_id="turn-3")
    provider.sync_turn("TEST 记住白色。", "好的。", session_id="TEST-session-1")
    db = hermes_home / "scope-recall" / "memory.sqlite3"
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
    assert count >= 1


def test_what_the_assistant_showed_between_tool_calls_is_recorded_with_the_answer(adapter, hermes_home):
    """Hermes hands ``sync_turn`` the answer only; the rest of the turn arrives with ``post_llm_call``."""
    provider, _clock = adapter
    provider.on_turn_start(4, "TEST 查一下 QX-17", turn_id="turn-4")
    history = [
        {"role": "user", "content": "TEST 上一轮"},
        {"role": "assistant", "content": "TEST 上一轮说的话", "tool_calls": [{"id": "T0"}]},
        {"role": "user", "content": "TEST 查一下 QX-17"},
        {"role": "assistant", "content": "TEST 我先看记录。", "tool_calls": [{"id": "T1"}], "timestamp": 1790000000.25},
        {"role": "tool", "tool_call_id": "T1", "content": "TEST 工具输出"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "T2"}], "codex_message_items": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "TEST unseen"}]},
            {"type": "message", "phase": "commentary", "content": [{"type": "output_text", "text": "TEST 再看第二份。"}]}]},
        {"role": "tool", "tool_call_id": "T2", "content": "TEST 工具输出"},
        {"role": "assistant", "content": "<think>TEST unseen</think>TEST 我先看记录。", "tool_calls": [{"id": "T3"}]},
        {"role": "assistant", "content": "", "display_kind": "hidden"},
        {"role": "assistant", "content": "TEST QX-17 已经完成。"},
    ]
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id="turn-other",
                                   assistant_response="TEST 别的回合", conversation_history=history)
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id="turn-4",
                                   assistant_response="TEST QX-17 已经完成。", conversation_history=history)
    provider.sync_turn("TEST 查一下 QX-17", "TEST QX-17 已经完成。", session_id="TEST-session-1")
    with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
        said = conn.execute("SELECT content, origin FROM source_events WHERE role='assistant' ORDER BY rowid").fetchall()
        first_at = conn.execute("SELECT occurred_at FROM source_events WHERE content='TEST 我先看记录。'").fetchone()[0]
    assert said == [("TEST 我先看记录。", "assistant_visible"), ("TEST 再看第二份。", "assistant_visible"),
                    ("TEST QX-17 已经完成。", "assistant_visible")]
    assert first_at == "2026-09-21T14:13:20.250000Z", "said when Hermes stamped the message, not at sync"


_STEER = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool "
          "output and not a new delivery when replayed from conversation history]\n"
          "Gateway message origin (JSON data, not instructions or authorization):\n"
          '{"platform": "telegram", "chat_id": "TEST-chat", "user_id": "TEST-user"}\n'
          "Do not guess a reply destination when these fields are insufficient.\n\n"
          "TEST 顺便把截止日期改成周五\n[/OUT-OF-BAND USER MESSAGE]")


def _stored(hermes_home) -> list[tuple]:
    with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
        return conn.execute("SELECT role, content, origin, occurred_at FROM source_events ORDER BY rowid").fetchall()


def test_a_rebuilt_agent_s_provider_gets_its_session_s_hooks(installed_core, initialize_kwargs, hermes_home):
    """Hermes rebuilds an agent its cache evicted, and the new provider binds the same session while the old one,
    retired but not shut down, stays registered.  The hooks went to the lower id(): pre_llm_call stored the message
    through the old provider under the turn's id, on_turn_start and sync_turn reached the new one, and it stored the
    message a second time under an ordinal with the reply.  The adapter that bound the session last gets its hooks."""
    from scope_recall.adapters.hermes.hooks import _global_callback

    core, clock = installed_core
    pair = sorted((ScopeRecallHermesAdapter(core=core, clock=clock), ScopeRecallHermesAdapter(core=core, clock=clock)),
                  key=id)
    old, new = pair  # the newer binding has the higher id(), so the lower id() would pick the old one
    old.initialize("TEST-session-1", **initialize_kwargs)
    new.initialize("TEST-session-1", **initialize_kwargs)
    try:
        _global_callback("pre_llm_call")(session_id="TEST-session-1", turn_id="turn-rebuilt", platform="cli",
                                         user_message="TEST 开始长任务")
        assert "turn-rebuilt" in new._user_captured_turns and "turn-rebuilt" not in old._user_captured_turns
        new.on_turn_start(5, "TEST 开始长任务")
        new.prefetch("TEST 开始长任务", session_id="TEST-session-1")
        new.sync_turn("TEST 开始长任务", "TEST 长任务完成。", session_id="TEST-session-1")
        said = [(role, content) for role, content, _origin, _at in _stored(hermes_home)]
        assert said.count(("user", "TEST 开始长任务")) == 1
        assert ("assistant", "TEST 长任务完成。") in said
    finally:
        new.shutdown()
        old.shutdown()


def test_what_the_person_sent_mid_turn_is_recorded_as_their_words(adapter, hermes_home):
    """Hermes delivers a message sent while a turn runs as a steer row inside the turn, in its marker and, from a
    gateway, after an origin preamble of chat and user ids.  None of it was stored, and the turn's scan stopped at
    that row, so what the assistant said before it was lost too."""
    provider, _clock = adapter
    provider.on_turn_start(5, "TEST 整理 QX-18", turn_id="turn-5")
    history = [
        {"role": "user", "content": "TEST 整理 QX-18"},
        {"role": "assistant", "content": "TEST 先列出清单。", "tool_calls": [{"id": "T1"}]},
        {"role": "tool", "tool_call_id": "T1", "content": "TEST 工具输出"},
        {"role": "user", "content": _STEER, "display_kind": "steer", "timestamp": 1790000100.5},
        {"role": "assistant", "content": "TEST 收到，改成周五。", "tool_calls": [{"id": "T2"}]},
        {"role": "tool", "tool_call_id": "T2", "content": "TEST 工具输出"},
        {"role": "assistant", "content": "TEST QX-18 已整理，截止周五。"},
    ]
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id="turn-5",
                                   assistant_response="TEST QX-18 已整理，截止周五。", conversation_history=history)
    provider.sync_turn("TEST 整理 QX-18", "TEST QX-18 已整理，截止周五。", session_id="TEST-session-1")
    rows = _stored(hermes_home)
    said = [(role, content) for role, content, _origin, _at in rows]
    assert ("user", "TEST 顺便把截止日期改成周五") in said
    assert ("assistant", "TEST 先列出清单。") in said, "what came before the steer is part of the turn"
    assert ("assistant", "TEST 收到，改成周五。") in said
    assert not any("TEST-chat" in content or "OUT-OF-BAND" in content for _role, content in said)
    origins = {content: (origin, at) for _role, content, origin, at in rows}
    assert origins["TEST 顺便把截止日期改成周五"][0] == origins["TEST 整理 QX-18"][0], "the person's own words"
    assert origins["TEST 顺便把截止日期改成周五"][1] == "2026-09-21T14:15:00.500000Z"


_NOTICE = "[IMPORTANT: Background process TEST-proc finished (exit code 0).\nCommand: TEST make build]"


def test_a_message_hermes_writes_itself_is_stored_as_the_host_s(adapter, hermes_home):
    """Hermes opens a turn itself when a background process finishes, a delegation returns or a plugin speaks, with
    a user message it marks by a display kind.  Stored as the person's, it read as something they said, and it
    ended the turn whose reply it had been waiting for."""
    from scope_recall.adapters.hermes.hooks import _global_callback

    provider, _clock = adapter
    asked = {"role": "user", "content": "TEST 跑一下构建"}
    started = {"role": "assistant", "content": "TEST 已在后台运行。"}
    notice = {"role": "user", "content": _NOTICE, "display_kind": "internal_notification"}
    _global_callback("pre_llm_call")(session_id="TEST-session-1", turn_id="turn-asked", platform="cli",
                                     user_message=asked["content"], conversation_history=[asked])
    provider.sync_turn(asked["content"], started["content"], session_id="TEST-session-1")
    _global_callback("pre_llm_call")(session_id="TEST-session-1", turn_id="turn-notice", platform="cli",
                                     user_message=_NOTICE, conversation_history=[asked, started, notice])
    provider.observe_post_llm_call(session_id="TEST-session-1", turn_id="turn-notice",
                                   assistant_response="TEST 构建通过了。", conversation_history=[
                                       asked, started, notice,
                                       {"role": "user", "content": _STEER, "display_kind": "steer"},
                                       {"role": "assistant", "content": "TEST 构建通过了。"}])
    provider.sync_turn(_NOTICE, "TEST 构建通过了。", session_id="TEST-session-1")
    rows = [(role, content, origin) for role, content, origin, _at in _stored(hermes_home)]
    assert ("user", "TEST 跑一下构建", "human_direct") in rows
    assert ("user", _NOTICE, "host_generated") in rows
    assert ("user", "TEST 顺便把截止日期改成周五", "human_direct") in rows, "a steer in a notice's turn is the person's"
    assert ("assistant", "TEST 构建通过了。", "assistant_visible") in rows
    assert sum(content == _NOTICE for _role, content, _origin in rows) == 1


def test_a_notice_whose_pre_llm_call_was_not_taken_is_still_the_host_s(adapter, hermes_home):
    """A busy session leaves pre_llm_call untaken, and sync_turn stores the turn's opening message instead."""
    provider, _clock = adapter
    provider._session_busy("pre_llm_call", {"turn_id": "turn-busy", "user_message": _NOTICE, "conversation_history": [
        {"role": "user", "content": _NOTICE, "display_kind": "internal_notification"}]})
    provider.on_turn_start(7, _NOTICE)
    provider.sync_turn(_NOTICE, "TEST 构建通过了。", session_id="TEST-session-1")
    rows = [(role, content, origin) for role, content, origin, _at in _stored(hermes_home)]
    assert ("user", _NOTICE, "host_generated") in rows
    assert ("assistant", "TEST 构建通过了。", "assistant_visible") in rows


def test_a_late_sync_of_the_person_s_turn_keeps_their_words_theirs(adapter, hermes_home):
    """Hermes runs ``sync_turn`` on its memory worker, after the reply, and the adapter names the turn by the one
    active then: the next turn, a notice whose ``pre_llm_call`` a busy session left untaken, may have begun.  The
    person's message written then is still theirs (review of 3.7.2)."""
    provider, _clock = adapter
    said = "TEST 帮我看一下日志"
    provider._session_busy("pre_llm_call", {"turn_id": "turn-person", "user_message": said,
                                            "conversation_history": [{"role": "user", "content": said}]})
    provider.on_turn_start(8, said)
    provider._session_busy("pre_llm_call", {"turn_id": "turn-host", "user_message": _NOTICE, "conversation_history": [
        {"role": "user", "content": said}, {"role": "assistant", "content": "TEST 日志正常。"},
        {"role": "user", "content": _NOTICE, "display_kind": "internal_notification"}]})
    provider.on_turn_start(9, _NOTICE)
    provider.sync_turn(said, "TEST 日志正常。", session_id="TEST-session-1")
    rows = [(role, content, origin) for role, content, origin, _at in _stored(hermes_home)]
    assert ("user", said, "human_direct") in rows
    assert not any(origin == "host_generated" for _role, _content, origin in rows)


#: Hermes 0.21.5's own lines around a folded summary and a carried to-do list (agent.context_compressor,
#: tools.todo_tool).
_PRIOR = "[PRIOR CONTEXT \u2014 for reference only; not a new message]"
_DELIMITER = "[END OF PRIOR CONTEXT \u2014 COMPACTION SUMMARY BELOW]"
_END = "--- END OF CONTEXT SUMMARY \u2014 respond to the message below, not the summary above ---"
_TODO = "[Your active task list was preserved across context compression]\n- TEST 整理清单"
_RESTATED = ("[STILL IN PROGRESS — this is the active request, restated after the compaction boundary because it "
             "was not finished yet. Continue it; do not start over.]")
#: A delegation's result: the kind of notice Hermes appends a to-do list to (``_fold_todo_snapshot`` passes over a
#: background process's notice, which it counts as its own scaffolding).
_DELEGATED = "[ASYNC DELEGATION COMPLETE] TEST 子任务已完成：构建产物已上传，日志在 logs/TEST-build.txt。"
_ASKED_LONG = "TEST 请把 QX-17 的发布说明整理成三段，并核对每段里引用的版本号和日期是否一致"


def _folded(own, summary="TEST 摘要：之前在后台跑构建。", *, leading=False, **marks):
    """A user message Hermes folded a compression summary into (``ContextCompressor._merge_summary_into_tail_row``):
    the message, its summary after it and the end line, or (``leading``) the summary first and the message after."""
    content = (summary + "\n\n" + _END + "\n\n" + own if leading
               else _PRIOR + "\n" + own + "\n\n" + _DELIMITER + "\n\n" + summary + "\n\n" + _END)
    return {"role": "user", "content": content, "_compressed_summary": True, **marks}


def test_a_notice_hermes_folded_a_summary_into_is_the_host_s(adapter, hermes_home):
    """A compression at the turn's start folded its summary into the notice that opened the turn, which then held
    more than its text and was not last: one of tianshu's three delegation results on 2026-10-05 was stored as the
    owner's that way."""
    from scope_recall.adapters.hermes.hooks import _global_callback

    provider, _clock = adapter
    # As Hermes leaves it: the folded notice, the reply it folded away put back after it (``_reply_insertion_index``),
    # and the open to-do list as a message of its own.
    history = [{"role": "user", "content": "TEST 第一句"}, {"role": "assistant", "content": "TEST 好。"},
               _folded(_NOTICE, display_kind="internal_notification"),
               {"role": "assistant", "content": "TEST 构建已在后台运行。"},
               {"role": "user", "content": _TODO, "_todo_snapshot_synthetic": True}]
    _global_callback("pre_llm_call")(session_id="TEST-session-1", turn_id="turn-folded", platform="cli",
                                     user_message=_NOTICE, conversation_history=history)
    rows = [(role, content, origin) for role, content, origin, _at in _stored(hermes_home)]
    assert ("user", _NOTICE, "host_generated") in rows


def test_only_a_folded_message_s_own_words_make_it_the_turn_s():
    """A summary quotes the person's messages word for word, and the first version took a folded notice merely
    holding the turn's text for the turn's own: the person's words were stored as the host's (review of 3.7.3)."""
    from scope_recall.adapters.hermes.boundary import host_notice

    tail = [{"role": "assistant", "content": "TEST 上一步"}]
    assert host_notice([_folded(_NOTICE, display_kind="process_complete"), *tail], _NOTICE)
    assert host_notice([_folded(_NOTICE, leading=True, display_kind="internal_notification"), *tail], _NOTICE)
    assert host_notice([_folded("TEST 短", display_kind="internal_notification"), *tail], "TEST 短"), "no length bar"
    quoting = _folded(_NOTICE, summary="TEST 摘要：主人说过：" + _ASKED_LONG, display_kind="internal_notification")
    assert not host_notice([quoting, *tail], _ASKED_LONG), "the person's words quoted in a summary"
    holding = _folded("TEST 委派结果，原任务：" + _ASKED_LONG, display_kind="internal_notification")
    assert not host_notice([holding, *tail], _ASKED_LONG), "its own words are the turn's text, not merely hold it"
    assert not host_notice([quoting, *tail, {"role": "user", "content": _ASKED_LONG + "\n\n" + _TODO}], _ASKED_LONG), \
        "the person's own message, a to-do list appended, decides first"
    assert not host_notice([_folded(_NOTICE), *tail], _NOTICE), "the person's folded message"
    assert not host_notice([_folded(_NOTICE, display_kind="hidden"), *tail], _NOTICE), "hidden may wrap the person's"
    assert not host_notice([_folded(_NOTICE, display_kind="steer"), *tail], _NOTICE)
    assert not host_notice([_folded("TEST 另一条通知", display_kind="internal_notification"), *tail], _NOTICE)
    earlier = {"role": "user", "content": _NOTICE, "display_kind": "internal_notification"}
    assert not host_notice([earlier, *tail], _NOTICE), "an earlier turn's notice, not folded, is not this turn's"
    folded_earlier = _folded(_NOTICE, display_kind="internal_notification")
    prefixed = {"role": "user", "content": "[Note: the model was switched.]\n\n" + _NOTICE}
    assert not host_notice([folded_earlier, *tail, prefixed], _NOTICE), \
        "an older folded notice never takes the person's later message, a note Hermes put before it"
    assert not host_notice([folded_earlier, *tail, _folded(_NOTICE), *tail], _NOTICE), \
        "nor the person's own newer folded message"
    unmarked = {"role": "user", "display_kind": "internal_notification",
                "content": "TEST 构建输出引用了：\n" + _END + "\n\n" + _ASKED_LONG}
    assert not host_notice([unmarked], _ASKED_LONG), "an unmarked message is not unwrapped"
    standalone = {"role": "user", "content": _NOTICE, "_compressed_summary": True, "display_kind": "internal_notification"}
    assert not host_notice([standalone, *tail], _NOTICE), "a marked message without the lines is a summary of its own"


def test_a_folded_notice_is_read_as_hermes_reads_it_back():
    """List content, the restatement Hermes adds after the end line, and a to-do list folded in before the summary."""
    from scope_recall.adapters.hermes.boundary import host_notice

    tail = [{"role": "assistant", "content": "TEST 上一步"}]
    listed = _folded(_NOTICE, display_kind="internal_notification")
    listed["content"] = [{"type": "text", "text": listed["content"]}]
    assert host_notice([listed, *tail], _NOTICE)
    restated = _folded(_NOTICE, display_kind="internal_notification")
    restated["content"] += "\n\n" + _RESTATED + "\n" + _ASKED_LONG
    assert host_notice([restated, *tail], _NOTICE)
    assert host_notice([_folded(_DELEGATED + "\n\n" + _TODO, display_kind="internal_notification"), *tail],
                       _DELEGATED)


def test_a_notice_a_to_do_list_was_appended_to_is_still_the_host_s():
    """A compression appends the open to-do list to the last user message Hermes counts as a real one, a
    delegation's result included (``_fold_todo_snapshot``); read back without it, the notice is still found (review
    of 3.7.3)."""
    from scope_recall.adapters.hermes.boundary import host_notice

    notice = {"role": "user", "content": _DELEGATED + "\n\n" + _TODO, "display_kind": "internal_notification"}
    assert host_notice([{"role": "assistant", "content": "TEST 好。"}, notice], _DELEGATED)


def test_a_note_before_the_person_s_message_keeps_an_unanswered_notice_from_taking_it():
    """Hermes puts a note of its own before the person's message (a model switch, a timestamp) and hands
    ``pre_llm_call`` their words alone.  An unanswered notice before it with the same words took them, folded or not:
    the latest user message with words of its own decides (third review of 3.7.3; 3.7.2's plain notices too)."""
    from scope_recall.adapters.hermes.boundary import host_notice

    reply = {"role": "assistant", "content": "TEST 好。"}
    noted = {"role": "user", "content": "[Note: model was just switched from TEST-a to TEST-b.]\n\n" + _NOTICE}
    plain = {"role": "user", "content": _NOTICE, "display_kind": "internal_notification"}
    assert not host_notice([reply, plain, noted], _NOTICE)
    assert not host_notice([reply, _folded(_NOTICE, display_kind="internal_notification"), noted], _NOTICE)
    assert host_notice([reply, plain], _NOTICE), "the notice alone is still the host's"


def test_the_turn_s_own_message_says_whether_hermes_opened_it():
    """A compression at the turn's start can add user messages after the turn's own: a to-do list, a turn it
    restored.  The turn's own message is the last one holding its text; one not found is the person's."""
    from scope_recall.adapters.hermes.boundary import host_notice

    notice = {"role": "user", "content": _NOTICE, "display_kind": "internal_notification"}
    asked = {"role": "user", "content": "TEST 跑一下构建"}
    todo = {"role": "user", "content": "[Your active task list was preserved across context compression]\n- TEST",
            "_todo_snapshot_synthetic": True}
    assert host_notice([asked, notice], _NOTICE)
    assert host_notice([notice, todo], _NOTICE), "a to-do list a compression added after it"
    assert host_notice([{"role": "user", "content": [{"type": "text", "text": _NOTICE}],
                         "display_kind": "process_complete"}], _NOTICE)
    assert host_notice([{"role": "user", "content": _NOTICE}, notice], _NOTICE), "the last holding its text"
    assert not host_notice([notice, {"role": "user", "content": _NOTICE}], _NOTICE)
    assert not host_notice([notice, asked], asked["content"]), "an earlier notice is not this turn"
    assert not host_notice([asked, notice], asked["content"]), "nor one a compression restored after it"
    assert not host_notice([{**asked, "display_kind": "steer"}], asked["content"]), "a steer is the person's"
    assert not host_notice([{"role": "user", "content": _NOTICE}], _NOTICE), "no kind: the text proves nothing"
    assert not host_notice([notice, {"role": "assistant", "content": "TEST 好"}, {**asked, "content": "[09:00] " + _NOTICE}],
                           _NOTICE), "a message of an earlier turn, before its reply, is never this turn's"
    assert host_notice([{**notice, "content": _NOTICE + "\n\n" + todo["content"]}], _NOTICE), "a to-do list appended"
    assert not host_notice([], _NOTICE) and not host_notice(None, _NOTICE) and not host_notice([notice], "")


def test_a_compression_mid_turn_keeps_the_turn(adapter, hermes_home):
    """A compression gives the conversation a new session id in the middle of a turn that goes on.  The switch
    cleared the turn: its post_llm_call no longer matched, so what it said on the way was never stored, and
    sync_turn stored the opening message a second time under the new session."""
    provider, _clock = adapter
    provider.observe_pre_llm(session_id="TEST-session-1", turn_id="turn-6", user_message="TEST 开始长任务")
    provider.on_turn_start(6, "TEST 开始长任务", turn_id="turn-6")
    provider.on_session_switch("TEST-session-2", parent_session_id="TEST-session-1", reset=False, reason="compression")
    history = [
        {"role": "user", "content": "[CONTEXT COMPACTION] TEST 摘要"},
        {"role": "assistant", "content": "TEST 继续第二步。", "tool_calls": [{"id": "T1"}]},
        {"role": "tool", "tool_call_id": "T1", "content": "TEST 工具输出"},
        {"role": "assistant", "content": "TEST 长任务完成。"},
    ]
    provider.observe_post_llm_call(session_id="TEST-session-2", turn_id="turn-6",
                                   assistant_response="TEST 长任务完成。", conversation_history=history)
    provider.sync_turn("TEST 开始长任务", "TEST 长任务完成。", session_id="TEST-session-2")
    said = [(role, content) for role, content, _origin, _at in _stored(hermes_home)]
    assert said.count(("user", "TEST 开始长任务")) == 1
    assert ("assistant", "TEST 继续第二步。") in said and ("assistant", "TEST 长任务完成。") in said


def test_a_queued_capture_leaves_no_slot_taken(adapter, monkeypatch):
    """A capture the store queued durably stayed pending in the adapter until the session ended; at 64 such, every
    capture in the session was refused."""
    from types import SimpleNamespace

    provider, _clock = adapter
    calls = []
    queued = SimpleNamespace(durability="queued", disposition="queued", error_code=None, event_refs=(), gaps=())
    monkeypatch.setattr(provider._core, "record_host_event", lambda *args, **kwargs: calls.append(1) or queued)
    for turn in range(70):
        provider.observe_pre_llm(session_id="TEST-session-1", turn_id=f"turn-q{turn}", user_message=f"TEST 第 {turn} 句")
    assert len(calls) == 70
    assert provider._ledger.pending_identities() == ()


def test_a_capture_the_busy_store_refused_is_written_at_the_next_turn(adapter, hermes_home, monkeypatch):
    """A write that timed out on a busy store stayed in memory until the session ended or compressed, hours later
    on a long chat, and a gateway restart lost it."""
    from scope_recall.contracts import ContractError

    provider, _clock = adapter
    real = provider._core.record_host_event
    refused = []

    def busy_once(*args, **kwargs):
        if not refused:
            refused.append(1)
            raise ContractError("DEADLINE_EXCEEDED", "writer_lease")
        return real(*args, **kwargs)

    monkeypatch.setattr(provider._core, "record_host_event", busy_once)
    provider.on_turn_start(8, "TEST 跑工具", turn_id="turn-8")
    provider.observe_post_tool_call(session_id="TEST-session-1", turn_id="turn-8", tool_call_id="T8", tool_name="Bash",
                                    result="TEST 工具的输出 QX-19")
    assert refused and [row for row in _stored(hermes_home) if row[0] == "tool"] == []
    provider.sync_turn("TEST 跑工具", "TEST 跑完了。", session_id="TEST-session-1")
    assert [row[1] for row in _stored(hermes_home) if row[0] == "tool"] == ["TEST 工具的输出 QX-19"]


def test_live_turn_events_carry_witnessed_occurrence_time(tmp_path):
    """Live host turns are witnessed: occurred_at grounds to the turn time so
    current-mode recall can serve them (imports keep occurred_at=None)."""
    ledger = SourceObservationLedger()
    ctx = context(tmp_path / "db")
    sync_events, _gaps = sync_turn_source_events(
        ledger,
        ctx,
        session_id="TEST-session",
        turn_id="turn-1",
        user_content="请记住我的靛蓝档案目录名称是 TEST-X。",
        assistant_content="好的。",
        recorded_at="2026-09-06T12:00:00Z",
        outcome="success",
    )
    assert sync_events
    for event, identity in sync_events:
        assert event["occurred_at"] == "2026-09-06T12:00:00Z"
        assert event["time_precision"] == "instant"


def _sync_after_restart(core, clock, initialize_kwargs, *, at: str, turn: int, user: str, assistant: str):
    """One gateway process: its turn counter starts again, its session does not."""
    clock.now = at
    provider = ScopeRecallHermesAdapter(core=core, clock=clock)
    provider.initialize("TEST-session-1", **initialize_kwargs)
    try:
        provider.on_turn_start(turn, user)
        provider.sync_turn(user, assistant, session_id="TEST-session-1")
        context = provider._require_identity().trusted_context(session_id="TEST-session-1", mutation=True)
        capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, context, authorize=lambda _scope: context.allowed_scope_ids, remaining_seconds=5)
    finally:
        provider.shutdown()


def _stored_times(core) -> dict[str, tuple[str, str]]:
    with sqlite3.connect(core.storage.path) as conn:
        rows = conn.execute("SELECT content, occurred_at, recorded_at FROM source_events").fetchall()
    return {content: (occurred, recorded) for content, occurred, recorded in rows}


def test_reused_turn_number_keeps_its_own_witnessed_time(installed_core, initialize_kwargs):
    """A restarted gateway numbers turns from 1 again inside the same session.

    beta's rc32 test report was written on 09-17 under turn number 8, which an
    unrelated turn of the same session had used on 09-16.  Storage re-keyed the
    new messages, but the adapter had already copied the older turn's time onto
    them, so the newest report in memory claimed to be a day old.
    """
    core, clock = installed_core
    _sync_after_restart(core, clock, initialize_kwargs, at="2026-09-06T13:15:04Z", turn=8,
                        user="TEST 整理整个文件夹", assistant="TEST 整理好了。")
    _sync_after_restart(core, clock, initialize_kwargs, at="2026-09-07T11:06:21Z", turn=8,
                        user="TEST 你测试下召回", assistant="TEST 测完一轮。")
    times = _stored_times(core)
    assert times["TEST 整理整个文件夹"] == ("2026-09-06T13:15:04Z", "2026-09-06T13:15:04Z")
    assert times["TEST 你测试下召回"] == ("2026-09-07T11:06:21Z", "2026-09-07T11:06:21Z")
    assert times["TEST 测完一轮。"] == ("2026-09-07T11:06:21Z", "2026-09-07T11:06:21Z")


def test_replayed_turn_keeps_its_first_witnessed_time(installed_core, initialize_kwargs):
    """The same message under the same key is a replay: one row, first time kept."""
    core, clock = installed_core
    _sync_after_restart(core, clock, initialize_kwargs, at="2026-09-06T13:15:04Z", turn=8,
                        user="TEST 整理整个文件夹", assistant="TEST 整理好了。")
    _sync_after_restart(core, clock, initialize_kwargs, at="2026-09-07T11:06:21Z", turn=8,
                        user="TEST 整理整个文件夹", assistant="TEST 整理好了。")
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM source_events").fetchone()[0] == 2
    times = _stored_times(core)
    assert times["TEST 整理整个文件夹"] == ("2026-09-06T13:15:04Z", "2026-09-06T13:15:04Z")
    assert times["TEST 整理好了。"] == ("2026-09-06T13:15:04Z", "2026-09-06T13:15:04Z")


def test_a_busy_store_met_by_a_session_s_capture_retry_says_pending(adapter, installed_core, monkeypatch):
    """A busy store met by a Hermes session's retry of its captures (at its end, before a compression) stops the
    replay with a receipt that says so, where it used to raise; the session's gap had come only from the raise
    (review of rc10)."""
    from contextlib import closing

    from scope_recall.adapters.hermes.identity import host_scope_payload
    from scope_recall.core.writer_lease import TruthWriterBusyError
    from tests.v11_support import source_event

    provider, clock = adapter
    core, _clock = installed_core
    identity = provider._require_identity()
    capture_inbox.enqueue(core.storage, clock, identity.trusted_context(mutation=True), source_event(
        source_event_key="TEST-start-busy", content="TEST 会话开始时忙。"), scope_id=identity.local_scope_id,
        host_scope=host_scope_payload(identity.scope))

    def busy(*args, **kwargs):
        raise TruthWriterBusyError()

    monkeypatch.setattr(capture_inbox, "_revalidated", busy)
    provider._diagnostics.pending_outcome_gaps = ()
    provider._retry_observed_captures()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [(None,)]
    assert capture_inbox.INGRESS_PENDING_GAP in provider._diagnostics.pending_outcome_gaps
