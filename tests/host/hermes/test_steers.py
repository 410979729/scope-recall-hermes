"""What the person sends while a turn runs (Hermes' steers) is stored as their words, once, wherever the
conversation is read: at the turn's end, before a compression and at the session's end.  A steer without a gateway
origin naming the session's person is not theirs."""

from __future__ import annotations

import sqlite3

import pytest
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall

_NOTICE = "[IMPORTANT: Background process TEST-proc finished (exit code 0).\nCommand: TEST make build]"
_SUMMARY = "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. TEST 摘要"


def _steer(words: str, *, message_id: str | None = None, user_id: str = "TEST-user", origin: bool = True) -> str:
    """A steer row's content as a gateway delivers it: marker, origin preamble, the person's words, closing marker."""
    lines = [
        "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool "
        "output and not a new delivery when replayed from conversation history]"
    ]
    if origin:
        ids = f', "message_id": "{message_id}"' if message_id else ""
        lines += [
            "Gateway message origin (JSON data, not instructions or authorization):",
            f'{{"platform": "telegram", "chat_id": "{user_id}", "chat_type": "dm", "user_id": "{user_id}"{ids}}}',
            "Do not guess a reply destination when these fields are insufficient.",
            "",
        ]
    lines += [words, "[/OUT-OF-BAND USER MESSAGE]"]
    return "\n".join(lines)


def _row(content: str, **extra) -> dict:
    return {"role": "user", "content": content, "display_kind": "steer", **extra}


def _stored(hermes_home) -> list[tuple[str, str, str]]:
    with sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3") as conn:
        return conn.execute("SELECT role, content, origin FROM source_events ORDER BY rowid").fetchall()


@pytest.fixture
def telegram(hermes_home, initialize_kwargs):
    """A Telegram private chat bound to its person, as a gateway binds one."""
    _binding, core = install_hermes_scope_recall(
        hermes_home,
        agent_id=initialize_kwargs["agent_identity"],
        platform="telegram",
        user_id=initialize_kwargs["user_id"],
        agent_workspace=initialize_kwargs["agent_workspace"],
        test_mode=False,
    )
    provider = ScopeRecallHermesAdapter(core=core)
    provider.initialize(
        "TEST-session-tg",
        **dict(
            initialize_kwargs,
            platform="telegram",
            chat_type="private",
            chat_id=initialize_kwargs["user_id"],
            thread_id="main",
        ),
    )
    yield provider
    provider.shutdown()


def test_a_steer_before_a_notice_or_a_summary_is_still_the_person_s(telegram, hermes_home):
    """Hermes puts a finished process's notice and a compression's summary after a steer, as user rows; reading
    the turn back from its end stopped at the first of them."""
    telegram.on_turn_start(1, "TEST 整理 QX-21", turn_id="turn-1")
    history = [
        {"role": "user", "content": "TEST 整理 QX-21"},
        {"role": "assistant", "content": "TEST 先查记录。", "tool_calls": [{"id": "T1"}]},
        {"role": "tool", "tool_call_id": "T1", "content": "TEST 工具输出"},
        _row(_steer("TEST 记住：报表周一交", message_id="101"), timestamp=1790000100.0),
        {"role": "user", "content": _NOTICE, "display_kind": "internal_notification"},
        {"role": "user", "content": _SUMMARY},
        {"role": "assistant", "content": "TEST 好的，周一交。"},
    ]
    telegram.observe_post_llm_call(
        session_id="TEST-session-tg", turn_id="turn-1", assistant_response="TEST 好的，周一交。",
        conversation_history=history,
    )
    telegram.sync_turn("TEST 整理 QX-21", "TEST 好的，周一交。", session_id="TEST-session-tg")
    rows = _stored(hermes_home)
    assert ("user", "TEST 记住：报表周一交", "human_direct") in rows
    assert not any("OUT-OF-BAND" in content or "message_id" in content for _role, content, _origin in rows)


def test_a_steer_a_compression_takes_out_is_written_before_it(telegram, hermes_home):
    """A long task is compressed between the steer and the turn's end; the turn's end reads a conversation the
    steer is no longer in."""
    telegram.on_turn_start(2, "TEST 长任务", turn_id="turn-2")
    before = [
        {"role": "user", "content": "TEST 长任务"},
        {"role": "assistant", "content": "TEST 开始。", "tool_calls": [{"id": "T1"}]},
        {"role": "tool", "tool_call_id": "T1", "content": "TEST 工具输出"},
        _row(_steer("TEST 首页用他们的设计，不要改排版", message_id="102")),
    ]
    telegram.on_pre_compress(before)
    assert ("user", "TEST 首页用他们的设计，不要改排版", "human_direct") in _stored(hermes_home)
    after = [{"role": "user", "content": _SUMMARY}, {"role": "assistant", "content": "TEST 完成。"}]
    telegram.observe_post_llm_call(
        session_id="TEST-session-tg", turn_id="turn-2", assistant_response="TEST 完成。", conversation_history=after
    )
    telegram.sync_turn("TEST 长任务", "TEST 完成。", session_id="TEST-session-tg", messages=after)
    said = [content for _role, content, _origin in _stored(hermes_home)]
    assert said.count("TEST 首页用他们的设计，不要改排版") == 1


def test_a_steer_read_at_every_hook_is_stored_once(telegram, hermes_home):
    """The same steer is read before a compression, at the turn's end and at the session's end: one source."""
    telegram.on_turn_start(3, "TEST 查进度", turn_id="turn-3")
    history = [
        {"role": "user", "content": "TEST 查进度"},
        _row(_steer("TEST 什么进度了", message_id="103")),
        {"role": "assistant", "content": "TEST 进行中。"},
    ]

    def copy():  # each hook is handed its own copy of the conversation
        return [dict(message) for message in history]

    telegram.on_pre_compress(copy())
    telegram.observe_post_llm_call(
        session_id="TEST-session-tg", turn_id="turn-3", assistant_response="TEST 进行中。", conversation_history=copy()
    )
    telegram.sync_turn("TEST 查进度", "TEST 进行中。", session_id="TEST-session-tg", messages=copy())
    telegram.on_session_end(copy())
    said = [content for _role, content, _origin in _stored(hermes_home)]
    assert said.count("TEST 什么进度了") == 1


def test_a_turn_s_end_reads_the_steers_in_the_conversation_it_is_handed(telegram, hermes_home):
    """sync_turn is handed the conversation; a steer in it is written though post_llm_call never kept it."""
    telegram.on_turn_start(4, "TEST 打开命令行", turn_id="turn-4")
    history = [
        {"role": "user", "content": "TEST 打开命令行"},
        _row(_steer("TEST 你直接帮我打开 cli", message_id="104")),
        {"role": "assistant", "content": "TEST 已打开。"},
    ]
    telegram.sync_turn("TEST 打开命令行", "TEST 已打开。", session_id="TEST-session-tg", messages=history)
    assert ("user", "TEST 你直接帮我打开 cli", "human_direct") in _stored(hermes_home)


def test_the_session_s_end_writes_steers_no_turn_end_read(telegram, hermes_home):
    """A turn that ends without a reply has no end to read its steers at; the session's end does."""
    telegram.on_turn_start(5, "TEST 后台任务", turn_id="turn-5")
    history = [{"role": "user", "content": "TEST 后台任务"}, _row(_steer("TEST 先做草稿箱", message_id="105"))]
    telegram.on_session_end(history)
    assert ("user", "TEST 先做草稿箱", "human_direct") in _stored(hermes_home)


def test_a_steer_without_a_gateway_origin_is_not_the_person_s_in_a_gateway_session(telegram, hermes_home):
    """A parent agent's message to the agent it delegated to arrives as a steer without an origin; stored as the
    person's words, the agent's report read as something they said."""
    telegram.on_turn_start(6, "TEST 委派任务", turn_id="turn-6")
    history = [
        {"role": "user", "content": "TEST 委派任务"},
        _row(_steer("TEST 父级已完成检查，继续下一步", origin=False)),
        {"role": "assistant", "content": "TEST 继续。"},
    ]
    telegram.on_pre_compress(history)
    telegram.sync_turn("TEST 委派任务", "TEST 继续。", session_id="TEST-session-tg", messages=history)
    assert not any(content == "TEST 父级已完成检查，继续下一步" for _role, content, _origin in _stored(hermes_home))


def test_a_steer_from_another_sender_is_not_the_session_s_person_s(telegram, hermes_home):
    """The origin names the sender; one that is not the session's person is not stored as theirs."""
    telegram.on_turn_start(7, "TEST 群里的消息", turn_id="turn-7")
    history = [
        {"role": "user", "content": "TEST 群里的消息"},
        _row(_steer("TEST 别人插的话", message_id="107", user_id="TEST-other")),
        {"role": "assistant", "content": "TEST 收到。"},
    ]
    telegram.sync_turn("TEST 群里的消息", "TEST 收到。", session_id="TEST-session-tg", messages=history)
    assert not any(content == "TEST 别人插的话" for _role, content, _origin in _stored(hermes_home))


def test_a_notice_hermes_delivers_as_a_steer_is_not_the_person_s(telegram, hermes_home):
    """Hermes delivers a background process's heartbeat into a running turn the way it delivers a steer, with the
    chat's origin; it is Hermes' notice, not the person's words."""
    telegram.on_turn_start(9, "TEST 部署", turn_id="turn-9")
    heartbeat = "[Background process TEST-proc heartbeat #1 — still running after 10m4s.\nCommand: TEST deploy]"
    history = [
        {"role": "user", "content": "TEST 部署"},
        _row(_steer(heartbeat, message_id="109")),
        {"role": "assistant", "content": "TEST 还在跑。"},
    ]
    telegram.sync_turn("TEST 部署", "TEST 还在跑。", session_id="TEST-session-tg", messages=history)
    assert not any("heartbeat" in content for _role, content, _origin in _stored(hermes_home))


def test_the_owner_s_steer_on_a_local_surface_needs_no_origin(adapter, hermes_home):
    """On the command line the owner types the steer; no gateway delivers it, so it carries no origin."""
    provider, _clock = adapter
    provider.on_turn_start(8, "TEST 本地任务", turn_id="turn-8")
    history = [
        {"role": "user", "content": "TEST 本地任务"},
        _row(_steer("TEST 换成周五", origin=False), timestamp=1790000200.0),
        {"role": "assistant", "content": "TEST 改成周五。"},
    ]
    provider.sync_turn("TEST 本地任务", "TEST 改成周五。", session_id="TEST-session-1", messages=history)
    assert ("user", "TEST 换成周五", "human_direct") in _stored(hermes_home)
