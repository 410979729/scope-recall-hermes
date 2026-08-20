"""R11 composite-scope heuristic candidate grouping.

Heuristic extraction must group by the stored (scope_id, session_id) pair, not
by a display label such as ``session:{id or 'unknown'}``. The same session
string in different physical scopes, and empty session versus literal
``unknown``, stay isolated candidate/source/checkpoint groups.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import scope_recall.journal as journal_module
import scope_recall.journal_candidates as journal_candidates
import scope_recall.journal_extractors as journal_extractors
from scope_recall.journal import append_journal_entry, run_journal_digest
from scope_recall.journal_llm import JournalDigestLLMError
from scope_recall.journal_store import JournalEntry, journal_entry_group_identity, load_session_digest_state
from scope_recall.scope import accessible_scope_ids, build_scope_id, build_shared_scope_id
from test_r110_final_integration import (
    _IDENTITY_OVERLAP,
    _append,
    _append_owned,
    _db,
    _home,
    _scope,
)


LOCAL_MARK = "LOCAL-WHEEL-MANIFEST-R11"
SHARED_MARK = "SHARED-TAILSCALE-FIREWALL-R11"
LEGACY_MARK = "LEGACY-CURSOR-RESTORE-R11"
EMPTY_MARK = "EMPTY-SESSION-BUCKET-R11"
UNKNOWN_MARK = "UNKNOWN-SESSION-BUCKET-R11"
SAME_MARK = "SAME-COMPOSITE-SESSION-R11"
FIFO_A_MARK = "FIFO-SCOPE-A-R11"
FIFO_B_MARK = "FIFO-SCOPE-B-R11"
TOOL_BODY = (
    "rollback verified: the covered prefix restore completed with a guardrail receipt."
)


def _journal_cfg(**overrides: Any) -> dict[str, Any]:
    payload = {
        "extractor": "heuristic",
        "allow_heuristic_fallback": False,
        "llm_max_attempts": 1,
        "llm_retry_delay": 0,
        "extraction_attempts_quarantine": 9,
        "retryable_failures_quarantine": 3,
    }
    payload.update(overrides)
    return payload


def _user(marker: str) -> str:
    return f"{marker} 这条记录必须留下可复用的 journal digest 工程结论。"


def _append_at(
    conn,
    scope,
    *,
    scope_id: str,
    session: str,
    turn: int,
    content: str,
    role: str = "user",
    shared_scope_id: str | None = None,
) -> int:
    return append_journal_entry(
        conn,
        scope=scope,
        scope_id=scope_id,
        shared_scope_id=shared_scope_id or build_shared_scope_id(scope),
        session_id=session,
        turn_number=turn,
        role=role,
        content=content,
    )


def _sources_by_memory(conn) -> dict[str, set[int]]:
    grouped: dict[str, set[int]] = {}
    for row in conn.execute(
        "SELECT memory_id, journal_entry_id FROM memory_journal_sources"
    ):
        grouped.setdefault(str(row["memory_id"]), set()).add(int(row["journal_entry_id"]))
    return grouped


def _memory_contents(conn) -> list[str]:
    return [str(row["content"]) for row in conn.execute("SELECT content FROM memories")]


def _leave_ids(result: dict[str, Any], key: str) -> set[int]:
    return {int(item) for item in (result.get("leave_states") or {}).get(key) or []}


def _insert_actions(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in (result.get("actions") or [])
        if item.get("action") == "insert"
    ]


def _assert_isolated_marker_memories(
    conn,
    *,
    groups: list[tuple[str, set[int]]],
) -> None:
    contents = _memory_contents(conn)
    by_memory = _sources_by_memory(conn)
    assert len(contents) == len(groups)
    assert len(by_memory) == len(groups)
    for marker, entry_ids in groups:
        matching = [body for body in contents if marker in body]
        assert len(matching) == 1, f"expected one memory for {marker}, got {matching}"
        others = [other for other, _ids in groups if other != marker]
        assert all(other not in matching[0] for other in others)
        assert any(entry_ids == source_ids for source_ids in by_memory.values())


def _timeout(*_args, **_kwargs) -> str:
    raise JournalDigestLLMError(
        "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
    )


def test_journal_candidates_does_not_import_digest_state():
    assert journal_candidates.__file__ is not None
    source = Path(journal_candidates.__file__).read_text(encoding="utf-8")
    assert "digest_state" not in source
    assert "from .journal import" not in source


def test_group_identity_keeps_empty_session_distinct_from_unknown():
    empty = JournalEntry(1, "scope-a", "shared-a", "", 1, "user", "empty", "2026-06-01T00:00:00+00:00")
    unknown = JournalEntry(
        2, "scope-a", "shared-a", "unknown", 1, "user", "unknown", "2026-06-01T00:00:01+00:00"
    )
    other = JournalEntry(3, "scope-b", "shared-b", "", 1, "user", "other", "2026-06-01T00:00:02+00:00")
    assert journal_entry_group_identity(empty) == ("scope-a", "")
    assert journal_entry_group_identity(unknown) == ("scope-a", "unknown")
    assert journal_entry_group_identity(other) == ("scope-b", "")
    assert journal_entry_group_identity(empty) != journal_entry_group_identity(unknown)
    assert journal_entry_group_identity(empty) != journal_entry_group_identity(other)


def test_heuristic_candidates_do_not_merge_same_session_across_scopes():
    entries = [
        JournalEntry(
            1, "scope-a", "shared-a", "shared-session", 1, "user", _user(LOCAL_MARK),
            "2026-06-01T00:00:01+00:00",
        ),
        JournalEntry(
            2, "scope-b", "shared-b", "shared-session", 1, "user", _user(SHARED_MARK),
            "2026-06-01T00:00:02+00:00",
        ),
    ]
    candidates = journal_candidates.heuristic_journal_candidates(entries)
    assert len(candidates) == 2
    assert [candidate.entry_ids for candidate in candidates] == [[1], [2]]
    assert LOCAL_MARK in candidates[0].content
    assert SHARED_MARK not in candidates[0].content
    assert SHARED_MARK in candidates[1].content
    assert LOCAL_MARK not in candidates[1].content
    assert all(candidate.covered_tool_ids is None for candidate in candidates)


def test_heuristic_candidates_keep_empty_and_unknown_sessions_apart():
    entries = [
        JournalEntry(1, "scope-a", "shared-a", "", 1, "user", _user(EMPTY_MARK), "2026-06-01T00:00:01+00:00"),
        JournalEntry(
            2, "scope-a", "shared-a", "unknown", 1, "user", _user(UNKNOWN_MARK),
            "2026-06-01T00:00:02+00:00",
        ),
        JournalEntry(3, "scope-b", "shared-b", "", 1, "user", _user(SHARED_MARK), "2026-06-01T00:00:03+00:00"),
        JournalEntry(
            4, "scope-b", "shared-b", "unknown", 1, "user", _user(LOCAL_MARK),
            "2026-06-01T00:00:04+00:00",
        ),
    ]
    candidates = journal_candidates.heuristic_journal_candidates(entries)
    assert [candidate.entry_ids for candidate in candidates] == [[1], [2], [3], [4]]
    assert EMPTY_MARK in candidates[0].content and UNKNOWN_MARK not in candidates[0].content
    assert UNKNOWN_MARK in candidates[1].content and EMPTY_MARK not in candidates[1].content


def test_heuristic_candidates_keep_same_composite_session_grouped_in_source_order():
    entries = [
        JournalEntry(
            1, "scope-a", "shared-a", "same-session", 1, "user",
            f"{SAME_MARK}-ONE scope-recall journal-first digest merge/upsert workflow。",
            "2026-06-01T00:00:01+00:00",
        ),
        JournalEntry(
            2, "scope-a", "shared-a", "same-session", 2, "assistant",
            f"{SAME_MARK}-TWO 已确定 journal-first digest 和 merge/upsert。",
            "2026-06-01T00:00:02+00:00",
        ),
        JournalEntry(
            3, "scope-a", "shared-a", "same-session", 3, "user",
            f"{SAME_MARK}-THREE 同一个 journal-first digest 任务还要保留 merge/upsert。",
            "2026-06-01T00:00:03+00:00",
        ),
        JournalEntry(
            4, "scope-a", "shared-a", "same-session", 4, "assistant",
            f"{SAME_MARK}-FOUR 同一 journal-first digest 主题会更新 merge/upsert 记忆。",
            "2026-06-01T00:00:04+00:00",
        ),
    ]
    candidates = journal_candidates.heuristic_journal_candidates(entries)
    assert len(candidates) == 1
    assert candidates[0].entry_ids == [1, 2, 3, 4]
    assert candidates[0].session_ids == ["same-session"]


def test_explicit_scope_heuristic_isolates_local_and_shared_same_session(tmp_path):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    local_id = build_scope_id(scope)
    shared_id = build_shared_scope_id(scope)
    session = "shared-session"
    local_row = _append_at(
        conn, scope, scope_id=local_id, session=session, turn=1, content=_user(LOCAL_MARK)
    )
    shared_row = _append_at(
        conn, scope, scope_id=shared_id, session=session, turn=1, content=_user(SHARED_MARK)
    )
    conn.close()

    result = run_journal_digest(
        hermes_home=hermes_home,
        extractor="heuristic",
        scope=scope,
        interval_label="r11-heuristic-scope",
        limit_entries=20,
    )
    verify = _db(hermes_home)
    assert result["ok"] is True
    assert result.get("extractor_used") == "heuristic"
    assert int(result.get("inserted") or 0) == 2
    _assert_isolated_marker_memories(
        verify,
        groups=[(LOCAL_MARK, {local_row}), (SHARED_MARK, {shared_row})],
    )
    actions = _insert_actions(result)
    assert {tuple(item.get("entry_ids") or []) for item in actions} == {
        (local_row,),
        (shared_row,),
    }
    leave_processed = _leave_ids(result, "processed_ids")
    assert leave_processed == {local_row, shared_row}
    assert load_session_digest_state(verify, scope_id=local_id, session_id=session) is not None
    assert load_session_digest_state(verify, scope_id=shared_id, session_id=session) is not None
    verify.close()


def test_heuristic_fallback_after_timeout_keeps_composite_isolation(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(
        tmp_path,
        _journal_cfg(extractor="llm", allow_heuristic_fallback=True),
    )
    scope = _scope()
    local_id = build_scope_id(scope)
    shared_id = build_shared_scope_id(scope)
    session = "shared-session"
    local_row = _append_at(
        conn, scope, scope_id=local_id, session=session, turn=1, content=_user(LOCAL_MARK)
    )
    shared_row = _append_at(
        conn, scope, scope_id=shared_id, session=session, turn=1, content=_user(SHARED_MARK)
    )
    conn.close()
    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", _timeout)

    result = run_journal_digest(
        hermes_home=hermes_home,
        scope=scope,
        interval_label="r11-fallback",
        limit_entries=20,
    )
    verify = _db(hermes_home)
    assert result["ok"] is True
    assert result.get("extractor_used") == "heuristic-fallback"
    assert int(result.get("inserted") or 0) == 2
    _assert_isolated_marker_memories(
        verify,
        groups=[(LOCAL_MARK, {local_row}), (SHARED_MARK, {shared_row})],
    )
    verify.close()


def test_empty_and_unknown_sessions_stay_isolated_across_and_within_scopes(tmp_path):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    local_id = build_scope_id(scope)
    shared_id = build_shared_scope_id(scope)
    empty_local = _append_at(
        conn,
        scope,
        scope_id=local_id,
        session="",
        turn=1,
        content=f"{EMPTY_MARK} local empty session 必须单独保留 journal-first digest 结论。",
    )
    unknown_local = _append_at(
        conn,
        scope,
        scope_id=local_id,
        session="unknown",
        turn=1,
        content=f"{UNKNOWN_MARK} local literal unknown session 必须单独保留 Tailscale firewall 排障。",
    )
    empty_shared = _append_at(
        conn,
        scope,
        scope_id=shared_id,
        session="",
        turn=1,
        content=f"{SHARED_MARK} shared empty session 必须单独保留 release gate wheel manifest。",
    )
    unknown_shared = _append_at(
        conn,
        scope,
        scope_id=shared_id,
        session="unknown",
        turn=1,
        content=f"{LOCAL_MARK} shared literal unknown session 必须单独保留 cursor restore 回滚。",
    )
    conn.close()

    result = run_journal_digest(
        hermes_home=hermes_home,
        extractor="heuristic",
        scope=scope,
        interval_label="r11-empty-unknown",
        limit_entries=20,
    )
    verify = _db(hermes_home)
    assert int(result.get("inserted") or 0) == 4
    _assert_isolated_marker_memories(
        verify,
        groups=[
            (EMPTY_MARK, {empty_local}),
            (UNKNOWN_MARK, {unknown_local}),
            (SHARED_MARK, {empty_shared}),
            (LOCAL_MARK, {unknown_shared}),
        ],
    )
    assert load_session_digest_state(verify, scope_id=local_id, session_id="") is not None
    assert load_session_digest_state(verify, scope_id=local_id, session_id="unknown") is not None
    assert load_session_digest_state(verify, scope_id=shared_id, session_id="") is not None
    verify.close()


def test_local_shared_legacy_aliases_are_three_heuristic_groups(tmp_path):
    hermes_home, conn = _home(tmp_path, _journal_cfg(), extra={"identity": _IDENTITY_OVERLAP})
    owner = _scope(user_id="r3-test-user-a")
    local_id = build_scope_id(owner, {"identity": _IDENTITY_OVERLAP})
    shared_id = build_shared_scope_id(owner, {"identity": _IDENTITY_OVERLAP})
    legacy_id = build_shared_scope_id(owner)
    readable = set(accessible_scope_ids(owner, {"identity": _IDENTITY_OVERLAP}))
    assert {local_id, shared_id, legacy_id} <= readable
    session = "alias-session"
    local_row = _append_owned(
        conn,
        owner,
        scope_id=local_id,
        shared_scope_id=shared_id,
        session=session,
        turn=1,
        content=_user(LOCAL_MARK),
    )
    shared_row = _append_owned(
        conn,
        owner,
        scope_id=shared_id,
        shared_scope_id=shared_id,
        session=session,
        turn=2,
        content=_user(SHARED_MARK),
    )
    legacy_row = _append_owned(
        conn,
        owner,
        scope_id=legacy_id,
        shared_scope_id=shared_id,
        session=session,
        turn=3,
        content=_user(LEGACY_MARK),
    )
    conn.close()

    result = run_journal_digest(
        hermes_home=hermes_home,
        extractor="heuristic",
        scope=owner,
        interval_label="r11-aliases",
        limit_entries=20,
    )
    verify = _db(hermes_home)
    assert int(result.get("inserted") or 0) == 3
    _assert_isolated_marker_memories(
        verify,
        groups=[
            (LOCAL_MARK, {local_row}),
            (SHARED_MARK, {shared_row}),
            (LEGACY_MARK, {legacy_row}),
        ],
    )
    assert _leave_ids(result, "processed_ids") == {local_row, shared_row, legacy_row}
    for scope_id in (local_id, shared_id, legacy_id):
        assert load_session_digest_state(verify, scope_id=scope_id, session_id=session) is not None
    verify.close()


def test_same_physical_scope_same_session_stays_one_heuristic_candidate(tmp_path):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    first = _append(
        conn,
        scope,
        session="same-session",
        turn=1,
        content=f"{SAME_MARK}-ONE scope-recall journal-first digest workflow。",
    )
    second = _append(
        conn,
        scope,
        session="same-session",
        turn=2,
        role="assistant",
        content=f"{SAME_MARK}-TWO 已确定 journal-first 和后台 digest。",
    )
    conn.close()

    result = run_journal_digest(
        hermes_home=hermes_home,
        extractor="heuristic",
        scope=scope,
        interval_label="r11-same-composite",
        limit_entries=20,
    )
    verify = _db(hermes_home)
    assert int(result.get("inserted") or 0) == 1
    by_memory = _sources_by_memory(verify)
    assert list(by_memory.values()) == [{first, second}]
    verify.close()


def test_fifo_scope_none_keeps_one_physical_scope_per_batch(tmp_path):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope_a = _scope(user_id="r3-test-user-a")
    scope_b = _scope(user_id="r3-test-user-b")
    session = "fifo-session"
    first = _append(conn, scope_a, session=session, turn=1, content=_user(FIFO_A_MARK))
    second = _append(conn, scope_b, session=session, turn=1, content=_user(FIFO_B_MARK))
    conn.close()

    result = run_journal_digest(
        hermes_home=hermes_home,
        extractor="heuristic",
        scope=None,
        interval_label="r11-fifo",
        limit_entries=50,
    )
    verify = _db(hermes_home)
    assert int(result.get("inserted") or 0) == 2
    _assert_isolated_marker_memories(
        verify,
        groups=[(FIFO_A_MARK, {first}), (FIFO_B_MARK, {second})],
    )
    contents = _memory_contents(verify)
    assert contents[0].find(FIFO_A_MARK) >= 0
    assert contents[1].find(FIFO_B_MARK) >= 0
    verify.close()


def test_heuristic_does_not_invent_tool_provenance_or_cross_scope_tools(tmp_path):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    local_id = build_scope_id(scope)
    shared_id = build_shared_scope_id(scope)
    session = "shared-session"
    local_user = _append_at(
        conn, scope, scope_id=local_id, session=session, turn=1, content=_user(LOCAL_MARK)
    )
    shared_tool = _append_at(
        conn,
        scope,
        scope_id=shared_id,
        session=session,
        turn=1,
        role="tool",
        content=TOOL_BODY,
    )
    shared_user = _append_at(
        conn, scope, scope_id=shared_id, session=session, turn=2, content=_user(SHARED_MARK)
    )
    conn.close()

    result = run_journal_digest(
        hermes_home=hermes_home,
        extractor="heuristic",
        scope=scope,
        interval_label="r11-tools",
        limit_entries=20,
    )
    verify = _db(hermes_home)
    sources = {
        int(row["journal_entry_id"])
        for row in verify.execute("SELECT journal_entry_id FROM memory_journal_sources")
    }
    assert local_user in sources
    assert shared_user in sources
    assert shared_tool not in sources
    by_memory = _sources_by_memory(verify)
    assert {local_user} in by_memory.values()
    assert {shared_user} in by_memory.values()
    assert all(shared_tool not in ids for ids in by_memory.values())
    collected, extractor_used, _error, _counts = journal_module._collect_journal_candidates(
        verify,
        entries=[
            JournalEntry(
                local_user, local_id, shared_id, session, 1, "user", _user(LOCAL_MARK),
                "2026-06-01T00:00:01+00:00",
            ),
            JournalEntry(
                shared_tool, shared_id, shared_id, session, 1, "tool", TOOL_BODY,
                "2026-06-01T00:00:02+00:00",
            ),
            JournalEntry(
                shared_user, shared_id, shared_id, session, 2, "user", _user(SHARED_MARK),
                "2026-06-01T00:00:03+00:00",
            ),
        ],
        hermes_home=hermes_home,
        scope=scope,
        journal_config=_journal_cfg(),
        requested_extractor="heuristic",
    )
    assert extractor_used == "heuristic"
    assert all(getattr(candidate, "covered_tool_ids", None) is None for candidate in collected)
    verify.close()
    assert result["ok"] is True


def test_collect_heuristic_and_fallback_use_the_same_helper(tmp_path, monkeypatch):
    seen: list[int] = []
    real = journal_candidates.heuristic_journal_candidates

    def wrapped(entries):
        seen.append(id(real))
        return real(entries)

    monkeypatch.setattr(journal_module, "heuristic_journal_candidates", wrapped)
    conn = __import__("sqlite3").connect(":memory:")
    try:
        entries = [
            JournalEntry(1, "scope-a", "shared-a", "s", 1, "user", _user(LOCAL_MARK), "2026-06-01T00:00:01+00:00"),
            JournalEntry(2, "scope-b", "shared-b", "s", 1, "user", _user(SHARED_MARK), "2026-06-01T00:00:02+00:00"),
        ]
        first = journal_module._collect_journal_candidates(
            conn,
            entries=entries,
            hermes_home=tmp_path,
            scope=_scope(),
            journal_config=_journal_cfg(),
            requested_extractor="heuristic",
        )
        monkeypatch.setattr(
            journal_module,
            "llm_journal_candidates",
            lambda *_a, **_k: (_ for _ in ()).throw(
                JournalDigestLLMError("timeout", attempts=1, error_kind="timeout", retryable=True)
            ),
        )
        second = journal_module._collect_journal_candidates(
            conn,
            entries=entries,
            hermes_home=tmp_path,
            scope=_scope(),
            journal_config=_journal_cfg(extractor="llm", allow_heuristic_fallback=True),
            requested_extractor="llm",
        )
    finally:
        conn.close()
    assert first[1] == "heuristic"
    assert second[1] == "heuristic-fallback"
    assert seen == [id(real), id(real)]
    assert [candidate.entry_ids for candidate in first[0]] == [[1], [2]]
    assert [candidate.entry_ids for candidate in second[0]] == [[1], [2]]
