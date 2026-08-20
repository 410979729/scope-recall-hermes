"""R9 decisive honesty: generic Exception paths must keep attempted metadata.

After a prefix chunk has truly invoked the LLM, a later plain Exception must
not bare-raise out of ``llm_journal_candidates``. Orchestration would otherwise
see no ``attempted_entry_ids``, charge zero, discard successful prefix
candidates, and repay that prefix forever.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import scope_recall.journal_extractors as journal_extractors
from scope_recall.journal import run_journal_digest
from scope_recall.journal_extractors import JournalCandidateList
from scope_recall.journal_llm import JournalDigestLLMError
from scope_recall.journal_store import load_unprocessed_journal_entries
from scope_recall.nightly_digest import SessionChunk
from scope_recall.scope import build_scope_id
from test_r110_final_integration import _append, _db, _home, _scope


SECRET_KEY = "sk-" + "R" * 24
SECRET_PATH = "/tmp/hermes-r9-secret-output.log"
SECRET_SCOPE = "telegram:r9-secret-scope"
PROMPT_MARK = "PROMPT-SECRET-R9-BODY"
RESPONSE_MARK = "RESPONSE-SECRET-R9-BODY"


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


def _insert_payload(entry_id: int, label: str) -> str:
    return json.dumps(
        [
            {
                "action": "insert",
                "evidence_message_ids": [entry_id],
                "content": (
                    f"Distinct durable {label} procedure for journal entry "
                    f"{entry_id} must keep verified rollback guardrail evidence."
                ),
                "target": "memory",
                "memory_type": "procedure",
                "importance": 0.9,
                "confidence": 0.86,
                "entities": ["scope-recall"],
                "tags": ["r9-prefix"],
                "reason": "cited the attempted prefix only.",
            }
        ]
    )


def _safe_prompt(_bundle, chunk, *_args, **_kwargs) -> str:
    return "journal-digest-prompt ids=" + ",".join(str(item) for item in chunk.message_ids)


def _poison_exc(kind: str) -> RuntimeError:
    return RuntimeError(
        f"{kind} collapsed api_key={SECRET_KEY} path={SECRET_PATH} "
        f"scope={SECRET_SCOPE} prompt={PROMPT_MARK} response={RESPONSE_MARK}"
    )


def _seed_window(conn, scope, *, suffix: bool, evidence: bool) -> dict[str, int]:
    session = "r9-poison-session"
    ids = {
        "prefix": _append(
            conn,
            scope,
            session=session,
            turn=1,
            content="PREFIX-R9 这条前缀会被真正送进抽取器并应留下可复用工程结论。",
        ),
        "later": _append(
            conn,
            scope,
            session=session,
            turn=2,
            content="LATER-R9 这条后续 chunk 会在抽取过程中抛出普通异常。",
        ),
    }
    if suffix:
        ids["suffix"] = _append(
            conn,
            scope,
            session=session,
            turn=3,
            content="SUFFIX-R9 这条未触达的后缀不得被整窗猜测计费。",
        )
    if evidence:
        ids["evidence"] = _append(
            conn,
            scope,
            session=session,
            turn=4,
            role="tool",
            content="tool execution trace must stay on the admission path.",
        )
    return ids


def _entry_row(conn, entry_id: int):
    return conn.execute(
        "SELECT processed_run_id, deferred_run_id, extraction_attempts, "
        "retryable_failures FROM journal_entries WHERE id=?",
        (entry_id,),
    ).fetchone()


def _serialized(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def _assert_no_sensitive_leak(payload: str) -> None:
    assert SECRET_KEY not in payload
    assert SECRET_PATH not in payload
    assert SECRET_SCOPE not in payload
    assert PROMPT_MARK not in payload
    assert RESPONSE_MARK not in payload
    assert "sk-" not in payload


def _install_common_extractor_stubs(monkeypatch, groups: list[tuple[int, ...]]) -> None:
    monkeypatch.setattr(journal_extractors, "session_chunks", _force_chunks(groups))
    monkeypatch.setattr(journal_extractors, "build_prompt", _safe_prompt)
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


def test_later_chunk_runtimeerror_from_llm_keeps_prefix_and_charges_failed_ids(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    ids = _seed_window(conn, scope, suffix=True, evidence=True)
    conn.close()
    groups = [(ids["prefix"],), (ids["later"],)]
    _install_common_extractor_stubs(monkeypatch, groups)

    def scripted_llm(prompt: str, **_kwargs) -> str:
        if f"ids={ids['prefix']}" in prompt:
            return _insert_payload(ids["prefix"], "prefix")
        raise _poison_exc("later-chunk _call_llm_with_retries RuntimeError")

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    probe_conn = _db(hermes_home)
    try:
        extractor = journal_extractors.llm_journal_candidates(
            probe_conn,
            entries=load_unprocessed_journal_entries(
                probe_conn,
                scope_ids=[build_scope_id(scope)],
                limit=20,
            ),
            hermes_home=hermes_home,
            scope=scope,
            journal_config=_journal_cfg(),
        )
    finally:
        probe_conn.close()
    assert isinstance(extractor, JournalCandidateList)
    assert ids["prefix"] in extractor.attempted_entry_ids
    assert ids["later"] in extractor.attempted_entry_ids
    assert ids["suffix"] not in extractor.attempted_entry_ids
    assert ids["suffix"] in extractor.deferred_entry_ids
    assert any(ids["prefix"] in candidate.entry_ids for candidate in extractor)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r9-runtime", limit_entries=20
    )
    verify = _db(hermes_home)
    prefix = _entry_row(verify, ids["prefix"])
    later = _entry_row(verify, ids["later"])
    suffix = _entry_row(verify, ids["suffix"])
    evidence = _entry_row(verify, ids["evidence"])
    leave = result.get("leave_states") or {}
    serialized = _serialized(result)
    assert int(result.get("inserted") or 0) == 1
    assert str(prefix["processed_run_id"] or "")
    assert int(prefix["retryable_failures"] or 0) == 0
    assert int(prefix["extraction_attempts"] or 0) == 0
    assert int(later["retryable_failures"] or 0) == 1
    assert int(later["extraction_attempts"] or 0) == 0
    assert not str(later["processed_run_id"] or "")
    assert str(suffix["deferred_run_id"] or "")
    assert int(suffix["retryable_failures"] or 0) == 0
    assert int(suffix["extraction_attempts"] or 0) == 0
    assert str(evidence["processed_run_id"] or "")
    assert int(evidence["retryable_failures"] or 0) == 0
    assert ids["prefix"] in {int(item) for item in leave.get("processed_ids") or []}
    assert ids["later"] in {int(item) for item in leave.get("retryable_pending_ids") or []}
    assert ids["suffix"] in {int(item) for item in leave.get("deferred_ids") or []}
    assert "extractor failure; source entries remain pending" not in serialized
    _assert_no_sensitive_leak(serialized)
    verify.close()


def test_later_chunk_generic_parse_exception_keeps_prefix_and_charges_once(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    ids = _seed_window(conn, scope, suffix=True, evidence=False)
    conn.close()
    groups = [(ids["prefix"],), (ids["later"],)]
    _install_common_extractor_stubs(monkeypatch, groups)
    real_parse = journal_extractors._parse_journal_llm_candidates

    def scripted_llm(_prompt: str, **_kwargs) -> str:
        return _insert_payload(ids["prefix"] if "ids=" + str(ids["prefix"]) in _prompt else ids["later"], "chunk")

    parse_calls = {"n": 0}

    def exploding_parse(*args, **kwargs):
        parse_calls["n"] += 1
        if parse_calls["n"] == 1:
            return real_parse(*args, **kwargs)
        raise Exception(
            f"json decode collapsed api_key={SECRET_KEY} path={SECRET_PATH} "
            f"scope={SECRET_SCOPE} prompt={PROMPT_MARK} response={RESPONSE_MARK}"
        )

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)
    monkeypatch.setattr(journal_extractors, "_parse_journal_llm_candidates", exploding_parse)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r9-parse", limit_entries=20
    )
    verify = _db(hermes_home)
    prefix = _entry_row(verify, ids["prefix"])
    later = _entry_row(verify, ids["later"])
    suffix = _entry_row(verify, ids["suffix"])
    serialized = _serialized(result)
    assert int(result.get("inserted") or 0) == 1
    assert str(prefix["processed_run_id"] or "")
    assert int(later["extraction_attempts"] or 0) == 1
    assert int(later["retryable_failures"] or 0) == 0
    assert not str(later["processed_run_id"] or "")
    assert str(suffix["deferred_run_id"] or "")
    assert int(suffix["extraction_attempts"] or 0) == 0
    _assert_no_sensitive_leak(serialized)
    verify.close()


@pytest.mark.parametrize(
    "site",
    ["build_prompt", "snapshot_release", "network_boundary"],
)
def test_later_chunk_precall_failure_preserves_prefix_without_charging_uncalled(
    tmp_path, monkeypatch, site
):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    ids = _seed_window(conn, scope, suffix=True, evidence=False)
    conn.close()
    groups = [(ids["prefix"],), (ids["later"],)]
    _install_common_extractor_stubs(monkeypatch, groups)
    llm_calls = {"n": 0}

    def scripted_llm(_prompt: str, **_kwargs) -> str:
        llm_calls["n"] += 1
        return _insert_payload(ids["prefix"], "prefix")

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    if site == "build_prompt":
        def exploding_prompt(_bundle, chunk, *_args, **_kwargs):
            if ids["later"] in set(chunk.message_ids):
                raise _poison_exc("later-chunk build_prompt")
            return _safe_prompt(_bundle, chunk)

        monkeypatch.setattr(journal_extractors, "build_prompt", exploding_prompt)
    elif site == "snapshot_release":
        real_release = journal_extractors.release_snapshot_transaction

        def exploding_release(conn_inner):
            if llm_calls["n"] >= 1:
                raise _poison_exc("later-chunk snapshot release")
            return real_release(conn_inner)

        monkeypatch.setattr(journal_extractors, "release_snapshot_transaction", exploding_release)
    else:
        real_prepare = journal_extractors.prepare_network_boundary

        def exploding_prepare(conn_inner, label):
            if str(label).endswith(".llm") and llm_calls["n"] >= 1:
                raise _poison_exc("later-chunk network-boundary")
            return real_prepare(conn_inner, label)

        monkeypatch.setattr(journal_extractors, "prepare_network_boundary", exploding_prepare)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label=f"r9-{site}", limit_entries=20
    )
    verify = _db(hermes_home)
    prefix = _entry_row(verify, ids["prefix"])
    later = _entry_row(verify, ids["later"])
    suffix = _entry_row(verify, ids["suffix"])
    serialized = _serialized(result)
    assert llm_calls["n"] == 1
    assert int(result.get("inserted") or 0) == 1
    assert str(prefix["processed_run_id"] or "")
    assert int(later["retryable_failures"] or 0) == 0
    assert int(later["extraction_attempts"] or 0) == 0
    assert str(later["deferred_run_id"] or ""), f"{site} uncalled chunk must stay deferred"
    assert str(suffix["deferred_run_id"] or "")
    assert int(suffix["retryable_failures"] or 0) == 0
    _assert_no_sensitive_leak(serialized)
    verify.close()


def test_three_poison_runs_advance_failed_budget_to_quarantine_without_repaying_prefix(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(
        tmp_path, _journal_cfg(retryable_failures_quarantine=3)
    )
    scope = _scope()
    ids = _seed_window(conn, scope, suffix=False, evidence=False)
    conn.close()
    groups = [(ids["prefix"],), (ids["later"],)]
    _install_common_extractor_stubs(monkeypatch, groups)
    prefix_calls = {"n": 0}
    later_calls = {"n": 0}

    def scripted_llm(prompt: str, **_kwargs) -> str:
        if f"ids={ids['prefix']}" in prompt:
            prefix_calls["n"] += 1
            return _insert_payload(ids["prefix"], "prefix")
        later_calls["n"] += 1
        raise _poison_exc("repeatable later-chunk RuntimeError")

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    transitions: list[dict[str, int]] = []
    last = None
    for index in range(1, 4):
        last = run_journal_digest(
            hermes_home=hermes_home,
            scope=scope,
            interval_label=f"r9-repeat-{index}",
            limit_entries=20,
        )
        verify = _db(hermes_home)
        prefix = _entry_row(verify, ids["prefix"])
        later = _entry_row(verify, ids["later"])
        transitions.append(
            {
                "run": index,
                "prefix_calls": prefix_calls["n"],
                "later_calls": later_calls["n"],
                "prefix_retryable": int(prefix["retryable_failures"] or 0),
                "prefix_attempts": int(prefix["extraction_attempts"] or 0),
                "later_retryable": int(later["retryable_failures"] or 0),
                "later_attempts": int(later["extraction_attempts"] or 0),
                "prefix_processed": 1 if str(prefix["processed_run_id"] or "") else 0,
                "later_processed": 1 if str(later["processed_run_id"] or "") else 0,
            }
        )
        verify.close()

    assert prefix_calls["n"] == 1, "successful prefix must not be endlessly repaid"
    assert later_calls["n"] == 3
    assert [item["later_retryable"] for item in transitions] == [1, 2, 3]
    assert [item["prefix_retryable"] for item in transitions] == [0, 0, 0]
    assert [item["prefix_attempts"] for item in transitions] == [0, 0, 0]
    assert transitions[0]["prefix_processed"] == 1
    assert transitions[2]["later_processed"] == 1
    leave = (last or {}).get("leave_states") or {}
    assert ids["later"] in {int(item) for item in leave.get("quarantined_ids") or []}
    _assert_no_sensitive_leak(_serialized(last or {}))


def test_pre_first_call_failure_stays_zero_attempt_fail_closed(tmp_path, monkeypatch):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    ids = _seed_window(conn, scope, suffix=True, evidence=True)
    conn.close()
    groups = [(ids["prefix"],), (ids["later"],)]
    _install_common_extractor_stubs(monkeypatch, groups)
    llm_calls = {"n": 0}

    def forbidden_llm(_prompt: str, **_kwargs) -> str:
        llm_calls["n"] += 1
        raise AssertionError("LLM must not be called before the first attempt boundary")

    def exploding_prompt(*_args, **_kwargs):
        raise RuntimeError("pre-first-call build_prompt collapsed")

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", forbidden_llm)
    monkeypatch.setattr(journal_extractors, "build_prompt", exploding_prompt)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r9-precall", limit_entries=20
    )
    verify = _db(hermes_home)
    prefix = _entry_row(verify, ids["prefix"])
    later = _entry_row(verify, ids["later"])
    suffix = _entry_row(verify, ids["suffix"])
    evidence = _entry_row(verify, ids["evidence"])
    serialized = _serialized(result)
    assert llm_calls["n"] == 0
    assert int(result.get("inserted") or 0) == 0
    assert int(prefix["retryable_failures"] or 0) == 0
    assert int(prefix["extraction_attempts"] or 0) == 0
    assert not str(prefix["processed_run_id"] or "")
    assert int(later["retryable_failures"] or 0) == 0
    assert int(later["extraction_attempts"] or 0) == 0
    assert int(suffix["retryable_failures"] or 0) == 0
    assert int(suffix["extraction_attempts"] or 0) == 0
    assert str(evidence["processed_run_id"] or "")
    assert "extractor failure; source entries remain pending" in serialized
    assert SECRET_KEY not in serialized
    assert SECRET_PATH not in serialized
    verify.close()


def test_structured_timeout_success_evidence_and_deferred_suffix_unchanged(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    ids = _seed_window(conn, scope, suffix=True, evidence=True)
    conn.close()
    groups = [(ids["prefix"],), (ids["later"],)]
    _install_common_extractor_stubs(monkeypatch, groups)
    calls = {"n": 0}

    def scripted_llm(prompt: str, **_kwargs) -> str:
        calls["n"] += 1
        if f"ids={ids['prefix']}" in prompt:
            return _insert_payload(ids["prefix"], "prefix")
        raise JournalDigestLLMError(
            "synthetic timeout", attempts=1, error_kind="timeout", retryable=True
        )

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r9-structured", limit_entries=20
    )
    verify = _db(hermes_home)
    prefix = _entry_row(verify, ids["prefix"])
    later = _entry_row(verify, ids["later"])
    suffix = _entry_row(verify, ids["suffix"])
    evidence = _entry_row(verify, ids["evidence"])
    leave = result.get("leave_states") or {}
    assert int(result.get("inserted") or 0) == 1
    assert str(prefix["processed_run_id"] or "")
    assert int(later["retryable_failures"] or 0) == 1
    assert int(later["extraction_attempts"] or 0) == 0
    assert str(suffix["deferred_run_id"] or "")
    assert int(suffix["retryable_failures"] or 0) == 0
    assert str(evidence["processed_run_id"] or "")
    assert int(evidence["retryable_failures"] or 0) == 0
    assert ids["later"] in {int(item) for item in leave.get("retryable_pending_ids") or []}
    assert ids["suffix"] in {int(item) for item in leave.get("deferred_ids") or []}
    verify.close()

    hermes_ok, conn_ok = _home(tmp_path / "ok", _journal_cfg())
    scope_ok = _scope()
    ok_ids = _seed_window(conn_ok, scope_ok, suffix=False, evidence=False)
    conn_ok.close()
    _install_common_extractor_stubs(
        monkeypatch, [(ok_ids["prefix"],), (ok_ids["later"],)]
    )

    def success_llm(prompt: str, **_kwargs) -> str:
        if f"ids={ok_ids['prefix']}" in prompt:
            return _insert_payload(ok_ids["prefix"], "ok-prefix")
        return _insert_payload(ok_ids["later"], "ok-later")

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", success_llm)
    ok = run_journal_digest(
        hermes_home=hermes_ok, scope=scope_ok, interval_label="r9-success", limit_entries=20
    )
    verify_ok = _db(hermes_ok)
    assert int(ok.get("inserted") or 0) == 2
    assert str(_entry_row(verify_ok, ok_ids["prefix"])["processed_run_id"] or "")
    assert str(_entry_row(verify_ok, ok_ids["later"])["processed_run_id"] or "")
    verify_ok.close()


def test_classification_sanitization_failure_uses_safe_fallback_without_losing_attempted(
    tmp_path, monkeypatch
):
    hermes_home, conn = _home(tmp_path, _journal_cfg())
    scope = _scope()
    ids = _seed_window(conn, scope, suffix=True, evidence=False)
    conn.close()
    groups = [(ids["prefix"],), (ids["later"],)]
    _install_common_extractor_stubs(monkeypatch, groups)
    calls = {"n": 0}

    def scripted_llm(prompt: str, **_kwargs) -> str:
        calls["n"] += 1
        if f"ids={ids['prefix']}" in prompt:
            return _insert_payload(ids["prefix"], "prefix")
        raise _poison_exc("later-chunk generic after prefix")

    def exploding_error_type():
        raise RuntimeError(
            f"exception classification collapsed api_key={SECRET_KEY} "
            f"path={SECRET_PATH} scope={SECRET_SCOPE}"
        )

    monkeypatch.setattr(journal_extractors, "_call_llm_with_retries", scripted_llm)
    monkeypatch.setattr(
        journal_extractors, "active_journal_digest_llm_error", exploding_error_type
    )

    result = run_journal_digest(
        hermes_home=hermes_home, scope=scope, interval_label="r9-classify", limit_entries=20
    )
    verify = _db(hermes_home)
    prefix = _entry_row(verify, ids["prefix"])
    later = _entry_row(verify, ids["later"])
    suffix = _entry_row(verify, ids["suffix"])
    serialized = _serialized(result)
    assert int(result.get("inserted") or 0) == 1
    assert str(prefix["processed_run_id"] or "")
    assert ids["later"] in {
        int(item)
        for key in ("retryable_pending_ids", "quarantined_ids")
        for item in (result.get("leave_states") or {}).get(key) or []
    }
    assert int(later["retryable_failures"] or 0) + int(later["extraction_attempts"] or 0) >= 1
    assert str(suffix["deferred_run_id"] or "")
    _assert_no_sensitive_leak(serialized)
    verify.close()
