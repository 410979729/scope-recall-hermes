"""R10 exact composite-session tool provenance.

A global numeric ``min(attempted)..max(attempted)`` span is not coverage.
A candidate may receive a tool source only when that tool belonged to the
same actually covered chunk and the same stored (scope_id, session_id) pair
as the candidate's attempted source rows.
"""

from __future__ import annotations

import json
from typing import Any

import scope_recall.journal_extractors as journal_extractors
from scope_recall.journal import append_journal_entry, run_journal_digest
from scope_recall.journal_extractors import JournalCandidateList
from scope_recall.journal_llm import JournalDigestLLMError
from scope_recall.journal_store import JournalEntry
from scope_recall.nightly_digest import SessionChunk
from scope_recall.scope import build_scope_id, build_shared_scope_id
from test_r110_final_integration import _append, _db, _home, _scope


SECRET_KEY = "sk-" + "R" * 24
TOOL_BODY = (
    "rollback verified: the covered prefix restore completed with a guardrail receipt."
)
SUFFIX_TOOL_BODY = (
    "verified rollback of the suffix restore guardrail is documented for later replay."
)


def _journal_cfg(**overrides: Any) -> dict[str, Any]:
    payload = {
        "extractor": "llm",
        "allow_heuristic_fallback": False,
        "llm_max_attempts": 1,
        "llm_retry_delay": 0,
        "extraction_attempts_quarantine": 9,
        "retryable_failures_quarantine": 3,
    }
    payload.update(overrides)
    return payload


def _force_chunks(groups: list[tuple[int, ...]]):
    def fake_session_chunks(bundle, **_kwargs):
        present = {int(message.id) for message in getattr(bundle, "messages", ())}
        return [
            SessionChunk(
                text=f"chunk ids={'/'.join(str(item) for item in group)}",
                message_ids=group,
                input_chars=80,
                exposed_chars=80,
                truncated=False,
            )
            for group in groups
            if present.issuperset(int(item) for item in group)
        ]

    return fake_session_chunks


_PAYLOAD_FACTS = {
    "session-a": (
        "Scope Recall release wheel manifest must keep verified rollback guardrail evidence.",
        ["scope-recall", "wheel"],
    ),
    "session-b": (
        "Tailscale firewall remote recovery must keep verified rollback guardrail evidence.",
        ["tailscale", "firewall"],
    ),
    "local-scope": (
        "Local scratch journal cursor must keep verified rollback guardrail evidence.",
        ["journal", "cursor"],
    ),
    "same-composite": (
        "Composite session mid-span tool restore must keep verified rollback guardrail evidence.",
        ["journal", "restore"],
    ),
    "prefix-only": (
        "Prefix-only extraction window must keep verified rollback guardrail evidence.",
        ["journal", "prefix"],
    ),
    "prefix": (
        "Successful prefix chunk must keep verified rollback guardrail evidence.",
        ["journal", "prefix"],
    ),
}


def _insert_payload(entry_ids: list[int], label: str) -> str:
    content, entities = _PAYLOAD_FACTS.get(
        label,
        (
            f"Distinct durable {label} procedure must keep verified rollback guardrail evidence.",
            ["scope-recall"],
        ),
    )
    return json.dumps(
        [
            {
                "action": "insert",
                "evidence_message_ids": list(entry_ids),
                "content": content,
                "target": "memory",
                "memory_type": "procedure",
                "importance": 0.9,
                "confidence": 0.86,
                "entities": entities,
                "tags": ["r10-tool-provenance", label],
                "reason": "cited the attempted chunk only.",
            }
        ]
    )


def _safe_prompt(_bundle, chunk, *_args, **_kwargs) -> str:
    return "journal-digest-prompt ids=" + ",".join(str(item) for item in chunk.message_ids)


def _append_at(
    conn,
    scope,
    *,
    scope_id: str,
    session: str,
    turn: int,
    content: str,
    role: str = "user",
) -> int:
    return append_journal_entry(
        conn,
        scope=scope,
        scope_id=scope_id,
        shared_scope_id=build_shared_scope_id(scope),
        session_id=session,
        turn_number=turn,
        role=role,
        content=content,
    )


def _user(label: str) -> str:
    return f"{label} 这条记录必须留下可复用的 journal digest 工程结论。"


def _install_digest_stubs(monkeypatch, groups: list[tuple[int, ...]]) -> None:
    monkeypatch.setattr(journal_extractors, "session_chunks", _force_chunks(groups))
    monkeypatch.setattr(journal_extractors, "build_prompt", _safe_prompt)


def _sources(conn) -> set[int]:
    return {
        int(row["journal_entry_id"])
        for row in conn.execute("SELECT journal_entry_id FROM memory_journal_sources")
    }


def _entry_row(conn, entry_id: int):
    return conn.execute(
        "SELECT processed_run_id, deferred_run_id, extraction_attempts, "
        "retryable_failures FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()


def _leave_ids(result: dict[str, Any], key: str) -> set[int]:
    return {int(item) for item in (result.get("leave_states") or {}).get(key) or []}


def _assert_no_sensitive_leak(result: dict[str, Any]) -> None:
    payload = json.dumps(result, ensure_ascii=False)
    assert SECRET_KEY not in payload
    assert "sk-" not in payload


def test_bundles_do_not_merge_same_session_string_across_scopes():
    entries = [
        JournalEntry(
            1,
            "scope-a",
            "shared-a",
            "shared-session",
            1,
            "user",
            _user("SCOPE-A"),
            "2026-06-01T00:00:01+00:00",
        ),
        JournalEntry(
            2,
            "scope-b",
            "shared-b",
            "shared-session",
            1,
            "user",
            _user("SCOPE-B"),
            "2026-06-01T00:00:02+00:00",
        ),
    ]
    bundles = journal_extractors._journal_session_bundles(entries)
    grouped = {frozenset(int(message.id) for message in bundle.messages) for bundle in bundles}
    assert grouped == {frozenset([1]), frozenset([2])}


def test_interleaved_foreign_session_tool_stays_off_candidate_and_pending(
    tmp_path, monkeypatch
):
    """RED 1: A 1/3 + B tool 2 + B user 4 timeout. Tool 2 stays with B."""

    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    a_user_1 = _append(conn, scope, session="session-a", turn=1, content=_user("A-1"))
    b_tool_2 = _append(
        conn, scope, session="session-b", turn=1, role="tool", content=TOOL_BODY
    )
    a_user_3 = _append(conn, scope, session="session-a", turn=2, content=_user("A-3"))
    b_user_4 = _append(conn, scope, session="session-b", turn=2, content=_user("B-4"))
    conn.close()
    assert [a_user_1, b_tool_2, a_user_3, b_user_4] == [a_user_1, a_user_1 + 1, a_user_1 + 2, a_user_1 + 3]

    _install_digest_stubs(monkeypatch, [(a_user_1, a_user_3), (b_user_4,)])

    def scripted_llm(prompt: str, **_kwargs) -> str:
        if f"ids={a_user_1},{a_user_3}" in prompt:
            return _insert_payload([a_user_1, a_user_3], "session-a")
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r10-cross-session", limit_entries=20
    )
    verify = _db(hermes_home)
    sources = _sources(verify)
    tool_row = _entry_row(verify, b_tool_2)
    user_b = _entry_row(verify, b_user_4)
    leave_processed = _leave_ids(result, "processed_ids")
    leave_pending = _leave_ids(result, "retryable_pending_ids")
    serialized = json.dumps(result, ensure_ascii=False)
    assert int(result.get("inserted") or 0) == 1
    assert a_user_1 in sources
    assert a_user_3 in sources
    assert b_tool_2 not in sources
    assert b_user_4 not in sources
    assert not str(tool_row["processed_run_id"] or "")
    assert not str(tool_row["deferred_run_id"] or "")
    assert int(tool_row["extraction_attempts"] or 0) == 0
    assert int(tool_row["retryable_failures"] or 0) == 0
    assert int(user_b["retryable_failures"] or 0) == 1
    assert not str(user_b["processed_run_id"] or "")
    assert b_tool_2 not in leave_processed
    assert b_tool_2 in leave_pending
    assert b_user_4 in leave_pending
    assert "sk-" not in serialized
    verify.close()


def test_same_session_string_different_scope_does_not_cross_link(tmp_path, monkeypatch):
    """RED 2: identical session_id in another stored scope_id never attaches."""

    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    local_id = build_scope_id(scope)
    shared_id = build_shared_scope_id(scope)
    assert local_id != shared_id
    session = "shared-session"
    local_1 = _append_at(
        conn, scope, scope_id=local_id, session=session, turn=1, content=_user("LOCAL-1")
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
    local_3 = _append_at(
        conn, scope, scope_id=local_id, session=session, turn=2, content=_user("LOCAL-3")
    )
    shared_user = _append_at(
        conn, scope, scope_id=shared_id, session=session, turn=2, content=_user("SHARED-4")
    )
    conn.close()

    _install_digest_stubs(monkeypatch, [(local_1, local_3), (shared_user,)])

    def scripted_llm(prompt: str, **_kwargs) -> str:
        if f"ids={local_1},{local_3}" in prompt:
            return _insert_payload([local_1, local_3], "local-scope")
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r10-cross-scope", limit_entries=20
    )
    verify = _db(hermes_home)
    sources = _sources(verify)
    tool_row = _entry_row(verify, shared_tool)
    assert int(result.get("inserted") or 0) == 1
    assert local_1 in sources and local_3 in sources
    assert shared_tool not in sources
    assert not str(tool_row["processed_run_id"] or "")
    assert int(tool_row["retryable_failures"] or 0) == 0
    assert shared_tool in _leave_ids(result, "retryable_pending_ids")
    _assert_no_sensitive_leak(result)
    verify.close()


def test_same_scope_different_session_does_not_cross_link(tmp_path, monkeypatch):
    """RED 3: same owner scope, two sessions, no cross-link."""

    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    a1 = _append(conn, scope, session="session-a", turn=1, content=_user("A-1"))
    b_tool = _append(conn, scope, session="session-b", turn=1, role="tool", content=TOOL_BODY)
    a3 = _append(conn, scope, session="session-a", turn=2, content=_user("A-3"))
    b_user = _append(conn, scope, session="session-b", turn=2, content=_user("B-4"))
    conn.close()

    _install_digest_stubs(monkeypatch, [(a1, a3), (b_user,)])

    def scripted_llm(prompt: str, **_kwargs) -> str:
        if f"ids={a1},{a3}" in prompt:
            return _insert_payload([a1, a3], "session-a")
        return _insert_payload([b_user], "session-b")

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r10-same-scope", limit_entries=20
    )
    verify = _db(hermes_home)
    sources = _sources(verify)
    assert int(result.get("inserted") or 0) == 2
    assert a1 in sources and a3 in sources
    assert b_user in sources
    assert b_tool not in sources
    assert not str(_entry_row(verify, b_tool)["processed_run_id"] or "")
    verify.close()


def test_same_composite_session_midspan_tool_attaches_once(tmp_path, monkeypatch):
    """RED 4: user 1 / tool 2 / user 3 in one covered chunk attaches once."""

    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    session = "same-composite"
    user_1 = _append(conn, scope, session=session, turn=1, content=_user("COVERED-1"))
    tool_2 = _append(conn, scope, session=session, turn=2, role="tool", content=TOOL_BODY)
    user_3 = _append(conn, scope, session=session, turn=3, content=_user("COVERED-3"))
    conn.close()

    _install_digest_stubs(monkeypatch, [(user_1, user_3)])
    monkeypatch.setattr(
        journal_extractors,
        "_call_llm_with_retries",
        lambda *_a, **_k: _insert_payload([user_1, user_3], "same-composite"),
    )

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r10-midspan", limit_entries=20
    )
    verify = _db(hermes_home)
    sources = _sources(verify)
    tool_row = _entry_row(verify, tool_2)
    assert int(result.get("inserted") or 0) == 1
    assert sources == {user_1, user_3, tool_2}
    assert str(tool_row["processed_run_id"] or "")
    assert int(tool_row["retryable_failures"] or 0) == 0
    assert tool_2 in _leave_ids(result, "processed_ids")
    verify.close()


def test_deferred_suffix_tool_never_attaches_this_run(tmp_path, monkeypatch):
    """RED 5: unattempted suffix tool stays deferred and off sources."""

    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    session = "suffix-session"
    prefix = _append(conn, scope, session=session, turn=1, content=_user("PREFIX"))
    mid_tool = _append(conn, scope, session=session, turn=2, role="tool", content=TOOL_BODY)
    covered = _append(conn, scope, session=session, turn=3, content=_user("COVERED"))
    suffix_tool = _append(
        conn, scope, session=session, turn=4, role="tool", content=SUFFIX_TOOL_BODY
    )
    suffix = _append(conn, scope, session=session, turn=5, content=_user("SUFFIX"))
    conn.close()

    _install_digest_stubs(monkeypatch, [(prefix, covered)])
    monkeypatch.setattr(
        journal_extractors,
        "_call_llm_with_retries",
        lambda *_a, **_k: _insert_payload([prefix, covered], "prefix-only"),
    )

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r10-suffix", limit_entries=20
    )
    verify = _db(hermes_home)
    sources = _sources(verify)
    suffix_row = _entry_row(verify, suffix_tool)
    assert prefix in sources and covered in sources and mid_tool in sources
    assert suffix_tool not in sources
    assert suffix not in sources
    assert not str(suffix_row["processed_run_id"] or "")
    assert str(suffix_row["deferred_run_id"] or "")
    assert int(suffix_row["extraction_attempts"] or 0) == 0
    assert int(suffix_row["retryable_failures"] or 0) == 0
    assert suffix_tool in _leave_ids(result, "deferred_ids")
    verify.close()


def test_each_session_chunk_candidate_receives_only_its_own_tools(tmp_path, monkeypatch):
    """RED 6: two successful session chunks do not share tool IDs."""

    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    a1 = _append(conn, scope, session="session-a", turn=1, content=_user("A-1"))
    a_tool = _append(conn, scope, session="session-a", turn=2, role="tool", content=TOOL_BODY)
    a3 = _append(conn, scope, session="session-a", turn=3, content=_user("A-3"))
    b4 = _append(conn, scope, session="session-b", turn=1, content=_user("B-4"))
    b_tool = _append(
        conn, scope, session="session-b", turn=2, role="tool", content=SUFFIX_TOOL_BODY
    )
    b6 = _append(conn, scope, session="session-b", turn=3, content=_user("B-6"))
    conn.close()

    _install_digest_stubs(monkeypatch, [(a1, a3), (b4, b6)])

    def scripted_llm(prompt: str, **_kwargs) -> str:
        if f"ids={a1},{a3}" in prompt:
            return _insert_payload([a1, a3], "session-a")
        return _insert_payload([b4, b6], "session-b")

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r10-multi-chunk", limit_entries=20
    )
    verify = _db(hermes_home)
    links = verify.execute(
        "SELECT journal_entry_id, memory_id FROM memory_journal_sources"
    ).fetchall()
    by_memory: dict[str, set[int]] = {}
    for row in links:
        by_memory.setdefault(str(row["memory_id"]), set()).add(int(row["journal_entry_id"]))
    assert int(result.get("inserted") or 0) == 2
    assert len(by_memory) == 2
    groups = set(frozenset(ids) for ids in by_memory.values())
    assert groups == {frozenset([a1, a3, a_tool]), frozenset([b4, b6, b_tool])}
    verify.close()


def test_failed_later_chunk_cannot_hitchhike_onto_prefix_provenance(
    tmp_path, monkeypatch
):
    """RED 7: prefix chunk keeps its own provenance; later failed chunk cannot donate tools."""

    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    session = "partial-session"
    prefix = _append(conn, scope, session=session, turn=1, content=_user("PREFIX"))
    mid_tool = _append(conn, scope, session=session, turn=2, role="tool", content=TOOL_BODY)
    later = _append(conn, scope, session=session, turn=3, content=_user("LATER"))
    conn.close()

    _install_digest_stubs(monkeypatch, [(prefix,), (later,)])

    def scripted_llm(prompt: str, **_kwargs) -> str:
        if f"ids={prefix}" in prompt:
            return _insert_payload([prefix], "prefix")
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r10-hitchhike", limit_entries=20
    )
    verify = _db(hermes_home)
    sources = _sources(verify)
    tool_row = _entry_row(verify, mid_tool)
    later_row = _entry_row(verify, later)
    assert int(result.get("inserted") or 0) == 1
    assert prefix in sources
    assert mid_tool not in sources
    assert later not in sources
    assert str(_entry_row(verify, prefix)["processed_run_id"] or "")
    assert not str(tool_row["processed_run_id"] or "")
    assert int(tool_row["retryable_failures"] or 0) == 0
    assert int(later_row["retryable_failures"] or 0) == 1
    assert later in _leave_ids(result, "retryable_pending_ids")
    _assert_no_sensitive_leak(result)
    verify.close()


def test_extractor_carries_only_exact_chunk_tool_ids(tmp_path, monkeypatch):
    """Extractor metadata, not a later global span, authorizes tool IDs."""

    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    a1 = _append(conn, scope, session="session-a", turn=1, content=_user("A-1"))
    b_tool = _append(conn, scope, session="session-b", turn=1, role="tool", content=TOOL_BODY)
    a3 = _append(conn, scope, session="session-a", turn=2, content=_user("A-3"))
    b4 = _append(conn, scope, session="session-b", turn=2, content=_user("B-4"))
    entries = list(
        conn.execute(
            "SELECT id, scope_id, shared_scope_id, session_id, turn_number, role, "
            "content, created_at FROM journal_entries ORDER BY id"
        )
    )
    journal_entries = [
        JournalEntry(
            int(row["id"]),
            str(row["scope_id"]),
            str(row["shared_scope_id"]),
            str(row["session_id"]),
            int(row["turn_number"]),
            str(row["role"]),
            str(row["content"]),
            str(row["created_at"]),
        )
        for row in entries
    ]
    conn.close()

    _install_digest_stubs(monkeypatch, [(a1, a3), (b4,)])
    monkeypatch.setattr(journal_extractors, "_runtime_config", lambda _home: {})
    monkeypatch.setattr(
        journal_extractors,
        "resolve_llm_config",
        lambda _home, _options: {
            "model": "test-model",
            "base_url": "https://example.invalid",
            "api_key": "test-only",
            "api_mode": "chat_completions",
            "endpoint": "",
            "append_v1": True,
            "allow_insecure_endpoint": False,
        },
    )
    monkeypatch.setattr(journal_extractors, "existing_memory_context", lambda *_a, **_k: [])
    monkeypatch.setattr(
        journal_extractors, "_existing_context_target_ids_by_scope", lambda *_a, **_k: {}
    )

    def scripted_llm(prompt: str, **_kwargs) -> str:
        if f"ids={a1},{a3}" in prompt:
            return _insert_payload([a1, a3], "session-a")
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    probe = _db(hermes_home)
    try:
        extracted = journal_extractors.llm_journal_candidates(
            probe,
            entries=journal_entries,
            hermes_home=hermes_home,
            scope=scope,
            journal_config=_journal_cfg(),
        )
    finally:
        probe.close()

    assert isinstance(extracted, JournalCandidateList)
    assert extracted
    assert extracted[0].entry_ids == [a1, a3]
    assert list(getattr(extracted[0], "covered_tool_ids", None) or []) == []
    assert b_tool not in set(extracted[0].entry_ids)
    assert b4 in extracted.attempted_entry_ids
    assert a1 in extracted.attempted_entry_ids
